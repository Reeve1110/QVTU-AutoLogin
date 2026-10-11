# -*- coding: utf-8 -*-
"""监测引擎：事件驱动的双状态模型，把「守住外网连通」跑在后台线程里。

状态 A【外网已连通】—— 轻量休眠：
  不轮询、不检测 WiFi、不登录。只挂两类唤醒源：
  ① 系统网络变更事件（netevents.py：网卡/WiFi 连接、断开、切换）
  ② 心跳到点（默认 60 秒一次；兜住「校园网认证过期不会产生任何网络事件」）
  醒来只快速校验一次外网：通 → 继续休眠；不通 → 进入状态 B。
  另外，若实际醒来时间比预定晚 20 秒以上，判定为休眠唤醒，同样触发校验。

状态 B【外网断开】—— 核心监听：
  每 2 秒检测一次当前 WiFi 名称：
  - 以设定前缀（QVTU）开头 → 连续确认后自动登录；成功回到状态 A；
    失败 → 记录原因并通知，进入冷却，冷却结束后自动再试
  - 不是校园网 → 不登录，继续监听（期间定期探测外网，
    捕捉「用户手动认证成功 / 切到其它能上网的网络」→ 回到状态 A）

防重复登录（重要）：
  - 登录全程持有互斥锁，进行中时任何新的登录请求（含手动）直接拒绝；
  - 一整轮（含重试）失败后设置冷却时间，冷却期内不发起自动登录，
    避免短时间大量提交请求被校园网限流或锁号。

程序退出后引擎随之结束，不再做任何检测；下次开机自启才重新开始。
"""
from __future__ import annotations

import threading
import time

from netcheck import get_wifi_ssid, is_internet_available, is_wifi_in_prefix

STATE_ONLINE = "online"          # 外网已连通（轻量休眠）
STATE_WATCHING = "watching"      # 外网断开，监听 WiFi 中
STATE_LOGGING_IN = "logging_in"  # 正在登录
STATE_COOLDOWN = "cooldown"      # 登录失败，冷却中

RESUME_LATE_SEC = 20             # 等待醒来比预定晚多少秒 → 视为休眠唤醒


class Engine:
    def __init__(self, cfg: dict, log):
        self.cfg = dict(cfg or {})
        self.log = log
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._net_evt = threading.Event()      # 网络事件 / 手动触发 共用的唤醒源
        self._force = False
        self._login_mutex = threading.Lock()   # 登录互斥：进行中拒绝新请求
        self._logining = False
        self._cooldown_until = 0.0
        self._wake_late = False
        self._notify_cb = None                 # (message) -> None，由界面弹气泡
        self._watcher = None
        self._last_note = None
        self._last_note_at = 0.0
        self._status = {
            "running": False, "paused": False, "state": "stopped",
            "state_text": "未启动", "ssid": None, "online": False, "is_qvtu": False,
            "started_at": None, "last_check": None, "last_login": None,
            "next_check_in": 0, "login_attempt": 0, "last_error": "",
            "message": "",
        }

    # ---------------- 对外控制 ----------------
    def set_notify_callback(self, cb) -> None:
        """引擎在后台线程回调 cb(message)；界面侧自行切回主线程。"""
        self._notify_cb = cb

    def update_config(self, cfg: dict) -> None:
        with self._lock:
            self.cfg = dict(cfg or {})

    def is_running(self) -> bool:
        t = self._thread
        return bool(t and t.is_alive())

    def is_paused(self) -> bool:
        return self._pause.is_set()

    def in_cooldown(self) -> bool:
        return time.time() < self._cooldown_until

    def is_logining(self) -> bool:
        return self._logining

    def start(self) -> bool:
        with self._lock:
            t = self._thread
            if t and t.is_alive():
                if not self._stop.is_set():
                    return False              # 正在跑，不重复启动
                # 上一轮刚被 stop、还在收尾：给它一点时间自然退出（通常 <0.3 秒）
                t.join(3.0)
                if t.is_alive():
                    self.log.warn("上一次监测尚未完全结束，请稍后再试")
                    return False
            self._stop.clear()
            self._pause.clear()
            self._net_evt.clear()
            self._last_note = None
            self._thread = threading.Thread(target=self._run_wrapper, name="autologin-engine", daemon=True)
            self._thread.start()
            self._prewarm()
            return True

    def _prewarm(self) -> None:
        """后台预热 playwright 导入：第一次登录能省掉约 1 秒的模块加载。"""
        def _job():
            try:
                import playwright.sync_api  # noqa: F401
            except Exception:
                pass
        threading.Thread(target=_job, name="autologin-prewarm", daemon=True).start()

    def stop(self, timeout: float = 0.0) -> None:
        """请求停止监测。

        默认**不做任何 join**：界面线程调用时立刻返回，绝不阻塞 UI（这是「点停止卡顿」的修复）。
        引擎线程会在下一个等待边界（≤0.25 秒）或当前登录流程退出后自行结束。
        需要同步等待（测试 / 命令行）时显式传 timeout。
        """
        self._stop.set()
        self._net_evt.set()
        self._stop_watcher()
        self._set_status(running=False, state="stopped", state_text="已停止",
                         next_check_in=0, login_attempt=0)
        t = self._thread
        if timeout and t and t.is_alive() and threading.current_thread() is not t:
            t.join(timeout)

    def pause(self) -> None:
        self._pause.set()
        self._net_evt.set()
        self._set_status(paused=True, state="paused", state_text="已暂停")

    def resume(self) -> None:
        self._pause.clear()
        self._net_evt.set()
        self._set_status(paused=False)

    def toggle_pause(self) -> bool:
        if self.is_paused():
            self.resume()
            return False
        self.pause()
        return True

    def force_login(self) -> None:
        """手动立即登录一次（无视冷却）；登录进行中则拒绝。"""
        if self.is_logining():
            self.log.warn("已有登录任务在执行，忽略本次手动请求")
            return
        if self.is_running():
            self._force = True
            self._net_evt.set()
            return

        def _once():
            self._stop.clear()
            self.log.info("手动触发登录（单次）")
            try:
                self._do_login(manual=True)
            except Exception as e:
                self.log.error("手动登录异常: %r" % e)

        threading.Thread(target=_once, name="autologin-force", daemon=True).start()

    def login_once(self) -> bool:
        """同步执行一次登录（阻塞到出结果），供命令行与测试使用。"""
        if self.is_logining():
            self.log.warn("已有登录任务在执行")
            return False
        self._stop.clear()
        return self._do_login(manual=True)

    def status(self) -> dict:
        with self._lock:
            st = dict(self._status)
        st["paused"] = self._pause.is_set()
        # 已被 stop 请求就当「未运行」——线程收尾期间界面也该立刻显示已停止
        st["running"] = self.is_running() and not self._stop.is_set()
        st["in_cooldown"] = self.in_cooldown()
        sa = st.get("started_at")
        st["uptime_sec"] = int(time.time() - sa) if sa else 0
        return st

    # ---------------- 内部 ----------------
    def _set_status(self, **kw) -> None:
        with self._lock:
            self._status.update(kw)

    def _notify_user(self, message: str) -> None:
        cb = self._notify_cb
        if cb is None:
            return
        try:
            cb(message)
        except Exception:
            pass

    def _note(self, key, text, level="info") -> None:
        """状态切换边界才记日志，避免每轮刷屏。"""
        if key != self._last_note:
            self._last_note = key
            self.log.add(text, level)

    def _note_throttled(self, key, text, every_sec: float = 15.0) -> None:
        """同一状态下的周期性提示（如冷却倒计时），限频记录。"""
        now = time.time()
        if key != self._last_note or now - self._last_note_at >= every_sec:
            self._last_note = key
            self._last_note_at = now
            self.log.add(text, "info")

    def _start_watcher(self) -> bool:
        try:
            import netevents
            w = netevents.NetworkWatcher(lambda: self._net_evt.set())
            if w.start():
                self._watcher = w
                return True
        except Exception:
            pass
        return False

    def _stop_watcher(self) -> None:
        w, self._watcher = self._watcher, None
        if w is not None:
            try:
                w.stop()
            except Exception:
                pass

    def _take_force(self) -> bool:
        got, self._force = self._force, False
        return got

    def _interruptible_wait(self, seconds: float) -> bool:
        """等待被「网络事件 / 手动触发 / 暂停 / 停止」打断；返回 False 表示应退出。

        若实际醒来时间比预定晚 RESUME_LATE_SEC 以上 → 标记休眠唤醒。
        """
        self._wake_late = False
        deadline = time.time() + max(0.2, seconds)
        while not self._stop.is_set():
            remain = deadline - time.time()
            if remain <= 0:
                break
            if self._net_evt.is_set() or self._force or self._pause.is_set():
                break
            self._stop.wait(min(0.25, remain))
        if time.time() - deadline > RESUME_LATE_SEC:
            self._wake_late = True
        return not self._stop.is_set()

    def _probe_online(self) -> bool:
        t = float(self.cfg.get("probe_timeout_sec", 3) or 3)
        online = is_internet_available(self.cfg.get("online_probe_urls", []), timeout=t)
        self._set_status(online=online, last_check=time.time())
        return online

    def _update_basic(self, online: bool, ssid) -> None:
        self._set_status(ssid=ssid, online=online,
                         is_qvtu=is_wifi_in_prefix(ssid, self.cfg.get("wifi_prefix", "QVTU")))

    # ---------------- 登录（互斥 + 冷却） ----------------
    def _do_login(self, manual: bool = False) -> bool:
        if not self._login_mutex.acquire(blocking=False):
            self.log.warn("已有登录任务在执行，忽略本次登录请求")
            return False
        self._logining = True
        try:
            return self._login_round(manual)
        finally:
            self._logining = False
            self._login_mutex.release()

    def _login_round(self, manual: bool) -> bool:
        from portal_login import PortalLogin

        cfg = self.cfg
        retry = max(1, int(cfg.get("login_retry", 3)))
        last_error = ""
        for i in range(1, retry + 1):
            if self._stop.is_set():
                return False
            self.log.info("%s登录：第 %d/%d 次尝试" % ("手动" if manual else "自动", i, retry))
            self._set_status(state=STATE_LOGGING_IN, login_attempt=i,
                             state_text="正在登录（第 %d/%d 次）" % (i, retry))
            attempt_error = ""
            captcha = False
            confirmed = False
            try:
                portal = PortalLogin(cfg, self.log.add, should_stop=self._stop.is_set)
                ok = portal.login()
                attempt_error = (getattr(portal, "last_error", "") or "").strip()
                captcha = bool(getattr(portal, "captcha_required", False))
                confirmed = bool(getattr(portal, "online_confirmed", False))
            except Exception as e:
                self.log.error("登录过程抛出异常: %r" % e)
                attempt_error = str(e)
                ok = False

            # 门户自己已确认联网就不用再等；否则最多等 12 秒
            if ok and (confirmed or self._wait_online(12)):
                self._set_status(last_login=time.time(), login_attempt=0,
                                 last_error="", next_check_in=0)
                self.log.success("登录成功，外网已连通")
                # 关键操作结果：自动/手动登录成功都通知（不依赖状态 B 的调用方）
                self._notify_user("校园网%s登录成功，外网已连通"
                                  % ("手动" if manual else "自动"))
                return True

            # 失败原因**只用本次的**，绝不沿用上一轮（旧实现会把上一轮的
            # 「用户名或密码错误」带到一次其实是联网校验失败的尝试上，误导排查）
            if ok:
                last_error = "登录已提交，但外网仍未连通"
                self.log.warn("登录流程返回成功，但联网校验未通过")
            else:
                last_error = attempt_error or "认证未通过"
                self.log.warn("本次未成功：%s" % last_error)

            if captcha:
                self.log.warn("门户要求图形验证码（风控），已停止重试；请先手动登录一次")
                break
            if i < retry:
                self._stop.wait(5)

        self._set_status(login_attempt=0, last_error=last_error)
        cooldown = max(1, int(cfg.get("login_cooldown_sec", 60)))
        self._cooldown_until = time.time() + cooldown
        reason = last_error or "多次尝试未成功"
        self.log.error("登录失败：%s（%d 秒后自动重试）" % (reason, cooldown))
        self._notify_user("校园网登录失败：%s" % reason)
        return False

    def _wait_online(self, seconds: float = 12.0) -> bool:
        """登录提交后确认外网是否真的通了；命中即返回，最长等 seconds 秒。

        旧值 60 秒是登录「看起来卡了十几秒到一分钟」的元凶之一：一旦门户误报成功，
        就要在这里干等到超时。现在 12 秒封顶，且多数情况下 1 秒内就返回。
        """
        end = time.time() + seconds
        while time.time() < end:
            if self._stop.is_set():
                return False
            remain = end - time.time()
            if is_internet_available(self.cfg.get("online_probe_urls", []),
                                     timeout=min(2.0, max(0.8, remain))):
                return True
            self._stop.wait(0.35)
        return False

    # ---------------- 主循环与两个状态 ----------------
    def _run_wrapper(self) -> None:
        try:
            self._loop()
        except Exception as e:
            try:
                self.log.error("监测线程异常退出: %r" % e)
            except Exception:
                pass
        finally:
            self._stop_watcher()
            self._set_status(running=False, state="stopped", state_text="已停止",
                             next_check_in=0, login_attempt=0)

    def _loop(self) -> None:
        started = time.time()
        self._set_status(running=True, started_at=started, state=STATE_WATCHING,
                         state_text="外网校验中")
        self.log.info("监测已启动（外网断开时每 %s 秒检测 WiFi）"
                      % self.cfg.get("offline_poll_sec", 2))
        if self._start_watcher():
            self.log.info("已注册系统网络变更事件监听（外网正常时不轮询）")
        else:
            self.log.warn("网络事件监听不可用，退化为心跳校验兜底")

        while not self._stop.is_set():
            try:
                if self._pause.is_set():
                    self._set_status(state="paused", state_text="已暂停")
                    self._stop.wait(1)
                    continue

                self._net_evt.clear()
                ssid = get_wifi_ssid()
                online = self._probe_online()
                self._update_basic(online, ssid)

                if online:
                    self._state_online()
                else:
                    self._state_offline(ssid)
            except Exception as e:
                self.log.error("监测循环出现异常（已忽略，继续）: %r" % e)
                self._stop.wait(2)

        self._set_status(running=False, state="stopped", state_text="已停止",
                         next_check_in=0)
        self.log.info("监测已停止")

    def _state_online(self) -> None:
        """状态 A：外网已连通。休眠等「网络事件 / 心跳 / 手动 / 暂停 / 停止」。

        返回（结束状态 A）条件：stop、pause（回到主循环）、或校验发现断网。
        """
        hb = max(10, int(self.cfg.get("online_heartbeat_sec", 60)))
        while not self._stop.is_set() and not self._pause.is_set():
            self._set_status(state=STATE_ONLINE, state_text="外网已连通，休眠监听中",
                             next_check_in=0)
            if not self._interruptible_wait(hb):
                return
            if self._wake_late:
                self.log.info("检测到系统可能刚从休眠中唤醒，校验网络状态")
            force = self._take_force()
            if not self._probe_online():
                self._note("offline", "外网校验失败，进入断网监听模式", "warn")
                if force:
                    self._force = True        # 手动触发的登录诉求转交给状态 B 立即执行
                return
            if force:
                self.log.info("外网正常，无需登录")
                self._notify_user("外网已连通，无需登录")

    def _state_offline(self, ssid_last) -> None:
        """状态 B：外网断开。每 2 秒检测 WiFi，校园网才登录。"""
        poll = max(1, int(self.cfg.get("offline_poll_sec", 2)))
        confirm_sec = max(0.0, float(self.cfg.get("offline_confirm_sec", 1) or 0))
        tick = 0
        last_ssid = ssid_last
        qvtu_since = None            # 连续处于「校园网 + 断网」的起点
        while not self._stop.is_set() and not self._pause.is_set():
            net_changed = self._net_evt.is_set()
            self._net_evt.clear()
            force = self._take_force()

            ssid = get_wifi_ssid()
            if ssid != last_ssid:
                self.log.info("WiFi 变化：%s → %s" % (last_ssid or "未连接", ssid or "未连接"))
                last_ssid = ssid
            is_q = is_wifi_in_prefix(ssid, self.cfg.get("wifi_prefix", "QVTU"))
            self._update_basic(False, ssid)

            # 周期探测外网：捕捉手动认证成功 / 切到其它能上网的网络
            tick += 1
            if net_changed or force or tick % 3 == 0:
                if self._probe_online():
                    self._note("re-online", "外网已恢复（WiFi：%s）" % (ssid or "未连接"), "success")
                    return

            if not is_q:
                qvtu_since = None
                if force:
                    self.log.warn("手动登录已请求，但当前 WiFi（%s）不是校园网，跳过"
                                  % (ssid or "未连接"))
                self._note("other", "当前 WiFi（%s）不是校园网，不执行登录，继续监听"
                           % (ssid or "未连接"))
                self._set_status(state=STATE_WATCHING,
                                 state_text="外网断开，监听中（非校园网）")
                if not self._interruptible_wait(poll):
                    return
                continue

            now = time.time()
            if not force and now < self._cooldown_until:
                left = max(1, int(self._cooldown_until - now))
                self._set_status(state=STATE_COOLDOWN,
                                 state_text="登录失败，%d 秒后重试" % left,
                                 next_check_in=left)
                self._note_throttled("cool", "登录失败冷却中，%d 秒后自动重试" % left, 15)
                if not self._interruptible_wait(poll):
                    return
                continue

            # 防抖：校园网 + 断网需持续 confirm_sec 秒才登录（默认 1 秒；手动触发不防抖）。
            # 用「持续时间」而非「轮询轮数」，避免轮询间隔一改就把等待时间放大。
            if not force:
                if qvtu_since is None:
                    qvtu_since = now
                left = confirm_sec - (now - qvtu_since)
                if left > 0:
                    self._set_status(state=STATE_WATCHING, state_text="外网断开，确认中…")
                    if not self._interruptible_wait(min(poll, max(0.3, left))):
                        return
                    continue
            else:
                qvtu_since = None

            self._note("login", "WiFi=%s，外网断开，开始自动登录" % (ssid or "未连接"), "warn")
            ok = self._do_login(manual=force)
            if self._stop.is_set():
                return
            if ok:
                self._set_status(online=True)
                return                        # 成功通知由 _login_round 统一发出
            # 失败：_do_login 已记录原因并设置冷却
            qvtu_since = None
            if not self._interruptible_wait(poll):
                return


if __name__ == "__main__":
    from config import load
    from logbus import LogBus

    bus = LogBus()
    en = Engine(load(), bus)
    en.start()
    time.sleep(3)
    print(en.status())
    en.stop()
