# -*- coding: utf-8 -*-
"""子进程调用工具：保证不弹出控制台黑窗。

本程序运行时是 GUI（pythonw / windowed exe），但每次调用 `netsh` 这类**控制台
程序**时，Windows 仍会为子进程新建一个控制台窗口并在屏幕上闪一下。必须在
创建子进程时显式指定 CREATE_NO_WINDOW（配合 STARTF_USESHOWWINDOW + SW_HIDE），
才能彻底消除这个黑窗。
"""
from __future__ import annotations

import subprocess
import sys

CREATE_NO_WINDOW = 0x08000000

_IS_WIN = sys.platform == "win32"


def _startup_info():
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return si


def hidden_kwargs() -> dict:
    """返回隐藏窗口所需的 Popen 关键字参数（非 Windows 平台为空）。"""
    if not _IS_WIN:
        return {}
    return {"creationflags": CREATE_NO_WINDOW, "startupinfo": _startup_info()}


def run_hidden(args, **kwargs):
    """等价于 subprocess.run，但绝不弹出黑窗。调用方传入的同名参数优先。"""
    merged = hidden_kwargs()
    merged.update(kwargs)
    return subprocess.run(args, **merged)


def popen_hidden(args, **kwargs):
    """等价于 subprocess.Popen，但绝不弹出黑窗。"""
    merged = hidden_kwargs()
    merged.update(kwargs)
    return subprocess.Popen(args, **merged)
