# -*- coding: utf-8 -*-
"""Windows 服务：开机**抢先**启动本程序（参考 Wallpaper Engine 的做法）。

思路（为什么需要服务）：
  · 计划任务 / 启动文件夹都要等用户登录、桌面（Explorer）初始化后才跑，启动偏晚；
  · Windows 服务在**开机阶段就由系统拉起**（早于任何用户登录），因此本服务一启动
    就常驻待命；一旦用户进入桌面（会话变为 WTSActive），**立刻**把托盘程序
    ``AutoLogin.exe --minimized`` 拉进该用户会话——这就是「抢先启动」。

服务本体只做「尽早把 GUI 拉进用户会话」这一件事，不碰登录逻辑；真正的自动登录
仍由用户会话里的托盘程序完成（它才能访问该用户的配置与网络）。

兼容性：全程只用 pywin32（win32service / win32ts / win32profile 等）+ 标准库，
不依赖 sc.exe / PowerShell / Git 等外部程序。安装/卸载需要管理员权限（服务注册
本身要求），失败一律以异常或 (ok, msg) 返回，交给上层提示。

说明：会话拉起的正确性（``CreateProcessAsUser`` + 用户令牌 + 用户环境块）在线下
无法通过「重启机器」验证，因此这里刻意写得保守：每一步都单独 try/except，日志
写进 ``<数据目录>\\logs\\service.log``，便于真机上排查。
"""
from __future__ import annotations

import os
import sys
import time

from paths import exe_path, data_dir, is_frozen
from version import SERVICE_NAME, SERVICE_DISPLAY, SERVICE_DESC

# ---- pywin32 可选导入（缺模块时整个服务功能优雅降级） ----------------------
try:
    import win32service
    import win32serviceutil
    import win32event
    import servicemanager
    import win32api
    import win32con
    import win32ts
    import win32security
    import win32process
    import win32profile
    _AVAILABLE = True
    _IMPORT_ERR = ""
except Exception as e:                       # pragma: no cover - 仅非 Windows / 缺依赖时
    _AVAILABLE = False
    _IMPORT_ERR = repr(e)

POLL_MS = 2000                                # 会话轮询间隔：登录后最多 2 秒内拉起
WTS_ACTIVE = 0                                # win32ts.WTSActive

_STATE_TEXT = {
    1: "已停止",
    2: "正在启动",
    3: "正在停止",
    4: "运行中",
    5: "正在继续",
    6: "正在暂停",
    7: "已暂停",
}


def available() -> bool:
    return _AVAILABLE


def unavailable_reason() -> str:
    return _IMPORT_ERR


def service_log(line: str) -> None:
    """把服务侧日志追加到数据目录（界面程序没有控制台，只能落文件）。"""
    try:
        d = os.path.join(data_dir(), "logs")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "service.log"), "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), line))
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 会话相关：找出「已进入桌面」的用户会话，并把程序拉进去
# --------------------------------------------------------------------------- #
def _field(obj, *names, default=None):
    if isinstance(obj, dict):
        for n in names:
            if n in obj:
                return obj[n]
        return default
    for n in names:
        if hasattr(obj, n):
            return getattr(obj, n)
    return default


def active_session_ids() -> list:
    """返回当前处于「活动（WTSActive）」状态的用户会话 id 列表。"""
    if not _AVAILABLE:
        return []
    out = []
    try:
        sessions = win32ts.WTSEnumerateSessions(win32ts.WTS_CURRENT_SERVER_HANDLE)
    except Exception as e:
        service_log("枚举会话失败：%r" % e)
        return []
    for s in sessions:
        sid = _field(s, "SessionId", "session_id", default=0)
        state = _field(s, "State", "state", default=-1)
        try:
            sid = int(sid)
            state = int(state)
        except Exception:
            continue
        if sid > 0 and state == WTS_ACTIVE:
            out.append(sid)
    return out


def _enable_privileges() -> None:
    """确保本进程令牌启用了 CreateProcessAsUser 所需权限（LocalSystem 常默认未启用）。"""
    if not _AVAILABLE:
        return
    try:
        h = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(),
            win32security.TOKEN_ADJUST_PRIVILEGES | win32security.TOKEN_QUERY)
    except Exception:
        return
    try:
        for name in ("SeAssignPrimaryTokenPrivilege", "SeIncreaseQuotaPrivilege",
                     "SeTcbPrivilege"):
            try:
                luid = win32security.LookupPrivilegeValue(None, name)
                win32security.AdjustTokenPrivileges(
                    h, False, [(luid, win32security.SE_PRIVILEGE_ENABLED)])
            except Exception:
                pass
    finally:
        try:
            win32api.CloseHandle(h)
        except Exception:
            pass


def launch_in_session(session_id: int, exe: str = None, args=("--minimized",)) -> tuple:
    """在指定用户会话里以该用户身份启动本程序。返回 (ok, msg)。

    用 ``WTSQueryUserToken`` 取该会话的用户令牌 → 复制成主令牌 → 建用户环境块 →
    ``CreateProcessAsUser``。令牌自带 SessionId，子进程自然出现在用户桌面。
    """
    if not _AVAILABLE:
        return False, "缺少 pywin32 服务组件：%s" % _IMPORT_ERR
    exe = exe or exe_path()
    try:
        h_tok = win32ts.WTSQueryUserToken(int(session_id))
    except Exception as e:
        return False, "取用户令牌失败：%r" % e
    h_dup = None
    try:
        try:
            h_dup = win32security.DuplicateTokenEx(
                h_tok, win32security.SecurityImpersonation, None,
                win32security.TokenPrimary, None)
        except Exception as e:
            return False, "复制令牌失败：%r" % e
    finally:
        try:
            win32api.CloseHandle(h_tok)
        except Exception:
            pass

    env = None
    try:
        env = win32profile.CreateEnvironmentBlock(h_dup, False)
    except Exception:
        env = None

    si = win32process.STARTUPINFO()
    si.dwFlags = win32process.STARTF_USESHOWWINDOW
    si.wShowWindow = win32con.SW_SHOWNORMAL
    cmdline = '"%s" %s' % (exe, " ".join(args))
    flags = 0
    if hasattr(win32process, "CREATE_UNICODE_ENVIRONMENT"):
        flags |= win32process.CREATE_UNICODE_ENVIRONMENT
    try:
        _hp, _ht, pid, _tid = win32process.CreateProcessAsUser(
            h_dup, exe, cmdline, None, None, True, flags, env or None,
            os.path.dirname(exe), si)
        return True, "已在会话 %d 启动（pid=%d）" % (session_id, pid)
    except Exception as e:
        return False, "CreateProcessAsUser 失败：%r" % e
    finally:
        try:
            win32api.CloseHandle(h_dup)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 服务本体
# --------------------------------------------------------------------------- #
if _AVAILABLE:
    class AutoLoginService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY
        _svc_description_ = SERVICE_DESC

        def __init__(self, args):
            win32serviceutil.ServiceFramework.__init__(self, args)
            self._stop_evt = win32event.CreateEvent(None, 0, 0, None)
            self._launched = set()

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            try:
                win32event.SetEvent(self._stop_evt)
            except Exception:
                pass

        def SvcDoRun(self):
            try:
                servicemanager.LogInfoMsg("%s 已启动" % SERVICE_DISPLAY)
            except Exception:
                pass
            service_log("服务进入运行")
            self.ReportServiceStatus(win32service.SERVICE_RUNNING)
            _enable_privileges()
            self._loop()

        def _loop(self):
            while True:
                rc = win32event.WaitForSingleObject(self._stop_evt, POLL_MS)
                if rc != win32event.WAIT_TIMEOUT:
                    break
                try:
                    self._tick()
                except Exception as e:
                    service_log("tick 异常：%r" % e)
            service_log("服务退出循环")

        def _tick(self):
            active = set(active_session_ids())
            for sid in active:
                if sid in self._launched:
                    continue
                ok, msg = launch_in_session(sid)
                service_log("会话 %d 拉起：%s" % (sid, msg))
                if ok:
                    self._launched.add(sid)
            # 会话注销后清掉记号，下次登录（哪怕复用同一 id）可以再次拉起
            self._launched &= active
else:                                            # pragma: no cover
    AutoLoginService = None


def run_service() -> int:
    """由 SCM 拉起时进入此函数：把控制权交给服务框架。"""
    if not _AVAILABLE:
        return 1
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(AutoLoginService)
    servicemanager.StartServiceCtrlDispatcher()
    return 0


# --------------------------------------------------------------------------- #
# 安装 / 卸载 / 状态（供界面与提权实例调用）
# --------------------------------------------------------------------------- #
def _binpath(exe: str = None) -> str:
    return '"%s" --service-run' % (exe or exe_path())


def query_state() -> dict:
    """查询服务状态：{"installed": bool, "state": int, "running": bool, "text": str}。"""
    info = {"installed": False, "state": 0, "running": False, "text": "未安装"}
    if not _AVAILABLE:
        info["text"] = "不可用（缺少 pywin32 服务组件）"
        return info
    hscm = None
    hs = None
    try:
        hscm = win32service.OpenSCManager(
            None, None, win32service.SC_MANAGER_CONNECT)
        try:
            hs = win32service.OpenService(
                hscm, SERVICE_NAME, win32service.SERVICE_QUERY_STATUS)
        except win32service.error as e:
            # 1060 = ERROR_SERVICE_DOES_NOT_EXIST
            if getattr(e, "winerror", None) == 1060:
                return info
            raise
        info["installed"] = True
        st = win32service.QueryServiceStatus(hs)
        state = int(st[1])
        info["state"] = state
        info["running"] = state == win32service.SERVICE_RUNNING
        info["text"] = _STATE_TEXT.get(state, "状态未知(%d)" % state)
    except Exception as e:
        info["text"] = "查询失败：%r" % e
    finally:
        for h in (hs, hscm):
            try:
                if h:
                    win32service.CloseServiceHandle(h)
            except Exception:
                pass
    return info


def is_installed() -> bool:
    return bool(query_state().get("installed"))


def install(exe: str = None) -> str:
    """安装（或更新）自启服务。需要管理员权限；失败抛异常。返回描述文本。"""
    if not _AVAILABLE:
        raise RuntimeError("缺少 pywin32 服务组件：%s" % _IMPORT_ERR)
    binpath = _binpath(exe)
    hscm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_ALL_ACCESS)
    hs = None
    try:
        try:
            hs = win32service.CreateService(
                hscm, SERVICE_NAME, SERVICE_DISPLAY,
                win32service.SERVICE_ALL_ACCESS,
                win32service.SERVICE_WIN32_OWN_PROCESS,
                win32service.SERVICE_AUTO_START,      # 开机自动启动（非延迟）
                win32service.SERVICE_ERROR_NORMAL,
                binpath, None, 0, None, None, None)
        except win32service.error as e:
            # 1073 = ERROR_SERVICE_EXISTS → 改为更新现有配置
            if getattr(e, "winerror", None) != 1073:
                raise
            hs = win32service.OpenService(
                hscm, SERVICE_NAME, win32service.SERVICE_ALL_ACCESS)
            win32service.ChangeServiceConfig(
                hs, win32service.SERVICE_NO_CHANGE, win32service.SERVICE_AUTO_START,
                win32service.SERVICE_NO_CHANGE, binpath, None, 0, None, None, None,
                SERVICE_DISPLAY)
        try:
            win32service.ChangeServiceConfig2(
                hs, win32service.SERVICE_CONFIG_DESCRIPTION, SERVICE_DESC)
        except Exception:
            pass
    finally:
        try:
            if hs:
                win32service.CloseServiceHandle(hs)
        except Exception:
            pass
        try:
            win32service.CloseServiceHandle(hscm)
        except Exception:
            pass
    return "已安装并设为开机自动启动：%s" % SERVICE_DISPLAY


def start(timeout: float = 15.0) -> tuple:
    """启动服务并等待进入「运行中」。返回 (ok, msg)。"""
    if not _AVAILABLE:
        return False, "缺少 pywin32 服务组件"
    hscm = None
    hs = None
    try:
        hscm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
        hs = win32service.OpenService(
            hscm, SERVICE_NAME, win32service.SERVICE_START | win32service.SERVICE_QUERY_STATUS)
        try:
            win32service.StartService(hs, None)
        except win32service.error as e:
            # 1056 = ERROR_SERVICE_ALREADY_RUNNING
            if getattr(e, "winerror", None) != 1056:
                raise
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = win32service.QueryServiceStatus(hs)
            if int(st[1]) == win32service.SERVICE_RUNNING:
                return True, "服务已运行"
            time.sleep(0.4)
        return True, "已请求启动（当前状态：%s）" % _STATE_TEXT.get(int(st[1]), "?")
    except Exception as e:
        return False, "启动服务失败：%r" % e
    finally:
        for h in (hs, hscm):
            try:
                if h:
                    win32service.CloseServiceHandle(h)
            except Exception:
                pass


def stop(timeout: float = 15.0) -> tuple:
    if not _AVAILABLE:
        return False, "缺少 pywin32 服务组件"
    hscm = None
    hs = None
    try:
        hscm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
        hs = win32service.OpenService(
            hscm, SERVICE_NAME, win32service.SERVICE_STOP | win32service.SERVICE_QUERY_STATUS)
        try:
            win32service.ControlService(hs, win32service.SERVICE_CONTROL_STOP)
        except win32service.error as e:
            if getattr(e, "winerror", None) not in (1062, 1060):   # 未运行/不存在
                raise
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = win32service.QueryServiceStatus(hs)
            if int(st[1]) == win32service.SERVICE_STOPPED:
                return True, "服务已停止"
            time.sleep(0.4)
        return True, "已请求停止"
    except Exception as e:
        return False, "停止服务失败：%r" % e
    finally:
        for h in (hs, hscm):
            try:
                if h:
                    win32service.CloseServiceHandle(h)
            except Exception:
                pass


def remove() -> tuple:
    """停止并删除服务。返回 (ok, msg)。服务本就不存在也算成功。"""
    if not _AVAILABLE:
        return False, "缺少 pywin32 服务组件"
    stop(10)
    hscm = None
    hs = None
    try:
        hscm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_ALL_ACCESS)
        try:
            hs = win32service.OpenService(
                hscm, SERVICE_NAME,
                win32service.SERVICE_STOP | win32service.SERVICE_QUERY_STATUS |
                win32service.DELETE)
        except win32service.error as e:
            if getattr(e, "winerror", None) == 1060:
                return True, "服务本就不存在"
            raise
        win32service.DeleteService(hs)
        return True, "服务已删除"
    except Exception as e:
        return False, "删除服务失败：%r" % e
    finally:
        for h in (hs, hscm):
            try:
                if h:
                    win32service.CloseServiceHandle(h)
            except Exception:
                pass


def describe() -> str:
    st = query_state()
    if not st.get("installed"):
        return "未启用（服务未安装）"
    return "已启用：Windows 服务（%s）" % st.get("text")


if __name__ == "__main__":
    print("available :", available())
    print("installed :", is_installed())
    print("state     :", query_state())
    print("sessions  :", active_session_ids())
    print("binpath   :", _binpath())
