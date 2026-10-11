# -*- coding: utf-8 -*-
"""打包成单文件 exe（PyInstaller）。

    .venv\\Scripts\\python.exe build_exe.py            # 默认 onefile（单个 exe）
    .venv\\Scripts\\python.exe build_exe.py --onedir   # 目录版，启动更快
    .venv\\Scripts\\python.exe build_exe.py --no-publish  # 只出 dist/，不复制到 Release

产物：
  - dist/AutoLogin.exe                              —— 打包产物
  - D:\\Desktop\\QVTU-AutoLogin-release\\AutoLogin.exe            —— 固定名副本（Gitee 仓库用）
  - D:\\Desktop\\QVTU-AutoLogin-release\\AutoLogin_v<版本号>.exe  —— 版本号命名副本
（Release 目录可用环境变量 QVTU_RELEASE_DIR 覆盖；无控制台黑窗）
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from version import APP_NAME, APP_TITLE, APP_VERSION  # noqa: E402
ICON = os.path.join(HERE, "assets", "app.ico")
BUILD_DIR = os.path.join(HERE, "build")
DIST_DIR = os.path.join(HERE, "dist")
VERSION_FILE = os.path.join(BUILD_DIR, "version_info.txt")
SPEC_FILE = os.path.join(BUILD_DIR, APP_NAME + ".spec")
UPX_DIR = os.path.join(HERE, "tools", "upx")          # 随项目携带的 UPX（可选）

# 明确排除、减小体积的模块：
#   - PIL 里本项目用不到的图片格式扩展（AVIF 单个 ~7.5MB）
#   - 其它与本程序无关的重量级可选依赖
EXCLUDES = [
    "PyInstaller", "pytest", "unittest", "pydoc", "doctest", "pdb", "IPython",
    # PIL 未使用的格式插件（artwork 只用 Image.new / ImageDraw / 存 ICO）
    "PIL.AvifImagePlugin", "PIL.WebPImagePlugin", "PIL.WmfImagePlugin",
    "PIL.ImageQt", "PIL.ImageTk", "PIL.ImageShow", "PIL._avif", "PIL._webp",
    "PIL._imagingcms", "PIL._imagingmath", "PIL._imagingmorph",
    # tkinter 之外用不到的 GUI 工具包
    "matplotlib", "numpy", "scipy", "pandas", "setuptools", "pip", "wheel",
]

# 打包时丢弃的数据/二进制文件（文件名或路径包含下列片段即剔除）——纯瘦身：
#   - playwright 的类型定义与 source-map（运行时只依赖 driver/package 下的 JS）
#   - 误被收集进来的 JDK 里的 ucrtbase.dll（不是本程序依赖）
DROP_PATH_HINTS = [
    os.path.join("driver", "package", "types"),
    ".d.ts",
    ".map",
    os.path.join("driver", "package", "README.md"),
    os.path.join("driver", "package", "NOTICE"),
    os.path.join("driver", "package", "ThirdPartyNotices.txt"),
    os.path.join("jdk-"),
]

VERSION_TEMPLATE = """VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=%(v4)s, prodvers=%(v4)s,
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('080404b0', [
        StringStruct('CompanyName', 'AutoLogin'),
        StringStruct('FileDescription', '%(title)s'),
        StringStruct('FileVersion', '%(ver)s'),
        StringStruct('InternalName', '%(name)s'),
        StringStruct('OriginalFilename', '%(name)s.exe'),
        StringStruct('ProductName', '%(title)s'),
        StringStruct('ProductVersion', '%(ver)s'),
        StringStruct('Comments', '微信 %(wechat)s')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""


def write_version_file() -> str:
    from version import CONTACT_WECHAT
    parts = [int(x) for x in APP_VERSION.split(".")]
    while len(parts) < 4:
        parts.append(0)
    os.makedirs(BUILD_DIR, exist_ok=True)
    body = VERSION_TEMPLATE % {
        "v4": tuple(parts[:4]),
        "ver": APP_VERSION,
        "title": APP_TITLE,
        "name": APP_NAME,
        "wechat": CONTACT_WECHAT,
    }
    with open(VERSION_FILE, "w", encoding="utf-8") as f:
        f.write(body)
    return VERSION_FILE


def ensure_icon() -> str:
    if not os.path.exists(ICON):
        import artwork
        artwork.save_ico(ICON)
    return ICON


def find_upx() -> str | None:
    """返回可用的 UPX 目录（含 upx.exe），找不到返回 None。"""
    exe = os.path.join(UPX_DIR, "upx.exe")
    if os.path.exists(exe):
        return UPX_DIR
    found = shutil.which("upx")
    if found:
        return os.path.dirname(os.path.abspath(found))
    return None


def write_spec(mode: str) -> str:
    """生成自己的 .spec（而不是让 PyInstaller 现推一个），以便：
      - 打开/关闭 UPX（指向随项目携带的 tools/upx）；
      - 在 collect_all('playwright') 之后**过滤掉**用不到的数据文件（类型定义等）瘦身。
    """
    upx_dir = find_upx()
    lines = [
        "# -*- mode: python ; coding: utf-8 -*-",
        "# 本文件由 build_exe.py 自动生成，请勿手动修改。",
        "from PyInstaller.utils.hooks import collect_all",
        "",
        "datas = [(%r, 'assets')]" % ICON,
        "binaries = []",
        "hiddenimports = [%s]" % ", ".join(
            repr(x) for x in [
                "pystray._win32",
                "servicemanager", "win32serviceutil", "win32service",
                "win32event", "win32ts", "win32profile", "win32security", "win32process",
            ]
        ),
        "tmp_ret = collect_all('playwright')",
        "datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]",
        "",
        "# ---- 瘦身：剔除 playwright 类型定义 / 误收集的 JDK 等运行时用不到的文件 ----",
        "DROP_HINTS = %r" % (DROP_PATH_HINTS,),
        "def _keep(entry):",
        "    parts = entry if isinstance(entry, (tuple, list)) else (entry,)",
        "    blob = ' '.join(str(x).replace('\\\\', '/').lower() for x in parts)",
        "    return not any(h.replace('\\\\', '/').lower() in blob for h in DROP_HINTS)",
        "datas = [d for d in datas if _keep(d)]",
        "binaries = [b for b in binaries if _keep(b)]",
        "",
        "a = Analysis(",
        "    [%r]," % os.path.join(HERE, "gui.py"),
        "    pathex=[],",
        "    binaries=binaries,",
        "    datas=datas,",
        "    hiddenimports=hiddenimports,",
        "    hookspath=[],",
        "    hooksconfig={},",
        "    runtime_hooks=[],",
        "    excludes=%r," % EXCLUDES,
        "    noarchive=False,",
        "    optimize=0,",
        ")",
        "pyz = PYZ(a.pure)",
        "",
        "exe = EXE(",
        "    pyz,",
        "    a.scripts,",
        "    a.binaries,",
        "    a.datas,",
        "    [],",
        "    name=%r," % APP_NAME,
        "    debug=False,",
        "    bootloader_ignore_signals=False,",
        "    strip=False,",
        "    upx=%r," % bool(upx_dir),
        "    upx_exclude=[],",
        "    runtime_tmpdir=None,",
        "    console=False,",
        "    disable_windowed_traceback=False,",
        "    argv_emulation=False,",
        "    target_arch=None,",
        "    codesign_identity=None,",
        "    entitlements_file=None,",
        "    version=%r," % VERSION_FILE,
        "    icon=[%r]," % ICON,
        ")",
    ]
    if mode == "onedir":
        lines += [
            "",
            "coll = COLLECT(",
            "    exe,",
            "    a.binaries,",
            "    a.datas,",
            "    strip=False,",
            "    upx=%r," % bool(upx_dir),
            "    upx_exclude=[],",
            "    name=%r," % APP_NAME,
            ")",
        ]
    lines.append("")
    os.makedirs(BUILD_DIR, exist_ok=True)
    with open(SPEC_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return SPEC_FILE


def build(mode: str = "onefile") -> str:
    write_version_file()
    ensure_icon()
    spec = write_spec(mode)

    upx_dir = find_upx()
    if upx_dir:
        print("[build] UPX: %s（已启用压缩）" % upx_dir)
    else:
        print("[build] 提示：未找到 UPX，跳过压缩（体积会偏大）")

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--workpath", os.path.join(BUILD_DIR, "work"),
        "--distpath", DIST_DIR,
    ]
    if upx_dir:
        args += ["--upx-dir", upx_dir]
    # 注意：给了 .spec 之后就**不能**再传 --onedir/--onefile，模式已写进 spec。
    args.append(spec)

    print("[build] mode=%s" % mode)
    r = subprocess.run(args, cwd=HERE)
    if r.returncode != 0:
        raise SystemExit("PyInstaller 失败，返回码 %d" % r.returncode)

    target = (os.path.join(DIST_DIR, APP_NAME, APP_NAME + ".exe") if mode == "onedir"
              else os.path.join(DIST_DIR, APP_NAME + ".exe"))
    if not os.path.exists(target):
        raise SystemExit("未找到产物：%s" % target)
    print("[build] 产物:", target, "%.1f MB" % (os.path.getsize(target) / 1024.0 / 1024.0))
    return target


def publish(target: str) -> str:
    """把产物复制到项目外的 Release 文件夹，并按版本号命名。

    产物同时保留固定名与版本号名：
      - Release/AutoLogin.exe            —— 固定名，供 Gitee 仓库直接引用；
      - Release/AutoLogin_v<版本号>.exe  —— 版本号名，历史留存。
    目标目录默认 ``D:\\Desktop\\QVTU-AutoLogin-release``，可用环境变量
    ``QVTU_RELEASE_DIR`` 覆盖。
    """
    rel_dir = os.environ.get("QVTU_RELEASE_DIR", r"D:\Desktop\QVTU-AutoLogin-release")
    os.makedirs(rel_dir, exist_ok=True)
    fixed = os.path.join(rel_dir, APP_NAME + ".exe")
    versioned = os.path.join(rel_dir, "%s_v%s.exe" % (APP_NAME, APP_VERSION))

    # 直接用二进制读写覆盖：绕过可能被劫持成"移到回收站"的删除实现（沙箱/安全软件下会失败）
    with open(target, "rb") as src, open(versioned, "wb") as dst:
        shutil.copyfileobj(src, dst, length=1024 * 1024)
    with open(target, "rb") as src, open(fixed, "wb") as dst:
        shutil.copyfileobj(src, dst, length=1024 * 1024)

    print("[build] 发布固定名:", fixed)
    print("[build] 发布版本名:", versioned)
    return versioned


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--onedir", action="store_true", help="目录版（启动更快，体积按目录算）")
    ap.add_argument("--clean-dist", action="store_true", help="先清空 dist/")
    ap.add_argument("--no-publish", action="store_true",
                    help="只打包到 dist/，不复制到 Release 文件夹")
    args = ap.parse_args()

    if args.clean_dist and os.path.isdir(DIST_DIR):
        shutil.rmtree(DIST_DIR, ignore_errors=True)
    os.makedirs(DIST_DIR, exist_ok=True)

    target = build("onedir" if args.onedir else "onefile")
    if not args.no_publish:
        try:
            publish(target)
        except Exception as e:
            print("[build] 警告：复制到 Release 文件夹失败：%s" % e)
    print("[build] 完成:", target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
