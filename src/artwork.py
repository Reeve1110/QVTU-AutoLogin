# -*- coding: utf-8 -*-
"""图标绘制：蓝底白色 WiFi 符号 + 右下角状态点。

同一个图形同时用于窗口图标（.ico）与托盘图标（运行时生成），保证辨识度一致。
状态点：绿=已联网，橙=未联网，灰=未监测。
"""
from __future__ import annotations

from PIL import Image, ImageDraw

BLUE = (26, 115, 232, 255)
WHITE = (255, 255, 255, 255)
STATUS_COLORS = {
    "online": (34, 160, 74, 255),
    "offline": (224, 108, 40, 255),
    "stopped": (150, 155, 160, 255),
}
ICO_SIZES = (256, 128, 64, 48, 32, 24, 16)


def draw_icon(size: int = 64, status: str = "stopped", with_status_dot: bool = True):
    """画出图标。size 小于 20 时会自动加粗线条，保证缩到任务栏仍看得清。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    d.rounded_rectangle((0, 0, size - 1, size - 1),
                        radius=max(2, int(size * 0.24)), fill=BLUE)

    cx, cy = size / 2.0, size * 0.76
    width = max(1, int(round(size * 0.085)))
    for ratio in (0.44, 0.30, 0.16):
        r = size * ratio
        d.arc((cx - r, cy - r, cx + r, cy + r), start=200, end=340, fill=WHITE, width=width)
    dot = max(1, int(round(size * 0.062)))
    d.ellipse((cx - dot, cy - dot, cx + dot, cy + dot), fill=WHITE)

    if with_status_dot and size >= 24:
        color = STATUS_COLORS.get(status, STATUS_COLORS["stopped"])
        pad = size * 0.045
        rr = size * 0.20
        d.ellipse((size - pad - 2 * rr, size - pad - 2 * rr, size - pad, size - pad),
                  fill=color, outline=WHITE, width=max(1, int(size * 0.03)))
    return img


def save_ico(path: str) -> str:
    """生成多分辨率 .ico（窗口图标与 exe 图标共用）。"""
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    base = draw_icon(256, "stopped", with_status_dot=False)
    base.save(path, format="ICO", sizes=[(s, s) for s in ICO_SIZES])
    return path
