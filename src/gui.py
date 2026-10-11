# -*- coding: utf-8 -*-
"""可视化界面（tkinter / ttk）。

三个页签：状态（实时网络与运行状态）、日志（可清除/保存/过滤）、设置。

启动规则（简化默认设定）：
  首次启动（尚未保存账号密码）→ 弹出主窗口并定位到设置页引导填写；
  之后每次启动 → 直接隐藏到托盘后台运行（--show / --minimized 可强制覆盖）。

线程约定：引擎在后台线程写日志，LogBus 回调只把记录塞进 queue，主线程用
root.after 周期泵出，绝不跨线程直接操作控件。
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import config as config_mod
import autostart as autostart_mod
import paths
from logbus import LogBus
from engine import Engine
import singleinstance
from version import (APP_TITLE, APP_VERSION, CONTACT_WECHAT, GITEE_URL,
                     SERVICE_NAME)

APP_TITLE = APP_TITLE
APP_VERSION = APP_VERSION
CONTACT_WECHAT = CONTACT_WECHAT

ICON_PATH = os.path.join(paths.resource_dir(), "assets", "app.ico")

CHANNEL_PAIRS = [("Chrome", "chrome"), ("Edge", "msedge"), ("自动（优先 Chrome，失败换 Edge）", "auto")]
CLOSE_PAIRS = [("隐藏到托盘", "tray"), ("退出程序", "exit")]
LEVEL_PAIRS = [("全部", "all"), ("仅重要（警告+错误）", "important")]

LOG_TAIL_RENDER = 800        # 首屏与刷新时最多渲染多少行，避免一次性铺满控件
LOG_MAX_LINES = 2500         # 控件里保留的最大行数
TICK_LOG_MS = 200            # 日志泵间隔
TICK_STATUS_MS = 600         # 状态刷新间隔

# 托盘模块惰性加载：PIL + pystray 的导入有明显开销，不该拖慢启动
_UNLOADED = object()
tray_mod = _UNLOADED


def _tray_module():
    global tray_mod
    if tray_mod is _UNLOADED:
        try:
            import tray as _t
            tray_mod = _t
        except Exception:
            tray_mod = None
    return tray_mod


# --------------------------------------------------------------------------- #
# Windows 环境准备与小工具
# --------------------------------------------------------------------------- #
def _enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)      # 按监视器感知
            return
        except Exception:
            pass
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    except Exception:
        pass


def _hide_console_window() -> None:
    """双保险：即使被 python.exe 而非 pythonw.exe 拉起，也把控制台窗口藏掉。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)            # SW_HIDE
    except Exception:
        pass


def _code_of(pairs, label, default):
    for lab, code in pairs:
        if lab == label:
            return code
    return default


def _label_of(pairs, code, default=None):
    for lab, c in pairs:
        if c == code:
            return lab
    return default if default is not None else pairs[0][0]


def _fmt_dur(sec) -> str:
    try:
        sec = int(sec or 0)
    except Exception:
        return "-"
    if sec < 60:
        return "%d 秒" % sec
    m, s = divmod(sec, 60)
    if m < 60:
        return "%d 分 %d 秒" % (m, s)
    h, m = divmod(m, 60)
    return "%d 小时 %d 分" % (h, m)


def _fmt_ts(ts) -> str:
    if not ts:
        return "-"
    try:
        import datetime
        return datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")
    except Exception:
        return "-"


def _copy_to_clipboard(widget, text: str) -> None:
    try:
        widget.clipboard_clear()
        widget.clipboard_append(text)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 主应用
# --------------------------------------------------------------------------- #
class App:
    def __init__(self, cfg: dict | None = None, log_file: str | None = None,
                 start_minimized: bool | None = None):
        self.cfg = cfg if cfg is not None else config_mod.load()
        self._start_minimized = start_minimized      # None = 用配置里的值
        self.log = LogBus(log_file or os.path.join(paths.data_dir(), "logs", "autologin.log"))
        self.log.preload()
        self.engine = Engine(self.cfg, self.log)
        self.engine.set_notify_callback(self._on_engine_notify)

        self._log_q: "queue.Queue" = queue.Queue()
        self.log.subscribe(self._on_log_record)

        self._ui_alive = True
        self._really_quit = False
        self._exit_func = os._exit                # 测试可替换
        self._tray = None
        self._tick_id = None
        self._status_id = None
        self._last_online = False
        self._pending_cfg = None
        self._applied_autostart = self._autostart_signature()
        self._settings_built = False
        self._set_canvas = None
        self._settings_holder = None
        self._log_line_count = 0
        self.txt_log = None
        self.txt_preview = None
        self.root = None

    # ==================== 界面构建 ====================
    def build(self) -> "App":
        _enable_dpi_awareness()
        _hide_console_window()
        self.root = tk.Tk()
        # 标题栏不带版本号：版本号只在「设置 → 关于与联系」里展示
        self.root.title(APP_TITLE)
        self.root.geometry("940x720")
        self.root.minsize(860, 620)
        if self._want_start_minimized():
            try:
                self.root.withdraw()        # 静默启动先藏起来，避免窗口一闪而过
            except Exception:
                pass
        if os.path.exists(ICON_PATH):
            try:
                self.root.iconbitmap(default=ICON_PATH)
            except Exception:
                pass

        self._create_vars()
        self._apply_config_to_ui(self.cfg)

        self._build_header()
        self._build_footer()
        self._build_notebook()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._refresh_log()
        self._refresh_status()
        self._refresh_autostart_state()
        return self

    def _create_vars(self) -> None:
        S, B = tk.StringVar, tk.BooleanVar
        # 账号
        self.v_username = S()
        self.v_password = S()
        self.v_show_pwd = B()
        self.v_wifi_prefix = S()
        self.v_portal_url = S()
        self.v_channel = S()
        self.v_headless = B()
        # 检测
        self.v_retry = S()
        self.v_timeout = S()
        self.v_cooldown = S()
        # 自启
        self.v_auto_enabled = B()
        self.v_auto_delay = S()
        self.v_service_state = S()
        self.v_startup_state = S()
        self.v_task_state = S()
        self.v_wifi_auto = S()
        # 托盘 / 关闭
        self.v_close_action = S()
        self.v_ask_close = B()
        self.v_notify = B()
        # 日志
        self.v_autoscroll = B()
        self.v_level = S()
        # 状态
        self.v_stat_wifi = S()
        self.v_stat_qvtu = S()
        self.v_stat_online = S()
        self.v_stat_state = S()
        self.v_stat_uptime = S()
        self.v_stat_last_login = S()
        self.v_stat_next = S()
        self.v_footer = S(value="就绪")

    def _build_header(self) -> None:
        bar = tk.Frame(self.root, bg="#f4f6f8")
        bar.pack(fill="x", side="top")
        inner = tk.Frame(bar, bg="#f4f6f8")
        inner.pack(fill="x", padx=16, pady=12)

        self.cv_dot = tk.Canvas(inner, width=22, height=22, highlightthickness=0,
                                bg="#f4f6f8")
        self.cv_dot.pack(side="left")
        self._dot = self.cv_dot.create_oval(3, 3, 19, 19, fill="#9aa0a6", outline="")

        txt = tk.Frame(inner, bg="#f4f6f8")
        txt.pack(side="left", padx=(10, 0))
        tk.Label(txt, text=APP_TITLE, bg="#f4f6f8", fg="#202124",
                 font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
        self.lb_head_sub = tk.Label(txt, text="未启动", bg="#f4f6f8", fg="#5f6368",
                                    font=("Microsoft YaHei UI", 9))
        self.lb_head_sub.pack(anchor="w")

    def _build_notebook(self) -> None:
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=12, pady=(4, 8))

        self.tab_status = ttk.Frame(self.nb)
        self.tab_log = ttk.Frame(self.nb)
        self.tab_settings = ttk.Frame(self.nb)
        self.nb.add(self.tab_status, text="  状态  ")
        self.nb.add(self.tab_log, text="  日志  ")
        self.nb.add(self.tab_settings, text="  设置  ")
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self._build_status_tab(self.tab_status)
        self._build_log_tab(self.tab_log)
        self._build_settings_shell(self.tab_settings)

    def _on_tab_changed(self, _evt=None) -> None:
        try:
            if self.nb.index("current") == 2:
                self._ensure_settings_built()
        except Exception:
            pass

    # ---------------- 状态页 ----------------
    def _build_status_tab(self, parent) -> None:
        box = ttk.LabelFrame(parent, text=" 实时状态 ")
        box.pack(fill="x", padx=10, pady=12)

        rows = [
            ("当前 WiFi", self.v_stat_wifi),
            ("是否校园网", self.v_stat_qvtu),
            ("外网状态", self.v_stat_online),
            ("运行状态", self.v_stat_state),
            ("本次运行时长", self.v_stat_uptime),
            ("上次登录成功", self.v_stat_last_login),
            ("登录重试倒计时", self.v_stat_next),
        ]
        for i, (label, var) in enumerate(rows):
            ttk.Label(box, text=label + "：").grid(row=i, column=0, sticky="e", padx=(14, 8), pady=5)
            ttk.Label(box, textvariable=var, foreground="#1a73e8").grid(
                row=i, column=1, sticky="w", pady=5)

        btns = ttk.Frame(parent)
        btns.pack(fill="x", padx=10, pady=(0, 12))
        self.btn_monitor = ttk.Button(btns, text="开始监测", command=self._toggle_monitor)
        self.btn_monitor.pack(side="left")
        ttk.Button(btns, text="立即登录一次", command=self._force_login).pack(side="left", padx=8)
        ttk.Button(btns, text="暂停 / 继续", command=self._toggle_pause).pack(side="left")
        ttk.Button(btns, text="打开日志文件", command=self._open_log_folder).pack(side="right")

        preview = ttk.LabelFrame(parent, text=" 最近日志 ")
        preview.pack(fill="both", expand=True, padx=10, pady=(0, 12))
        self.txt_preview = tk.Text(preview, height=8, state="disabled", wrap="none",
                                   bg="#ffffff", fg="#3c4043", relief="flat",
                                   font=("Consolas", 9))
        self.txt_preview.pack(fill="both", expand=True, padx=6, pady=6)
        for tag, color in (("error", "#c0392b"), ("warn", "#b9770e"), ("success", "#1e8449")):
            self.txt_preview.tag_config(tag, foreground=color)

    # ---------------- 日志页 ----------------
    def _build_log_tab(self, parent) -> None:
        top = ttk.Frame(parent)
        top.pack(fill="x", padx=10, pady=(10, 6))
        ttk.Button(top, text="清除日志", command=self._clear_log).pack(side="left")
        ttk.Button(top, text="保存日志…", command=self._save_log).pack(side="left", padx=8)
        ttk.Button(top, text="刷新", command=self._refresh_log).pack(side="left")
        ttk.Button(top, text="日志文件夹", command=self._open_log_folder).pack(side="left", padx=8)

        ttk.Checkbutton(top, text="自动滚动", variable=self.v_autoscroll).pack(side="right")

        cb = ttk.Combobox(top, textvariable=self.v_level, state="readonly", width=14,
                          values=[p[0] for p in LEVEL_PAIRS])
        cb.pack(side="right")
        cb.bind("<<ComboboxSelected>>", lambda e: self._refresh_log())
        ttk.Label(top, text="显示：").pack(side="right", padx=(12, 4))

        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.txt_log = tk.Text(wrap, state="disabled", wrap="none", bg="#ffffff",
                               fg="#3c4043", relief="flat", font=("Consolas", 9))
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.txt_log.yview)
        hsb = ttk.Scrollbar(wrap, orient="horizontal", command=self.txt_log.xview)
        self.txt_log.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.txt_log.pack(side="top", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        hsb.pack(side="bottom", fill="x")
        for tag, color in (("error", "#c0392b"), ("warn", "#b9770e"), ("success", "#1e8449")):
            self.txt_log.tag_config(tag, foreground=color)

    # ---------------- 设置页 ----------------
    def _build_settings_shell(self, parent) -> None:
        """只先搭滚动容器，六个设置分节等第一次切到本页时再建（省启动时间）。"""
        outer = ttk.Frame(parent)
        outer.pack(fill="both", expand=True)
        self._set_canvas = tk.Canvas(outer, highlightthickness=0, bd=0, bg="#ffffff")
        vsb = ttk.Scrollbar(outer, orient="vertical", command=self._set_canvas.yview)
        holder = ttk.Frame(self._set_canvas)
        win = self._set_canvas.create_window((0, 0), window=holder, anchor="nw")
        holder.bind("<Configure>",
                    lambda e: self._set_canvas.configure(scrollregion=self._set_canvas.bbox("all")))
        self._set_canvas.bind("<Configure>",
                              lambda e: self._set_canvas.itemconfigure(win, width=e.width))
        self._set_canvas.configure(yscrollcommand=vsb.set)
        self._set_canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self._settings_holder = holder

    def _ensure_settings_built(self) -> None:
        if self._settings_built or self._settings_holder is None:
            return
        self._settings_built = True
        holder = self._settings_holder
        self._sec_account(holder)
        self._sec_detect(holder)
        self._sec_autostart(holder)
        self._sec_tray(holder)
        self._sec_about(holder)

        btns = ttk.Frame(holder)
        btns.pack(fill="x", padx=14, pady=(4, 18))
        ttk.Button(btns, text="保存设置", command=self._save_settings).pack(side="left")
        ttk.Label(btns, text="　", foreground="#9aa0a6").pack(side="left")
        ttk.Button(btns, text="清除设置", command=self._purge_system_entries).pack(side="left")
        ttk.Button(btns, text="恢复默认", command=self._restore_defaults).pack(side="left", padx=8)

        self._bind_wheel_recursive(holder)

    def _sec_account(self, parent) -> None:
        f = ttk.LabelFrame(parent, text=" 账号与门户 ")
        f.pack(fill="x", padx=10, pady=(10, 6))
        r = 0
        r = self._row(f, r, "账号：", ttk.Entry(f, textvariable=self.v_username, width=22))
        pwd_row = ttk.Frame(f)
        self.ent_pwd = ttk.Entry(pwd_row, textvariable=self.v_password, width=22, show="●")
        self.ent_pwd.pack(side="left")
        ttk.Checkbutton(pwd_row, text="显示", variable=self.v_show_pwd,
                        command=self._toggle_show_pwd).pack(side="left", padx=(8, 0))
        r = self._row(f, r, "密码：", pwd_row, "仅存本机")
        r = self._row(f, r, "WiFi 前缀：", ttk.Entry(f, textvariable=self.v_wifi_prefix, width=22),
                      "以此开头的 WiFi 才登录")
        r = self._row(f, r, "门户地址：", ttk.Entry(f, textvariable=self.v_portal_url, width=34))
        self.cb_channel = ttk.Combobox(f, textvariable=self.v_channel, state="readonly", width=12,
                                       values=[p[0] for p in CHANNEL_PAIRS])
        r = self._row(f, r, "浏览器：", self.cb_channel, "需已安装")
        r = self._row(f, r, "后台运行：", ttk.Checkbutton(f, text="登录时不显示浏览器窗口",
                                                      variable=self.v_headless))
        row = ttk.Frame(f)
        row.grid(row=r, column=0, columnspan=3, sticky="w", padx=14, pady=(2, 10))
        ttk.Button(row, text="清空账号密码", command=self._clear_credentials).pack(side="left")

    def _sec_detect(self, parent) -> None:
        f = ttk.LabelFrame(parent, text=" 登录与监听 ")
        f.pack(fill="x", padx=10, pady=6)
        r = 0
        r = self._row(f, r, "登录重试次数：",
                      self._spin(f, self.v_retry, config_mod.NUM_RANGES["login_retry"]), "次")
        r = self._row(f, r, "登录超时：",
                      self._spin(f, self.v_timeout, config_mod.NUM_RANGES["login_timeout_sec"]),
                      "秒")
        r = self._row(f, r, "失败冷却：",
                      self._spin(f, self.v_cooldown, config_mod.NUM_RANGES["login_cooldown_sec"]),
                      "秒")

    def _sec_autostart(self, parent) -> None:
        f = ttk.LabelFrame(parent, text=" 开机自启（Windows 服务，抢先启动） ")
        f.pack(fill="x", padx=10, pady=6)
        r = 0

        # 唯一开关：默认走 Windows 服务——服务先于用户登录就绪，用户一进桌面立即拉起。
        # 勾选即自动获取管理员权限；万一服务装不上，会自动退回计划任务 + 启动文件夹。
        r = self._row(f, r, "", ttk.Checkbutton(
            f, text="开机自启（Windows 服务抢先启动）",
            variable=self.v_auto_enabled, command=self._on_auto_toggle))

        r = self._row(f, r, "登录后延迟：",
                      self._spin(f, self.v_auto_delay, config_mod.NUM_RANGES["delay_sec"]),
                      "秒")

        # 三种自启方式的落地状态（只读展示）
        r = self._row(f, r, "Windows 服务：",
                      ttk.Label(f, textvariable=self.v_service_state))
        r = self._row(f, r, "计划任务：",
                      ttk.Label(f, textvariable=self.v_task_state))
        r = self._row(f, r, "启动文件夹：",
                      ttk.Label(f, textvariable=self.v_startup_state))

        wbtn = ttk.Frame(f)
        wbtn.grid(row=r, column=0, columnspan=3, sticky="w", padx=14, pady=(4, 10))
        ttk.Button(wbtn, text="WiFi 自动连接", command=self._check_wifi_auto).pack(side="left")
        ttk.Label(wbtn, textvariable=self.v_wifi_auto, foreground="#5f6368",
                  wraplength=420, justify="left").pack(side="left", padx=8)

    def _sec_tray(self, parent) -> None:
        f = ttk.LabelFrame(parent, text=" 托盘与关闭行为 ")
        f.pack(fill="x", padx=10, pady=6)
        r = 0
        self.cb_close = ttk.Combobox(f, textvariable=self.v_close_action, state="readonly",
                                     width=20, values=[p[0] for p in CLOSE_PAIRS])
        r = self._row(f, r, "关闭窗口时：", self.cb_close)
        r = self._row(f, r, "", ttk.Checkbutton(f, text="关闭时弹窗询问",
                                                variable=self.v_ask_close))
        r = self._row(f, r, "", ttk.Checkbutton(f, text="显示系统通知",
                                                variable=self.v_notify))
        tm = _tray_module()
        if tm is None or not tm.available():
            ttk.Label(f, text="提示：未检测到 pystray / Pillow，托盘功能不可用。",
                      foreground="#c0392b").grid(row=r, column=0, columnspan=3,
                                                 sticky="w", padx=14, pady=(0, 8))

    def _sec_about(self, parent) -> None:
        f = ttk.LabelFrame(parent, text=" 关于与联系 ")
        f.pack(fill="x", padx=10, pady=6)
        r = 0
        r = self._row(f, r, "版本：", ttk.Label(f, text=APP_VERSION, foreground="#202124",
                                            font=("Microsoft YaHei UI", 10, "bold")))
        wechat = ttk.Frame(f)
        ttk.Label(wechat, text=CONTACT_WECHAT, foreground="#1a73e8",
                  font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        ttk.Button(wechat, text="复制微信号",
                   command=self._copy_wechat).pack(side="left", padx=8)
        r = self._row(f, r, "wx：", wechat)

        gitee = ttk.Frame(f)
        self.lb_gitee = tk.Label(gitee, text=GITEE_URL, fg="#1a73e8", cursor="hand2",
                                 font=("Microsoft YaHei UI", 9, "underline"))
        self.lb_gitee.pack(side="left")
        self.lb_gitee.bind("<Button-1>", lambda e: self._open_gitee())
        ttk.Button(gitee, text="复制地址", command=self._copy_gitee).pack(side="left", padx=8)
        ttk.Button(gitee, text="打开链接", command=self._open_gitee).pack(side="left")
        r = self._row(f, r, "项目地址：", gitee)

    # ---------------- 小工具 ----------------
    def _row(self, parent, r, label, widget, hint=""):
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="e", padx=(14, 8), pady=5)
        widget.grid(row=r, column=1, sticky="w", pady=5)
        if hint:
            # wraplength 让长提示自动折行，避免把设置区撑宽而被窗口裁掉
            ttk.Label(parent, text=hint, foreground="#9aa0a6", wraplength=230,
                      justify="left").grid(row=r, column=2, sticky="w", padx=(10, 14), pady=5)
        return r + 1

    @staticmethod
    def _spin(parent, var, rng, width=6):
        lo, hi = rng
        return ttk.Spinbox(parent, from_=lo, to=hi, textvariable=var, width=width)

    def _bind_wheel_recursive(self, widget):
        def _wheel(event):
            try:
                self._set_canvas.yview_scroll(int(-event.delta / 120), "units")
            except Exception:
                pass
        try:
            widget.bind("<MouseWheel>", _wheel)
        except Exception:
            pass
        for ch in widget.winfo_children():
            self._bind_wheel_recursive(ch)

    def _build_footer(self) -> None:
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", side="bottom")
        ttk.Label(bar, textvariable=self.v_footer, foreground="#5f6368").pack(
            side="left", padx=14, pady=6)

    # ==================== 配置 <-> 界面 ====================
    def _apply_config_to_ui(self, cfg: dict) -> None:
        self.v_username.set(cfg.get("username", ""))
        self.v_password.set(cfg.get("password", ""))
        self.v_wifi_prefix.set(cfg.get("wifi_prefix", "QVTU"))
        self.v_portal_url.set(cfg.get("portal_url", ""))
        self.v_channel.set(_label_of(CHANNEL_PAIRS, cfg.get("chrome_channel", "chrome")))
        self.v_headless.set(bool(cfg.get("browser_headless", True)))

        self.v_retry.set(str(cfg.get("login_retry", 3)))
        self.v_timeout.set(str(cfg.get("login_timeout_sec", 90)))
        self.v_cooldown.set(str(cfg.get("login_cooldown_sec", 60)))

        a = cfg.get("autostart", {}) or {}
        self.v_auto_enabled.set(bool(a.get("enabled", False)))
        self.v_auto_delay.set(str(a.get("delay_sec", 5)))
        self.v_service_state.set("-")
        self.v_startup_state.set("-")
        self.v_task_state.set("-")

        self.v_close_action.set(_label_of(CLOSE_PAIRS, cfg.get("on_window_close", "tray")))
        self.v_ask_close.set(bool(cfg.get("ask_on_close", True)))
        self.v_notify.set(bool(cfg.get("show_notifications", True)))

        self.v_autoscroll.set(True)
        self.v_level.set(LEVEL_PAIRS[0][0])
        self.v_wifi_auto.set("未检测")

    def _build_cfg(self) -> dict:
        """从界面读值并组装配置；非法输入抛 ValueError。"""
        def _int(var, key, label):
            raw = str(var.get()).strip()
            if not raw:
                raise ValueError("%s 不能为空" % label)
            try:
                v = int(float(raw))
            except Exception:
                raise ValueError("%s 必须是整数（当前为 %r）" % (label, raw))
            lo, hi = config_mod.NUM_RANGES[key]
            if not (lo <= v <= hi):
                raise ValueError("%s 需在 %d ~ %d 之间" % (label, lo, hi))
            return v

        cfg = dict(self.cfg)
        cfg["username"] = self.v_username.get().strip()
        cfg["password"] = self.v_password.get()
        cfg["wifi_prefix"] = self.v_wifi_prefix.get().strip()
        cfg["portal_url"] = self.v_portal_url.get().strip()
        cfg["chrome_channel"] = _code_of(CHANNEL_PAIRS, self.v_channel.get(), "chrome")
        cfg["browser_headless"] = bool(self.v_headless.get())
        cfg["login_retry"] = _int(self.v_retry, "login_retry", "登录重试次数")
        cfg["login_timeout_sec"] = _int(self.v_timeout, "login_timeout_sec", "登录超时")
        cfg["login_cooldown_sec"] = _int(self.v_cooldown, "login_cooldown_sec", "失败冷却")
        cfg["autostart"] = {
            "enabled": bool(self.v_auto_enabled.get()),
            # v26.10.5：默认走 Windows 服务；界面不提供方式下拉，沿用已保存的方式
            "mode": (self.cfg.get("autostart", {}) or {}).get("mode", "service"),
            "delay_sec": _int(self.v_auto_delay, "delay_sec", "登录后延迟"),
            "run_as_admin": True,
        }
        cfg["on_window_close"] = _code_of(CLOSE_PAIRS, self.v_close_action.get(), "tray")
        cfg["ask_on_close"] = bool(self.v_ask_close.get())
        cfg["show_notifications"] = bool(self.v_notify.get())
        return cfg

    def _collect_config(self) -> bool:
        try:
            cfg = self._build_cfg()
        except ValueError as e:
            messagebox.showerror("输入有误", str(e))
            return False
        ok, msg = config_mod.validate(cfg)
        if not ok:
            messagebox.showerror("设置不合格", msg)
            return False
        self._pending_cfg = cfg
        return True

    def _autostart_signature(self):
        a = (self.cfg or {}).get("autostart", {}) or {}
        return (bool(a.get("enabled")), int(a.get("delay_sec", 0) or 0))

    # ==================== 动作 ====================
    def _save_settings(self) -> bool:
        """保存界面上的设置到 config.json（不含系统自启项）。"""
        if not self._collect_config():
            return False
        self.cfg = config_mod.save(self._pending_cfg)
        self.engine.update_config(self.cfg)
        self.log.success("设置已保存")
        self.v_footer.set("设置已保存")
        self._notify_result(True, "设置已保存")
        self._refresh_autostart_state()
        return True

    def _restore_defaults(self):
        if not messagebox.askyesno("恢复默认", "确定要把所有设置恢复为默认值吗？\n（账号密码也会被清空）"):
            return
        self.cfg = config_mod.reset()
        self.engine.update_config(self.cfg)
        self._apply_config_to_ui(self.cfg)
        self._refresh_autostart_state()
        self.log.warn("已恢复默认设置")
        self.v_footer.set("已恢复默认设置")
        self._notify_result(True, "已恢复默认设置（账号密码已清空）")

    def _clear_credentials(self):
        if not messagebox.askyesno("清空账号密码",
                                   "确定要清空已保存的账号和密码吗？\n（清空后需重新填写才能自动登录）"):
            return
        self.v_username.set("")
        self.v_password.set("")
        cfg = self._build_cfg()
        cfg["username"] = ""
        cfg["password"] = ""
        self.cfg = config_mod.save(cfg)
        self.engine.update_config(self.cfg)
        self.log.warn("已清空本机保存的账号密码")
        self.v_footer.set("账号密码已清空")
        self._notify_result(True, "账号密码已清空")

    def _toggle_show_pwd(self):
        try:
            self.ent_pwd.configure(show="" if self.v_show_pwd.get() else "●")
        except Exception:
            pass

    def _copy_wechat(self):
        _copy_to_clipboard(self.root, CONTACT_WECHAT)
        self.v_footer.set("微信号 %s 已复制到剪贴板" % CONTACT_WECHAT)

    def _copy_gitee(self):
        _copy_to_clipboard(self.root, GITEE_URL)
        self.v_footer.set("项目地址已复制到剪贴板")

    def _open_gitee(self):
        try:
            if sys.platform == "win32":
                os.startfile(GITEE_URL)        # noqa: S606
            else:
                import subprocess
                subprocess.Popen(["xdg-open", GITEE_URL])
            self.v_footer.set("已在浏览器中打开项目地址")
        except Exception as e:
            messagebox.showerror("打开失败", str(e))

    # ---------------- 开机自启（Windows 服务 / 计划任务 / 启动文件夹） ----------------
    def _on_auto_toggle(self):
        """「开机自启」勾选框的回调：勾选→提权写服务；取消→移除系统条目。"""
        want = bool(self.v_auto_enabled.get())
        if want:
            self._enable_autostart()
        else:
            self._disable_autostart()

    def _enable_autostart(self):
        """开启开机自启：默认写 Windows 服务（开机抢先启动），需要管理员权限。

        普通权限下注册服务会被系统拒绝，因此：
          · 已是管理员 → 直接写入系统；
          · 普通用户 → 保存设置后请求一次 UAC 提权，由提权后的实例
            （--apply-autostart）安装服务，写完即退出；当前窗口继续在托盘运行。
        若服务装不上，autostart.apply_autostart 会自动退回计划任务 + 启动文件夹。
        """
        if not self._collect_config():
            self.v_auto_enabled.set(False)      # 收集失败则回滚勾选
            return
        self._pending_cfg["autostart"] = dict(
            self._pending_cfg.get("autostart", {}) or {},
            enabled=True, mode="service", run_as_admin=True)
        try:
            self.cfg = config_mod.save(self._pending_cfg)
            self.engine.update_config(self.cfg)
        except Exception as e:
            messagebox.showerror("开启失败", "保存设置失败：%s" % e)
            self.v_auto_enabled.set(False)
            self._notify_result(False, "开启开机自启失败：保存设置出错")
            return

        if autostart_mod.is_admin():
            self.log.info("开启开机自启：当前已是管理员，直接写入系统")
            self._do_apply_autostart(quiet=False)
            return

        if not messagebox.askyesno(
                "开启开机自启",
                "开启开机自启需要管理员权限（安装 Windows 服务），接下来会弹出 UAC 授权框，"
                "请点『是』。\n\n确认后将自动完成：\n"
                "  · 安装「开机抢先启动」Windows 服务（服务先于登录就绪，用户一进桌面立即拉起程序）\n\n"
                "（程序窗口会保持不变，仍在托盘后台运行）\n\n现在继续吗？"):
            self.log.info("用户取消开启开机自启")
            self._rollback_autostart_enabled()
            return

        if autostart_mod.relaunch_as_admin(extra_args="--apply-autostart"):
            self.log.info("已请求提权安装自启服务，请在 UAC 授权框中确认")
            self.v_footer.set("请在 UAC 授权框中点『是』，完成后自动安装开机自启服务…")
            # 提权实例写完即退出，界面无法直接收到回执；稍后多刷几次状态把结果带回来
            for delay in (2500, 6000, 11000):
                try:
                    self.root.after(delay, self._refresh_autostart_state)
                except Exception:
                    pass
        else:
            self.log.error("开启开机自启失败：未取得管理员权限")
            messagebox.showerror("开启失败", "没能取得管理员权限（可能取消了 UAC 授权）。")
            self._rollback_autostart_enabled()
            self._notify_result(False, "开启开机自启失败：未取得管理员权限")
        self._refresh_autostart_state()

    def _rollback_autostart_enabled(self):
        """开启流程被取消/失败：把界面上刚勾上的自启回滚为关闭，并同步配置。"""
        try:
            self.v_auto_enabled.set(False)
        except Exception:
            pass
        try:
            if self.cfg is not None:
                self.cfg["autostart"] = dict(self.cfg.get("autostart", {}), enabled=False)
                self.cfg = config_mod.save(self.cfg)
                self.engine.update_config(self.cfg)
        except Exception:
            pass
        self._refresh_autostart_state()

    def _do_apply_autostart(self, quiet=False) -> bool:
        """把当前配置的自启设置落到系统（服务/计划任务/启动文件夹），并刷新状态。

        由 `_enable_autostart` / `_disable_autostart` 调用；提权实例
        （``--apply-autostart``）也走同一个函数，保证行为一致。
        """
        try:
            r = autostart_mod.apply_autostart(self.cfg)
        except Exception as e:
            self.log.error("写入开机自启失败：%s" % e)
            if not quiet:
                messagebox.showerror("开启失败", "写入系统失败：%s" % e)
            self.v_auto_enabled.set(False)
            self._notify_result(False, "写入开机自启失败：%s" % e)
            self._refresh_autostart_state()
            return False

        ok = bool(r.get("ok"))
        detail = r.get("detail") or ("；".join(r.get("errors") or []) or "未产生变更")
        if ok:
            self.log.success(detail)
            self.v_footer.set(detail)
            if not quiet:
                messagebox.showinfo("开机自启", detail)
            self._notify_result(True, detail)
        else:
            self.log.error(detail)
            if not quiet:
                messagebox.showerror("开机自启失败", detail)
            self._notify_result(False, detail)

        # 把「真实生效状态」回写配置与界面（失败就自动取消勾选）
        shown = bool(r.get("enabled")) if ok else False
        try:
            self.cfg["autostart"] = dict(self.cfg.get("autostart", {}), enabled=shown)
            self.cfg = config_mod.save(self.cfg)
            self.engine.update_config(self.cfg)
            self._applied_autostart = self._autostart_signature()
        except Exception:
            pass
        try:
            self.v_auto_enabled.set(shown)
        except Exception:
            pass
        self._refresh_autostart_state()
        return ok

    def _disable_autostart(self):
        """关闭开机自启：移除 Windows 服务与备用条目（计划任务 / 启动文件夹）。

        移除 Windows 服务需要管理员权限——若当前不是管理员且确实装了服务，
        就先保存配置并请求一次 UAC（由提权实例完成移除）。
        """
        try:
            self.cfg["autostart"] = dict(self.cfg.get("autostart", {}), enabled=False)
            self.cfg = config_mod.save(self.cfg)
            self.engine.update_config(self.cfg)
        except Exception as e:
            messagebox.showerror("关闭失败", "保存设置失败：%s" % e)
            return

        need_admin = autostart_mod.service_installed() and not autostart_mod.is_admin()
        if need_admin:
            if not messagebox.askyesno(
                    "关闭开机自启",
                    "移除 Windows 服务需要管理员权限，接下来会弹出 UAC 授权框，请点『是』。\n\n"
                    "（程序窗口会保持不变）\n\n现在继续吗？"):
                self.log.info("用户取消关闭开机自启")
                self.v_auto_enabled.set(True)     # 还没真正关掉，恢复勾选
                return
            if autostart_mod.relaunch_as_admin(extra_args="--apply-autostart"):
                self.v_footer.set("请在 UAC 授权框中点『是』，完成后自动移除自启服务…")
                for delay in (2500, 6000, 11000):
                    try:
                        self.root.after(delay, self._refresh_autostart_state)
                    except Exception:
                        pass
            else:
                messagebox.showerror("关闭失败", "没能取得管理员权限（可能取消了 UAC 授权）。")
                self.v_auto_enabled.set(True)
            return

        self._do_apply_autostart(quiet=True)


    # ---------------- WiFi 自动连接 / 系统残留清理 ----------------
    def _check_wifi_auto(self):
        """手动检测校园网 WiFi 配置是否开启自动连接；未开启的提示一键设置。"""
        prefix = self.v_wifi_prefix.get().strip() or "QVTU"
        self.v_wifi_auto.set("检测中…")
        try:
            import netcheck
            missing = netcheck.profiles_missing_autoconnect(prefix)
        except Exception as e:
            self.v_wifi_auto.set("检测失败：%s" % e)
            self._notify_result(False, "WiFi 自动连接检测失败：%s" % e)
            return
        if not missing:
            self.v_wifi_auto.set("全部已开启自动连接")
            self._notify_result(True, "校园网 WiFi 均已开启「自动连接」")
            return
        names = "、".join(missing)
        self.v_wifi_auto.set("%s 未开启自动连接" % names)
        self._notify_result(True, "检测完成：%s 未开启自动连接" % names)
        if not messagebox.askyesno(
                "开启自动连接",
                "以下校园网 WiFi 配置未开启「自动连接」：\n\n%s\n\n"
                "开启后，电脑在校园内会自动连上校园网（否则开机后可能停留在别的网络"
                "或断网状态）。\n\n现在开启吗？（需要管理员权限，会弹 UAC 确认框）" % names):
            self.log.info("用户选择暂不开启 WiFi 自动连接")
            return
        for name in missing:
            try:
                import netcheck
                ok, msg = netcheck.set_profile_autoconnect(name)
                self.log.info("WiFi %s 自动连接：%s" % (name, msg))
            except Exception as e:
                ok, msg = False, str(e)
            if not ok:
                messagebox.showerror("开启失败", "%s：%s" % (name, msg))
                self._notify_result(False, "开启「%s」自动连接失败：%s" % (name, msg))
                return
        self.v_wifi_auto.set("已请求开启，稍后可重新检测确认")
        self._notify_result(True, "已请求开启自动连接（如有 UAC 请确认），稍后可重新检测")

    def _check_wifi_autoconnect_async(self):
        """启动时后台检测一次；发现未开启自动连接就用托盘气泡提醒（不打断使用）。"""
        prefix = (self.cfg or {}).get("wifi_prefix", "QVTU")

        def _job():
            try:
                import netcheck
                missing = netcheck.profiles_missing_autoconnect(prefix)
            except Exception:
                return
            if not missing:
                return
            names = "、".join(missing)
            self.log.warn("检测到 WiFi（%s）未开启「自动连接」，开机后可能不会自动连上校园网；"
                          "可在设置 → 开机自启中一键开启" % names)
            self._ui_call(self._notify,
                          "WiFi（%s）未开启自动连接，可能影响开机自动联网；"
                          "可打开主界面在设置中一键开启" % names)

        threading.Thread(target=_job, name="autologin-wifiauto", daemon=True).start()

    def _purge_system_entries(self):
        """清除设置：移除本程序在系统里留下的自启痕迹（只删本程序的条目）。

        与「恢复默认」区分：本操作**只清理系统侧**（任务计划程序 / 启动文件夹里
        本程序 QVTUAutoLogin 的条目），**不改动 config.json 里的设置与账号密码**。
        """
        try:
            entries = autostart_mod.list_system_entries()
        except Exception as e:
            messagebox.showerror("读取失败", str(e))
            self._notify_result(False, "读取系统自启条目失败：%s" % e)
            return
        items = []
        if entries.get("service"):
            items.append("Windows 服务：QVTUAutoLoginService")
        if entries["tasks"]:
            items.append("计划任务：" + "、".join(entries["tasks"]))
        if entries["shortcuts"]:
            items.append("启动文件夹快捷方式：" + "、".join(entries["shortcuts"]))
        if not items:
            messagebox.showinfo("清除设置", "系统中没有找到本程序留下的自启条目，无需清理。")
            self._notify_result(True, "系统中没有需要清理的自启条目")
            return
        if not messagebox.askyesno(
                "清除设置",
                "将删除以下系统自启条目（仅限本程序 QVTUAutoLogin 创建的内容，"
                "不影响其它任何程序；**不改变你的配置与账号密码**）：\n\n%s\n\n"
                "删除后将不再开机自启，确定继续吗？" % "\n".join(items)):
            return
        try:
            removed = autostart_mod.purge_system_entries()
        except Exception as e:
            messagebox.showerror("清除失败", str(e))
            self._notify_result(False, "清除设置失败：%s" % e)
            return
        parts = []
        if removed.get("service"):
            parts.append("Windows 服务")
        if removed["tasks"]:
            parts.append("计划任务 %s" % "、".join(removed["tasks"]))
        if removed["shortcuts"]:
            parts.append("快捷方式 %s" % "、".join(removed["shortcuts"]))
        msg = "已清除：%s" % "；".join(parts) if parts else "没有需要清除的条目"
        if entries.get("service") and not removed.get("service"):
            msg += "\n（Windows 服务需要管理员权限才能移除，请以管理员身份重新打开本程序后再清除）"
        # 系统条目已被移除 → 把配置里的自启开关同步为关闭，避免状态显示不一致
        try:
            self.cfg["autostart"] = dict(self.cfg.get("autostart", {}), enabled=False)
            self.cfg = config_mod.save(self.cfg)
            self.v_auto_enabled.set(False)
            self._applied_autostart = self._autostart_signature()
            self.engine.update_config(self.cfg)
        except Exception:
            pass
        self.log.warn("已清除系统自启条目：%s" % (parts and "；".join(parts) or "无"))
        messagebox.showinfo("清除设置", msg + "\n\n（开机自启已关闭，可在上方重新勾选开启）")
        self._notify_result(True, "已清除设置：%s" % (parts and "；".join(parts) or "无"))
        self._refresh_autostart_state()

    # ---------------- 引擎控制 ----------------
    def start_monitoring(self) -> None:
        if self.engine.start():
            self.log.info("已开始监测")
        self._refresh_status()

    def _toggle_monitor(self):
        if self.engine.is_running():
            self.engine.stop()
            self.log.info("已停止监测")
            self._notify_result(True, "已停止监测网络")
        else:
            self.engine.start()
            self.log.info("已开始监测")
            self._notify_result(True, "已开始监测网络")
        self._refresh_status()

    def _toggle_pause(self):
        paused = self.engine.toggle_pause()
        self.log.info("已暂停监测" if paused else "已继续监测")
        self._refresh_status()

    def _force_login(self):
        self.engine.force_login()
        self.log.info("已请求立即登录一次")
        self.v_footer.set("正在尝试登录…")
        self._refresh_status()

    # ---------------- 日志 ----------------
    def _on_log_record(self, rec):
        """可能来自后台线程，只入队，绝不碰控件。"""
        try:
            self._log_q.put(rec)
        except Exception:
            pass

    def _level_pass(self, level: str) -> bool:
        want = _code_of(LEVEL_PAIRS, self.v_level.get() if hasattr(self, "v_level") else "全部", "all")
        if want == "important":
            return level in ("warn", "error")
        return True

    def _insert_line(self, widget, text, tag) -> None:
        if widget is None:
            return
        try:
            widget.configure(state="normal")
            widget.insert(tk.END, text + "\n", tag)
            widget.configure(state="disabled")
        except Exception:
            pass

    def _trim_widget(self, widget, force=False) -> None:
        """按计数器判断是否需要裁剪，避免每次都去问 Tcl 要行号。"""
        if widget is None:
            return
        if not force and self._log_line_count <= LOG_MAX_LINES:
            return
        try:
            total = int(widget.index("end-1c").split(".")[0])
            if total > LOG_MAX_LINES:
                cut = total - LOG_MAX_LINES // 2
                widget.configure(state="normal")
                widget.delete("1.0", "%d.0" % (cut + 1))
                widget.configure(state="disabled")
                self._log_line_count = int(widget.index("end-1c").split(".")[0])
        except Exception:
            pass

    def _append_log(self, rec) -> None:
        level = rec.get("level", "info")
        if level == "clear":
            self._reset_texts()
            return
        if not self._level_pass(level):
            return
        text = rec.get("text", "")
        tag = level if level in ("warn", "error", "success") else ""
        self._insert_line(self.txt_log, text, tag)
        self._insert_line(self.txt_preview, text, tag)
        self._log_line_count += 1
        self._trim_widget(self.txt_log)
        self._trim_widget(self.txt_preview)
        if self.v_autoscroll.get():
            try:
                self.txt_log.see(tk.END)
                self.txt_preview.see(tk.END)
            except Exception:
                pass

    def _reset_texts(self):
        for widget in (self.txt_log, self.txt_preview):
            self._set_text(widget, "")
        self._log_line_count = 0

    @staticmethod
    def _set_text(widget, s):
        if widget is None:
            return
        try:
            widget.configure(state="normal")
            widget.delete("1.0", tk.END)
            if s:
                widget.insert("1.0", s)
            widget.configure(state="disabled")
        except Exception:
            pass

    def _refresh_log(self):
        """整体重绘日志（只渲染末尾 LOG_TAIL_RENDER 行，避免一次性铺满大文本）。"""
        if self.txt_log is None:
            return
        recs = [r for r in self.log.records_tail(LOG_TAIL_RENDER) if self._level_pass(r.get("level", "info"))]
        lines = [r["text"] for r in recs]
        body = "\n".join(lines) + ("\n" if lines else "")
        self._set_text(self.txt_log, body)
        self._set_text(self.txt_preview, "\n".join(lines[-60:]) + ("\n" if lines else ""))
        self._log_line_count = len(lines)
        if self.v_autoscroll.get():
            try:
                self.txt_log.see(tk.END)
            except Exception:
                pass

    def _clear_log(self):
        if not messagebox.askyesno("清除日志", "确定要清除当前所有日志吗？（日志文件也会被清空）"):
            return
        self.log.clear()
        self._reset_texts()
        self.v_footer.set("日志已清除")

    def _save_log(self):
        path = filedialog.asksaveasfilename(
            title="保存日志", defaultextension=".txt",
            initialfile="autologin_log.txt",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.log.text() + "\n")
            self.v_footer.set("日志已保存到 %s" % path)
            messagebox.showinfo("保存日志", "已保存到：\n%s" % path)
        except Exception as e:
            messagebox.showerror("保存失败", str(e))

    def _open_log_folder(self):
        try:
            folder = os.path.dirname(self.log.log_file)
            os.makedirs(folder, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(folder)          # noqa: S606
            else:
                import subprocess
                subprocess.Popen(["xdg-open", folder])
        except Exception as e:
            messagebox.showerror("打开失败", str(e))

    # ---------------- 状态刷新 ----------------
    def _pump_once(self) -> None:
        """把后台线程塞进队列的日志记录搬到控件上（只做这一件事）。"""
        drained = 0
        try:
            while drained < 200:
                rec = self._log_q.get_nowait()
                self._append_log(rec)
                drained += 1
        except queue.Empty:
            pass

    def _tick(self) -> None:
        if not self._ui_alive:
            return
        try:
            self._pump_once()
        except Exception:
            pass
        try:
            self._tick_id = self.root.after(TICK_LOG_MS, self._tick)
        except Exception:
            pass

    def _status_tick(self) -> None:
        if not self._ui_alive:
            return
        try:
            self._refresh_status()
        except Exception:
            pass
        try:
            self._status_id = self.root.after(TICK_STATUS_MS, self._status_tick)
        except Exception:
            pass

    def _svar(self, var, text) -> None:
        """只在值真变化时才写回，减少 tkinter 重绘。"""
        try:
            if var.get() != text:
                var.set(text)
        except Exception:
            pass

    def _refresh_status(self) -> None:
        st = self.engine.status()
        self._last_online = bool(st.get("online"))

        self._svar(self.v_stat_wifi, st.get("ssid") or "（未连接）")
        self._svar(self.v_stat_qvtu, "是" if st.get("is_qvtu") else "否")
        self._svar(self.v_stat_online, "外网已连通" if st.get("online") else "外网断开")
        self._svar(self.v_stat_state, st.get("state_text") or "-")
        self._svar(self.v_stat_uptime, _fmt_dur(st.get("uptime_sec", 0)) if st.get("running") else "-")
        self._svar(self.v_stat_last_login, _fmt_ts(st.get("last_login")))
        nxt = st.get("next_check_in", 0)
        self._svar(self.v_stat_next, ("%d 秒" % nxt) if (st.get("running") and nxt) else "-")

        state = st.get("state")
        if not st.get("running"):
            color, subtitle = "#9aa0a6", "未启动"
        elif st.get("paused"):
            color, subtitle = "#9aa0a6", "已暂停"
        elif state == "logging_in":
            color, subtitle = "#1a73e8", "正在登录…"
        elif st.get("online"):
            color, subtitle = "#1e8449", "外网已连通"
        elif state == "cooldown":
            color, subtitle = "#e37400", "登录失败，冷却中"
        else:
            color, subtitle = "#e37400", "外网断开，监听中"

        try:
            self.cv_dot.itemconfigure(self._dot, fill=color)
            if self.lb_head_sub.cget("text") != subtitle:
                self.lb_head_sub.configure(text=subtitle)
        except Exception:
            pass
        try:
            want = "停止监测" if st.get("running") else "开始监测"
            if self.btn_monitor.cget("text") != want:
                self.btn_monitor.configure(text=want)
        except Exception:
            pass

        if self._tray is not None:
            status = "online" if st.get("online") else ("stopped" if not st.get("running") else "offline")
            self._tray.update(status=status,
                              title="%s（%s）" % (APP_TITLE, subtitle))

    def _refresh_autostart_state(self) -> None:
        """刷新「Windows 服务 / 计划任务 / 启动文件夹」三处的落地状态（只读展示）。"""
        try:
            st = autostart_mod.status()
        except Exception:
            st = {"service": False, "startup": False, "task": False}
        try:
            self.v_service_state.set(autostart_mod.service_info().get("text", "-"))
        except Exception:
            self.v_service_state.set("-")
        try:
            self.v_startup_state.set("已启用" if st.get("startup") else "未启用")
            self.v_task_state.set("已启用" if st.get("task") else "未启用")
        except Exception:
            pass

    # ---------------- 托盘 / 关闭 ----------------
    def _ui_call(self, fn, *a) -> None:
        if not getattr(self, "_ui_alive", True):
            return
        try:
            self.root.after(0, lambda: fn(*a))
        except Exception:
            pass

    def _ensure_tray(self) -> bool:
        if self._tray is not None:
            return True
        tm = _tray_module()
        if tm is None or not tm.available():
            return False
        self._tray = tm.TrayController(
            on_show=lambda: self._ui_call(self._show_window),
            on_login=lambda: self._ui_call(self.engine.force_login),
            on_toggle=lambda: self._ui_call(self._toggle_monitor),
            on_quit=lambda: self._ui_call(self._quit_app),
            get_state=lambda: (self.engine.is_running(), self.engine.is_paused(), self._last_online),
        )
        return self._tray.start()

    def _notify(self, message: str, title: str | None = None) -> bool:
        """发一条系统（托盘）通知；返回是否真的发出（供退出前决定要不要等它显示）。

        优先用 pystray 的 notify —— 在 Windows 上就是系统自带的托盘通知（气泡），
        会进「操作中心 / 通知中心」。托盘不可用时静默跳过，不影响主功能。
        """
        if not self.cfg.get("show_notifications", True):
            return False
        if self._tray is None:
            return False
        try:
            self._tray.notify(message, title or APP_TITLE)
            return True
        except Exception:
            return False

    def _notify_result(self, ok: bool, text: str, title: str | None = None) -> bool:
        """关键操作完成时的统一通知：明确标出「完成 / 失败」。"""
        return self._notify("%s：%s" % ("完成" if ok else "失败", text), title)

    def _show_window(self) -> None:
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass

    def _hide_to_tray(self) -> None:
        if not self._ensure_tray():
            self.log.warn("托盘不可用，改为直接退出")
            self._quit_app()
            return
        try:
            self.root.withdraw()
        except Exception:
            pass
        self._notify("程序已隐藏到托盘，仍在后台运行")

    def _ask_close_choice(self, default_action: str):
        """自定义模态框：返回 (choice, dont_ask)，choice ∈ {tray, exit, cancel}。"""
        win = tk.Toplevel(self.root)
        win.title("关闭")
        win.resizable(False, False)
        win.transient(self.root)
        res = {"choice": "cancel", "dont_ask": False}
        v_dont = tk.BooleanVar(value=False)

        def _pick(c):
            res["choice"] = c
            res["dont_ask"] = bool(v_dont.get())
            try:
                win.destroy()
            except Exception:
                pass

        win.protocol("WM_DELETE_WINDOW", lambda: _pick("cancel"))
        ttk.Label(win, text="要把程序隐藏到托盘继续在后台运行，还是直接退出？",
                  font=("Microsoft YaHei UI", 10)).pack(padx=18, pady=(16, 6))
        row = ttk.Frame(win)
        row.pack(padx=18, pady=10)
        ttk.Button(row, text="隐藏到托盘", command=lambda: _pick("tray")).pack(side="left", padx=6)
        ttk.Button(row, text="退出程序", command=lambda: _pick("exit")).pack(side="left", padx=6)
        ttk.Button(row, text="取消", command=lambda: _pick("cancel")).pack(side="left", padx=6)
        ttk.Checkbutton(win, text="不再询问，记住我的选择",
                        variable=v_dont).pack(padx=18, pady=(2, 16))
        try:
            win.grab_set()
            win.update_idletasks()
        except Exception:
            pass
        try:
            self.root.wait_window(win)
        except Exception:
            pass
        return res["choice"], res["dont_ask"]

    def _ui_close_action(self) -> str:
        try:
            return _code_of(CLOSE_PAIRS, self.v_close_action.get(),
                            self.cfg.get("on_window_close", "tray"))
        except Exception:
            return self.cfg.get("on_window_close", "tray")

    def _on_close(self):
        if self._really_quit:
            return
        action = self._ui_close_action()

        ask = True
        try:
            ask = bool(self.v_ask_close.get())
        except Exception:
            pass

        if ask:
            choice, dont_ask = self._ask_close_choice(action)
            if choice == "cancel":
                return
            if dont_ask:
                try:
                    self.v_ask_close.set(False)
                except Exception:
                    pass
                self.cfg["ask_on_close"] = False
                self.cfg["on_window_close"] = choice
                try:
                    self.cfg = config_mod.save(self.cfg)
                except Exception:
                    pass
            action = choice

        if action == "tray":
            self._hide_to_tray()
        else:
            self._quit_app()

    def _on_engine_notify(self, message: str) -> None:
        """引擎线程回调：登录成功/失败等气泡通知，切回主线程再发。"""
        self._ui_call(self._notify, message)

    def _quit_app(self, silent: bool = False) -> None:
        if self._really_quit:
            return
        self._really_quit = True
        self._ui_alive = False
        sent = False if silent else self._notify("程序已退出，不再监测网络")
        try:
            self.engine.stop(3)
        except Exception:
            pass
        if sent:
            # 气泡是异步显示的，稍等一下再拆托盘，否则通知会被立刻收走
            try:
                self.root.update()
            except Exception:
                pass
            time.sleep(0.8)
        try:
            if self._tray is not None:
                self._tray.stop()
        except Exception:
            pass
        try:
            self.log.unsubscribe(self._on_log_record)
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        # 用过托盘后 pystray 的 win32 消息循环会让进程不退出，强制收尾
        try:
            self._exit_func(0)
        except Exception:
            pass

    # ==================== 运行 ====================
    def _has_credentials(self) -> bool:
        return config_mod.has_credentials(self.cfg or {})

    def _want_start_minimized(self) -> bool:
        """首次启动（没存过账号密码）弹窗口引导；之后一律后台托盘。"""
        if self._start_minimized is not None:
            return bool(self._start_minimized)
        return self._has_credentials()

    def _warn_missing_credentials(self) -> None:
        if str(self.cfg.get("username", "")).strip() and str(self.cfg.get("password", "")):
            return
        self.log.warn("尚未设置账号密码，自动登录不会生效；请到「设置 → 账号与门户」填写后保存。")
        self.v_footer.set("请先到「设置」填写账号和密码")

    def _schedule_start_notice(self) -> None:
        """等托盘图标在系统里注册好再发「已启动」通知（run_detached 是异步的）。"""
        if not self.cfg.get("show_notifications", True):
            return
        if self._has_credentials():
            msg = ("程序已在后台启动，正在监测网络，双击托盘图标可显示主界面"
                   if self._want_start_minimized() else "程序已启动，正在监测网络")
        else:
            msg = "首次使用：请在设置中填写校园网账号密码并保存"

        def _fire():
            if self._ui_alive:
                self._notify(msg)

        try:
            self.root.after(900, _fire)
        except Exception:
            pass

    def run(self) -> None:
        self.build()
        first_run = not self._has_credentials()
        minimized = self._want_start_minimized()
        # 托盘常驻：启动/退出通知、以及「关闭窗口隐藏到托盘」都依赖它
        tray_ok = self._ensure_tray()
        if minimized:
            if tray_ok:
                try:
                    self.root.withdraw()
                except Exception:
                    pass
            else:
                self.log.warn("托盘不可用，已改为显示主界面")
                self._show_window()
        elif first_run:
            # 首次启动：直接定位到设置页，引导填写账号密码
            self._warn_missing_credentials()
            try:
                self.nb.select(2)
            except Exception:
                pass
        self.start_monitoring()
        self._tick()
        self._status_tick()
        self._schedule_start_notice()
        self._check_wifi_autoconnect_async()
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            pass
        finally:
            if not self._really_quit:
                self._really_quit = True
                try:
                    self.engine.stop(2)
                except Exception:
                    pass
                try:
                    if self._tray is not None:
                        self._tray.stop()
                except Exception:
                    pass


def _first_line(exc) -> str:
    """取异常消息的第一行（截断），用于把浏览器启动失败原因写进报告。"""
    s = str(exc)
    return s.splitlines()[0][:140] if s else ""


def run_diag(out_path: str | None = None) -> int:
    """自检：把运行环境、依赖、浏览器启动、网络状态写成 JSON，便于排查。

    界面程序没有控制台，所以结果落文件而不是打印。
    """
    import json
    import platform
    import traceback

    out_path = out_path or os.path.join(paths.data_dir(), "logs", "diag.json")
    info = {
        "version": APP_VERSION,
        "wechat": CONTACT_WECHAT,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "frozen": paths.is_frozen(),
        "app_dir": paths.app_dir(),
        "data_dir": paths.data_dir(),
        "resource_dir": paths.resource_dir(),
        "executable": sys.executable,
        "icon_exists": os.path.exists(ICON_PATH),
        "modules": {},
        "browser_launch": None,
        "net": {},
    }

    for mod in ("tkinter", "playwright", "pystray", "PIL", "win32com.client", "win32api",
                "win32serviceutil", "win32ts", "win32profile"):
        try:
            __import__(mod)
            info["modules"][mod] = True
        except Exception as e:
            info["modules"][mod] = "FAIL: %r" % e

    try:
        import playwright
        drv = os.path.join(os.path.dirname(os.path.abspath(playwright.__file__)), "driver")
        info["driver"] = {"path": drv, "exists": os.path.isdir(drv),
                          "node": os.path.exists(os.path.join(drv, "node.exe"))}
    except Exception as e:
        info["driver"] = {"error": repr(e)}

    cfg = config_mod.load()
    try:
        from playwright.sync_api import sync_playwright
        from portal_login import channel_order
        errors = []
        launched = ""
        with sync_playwright() as p:
            # 依次尝试各通道（"auto" 会展开成 chrome → msedge），全失败再兜底内置内核。
            # 注意：不能把 "auto" 直接当通道名传给 playwright，那不是合法通道。
            for ch in channel_order(cfg.get("chrome_channel", "chrome")):
                try:
                    browser = p.chromium.launch(channel=ch, headless=True)
                    launched = ch
                    browser.close()
                    break
                except Exception as e:
                    errors.append("%s → %s" % (ch, _first_line(e)))
            if not launched:
                try:
                    browser = p.chromium.launch(headless=True)
                    launched = "内置 chromium"
                    browser.close()
                except Exception as e:
                    errors.append("内置 chromium → %s" % _first_line(e))
        info["browser_launch"] = bool(launched)
        info["browser_used"] = launched
        if not launched:
            info["browser_error"] = "；".join(errors)
    except Exception as e:
        info["browser_launch"] = False
        info["browser_error"] = repr(e)
        info["browser_trace"] = traceback.format_exc()[-1200:]

    try:
        from netcheck import get_wifi_ssid, is_internet_available, is_wifi_in_prefix
        ssid = get_wifi_ssid()
        info["net"] = {
            "ssid": ssid,
            "is_qvtu": is_wifi_in_prefix(ssid, cfg.get("wifi_prefix", "QVTU")),
            "online": is_internet_available(cfg.get("online_probe_urls", [])),
        }
    except Exception as e:
        info["net"] = {"error": repr(e)}

    try:
        info["autostart"] = autostart_mod.describe(cfg)
        info["admin"] = autostart_mod.is_admin()
    except Exception as e:
        info["autostart"] = repr(e)

    try:
        info["service"] = dict(autostart_mod.service_info(), name=SERVICE_NAME)
    except Exception as e:
        info["service"] = repr(e)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    return 0 if info.get("browser_launch") else 1


def apply_autostart_cli() -> int:
    """提权后的「一次性」入口：把配置里的自启设置写入系统，然后退出。

    由「一键设置开机自启」通过 UAC 拉起（--apply-autostart）。因为它在写入
    计划任务的过程中需要管理员权限，所以必须在单实例检查之前处理，避免和
    用户正在运行的那个普通实例抢互斥体。
    """
    _enable_dpi_awareness()
    _hide_console_window()
    try:
        cfg = config_mod.load()
    except Exception as e:
        cfg = None
        err = str(e)
    root = None
    try:
        root = tk.Tk()
        root.withdraw()
    except Exception:
        root = None

    ok = False
    msg = ""
    if cfg is None:
        msg = err
    else:
        try:
            r = autostart_mod.apply_autostart(cfg)
            ok = bool(r.get("ok"))
            msg = r.get("detail") or ("；".join(r.get("errors") or []) or "未产生变更")
        except Exception as e:
            msg = str(e)

    try:
        if ok:
            messagebox.showinfo("开机自启", "已完成开机自启设置：\n\n%s" % msg)
        else:
            messagebox.showerror("开机自启失败", "设置开机自启失败：\n\n%s" % msg)
    except Exception:
        pass
    try:
        if root is not None:
            root.destroy()
    except Exception:
        pass
    return 0 if ok else 1


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # 作为 Windows 服务被 SCM 拉起：交给服务框架，别做界面/单实例
    if "--service-run" in argv:
        import service
        return service.run_service()
    if "--version" in argv:
        try:
            print(APP_VERSION)
        except Exception:
            pass
        return 0
    # 提权后的一次性动作：写入自启项后立即退出，不参与单实例/界面
    if "--apply-autostart" in argv:
        return apply_autostart_cli()
    if "--diag" in argv:
        idx = argv.index("--diag")
        out = argv[idx + 1] if len(argv) > idx + 1 and not argv[idx + 1].startswith("-") else None
        return run_diag(out)
    if "--minimized" in argv:
        minimized = True
    elif "--show" in argv:
        minimized = False
    else:
        minimized = None

    if not singleinstance.acquire():
        # 已经有一个实例在跑：尽量把它的窗口叫到前台；叫不动（托盘里）就提示一下
        if not singleinstance.activate_window():
            singleinstance.notice_already_running()
        return 0

    try:
        app = App(start_minimized=minimized)
        app.run()
    finally:
        singleinstance.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
