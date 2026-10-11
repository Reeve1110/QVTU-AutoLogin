# -*- coding: utf-8 -*-
"""生成窗口/exe 图标 assets/app.ico（打包前会自动跑一次，也可手动执行）。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, PROJ)

import artwork  # noqa: E402

if __name__ == "__main__":
    out = os.path.join(PROJ, "assets", "app.ico")
    artwork.save_ico(out)
    print("saved:", out, os.path.getsize(out), "bytes")
