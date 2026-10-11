# -*- coding: utf-8 -*-
"""配置模型：读写 config.json，提供默认值、深合并与升级兼容。

DEFAULTS 是唯一权威的结构定义，深合并后旧版 config.json 会自动补齐新键。
save() 时会剔除 DEFAULTS 里已不存在的键（如旧版的 triggers / exit_rules /
check_interval_sec / start_minimized），保证落盘配置永远干净。

默认行为（对应「简化默认设定」）：
  - 首次启动（未保存过账号密码）→ 弹出主窗口引导填写；
    之后每次启动 → 直接隐藏到托盘后台运行（由界面按是否有凭据自动决定）。
  - **开机自启默认不启用**（需要用户在「设置 → 开机自启」里主动勾选）。
  - 引擎为双状态事件驱动：外网连通时只监听系统网络事件 + 每分钟一次心跳，
    外网断开时每 2 秒检测 WiFi；登录失败进入冷却，防止频繁提交被限流。

配置与日志都放在「我的文档\\AutoLogin」下（见 paths.data_dir()）；老版本放在
exe 目录的 config.json 会在首次以 exe 运行时自动迁移过去。
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import threading

from paths import app_dir, data_dir, is_frozen

CONFIG_PATH = os.path.join(data_dir(), "config.json")

DEFAULTS: dict = {
    # 账号与门户
    "wifi_prefix": "QVTU",
    "portal_url": "http://192.168.120.156",
    "username": "",
    "password": "",
    "chrome_channel": "chrome",      # chrome | msedge
    "browser_headless": True,

    # 登录与监听
    "login_retry": 3,
    "login_timeout_sec": 90,
    "login_cooldown_sec": 60,        # 登录失败整轮后的冷却时间
    "offline_poll_sec": 2,           # 外网断开时检测 WiFi 的间隔
    "online_heartbeat_sec": 60,      # 外网正常时的心跳校验间隔
    "probe_timeout_sec": 3,          # 单个联网探针超时；多探针并行，整体耗时≈此值
    "offline_confirm_sec": 1,        # 校园网+断网持续多久才登录（防抖，越小越快）
    "online_probe_urls": [
        "http://www.msftconnecttest.com/connecttest.txt",
        "http://captive.apple.com/hotspot-detect.html",
        "http://connect.rom.miui.com/generate_204",
        "http://www.baidu.com",
    ],

    # 关闭窗口行为
    "on_window_close": "tray",       # exit | tray
    "ask_on_close": True,

    # 开机自启（默认不启用，由用户在设置里主动勾选）
    # mode: service Windows 服务（最快，先于登录就绪）| task 计划任务 | startup 启动文件夹
    "autostart": {
        "enabled": False,
        "mode": "service",
        "delay_sec": 5,              # 登录后延迟秒数，建议 3~8（仅 task 模式用）
        "run_as_admin": False,
    },

    # 通知
    "show_notifications": True,
}

ENUMS = {
    "chrome_channel": ("chrome", "msedge", "auto"),
    "on_window_close": ("tray", "exit"),
    "autostart_mode": ("service", "task", "startup"),
}

# 数值字段取值范围，GUI 的 Spinbox 与保存校验共用
NUM_RANGES = {
    "login_retry": (1, 10),
    "login_timeout_sec": (10, 600),
    "login_cooldown_sec": (10, 3600),
    "offline_poll_sec": (1, 60),
    "online_heartbeat_sec": (10, 3600),
    "probe_timeout_sec": (1, 15),
    "offline_confirm_sec": (0, 10),
    "delay_sec": (3, 600),
}

_lock = threading.RLock()


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _strip_unknown(cfg: dict, ref: dict) -> dict:
    """剔除 DEFAULTS 里已不存在的键（旧版本遗留的 triggers / exit_rules 等）。"""
    out = {}
    for k, v in (cfg or {}).items():
        if k not in ref:
            continue
        if isinstance(v, dict) and isinstance(ref[k], dict):
            out[k] = _strip_unknown(v, ref[k])
        else:
            out[k] = copy.deepcopy(v)
    return out


def has_credentials(cfg: dict) -> bool:
    return bool(str((cfg or {}).get("username", "")).strip()
                and str((cfg or {}).get("password", "")))


def legacy_config_path() -> str:
    """老版本放置 config.json 的位置（程序 / exe 所在目录）。"""
    return os.path.join(app_dir(), "config.json")


def migrate_legacy_config() -> bool:
    """把老版本放在程序目录的 config.json 搬到「我的文档\\AutoLogin」（仅一次）。

    只有当「新位置还没有配置」「老位置确实存在」「当前是打包运行（frozen）」三者
    同时满足时才迁移——源码调试时不动任何文件，避免误搬开发目录里的模板。
    返回是否真的发生了迁移。
    """
    try:
        if not is_frozen():
            return False
        if os.path.exists(CONFIG_PATH):
            return False
        legacy = legacy_config_path()
        if os.path.normcase(os.path.abspath(legacy)) == \
                os.path.normcase(os.path.abspath(CONFIG_PATH)):
            return False
        if not os.path.exists(legacy):
            return False
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        shutil.copyfile(legacy, CONFIG_PATH)
        return True
    except Exception:
        return False


def load() -> dict:
    """读取配置；文件缺失或损坏时回退默认值（顺带完成一次老配置迁移）。"""
    with _lock:
        migrate_legacy_config()
        data = {}
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
            except Exception:
                data = {}
        if not isinstance(data, dict):
            data = {}
        return _deep_merge(DEFAULTS, data)


def save(cfg: dict) -> dict:
    """原子写入（临时文件 + 替换），同时剔除已废弃的键。"""
    with _lock:
        full = _strip_unknown(_deep_merge(DEFAULTS, cfg), DEFAULTS)
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(full, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
        return full


def reset() -> dict:
    return save(copy.deepcopy(DEFAULTS))


def validate(cfg: dict) -> tuple:
    """基础校验，返回 (是否通过, 错误信息)。"""
    if not str(cfg.get("username", "")).strip():
        return False, "账号不能为空"
    if not str(cfg.get("password", "")):
        return False, "密码不能为空"
    if not str(cfg.get("portal_url", "")).strip():
        return False, "门户地址不能为空"
    if not str(cfg.get("wifi_prefix", "")).strip():
        return False, "WiFi 前缀不能为空"

    def _int(section, key, label):
        try:
            v = int(section.get(key))
        except (TypeError, ValueError):
            return None, "%s 必须是整数" % label
        lo, hi = NUM_RANGES[key]
        if not (lo <= v <= hi):
            return None, "%s 需在 %d ~ %d 之间" % (label, lo, hi)
        return v, ""

    for key, label in (("login_retry", "登录重试次数"),
                       ("login_timeout_sec", "登录超时"),
                       ("login_cooldown_sec", "登录失败冷却"),
                       ("offline_poll_sec", "断网检测间隔"),
                       ("online_heartbeat_sec", "外网心跳间隔")):
        _, err = _int(cfg, key, label)
        if err:
            return False, err

    a = cfg.get("autostart", {}) or {}
    if a.get("mode") not in ENUMS["autostart_mode"]:
        return False, "自启方式取值非法"
    if cfg.get("on_window_close") not in ENUMS["on_window_close"]:
        return False, "关闭窗口行为取值非法"
    if cfg.get("chrome_channel") not in ENUMS["chrome_channel"]:
        return False, "浏览器取值非法"
    return True, ""


if __name__ == "__main__":
    c = load()
    print(json.dumps(c, ensure_ascii=False, indent=2))
    print("validate:", validate(c))
