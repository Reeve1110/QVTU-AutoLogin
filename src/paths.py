# -*- coding: utf-8 -*-
"""运行路径解析：同一份代码既能在源码目录运行，也能在打包后的 exe 里运行。

三类目录分工：
  - resource_dir()：只读资源（图标等）。exe 运行时是解压出来的临时目录。
  - app_dir()     ：程序 / exe 自身所在目录。用来定位 exe、写自启快捷方式的目标。
  - data_dir()    ：**用户数据目录**——config.json 与 logs 都放这里
                    （「我的文档\\AutoLogin」）。exe 换位置、换机器，配置与日志都还在，
                    也避免 Program Files 等只读位置写不进去。
"""
from __future__ import annotations

import os
import sys

DATA_DIR_NAME = "AutoLogin"      # 「我的文档」下新建的文件夹名


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> str:
    """程序 / exe 所在目录：源码运行时是项目目录；exe 运行时是 exe 所在目录。"""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def documents_dir() -> str:
    """「我的文档」目录。

    优先用系统已知文件夹 API（能正确处理被重定向到 OneDrive / 自定义位置的
    「文档」），拿不到再回退到 用户目录\\Documents。
    """
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        # CSIDL_PERSONAL = 5（我的文档）；返回 0（S_OK）才算成功
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0 and buf.value:
            return buf.value
    except Exception:
        pass
    home = os.path.expanduser("~")
    for name in ("Documents", "My Documents", "文档"):
        p = os.path.join(home, name)
        if os.path.isdir(p):
            return p
    return os.path.join(home, "Documents")


def data_dir() -> str:
    """用户数据目录：``「我的文档」\\AutoLogin``（config.json / logs 都放这里）。

    可用环境变量 ``QVTU_DATA_DIR`` 覆盖（测试隔离 / 便携运行用）。
    """
    override = os.environ.get("QVTU_DATA_DIR")
    if override:
        return os.path.abspath(override)
    return os.path.join(documents_dir(), DATA_DIR_NAME)


def resource_dir() -> str:
    """只读资源目录（图标等）：exe 运行时是解压出来的临时目录。"""
    if is_frozen():
        return getattr(sys, "_MEIPASS", app_dir())
    return os.path.dirname(os.path.abspath(__file__))


def exe_path() -> str:
    return os.path.abspath(sys.executable)
