# -*- coding: utf-8 -*-
"""系统托盘图标（pystray + Pillow）。依赖缺失时优雅降级为「不可用」，不影响主功能。"""
from __future__ import annotations

import threading

try:
    import pystray
    AVAILABLE = True
except Exception:                                    # noqa: BLE001
    pystray = None
    AVAILABLE = False

from artwork import draw_icon
from version import APP_TITLE

_IMG_CACHE: dict = {}


def available() -> bool:
    return AVAILABLE


def make_image(status: str = "stopped"):
    """按状态生成托盘图标；同一状态只画一次（绘制有开销，界面会频繁调用）。"""
    img = _IMG_CACHE.get(status)
    if img is None:
        img = draw_icon(64, status)
        _IMG_CACHE[status] = img
    return img


class TrayController:
    """封装托盘生命周期。回调由调用方负责切回主线程（如 root.after）。"""

    def __init__(self, on_show, on_login, on_toggle, on_quit, get_state):
        self.on_show = on_show
        self.on_login = on_login
        self.on_toggle = on_toggle
        self.on_quit = on_quit
        self.get_state = get_state          # -> (running, paused, online)
        self._icon = None
        self._status = None

    def available(self) -> bool:
        return AVAILABLE

    def _wrap(self, fn):
        def _inner(icon=None, item=None):
            try:
                fn()
            except Exception:
                pass
        return _inner

    def _menu(self):
        def toggle_text(item):
            try:
                running, paused, _online = self.get_state()
            except Exception:
                running, paused = True, False
            return "暂停监测" if (running and not paused) else "开始监测"

        return pystray.Menu(
            pystray.MenuItem("显示主界面", self._wrap(self.on_show), default=True),
            pystray.MenuItem("立即登录", self._wrap(self.on_login)),
            pystray.MenuItem(toggle_text, self._wrap(self.on_toggle)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出程序", self._wrap(self.on_quit)),
        )

    def start(self, status: str = "stopped", title: str | None = None) -> bool:
        if not AVAILABLE:
            return False
        if self._icon is not None:
            return True
        try:
            self._status = status
            self._icon = pystray.Icon("autologin", make_image(status),
                                      title or APP_TITLE, self._menu())
            self._icon.run_detached()
            return True
        except Exception:
            self._icon = None
            return False

    def update(self, status: str = "stopped", title: str | None = None) -> None:
        """只在状态真变化时才替换图标，避免无谓的重绘。"""
        if self._icon is None or status == self._status:
            return
        self._status = status
        try:
            self._icon.icon = make_image(status)
        except Exception:
            pass
        if title:
            try:
                self._icon.title = title
            except Exception:
                pass

    def notify(self, message: str, title: str | None = None) -> None:
        if self._icon is None:
            return
        try:
            self._icon.notify(message, title or APP_TITLE)
        except Exception:
            pass

    def stop(self) -> None:
        icon, self._icon = self._icon, None
        if icon is None:
            return

        def _safe():
            try:
                icon.stop()
            except Exception:
                pass

        # Icon.stop() 在 win32 后端会阻塞，放后台线程，主流程只放弃引用
        threading.Thread(target=_safe, daemon=True).start()
