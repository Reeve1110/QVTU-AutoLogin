# -*- coding: utf-8 -*-
"""单实例保护：同一个登录会话里只允许跑一个 AutoLogin。

做法是 Windows 命名互斥体（CreateMutex）。进程一退出，互斥体自动释放，
所以不需要额外的「崩溃清理」逻辑。第二个实例启动时会尽量把已有窗口
提到前台；实在找不到（比如它最小化在托盘里）就弹个提示框告知用户。

互斥体名可用环境变量 ``QVTU_MUTEX_NAME`` 覆盖——自动化测试用它开一个独立
命名空间，免得和用户正在运行的那个实例抢锁。
"""
from __future__ import annotations

import os
import sys

from version import APP_TITLE, MUTEX_NAME

ERROR_ALREADY_EXISTS = 183

_handle = None          # 持有互斥体句柄，进程存活期间不能关闭


def mutex_name() -> str:
    """本次实际使用的互斥体名（可被 QVTU_MUTEX_NAME 覆盖）。"""
    return os.environ.get("QVTU_MUTEX_NAME") or MUTEX_NAME


def _k32():
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    return k32


def acquire(name: str | None = None) -> bool:
    """抢占单实例锁；返回 False 表示已有实例在跑。"""
    global _handle
    if sys.platform != "win32":
        return True
    if _handle is not None:
        return True
    name = name or mutex_name()
    try:
        import ctypes
        k32 = _k32()
        ctypes.set_last_error(0)
        h = k32.CreateMutexW(None, False, name)
        err = ctypes.get_last_error()
        if not h:
            # 创建不出来（多为已存在但权限不同）——当作已在运行，宁可不重复启动
            return False
        if err == ERROR_ALREADY_EXISTS:
            k32.CloseHandle(h)
            return False
        _handle = h
        return True
    except Exception:
        return True     # 非 Windows 或调用异常时不阻塞程序启动


def release() -> None:
    global _handle
    if _handle is None:
        return
    try:
        _k32().CloseHandle(_handle)
    except Exception:
        pass
    _handle = None


def find_window(title_substr: str = APP_TITLE) -> int:
    """返回标题包含 title_substr 的顶层窗口句柄；找不到返回 0。"""
    if sys.platform != "win32":
        return 0
    try:
        import ctypes
        u32 = ctypes.windll.user32
        u32.GetWindowTextLengthW.restype = ctypes.c_int
        hits = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def _cb(hwnd, _lparam):
            n = u32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                u32.GetWindowTextW(hwnd, buf, n + 1)
                if title_substr in buf.value:
                    hits.append(hwnd)
            return True

        u32.EnumWindows(_cb, 0)
        return int(hits[0]) if hits else 0
    except Exception:
        return 0


def activate_window(title_substr: str = APP_TITLE) -> bool:
    """把已有实例的窗口还原并置前（含最小化到托盘、被 withdraw 的情况）。"""
    hwnd = find_window(title_substr)
    if not hwnd:
        return False
    try:
        import ctypes
        u32 = ctypes.windll.user32
        SW_SHOW, SW_RESTORE = 5, 9
        u32.ShowWindow(hwnd, SW_SHOW)
        u32.ShowWindow(hwnd, SW_RESTORE)
        try:
            u32.SetForegroundWindow(hwnd)
        except Exception:
            pass
        return True
    except Exception:
        return False


def notice_already_running(title: str = APP_TITLE) -> None:
    """第二个实例无法唤起已有窗口时，给用户一个明确提示。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None, "%s 已经在运行了。" % APP_TITLE, title, 0x40)
    except Exception:
        pass
