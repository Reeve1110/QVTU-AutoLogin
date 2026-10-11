# -*- coding: utf-8 -*-
"""网络状态检测：WiFi SSID、是否真正联网。

校园网未认证时，请求会被门户劫持并返回 200（假联网），所以必须校验响应内容的
特征，而不能只看状态码。同时强制不走系统代理，避免把「代理能上网」误判成
「本机已联网」。
"""
from __future__ import annotations

import re
import sys
import threading
import time
import urllib.request

import procutil

# 强制不走系统/环境代理
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 记住上一次探测成功的 URL：校园网环境下不同探针快慢差异很大，优先复用命中的那个
_good_url = None
_good_lock = threading.Lock()


def _decode(raw: bytes) -> str:
    # 本机开了「UTF-8 全球语言支持」时 netsh 输出是 UTF-8；默认中文系统是 GBK。
    # UTF-8 严格解码失败才会落到 GBK，避免 GBK「什么都能解」把 UTF-8 解成乱码。
    for enc in ("utf-8", "gbk", "cp936"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def get_wifi_ssid() -> str | None:
    """返回当前已连接 WiFi 的名称；未连接返回 None。"""
    try:
        # run_hidden：netsh 是控制台程序，不隐藏就会每轮闪一次黑窗
        raw = procutil.run_hidden(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True, timeout=10,
        ).stdout
    except Exception:
        return None
    text = _decode(raw)

    state_auth = None
    ssid = None
    for line in text.splitlines():
        line = line.strip()
        # 中文系统 "状态 : 已连接"；英文系统 "State : connected"
        if re.match(r"^(状态|State)\s*[:：]", line):
            val = line.split(":", 1)[-1].strip().lower()
            if "已连接" in val or "connected" in val:
                state_auth = True
            elif "已断开" in val or "disconnected" in val:
                state_auth = False
        # 必须锚定行首 SSID，否则会把 "AP BSSID" 也当成 SSID
        m = re.match(r"^(SSID)\s*[:：]\s*(.+)$", line)
        if m and not line.upper().startswith("AP "):
            ssid = m.group(2).strip()

    if state_auth is False:
        return None
    return ssid or None


def is_wifi_in_prefix(ssid: str | None, prefix: str) -> bool:
    if not ssid or not prefix:
        return False
    return ssid.strip().upper().startswith(prefix.strip().upper())


# --------------------------------------------------------------------------- #
# WiFi 配置（profile）的「自动连接」检测与设置
# --------------------------------------------------------------------------- #
def _wlan(*args, timeout: int = 15) -> str:
    return _decode(procutil.run_hidden(
        ["netsh", "wlan"] + list(args), capture_output=True, timeout=timeout).stdout or b"")


def list_wifi_profiles() -> list:
    """本机已保存的全部 WiFi 配置名。"""
    text = _wlan("show", "profiles")
    names = []
    for line in text.splitlines():
        m = re.search(r"(?:所有用户配置文件|当前用户配置文件|All User Profile"
                      r"|Current User Profile)\s*[:：]\s*(.+)$", line.strip())
        if m:
            n = m.group(1).strip()
            if n and n not in names:
                names.append(n)
    return names


def profile_autoconnect(profile: str) -> bool | None:
    """配置是否「自动连接」；配置不存在返回 None。"""
    text = _wlan("show", "profile", "name=%s" % profile)
    if not text or "找不到" in text or "not found" in text.lower():
        return None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("连接模式") or s.lower().startswith("connection mode"):
            val = re.split(r"[:：]", s, 1)[-1].strip().lower()
            return ("自动连接" in val) or ("auto" in val) or ("automatically" in val)
    return None


def profiles_missing_autoconnect(prefix: str) -> list:
    """列出以 prefix 开头、且未开启自动连接的已保存 WiFi 配置。"""
    out = []
    for name in list_wifi_profiles():
        if is_wifi_in_prefix(name, prefix) and profile_autoconnect(name) is False:
            out.append(name)
    return out


def set_profile_autoconnect(profile: str) -> tuple:
    """把某个 WiFi 配置改为自动连接，返回 (是否成功, 说明)。

    优先普通权限直接改；被拒绝（需要管理员）时弹 UAC 用提权的 netsh 执行。
    """
    if sys.platform != "win32":
        return False, "仅支持 Windows"
    r = procutil.run_hidden(
        ["netsh", "wlan", "set", "profileparameter",
         "name=%s" % profile, "connectionmode=auto"],
        capture_output=True, timeout=20)
    out = _decode((r.stdout or b"") + (r.stderr or b""))
    if r.returncode == 0 and "拒绝" not in out and "denied" not in out.lower():
        return True, "已开启自动连接"

    # 需要管理员：弹 UAC，用 netsh 直接提权执行（不重启整个程序）
    try:
        import ctypes
        args = 'wlan set profileparameter name="%s" connectionmode=auto' % profile
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "netsh", args, None, 0)
        if int(rc) > 32:
            return True, "已请求管理员权限执行，稍后可重新检测确认"
        return False, "未能开启（可能取消了 UAC 授权）"
    except Exception as e:
        return False, str(e)


def _body_ok(url: str, body: str) -> bool:
    low = body.lower()
    if "msftconnecttest" in url:
        return "microsoft connect test" in low
    if "captive.apple.com" in url:
        return "<title>success</title>" in low
    if "generate_204" in url:
        return len(body.strip()) == 0
    if "baidu.com" in url:
        return "baidu" in low
    return bool(body.strip())


def _probe(url: str, timeout: float) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with _OPENER.open(req, timeout=timeout) as resp:
            if resp.status != 200:
                return False
            body = resp.read(4096).decode("utf-8", errors="ignore")
    except Exception:
        return False
    return _body_ok(url, body)


def is_internet_available(probe_urls: list[str], timeout: float = 3.0) -> bool:
    """真正能访问互联网才为 True。

    多个探针并行发起、任一成功立即返回。串行发起时最坏耗时为「探针数 × timeout」，
    校园网里被墙的探针（captive.apple.com 等）会各挂满 timeout，累计可达十几秒；
    并行后整体耗时被压到约 timeout，这一点对「登录后快速确认联网」很关键。
    上一个命中的探针仍会优先发起。
    """
    global _good_url
    if not probe_urls:
        return False

    with _good_lock:
        first = _good_url if _good_url in probe_urls else None
    ordered = ([first] + [u for u in probe_urls if u != first]) if first else list(probe_urls)

    found = threading.Event()
    hit: dict = {}

    def _worker(u: str) -> None:
        if found.is_set():
            return
        try:
            if _probe(u, timeout) and not found.is_set():
                hit["url"] = u
                found.set()
        except Exception:
            pass

    workers = [threading.Thread(target=_worker, args=(u,), daemon=True) for u in ordered]
    for t in workers:
        t.start()

    # 「所有探针都结束了」就立刻返回 False，不必死等满 timeout。
    # 校园网里探针大多会被门户快速劫持或直接拒绝，几十~几百毫秒就全部返回；
    # 旧实现固定等 timeout+0.35s，一次失败就白等 3.35s（probe_timeout_sec=3），
    # 在登录链路和状态切换里累积起来非常可观。
    deadline = time.time() + max(0.5, float(timeout)) + 0.35
    while True:
        if found.wait(0.05):
            break
        if not any(t.is_alive() for t in workers):
            break
        if time.time() >= deadline:
            break
    ok = found.is_set()

    if ok:
        u = hit.get("url")
        if u and u != _good_url:
            with _good_lock:
                _good_url = u
    return bool(ok)


if __name__ == "__main__":
    ssid = get_wifi_ssid()
    print("SSID:", ssid)
    print("in campus prefix:", is_wifi_in_prefix(ssid, "QVTU"))
    print("internet:", is_internet_available([
        "http://www.msftconnecttest.com/connecttest.txt",
        "http://captive.apple.com/hotspot-detect.html",
        "http://www.baidu.com",
    ]))
