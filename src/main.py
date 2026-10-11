# -*- coding: utf-8 -*-
"""命令行入口（界面版的备用通道）。

  python main.py            常驻监测（Ctrl+C 退出）
  python main.py --gui      打开可视化界面
  python main.py --once     只检测一次并打印结果（自检）
  python main.py --force    立即同步走一次登录流程
"""
from __future__ import annotations

import argparse
import sys
import time

from paths import data_dir
from config import load
from logbus import LogBus
from engine import Engine
from netcheck import get_wifi_ssid, is_internet_available, is_wifi_in_prefix


def make_console_bus() -> LogBus:
    """写文件的同时把日志打到控制台（兼容 GBK 控制台）。"""
    bus = LogBus()

    def _printer(rec):
        if rec.get("level") == "clear":
            return
        line = rec.get("text", "")
        try:
            print(line, flush=True)
        except Exception:
            try:
                sys.stdout.write(line.encode("gbk", "replace").decode("gbk") + "\n")
                sys.stdout.flush()
            except Exception:
                pass

    bus.subscribe(_printer)
    return bus


def run_once(cfg: dict) -> None:
    ssid = get_wifi_ssid()
    online = is_internet_available(cfg["online_probe_urls"])
    print("当前 WiFi : %s" % (ssid or "(未连接)"))
    print("是否校园网: %s" % is_wifi_in_prefix(ssid, cfg["wifi_prefix"]))
    print("可访问外网: %s" % online)
    print("账号已设置: %s" % bool(str(cfg.get("username", "")).strip()))


def main() -> int:
    ap = argparse.ArgumentParser(description="AutoLogin 校园网自动登录（命令行）")
    ap.add_argument("--gui", action="store_true", help="打开可视化界面")
    ap.add_argument("--once", action="store_true", help="只检测一次并退出")
    ap.add_argument("--force", action="store_true", help="立即同步登录一次")
    args = ap.parse_args()

    if args.gui:
        import gui
        gui.App().run()
        return 0

    cfg = load()

    if args.once:
        run_once(cfg)
        return 0

    bus = make_console_bus()
    engine = Engine(cfg, bus)

    if args.force:
        bus.info("=" * 56)
        bus.info("手动强制登录（CLI）")
        return 0 if engine.login_once() else 1

    bus.info("=" * 56)
    bus.info("AutoLogin 校园网自动登录（CLI）已启动（前缀=%s）" % cfg["wifi_prefix"])
    bus.info("数据目录: %s" % data_dir())
    engine.start()
    try:
        while engine.is_running():
            time.sleep(1)
    except KeyboardInterrupt:
        bus.info("收到中断信号，正在退出…")
    finally:
        engine.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
