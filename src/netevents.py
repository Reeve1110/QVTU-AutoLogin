# -*- coding: utf-8 -*-
"""系统网络变更事件监听（Windows IP Helper API）。

用 NotifyIpInterfaceChange 注册系统级回调：网卡 / 无线网连接、断开、启用
禁用等接口状态变化时由系统主动通知，程序平时完全休眠、不轮询。

回调发生在系统线程上，因此只做「置位」这一件事，绝不回调进 tkinter。
"""
from __future__ import annotations

import ctypes
import sys

AF_UNSPEC = 0
NO_ERROR = 0

# VOID (PVOID CallerContext, PMIB_IPINTERFACE_ROW Row, MIB_NOTIFICATION_TYPE)
_CHANGE_CB = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint)


class NetworkWatcher:
    """把 Windows 的网络接口变更通知翻译成调用方给的可等待回调。"""

    def __init__(self, on_change):
        self._on_change = on_change
        self._dll = None
        self._handle = None
        self._cbref = None

    def start(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            self._dll = ctypes.WinDLL("iphlpapi")
            self._cbref = _CHANGE_CB(self._callback)
            handle = ctypes.c_void_p()
            rc = self._dll.NotifyIpInterfaceChange(
                AF_UNSPEC, self._cbref, None, False, ctypes.byref(handle))
            if rc != NO_ERROR or not handle:
                self._dll = None
                return False
            self._handle = handle
            return True
        except Exception:
            self._dll = None
            self._handle = None
            return False

    def _callback(self, _ctx, _row, _ntype):
        try:
            self._on_change()
        except Exception:
            pass

    def stop(self) -> None:
        handle, self._handle = self._handle, None
        if handle and self._dll:
            try:
                self._dll.CancelMibChangeNotify(handle)
            except Exception:
                pass
        self._dll = None
        self._cbref = None

    def alive(self) -> bool:
        return self._handle is not None
