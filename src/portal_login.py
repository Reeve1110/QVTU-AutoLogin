# -*- coding: utf-8 -*-
"""校园网登录引擎：用真实浏览器复刻「打开门户 → 填账号密码 → 点登录」。

门户是泉州职业技术大学的统一身份认证（Apereo CAS + Angular 前端，未认证时会被
网关劫持到 sid.qvtu.edu.cn/cas/login）。密码可能由页面 JS（jsencrypt）处理后随表单
一并提交，因此驱动真实浏览器比逆向 HTTP 接口稳定得多。使用本机已装的 Chrome/Edge
（channel），不需要 playwright install 下载内核。

三条关键设计（都是踩坑换来的，改动前请先看注释）：
  1. **联网检测一律放后台线程**（`_OnlineWatcher`），登录主链路只读标志位、不阻塞。
     旧实现把 `is_internet_available` 直接塞进等待循环，每轮最多白等 `timeout+0.35s`，
     光「定位登录表单」一步就慢了 4~6 秒。
  2. **「认证成功」必须同时满足**「已离开 CAS 登录页」且「登录表单确实消失」。
     不能只看表单是否消失——失败时页面会重载，表单同样会短暂消失；旧实现用
     `input[name=password]` 判断（新页面该属性不存在），于是**永远判为成功**，
     登录明明失败却要再空等 60 秒联网校验。以「真联网」或「离开登录页」为准才可靠。
  3. **验证码出现时立即失败、不再重试**：多次失败会被风控拉起图形验证码，
     继续提交只会加重风控，必须让用户手动登录一次。
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime

from paths import data_dir

LOGIN_FAIL_HINTS = (
    "password", "incorrect", "错误", "失败", "invalid",
    "验证码", "captcha", "锁定", "lock",
)

# 认证结果最长等待时间（秒）。超过就判定失败，避免久等。
_MAX_RESULT_WAIT = 15.0
# 表单出现的最长等待（秒），再长也没意义
_MAX_FORM_WAIT = 40.0

# 表单选择器。新版页面是 Angular，#normalLoginForm 仍在，但**密码框没有 name 属性**，
# 所以第二列必须用 input[type=password]；旧的 name=password 只在更老的版本上有效。
_FORM_PAIRS = (
    ("#normalLoginForm input[name=username]", "#normalLoginForm input[type=password]"),
    ("#normalLoginForm input[name=username]", "#normalLoginForm .passwordInput input"),
    ("input[name=username]", "input[type=password]"),
)
# 用于判断「登录表单是否还在」
_PWD_SELECTORS = ("#normalLoginForm input[type=password]",
                  "#normalLoginForm .passwordInput input",
                  "input[type=password]")
_SUBMIT_SELECTORS = ("#submitBtn", ".login-button", "button[type=submit]", "button.submitter")
# 错误提示：新版是 antd 的 .ant-alert-message（如「用户名或密码错误，请确认后重新输入」）
_ERROR_SELECTORS = ("#loginFailMessage", ".ant-alert-message", ".ant-alert-description",
                    ".error-msg-new", ".error-tips", ".toast-message",
                    ".normal-toast .error-msg", ".valid-error")
# 验证码：新版图形验证码的输入框 placeholder 是「请分别大小写」
_CAPTCHA_SELECTORS = ("input[name=captcha_code]",
                      "input[placeholder*='大小写']",
                      "input[placeholder*='验证码']",
                      "#google_recaptcha",
                      "img[src*=captcha]",
                      "[class*=captcha-code]")

# 浏览器启动参数
_LAUNCH_ARGS = [
    "--ignore-certificate-errors",
    "--no-proxy-server",            # 不跟着系统代理走：代理关掉但没清设置时会连门户都打不开
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-features=Translate,MediaRouter",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-extensions",
    "--disable-component-update",
    "--mute-audio",
]
# 通道尝试顺序：所选通道 → 另一个 → 内置 chromium（没装内核时会失败，日志会说明）
_CHANNEL_CHAIN = ("chrome", "msedge")
_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def channel_order(channel: str) -> list:
    """把配置里的通道值展开成「依次尝试」的顺序。

    ``"auto"``（自动）不是 Playwright 认得的通道名——它表示「不指定首选，按
    chrome → msedge 顺序找」。所以这里必须**不要把 "auto" 直接当通道名传下去**。
    返回的顺序里始终包含 chrome 与 msedge；内置内核由调用方最后兜底。
    """
    order = []
    channel = str(channel or "").strip()
    if channel and channel != "auto":
        order.append(channel)
    for c in _CHANNEL_CHAIN:
        if c not in order:
            order.append(c)
    return order


def _first_line(exc) -> str:
    """取异常消息的第一行（截断），用于把启动失败原因写进日志/错误。"""
    s = str(exc)
    return s.splitlines()[0][:140] if s else ""


def launch_channeled(p, channel: str, headless: bool, args=None):
    """按「所选通道 → 另一个通道 → 内置 chromium」依次启动浏览器。

    返回 ``(browser, used_label)``；全部失败抛 ``RuntimeError``（含每步原因）。
    调用方不必关心 ``"auto"`` 的展开与回退——这正是**唯一**的浏览器启动入口，
    绝不要把配置里的 ``chrome_channel`` 原样当作 ``channel=`` 传给 Playwright
    （``"auto"`` 不是合法通道名，必然启动失败）。
    """
    args = list(args or [])
    errors = []
    for ch in channel_order(channel):
        try:
            b = p.chromium.launch(channel=ch, headless=headless, args=args)
            return b, "%s %s" % (ch, getattr(b, "version", "?"))
        except Exception as e:
            errors.append("%s → %s" % (ch, _first_line(e)))
    try:
        b = p.chromium.launch(headless=headless, args=args)
        return b, "内置 chromium %s" % getattr(b, "version", "?")
    except Exception as e:
        errors.append("内置 chromium → %s" % _first_line(e))
    raise RuntimeError("无法启动任何浏览器：" + "；".join(errors))


class _OnlineWatcher:
    """后台线程周期探测外网；一旦连通就置位并自行结束。

    登录主链路只读 `online.is_set()`，**零阻塞**——这是登录提速的关键。
    """

    def __init__(self, urls, timeout: float = 1.5, interval: float = 0.8):
        self._urls = list(urls or [])
        self._timeout = timeout
        self._interval = interval
        self.online = threading.Event()
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, name="portal-online-watch", daemon=True)

    def start(self) -> "_OnlineWatcher":
        if self._urls:
            self._t.start()
        return self

    def _run(self) -> None:
        from netcheck import is_internet_available
        while not self._stop.is_set() and not self.online.is_set():
            try:
                if is_internet_available(self._urls, timeout=self._timeout):
                    self.online.set()
                    return
            except Exception:
                pass
            if self._stop.wait(self._interval):
                return

    def stop(self) -> None:
        self._stop.set()


class PortalLogin:
    def __init__(self, cfg: dict, log, artifacts_dir: str | None = None,
                 should_stop=None):
        self.cfg = cfg
        self.log = log
        self.artifacts_dir = artifacts_dir or os.path.join(data_dir(), "logs")
        self.should_stop = should_stop          # 可选：返回 True 时尽快中断（停止监测用）
        # 对外暴露的结果信息
        self.last_error = ""          # 最近一次失败的人类可读原因（成功时清空）
        self.online_confirmed = False  # 是否已由后台探针确认「外网真的通了」
        self.captcha_required = False  # 是否被风控要求验证码（不可重试）
        self.used_browser = ""         # 实际使用的浏览器（通道 + 版本）

    # ---------- 对外入口 ----------
    def login(self) -> bool:
        """执行一次登录流程，成功返回 True。"""
        from playwright.sync_api import sync_playwright, Error as PWError

        t_start = time.time()
        self.last_error = ""
        self.online_confirmed = False
        self.captcha_required = False
        self.used_browser = ""

        portal = str(self.cfg.get("portal_url", "")).rstrip("/")
        timeout_ms = int(self.cfg.get("login_timeout_sec", 90)) * 1000
        headless = bool(self.cfg.get("browser_headless", True))
        channel = str(self.cfg.get("chrome_channel", "chrome") or "chrome")

        watcher = _OnlineWatcher(self.cfg.get("online_probe_urls", [])).start()
        ok = False
        try:
            with sync_playwright() as p:
                browser = self._launch(p, channel, headless)
                try:
                    ctx = browser.new_context(
                        ignore_https_errors=True,
                        viewport={"width": 1280, "height": 800},
                        user_agent=_USER_AGENT,
                    )
                    page = ctx.new_page()
                    self.log("打开门户页面: %s" % portal)
                    try:
                        page.goto(portal, wait_until="domcontentloaded",
                                  timeout=max(8000, min(45000, timeout_ms)))
                    except PWError as e:
                        self.log("页面加载异常（继续尝试）: %s" % e)

                    frame, user_el, pwd_el = self._wait_login_form(page, timeout_ms, watcher)
                    if user_el == "ONLINE":
                        self.online_confirmed = True
                        ok = True
                    elif frame is None:
                        if self._stopping():
                            self.log("收到停止信号，放弃本次登录")
                        else:
                            self.log("未能在页面上找到登录表单")
                            self._dump(page, "no_form")
                            self.last_error = "未找到登录表单（门户页面结构可能已变化）"
                    else:
                        self.log("已定位登录表单（frame: %s）" % (frame.url or "main"))
                        if self._detect_captcha(page):
                            self.captcha_required = True
                            self.last_error = ("登录页要求填写图形验证码（多次失败后被风控），"
                                               "请先在浏览器里手动登录一次")
                            self.log.warn(self.last_error)
                            self._dump(page, "captcha")
                        elif self._stopping():
                            self.log("收到停止信号，放弃本次登录")
                        else:
                            self._fill(user_el, self.cfg["username"])
                            self._fill(pwd_el, self.cfg["password"])
                            time.sleep(0.15)      # 给页面 JS 一点反应时间
                            if not self._submit(frame):
                                self.log("未能触发登录提交")
                                self._dump(page, "no_submit")
                                self.last_error = "未能触发登录提交"
                            else:
                                self.log("已提交账号密码，等待认证结果…")
                                if self._wait_result(page, timeout_ms, watcher):
                                    ok = True
                                else:
                                    self._dump(page, "login_failed")
                                    if not self.last_error:
                                        self.last_error = ("认证未通过（账号密码错误，"
                                                           "或门户未放行）")
                finally:
                    try:
                        browser.close()
                    except Exception:
                        pass
        except Exception as e:
            self.last_error = self.last_error or ("启动浏览器失败：%s" % e)
            try:
                self.log.error("登录流程异常：%r" % e)
            except Exception:
                pass
        finally:
            watcher.stop()

        try:
            self.log("登录流程耗时 %.1f 秒（浏览器：%s）"
                     % (time.time() - t_start, self.used_browser or "未启动"))
        except Exception:
            pass
        return ok

    # ---------- 内部实现 ----------
    def _stopping(self) -> bool:
        cb = self.should_stop
        if cb is None:
            return False
        try:
            return bool(cb())
        except Exception:
            return False

    def _launch(self, p, channel: str, headless: bool):
        """按「所选通道 → 另一个通道 → 内置 chromium」依次尝试，失败日志写明原因。"""
        try:
            browser, used = launch_channeled(p, channel, headless, _LAUNCH_ARGS)
        except RuntimeError as e:
            self.last_error = str(e)
            raise
        self.used_browser = used
        if used.startswith("内置"):
            self.log("已启动内置 chromium（未使用系统 Chrome/Edge）")
        else:
            self.log("已启动浏览器：%s（headless=%s）" % (used, headless))
        return browser

    def _wait_login_form(self, page, timeout_ms: int, watcher: "_OnlineWatcher"):
        """遍历所有 frame 找账号/密码框，返回 (frame, user_el, pwd_el)。

        真实的登录页是学校统一身份认证，被 iframe 引入，所以必须跨 frame 查找。
        若后台探针发现网络已通（无需登录），返回 (None, "ONLINE", None)。
        """
        deadline = time.time() + min(_MAX_FORM_WAIT, max(3.0, timeout_ms / 1000.0))
        while time.time() < deadline:
            if self._stopping():
                return None, None, None
            if watcher.online.is_set():
                self.log("检测到网络已连通，无需登录")
                return None, "ONLINE", None

            for frame in list(page.frames):
                for u_sel, p_sel in _FORM_PAIRS:
                    try:
                        user = frame.query_selector(u_sel)
                        pwd = frame.query_selector(p_sel)
                        if user and pwd and user.is_visible() and pwd.is_visible():
                            return frame, user, pwd
                    except Exception:
                        continue

                # 兜底：任意可见密码框 + 同一表单内的邻近文本框
                try:
                    pwd = None
                    for cand in frame.query_selector_all("input[type=password]"):
                        if cand.is_visible():
                            pwd = cand
                            break
                    if pwd is None:
                        continue
                    user = frame.evaluate_handle(
                        """p => {
                            const form = p.closest('form') || document;
                            const cands = [...form.querySelectorAll('input')].filter(
                                e => e !== p && e.offsetParent !== null &&
                                     ['text','tel','email',''].includes((e.type || '').toLowerCase()));
                            return cands[0] || null;
                        }""", pwd)
                    user = user.as_element()
                    if user and user.is_visible():
                        return frame, user, pwd
                except Exception:
                    continue

            try:
                page.wait_for_timeout(150)
            except Exception:
                return None, None, None
        return None, None, None

    def _detect_captcha(self, page) -> bool:
        for frame in list(page.frames):
            for sel in _CAPTCHA_SELECTORS:
                try:
                    el = frame.query_selector(sel)
                    if el and el.is_visible():
                        return True
                except Exception:
                    continue
        return False

    @staticmethod
    def _login_form_present(page) -> bool:
        for frame in list(page.frames):
            for sel in _PWD_SELECTORS:
                try:
                    if frame.query_selector(sel):
                        return True
                except Exception:
                    continue
        return False

    @staticmethod
    def _left_login_page(url: str) -> bool:
        """是否已经离开 CAS 登录页（进入 cas-sso 回调或门户才算离开）。

        注意：服务地址里含 `cas-sso/login`，它不是登录页，别误判。
        """
        u = (url or "").lower()
        if not u:
            return False
        return "/cas/login" not in u

    @staticmethod
    def _fill(el, value: str) -> None:
        el.click()
        try:
            el.fill("")
        except Exception:
            pass
        el.type(value, delay=8)

    def _submit(self, frame) -> bool:
        for sel in _SUBMIT_SELECTORS:
            try:
                el = frame.query_selector(sel)
                if el and el.is_visible() and el.is_enabled():
                    el.click()
                    return True
            except Exception:
                continue
        try:
            form = frame.query_selector("#normalLoginForm")
            if form:
                frame.evaluate("f => f.requestSubmit ? f.requestSubmit() : f.submit()", form)
                return True
        except Exception:
            pass
        try:
            frame.press("#normalLoginForm input[type=password]", "Enter")
            return True
        except Exception:
            try:
                frame.press("#normalLoginForm input[name=username]", "Enter")
                return True
            except Exception:
                return False

    def _wait_result(self, page, timeout_ms: int, watcher: "_OnlineWatcher") -> bool:
        """等待认证结果。

        成功（任一）：
          a. 后台探针确认外网已通（最可靠）；
          b. 已经离开 CAS 登录页 **且** 登录表单确实消失（CAS 认证通过会跳回 cas-sso 回调）。
        失败：页面上出现明确的错误提示。
        只看「表单消失」会把「失败后页面重载」误判成成功，所以加了「离开登录页」这个必要条件。
        """
        deadline = time.time() + min(timeout_ms / 1000.0, _MAX_RESULT_WAIT)
        while time.time() < deadline:
            if self._stopping():
                self.log("收到停止信号，中断等待认证结果")
                return False
            if watcher.online.is_set():
                self.online_confirmed = True
                self.log("外网已连通，认证成功")
                return True

            try:
                page.wait_for_timeout(250)
            except Exception:
                break

            msg = self._read_error(page)
            if msg:
                self.log("认证返回错误信息: %s" % msg)
                self.last_error = msg
                return False

            try:
                url = page.url
            except Exception:
                url = ""
            if self._left_login_page(url) and not self._login_form_present(page):
                self.log("已离开登录页，判定认证成功")
                self.online_confirmed = watcher.online.wait(2.5)
                return True

        if not self.last_error:
            self.log("等待认证结果超时")
            self.last_error = "等待认证结果超时"
        return False

    def _read_error(self, page) -> str | None:
        for frame in list(page.frames):
            for sel in _ERROR_SELECTORS:
                try:
                    el = frame.query_selector(sel)
                except Exception:
                    continue
                if not el:
                    continue
                try:
                    txt = (el.inner_text() or "").strip()
                except Exception:
                    continue
                if txt and any(h in txt.lower() for h in LOGIN_FAIL_HINTS):
                    return txt[:200]
        return None

    def _dump(self, page, tag: str) -> None:
        """失败时保存各 frame 的 HTML 文本现场，便于排查。

        只输出文本（.html），**不再截图**——日志与现场一律保持纯文本，
        既省磁盘也避免在无人值守时产生大量图片。
        """
        try:
            os.makedirs(self.artifacts_dir, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            base = os.path.join(self.artifacts_dir, "fail_%s_%s" % (tag, stamp))
            parts = []
            for i, f in enumerate(page.frames):
                try:
                    parts.append("===== FRAME %d url=%s =====\n%s" % (i, f.url, f.content()))
                except Exception:
                    continue
            with open(base + ".html", "w", encoding="utf-8", errors="ignore") as fh:
                fh.write("\n\n".join(parts))
            self.log("已保存失败现场: %s(.html)" % base)
        except Exception as e:
            self.log("保存失败现场时出错: %s" % e)
