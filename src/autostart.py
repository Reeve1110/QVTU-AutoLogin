# -*- coding: utf-8 -*-
"""开机自启与权限工具。

两种自启方式：
  startup —— 在当前用户「启动文件夹」放一个 .lnk（无需管理员，登录即运行）
  task    —— 注册「登录时触发」的计划任务（可延迟、可以用最高权限运行），
             普通权限也能注册（关键是把触发器的 UserId 设成当前用户）

系统侧条目统一命名 QVTUAutoLogin（计划任务名 / 快捷方式名），
方便在任务计划程序里查找；历史用过的旧名（AutoLogin / QVTU_AutoLogin）
升级时自动清理，保证系统里永远只有一个自启项。

优先用 pywin32 的 COM 在进程内创建，不调用外部命令；没有 pywin32 时回退 PowerShell。
"""
from __future__ import annotations

import os
import sys

import procutil
from paths import app_dir, exe_path, is_frozen
from version import APP_TITLE, SYSTEM_TAG

HERE = app_dir()
TASK_NAME = SYSTEM_TAG                    # QVTUAutoLogin
LNK_NAME = SYSTEM_TAG + ".lnk"            # QVTUAutoLogin.lnk
# 旧版本用过的名字，升级时顺手清掉，避免开机启动两次
LEGACY_TASK_NAMES = ("AutoLogin", "QVTU_AutoLogin")
LEGACY_LNK_NAMES = ("AutoLogin.lnk", "QVTU AutoLogin.lnk")


# --------------------------------------------------------------------------- #
# 路径与命令
# --------------------------------------------------------------------------- #
def pythonw_path() -> str:
    """无控制台窗口的解释器路径（打包成 exe 后就是 exe 自己）。"""
    if is_frozen():
        return exe_path()
    cand = os.path.join(HERE, ".venv", "Scripts", "pythonw.exe")
    if os.path.exists(cand):
        return cand
    exe = sys.executable
    low = exe.lower()
    if low.endswith("python.exe"):
        w = exe[: -len("python.exe")] + "pythonw.exe"
        if os.path.exists(w):
            return w
    return exe


def launch_command(entry: str = "gui.py", minimized: bool = False) -> tuple:
    """返回 (可执行文件, 参数, 工作目录)。

    默认不带参数：启动后是显示主窗口还是直接驻留托盘，由界面按
    「是否已保存账号密码」自动决定（首次启动引导填写，之后静默后台），
    这样自启与手动双击行为一致。minimized=True 才追加 --minimized 强制静默。
    """
    if is_frozen():
        return exe_path(), ("--minimized" if minimized else ""), HERE
    args = '"%s"' % os.path.join(HERE, entry)
    if minimized:
        args += " --minimized"
    return pythonw_path(), args, HERE


def startup_dir() -> str:
    return os.path.join(os.environ.get("APPDATA", ""),
                        r"Microsoft\Windows\Start Menu\Programs\Startup")


def shortcut_path() -> str:
    return os.path.join(startup_dir(), LNK_NAME)


def legacy_shortcut_paths() -> list:
    return [os.path.join(startup_dir(), n) for n in LEGACY_LNK_NAMES]


def _is_ours(target: str, args: str = "") -> bool:
    """判断某个自启项（快捷方式 / 计划任务动作）是不是本程序自己的。"""
    if not target:
        return False
    try:
        t = os.path.normcase(os.path.abspath(target))
    except Exception:
        return False
    try:
        ours = os.path.normcase(os.path.abspath(exe_path()))
    except Exception:
        ours = ""
    if ours and t == ours:
        return True
    if os.path.basename(t) in ("python.exe", "pythonw.exe"):
        return os.path.normcase(HERE) in os.path.normcase(args or "")
    return False


def _startup_lnk_files() -> list:
    try:
        d = startup_dir()
        return [os.path.join(d, n) for n in os.listdir(d) if n.lower().endswith(".lnk")]
    except Exception:
        return []


def _shortcut_is_ours(path: str) -> bool:
    info = read_shortcut(path)
    return _is_ours(info.get("target", ""), info.get("args", ""))


# --------------------------------------------------------------------------- #
# 管理员权限
# --------------------------------------------------------------------------- #
def is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin(entry: str = "gui.py", extra_args: str = "") -> bool:
    """以管理员身份重新启动本程序；返回是否成功唤起 UAC。

    extra_args 会追加到启动参数里。例如「一键设置开机自启」会传
    ``--apply-autostart``：提权后的实例启动即把自启项写入系统，写完就退出，
    这样用户当前这个（非管理员）实例可以继续在托盘里跑，无需整体重启。
    """
    try:
        import ctypes
        exe, args, workdir = launch_command(entry, minimized=False)
        if extra_args:
            args = ("%s %s" % (args, extra_args)).strip()
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args, workdir, 1)
        return int(rc) > 32
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# 启动文件夹快捷方式
# --------------------------------------------------------------------------- #
def startup_installed() -> bool:
    return os.path.exists(shortcut_path())


def _remove_legacy_shortcuts() -> None:
    for p in legacy_shortcut_paths():
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


def install_startup_shortcut(entry: str = "gui.py") -> str:
    path = shortcut_path()
    exe, args, workdir = launch_command(entry)
    os.makedirs(startup_dir(), exist_ok=True)
    _remove_legacy_shortcuts()

    try:
        import win32com.client
        shell = win32com.client.Dispatch("WScript.Shell")
        sc = shell.CreateShortCut(path)
        sc.TargetPath = exe
        sc.Arguments = args
        sc.WorkingDirectory = workdir
        sc.WindowStyle = 7          # 最小化
        sc.Description = APP_TITLE
        sc.Save()
        return path
    except Exception as e_com:
        ps = (
            "$w=New-Object -ComObject WScript.Shell;"
            "$s=$w.CreateShortcut('%s');"
            "$s.TargetPath='%s';$s.Arguments='%s';$s.WorkingDirectory='%s';"
            "$s.WindowStyle=7;$s.Description='%s';$s.Save()"
            % (path, exe, args, workdir, APP_TITLE)
        )
        try:
            r = procutil.run_hidden(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                     "-Command", ps], capture_output=True, timeout=60)
            if r.returncode == 0 and os.path.exists(path):
                return path
            raise RuntimeError((r.stderr or b"").decode("gbk", "replace")
                               or "PowerShell 创建快捷方式失败")
        except Exception as e_ps:
            raise RuntimeError("创建启动快捷方式失败：%s / %s" % (e_com, e_ps))


def remove_startup_shortcut() -> bool:
    removed = False
    for p in [shortcut_path()] + legacy_shortcut_paths():
        if not os.path.exists(p):
            continue
        try:
            os.remove(p)
            removed = True
        except Exception as e:
            if p == shortcut_path():
                raise RuntimeError("删除启动快捷方式失败：%s" % e)
    # 顺手清掉启动文件夹里其它指向本程序的快捷方式，避免开机启动多次
    for p in _startup_lnk_files():
        if os.path.exists(p) and _shortcut_is_ours(p):
            try:
                os.remove(p)
                removed = True
            except Exception:
                pass
    return removed


def read_shortcut(path: str | None = None) -> dict:
    path = path or shortcut_path()
    try:
        import win32com.client
        shell = win32com.client.Dispatch("WScript.Shell")
        sc = shell.CreateShortCut(path)
        return {"exists": os.path.exists(path), "target": sc.TargetPath,
                "args": sc.Arguments, "workdir": sc.WorkingDirectory}
    except Exception as e:
        return {"exists": os.path.exists(path), "error": str(e)}


# --------------------------------------------------------------------------- #
# 计划任务
# --------------------------------------------------------------------------- #
def _task_service():
    import win32com.client
    service = win32com.client.Dispatch("Schedule.Service")
    service.Connect()
    return service


def task_installed(name: str = TASK_NAME) -> bool:
    try:
        _task_service().GetFolder("\\").GetTask(name)
        return True
    except Exception:
        return False


def install_task(name: str = TASK_NAME, delay_sec: int = 20,
                 highest: bool = False, entry: str = "gui.py") -> None:
    """注册「登录时触发」的计划任务。highest=True 需要管理员权限。

    普通权限注册的关键：登录触发器必须把 UserId 指定为当前用户，
    否则 Task Scheduler 会按「所有用户」处理而拒绝访问。
    """
    exe, args, workdir = launch_command(entry)
    service = _task_service()
    root = service.GetFolder("\\")

    TASK_CREATE_OR_UPDATE = 6
    TASK_TRIGGER_LOGON = 9
    TASK_ACTION_EXEC = 0
    TASK_LOGON_INTERACTIVE_TOKEN = 3
    TASK_RUNLEVEL_HIGHEST = 1
    TASK_RUNLEVEL_LUA = 0

    td = service.NewTask(0)
    td.RegistrationInfo.Description = APP_TITLE
    td.Settings.Enabled = True
    td.Settings.StartWhenAvailable = True
    td.Settings.DisallowStartIfOnBatteries = False
    td.Settings.StopIfGoingOnBatteries = False
    td.Settings.ExecutionTimeLimit = "PT0S"          # 不限时
    td.Settings.AllowDemandStart = True
    td.Settings.Hidden = False

    trig = td.Triggers.Create(TASK_TRIGGER_LOGON)
    try:
        trig.UserId = "%s\\%s" % (os.environ.get("USERDOMAIN", "."),
                                  os.environ.get("USERNAME", ""))
    except Exception:
        pass
    if int(delay_sec) > 0:
        trig.Delay = "PT%dS" % int(delay_sec)

    act = td.Actions.Create(TASK_ACTION_EXEC)
    act.Path = exe
    act.Arguments = args
    act.WorkingDirectory = workdir

    td.Principal.LogonType = TASK_LOGON_INTERACTIVE_TOKEN
    td.Principal.RunLevel = TASK_RUNLEVEL_HIGHEST if highest else TASK_RUNLEVEL_LUA

    try:
        root.RegisterTaskDefinition(name, td, TASK_CREATE_OR_UPDATE,
                                    None, None, TASK_LOGON_INTERACTIVE_TOKEN)
    except Exception as e:
        raise RuntimeError("注册计划任务失败（若勾选了『以管理员身份运行』，"
                           "需先以管理员身份启动本程序）：%s" % e)


def remove_legacy_tasks() -> bool:
    """移除旧版本遗留的计划任务（程序改名后清理用）。"""
    removed = False
    for old in LEGACY_TASK_NAMES:
        if task_installed(old):
            try:
                _task_service().GetFolder("\\").DeleteTask(old, 0)
                removed = True
            except Exception:
                pass
    return removed


def enum_our_tasks() -> list:
    """列出所有「动作指向本程序」的计划任务名（含当前正式名）。"""
    names = []
    try:
        folder = _task_service().GetFolder("\\")
        tasks = folder.GetTasks(1)          # 1 = 含隐藏任务
        count = int(tasks.Count)
    except Exception:
        return names
    for i in range(1, count + 1):
        try:
            t = tasks.Item(i)
            acts = t.Definition.Actions
            if int(acts.Count) < 1:
                continue
            a = acts.Item(1)
            if _is_ours(getattr(a, "Path", "") or "", getattr(a, "Arguments", "") or ""):
                names.append(t.Name)
        except Exception:
            continue
    return names


def remove_task(name: str = TASK_NAME, keep: str | None = None) -> bool:
    """删除本程序的自启任务（含旧名与其它指向本程序的任务）。

    keep 指定要保留的任务名——「先装新的再清旧的」时用它跳过刚建好的那个。
    """
    try:
        folder = _task_service().GetFolder("\\")
    except Exception as e:
        raise RuntimeError("访问计划任务失败：%s" % e)

    targets = [name] + list(LEGACY_TASK_NAMES) + enum_our_tasks()
    removed = False
    seen = set()
    for nm in targets:
        if not nm or nm in seen or nm == keep:
            continue
        seen.add(nm)
        try:
            folder.GetTask(nm)              # 不存在会抛异常
        except Exception:
            continue
        try:
            folder.DeleteTask(nm, 0)
            removed = True
        except Exception as e:
            if nm == name:
                raise RuntimeError("删除计划任务失败：%s" % e)
    return removed


def task_info(name: str = TASK_NAME) -> dict:
    try:
        t = _task_service().GetFolder("\\").GetTask(name)
        return {"exists": True, "enabled": bool(t.Enabled),
                "run_level": int(t.Definition.Principal.RunLevel)}
    except Exception:
        return {"exists": False}


def list_system_entries() -> dict:
    """列出本程序当前在系统里的全部自启痕迹（只读，供确认与展示）。"""
    shortcuts = []
    named = [shortcut_path()] + legacy_shortcut_paths()
    for p in named + _startup_lnk_files():
        if not os.path.exists(p) or os.path.basename(p) in shortcuts:
            continue
        if p in named or _shortcut_is_ours(p):
            shortcuts.append(os.path.basename(p))
    tasks = []
    try:
        installed = set([TASK_NAME] + list(LEGACY_TASK_NAMES) + enum_our_tasks())
        folder = _task_service().GetFolder("\\")
        for nm in installed:
            if not nm or nm in tasks:
                continue
            try:
                folder.GetTask(nm)
                tasks.append(nm)
            except Exception:
                continue
    except Exception:
        pass
    return {"service": service_installed(), "shortcuts": shortcuts, "tasks": tasks}


def purge_system_entries() -> dict:
    """一键清除本程序在系统里留下的所有自启痕迹。

    只删两类东西：① 命名属于本程序的条目（QVTUAutoLogin 及历史旧名）；
    ② 无论叫什么名字、但目标确实指向本程序 exe / 本项目脚本的条目。
    其它任何程序的快捷方式与计划任务都不会碰。返回实际删除的清单。
    """
    removed = {"service": False, "shortcuts": [], "tasks": []}
    if service_installed():
        ok_svc, _msg = remove_service()
        removed["service"] = bool(ok_svc)
    named = [shortcut_path()] + legacy_shortcut_paths()
    seen = set()
    for p in named + _startup_lnk_files():
        base = os.path.basename(p)
        if not os.path.exists(p) or base in seen:
            continue
        seen.add(base)
        if p not in named and not _shortcut_is_ours(p):
            continue
        try:
            os.remove(p)
            removed["shortcuts"].append(base)
        except Exception:
            pass
    try:
        folder = _task_service().GetFolder("\\")
        targets = set([TASK_NAME] + list(LEGACY_TASK_NAMES) + enum_our_tasks())
        for nm in targets:
            if not nm:
                continue
            try:
                folder.GetTask(nm)
            except Exception:
                continue
            try:
                folder.DeleteTask(nm, 0)
                removed["tasks"].append(nm)
            except Exception:
                pass
    except Exception:
        pass
    return removed


# --------------------------------------------------------------------------- #
# 统一入口
# --------------------------------------------------------------------------- #
def apply(cfg: dict, entry: str = "gui.py") -> str:
    """按 cfg['autostart'] 应用自启设置，返回结果描述。"""
    a = cfg.get("autostart", {}) or {}
    want = bool(a.get("enabled"))
    mode = a.get("mode") or SERVICE_MODE
    delay = int(a.get("delay_sec", 20) or 0)
    highest = bool(a.get("run_as_admin"))

    # v26.10.5：默认方式是「Windows 服务」，统一走 apply_autostart（含关闭时的清理）
    if mode == SERVICE_MODE:
        return apply_autostart(cfg, entry).get("detail", "")

    if not want:
        removed = []
        try:
            if remove_startup_shortcut():
                removed.append("启动文件夹快捷方式")
        except Exception:
            pass
        try:
            if remove_task():
                removed.append("计划任务")
        except Exception:
            pass
        if service_installed():                     # 装了服务也一并移除
            ok_svc, _msg = remove_service()
            if ok_svc:
                removed.append("Windows 服务")
        return "已关闭开机自启" + ("（移除：%s）" % "、".join(removed) if removed else "")

    # 只允许存在一个自启项。顺序很重要：**先装新的，再清旧的**——
    # 万一新方式装不上（比如没管理员权限注册计划任务），原有的自启还在，
    # 不至于落得「两头空」；然后再把其它痕迹（另一种方式 / 旧名 / 指向本程序的散项）清掉，
    # 于是无论怎么反复切换，系统里永远只剩最后设置的那一项。
    if mode == "task":
        install_task(delay_sec=delay, highest=highest, entry=entry)   # 装不上会抛，交给调用方
        for fn in (lambda: remove_startup_shortcut(),
                   remove_legacy_tasks,
                   lambda: remove_task(keep=TASK_NAME)):             # 别删掉刚建的任务
            try:
                fn()
            except Exception:
                pass
        return "已启用开机自启（计划任务%s，登录后延迟 %d 秒后台启动）" % (
            "，最高权限" if highest else "", delay)

    p = install_startup_shortcut(entry=entry)                         # 装不上会抛
    for fn in (remove_legacy_tasks, remove_task):
        try:
            fn()
        except Exception:
            pass
    return "已启用开机自启（启动文件夹快捷方式）\n%s" % p


def describe(cfg: dict) -> str:
    """自启方式的人类可读描述（服务 / 计划任务 / 启动文件夹，供界面与自检）。"""
    try:
        return describe_full(cfg)
    except Exception:
        a = cfg.get("autostart", {}) or {}
        parts = []
        if startup_installed():
            parts.append("启动文件夹快捷方式")
        if task_installed():
            info = task_info()
            parts.append("计划任务%s" % ("（最高权限）" if info.get("run_level") == 1 else ""))
        if parts:
            return "已启用：" + "、".join(parts)
        if a.get("enabled"):
            return "配置为启用，但系统里未找到自启项（请点『应用自启设置』）"
        return "未启用"


# --------------------------------------------------------------------------- #
# 开机自启：计划任务 + 启动文件夹（登录时运行，同时注册两处）
# --------------------------------------------------------------------------- #
def register_both(entry: str = "gui.py") -> dict:
    """同时注册「计划任务」与「启动文件夹快捷方式」，返回落地结果。

    两处都以最高权限运行（计划任务 RunLevel=最高，快捷方式是否提权由
    Windows 决定），且**登录 Windows 后立即运行**（计划任务延迟设为 0）。
    计划任务注册需要管理员权限（普通权限会被系统拒绝）；调用方应先确保
    已提权，这里直接把异常抛给上层展示。返回 {"task": bool, "startup": bool,
    "errors": [str, ...]}——某个方式失败不影响另一个，尽量把能装的都装上。
    """
    errors = []
    task_ok = False
    startup_ok = False
    try:
        # delay_sec=0 → 登录后立即运行（不再延迟）
        install_task(delay_sec=0, highest=True, entry=entry)
        task_ok = True
    except Exception as e:
        errors.append("计划任务：%s" % e)
    try:
        install_startup_shortcut(entry=entry)
        startup_ok = True
    except Exception as e:
        errors.append("启动文件夹：%s" % e)
    # 顺手清掉历史旧名条目，保证系统里不留重复的自启项
    for fn in (remove_legacy_tasks, _remove_legacy_shortcuts):
        try:
            fn()
        except Exception:
            pass
    return {"task": task_ok, "startup": startup_ok, "errors": errors}


def unregister_both() -> dict:
    """同时移除「计划任务」与「启动文件夹快捷方式」（仅限本程序）。

    返回 {"task": bool, "startup": bool}，表示各自是否确实删掉了东西。
    """
    removed_task = False
    removed_startup = False
    try:
        removed_startup = bool(remove_startup_shortcut())
    except Exception:
        pass
    try:
        removed_task = bool(remove_task())
    except Exception:
        pass
    for fn in (remove_legacy_tasks, _remove_legacy_shortcuts):
        try:
            fn()
        except Exception:
            pass
    return {"task": removed_task, "startup": removed_startup}


def status() -> dict:
    """返回各方式当前是否已在系统中生效（供界面显示）。"""
    return {"service": service_installed(), "startup": startup_installed(),
            "task": task_installed()}


# --------------------------------------------------------------------------- #
# Windows 服务（Windows 服务抢先启动，v26.10.5 起为首选方式）
# --------------------------------------------------------------------------- #
SERVICE_MODE = "service"


def _service():
    """惰性导入服务模块（pywin32 服务组件较重，不拖慢界面启动）。"""
    import service
    return service


def service_installed() -> bool:
    try:
        return bool(_service().is_installed())
    except Exception:
        return False


def service_info() -> dict:
    """服务状态字典：{"available", "installed", "running", "text"}。"""
    try:
        s = _service()
        st = s.query_state()
        st["available"] = s.available()
        return st
    except Exception as e:
        return {"available": False, "installed": False, "running": False,
                "text": "不可用：%r" % e}


def install_service(entry: str = "gui.py") -> str:
    """安装（或更新）Windows 服务。需要管理员权限，失败抛异常。"""
    exe, _args, _wd = launch_command(entry)
    return _service().install(exe=exe)


def remove_service() -> tuple:
    """停止并删除 Windows 服务。返回 (ok, msg)。"""
    return _service().remove()


def start_service() -> tuple:
    return _service().start()


# --------------------------------------------------------------------------- #
# 统一应用（v26.10.5：服务优先，可选 task / startup 作为备用）
# --------------------------------------------------------------------------- #
def apply_autostart(cfg: dict, entry: str = "gui.py") -> dict:
    """按 ``cfg['autostart']`` 把自启设置落到系统，并保证**只留一种**方式。

    mode:
      · service  —— Windows 服务（默认，开机抢先启动）；装好后清掉计划任务/启动文件夹；
      · task     —— 计划任务（登录时触发，最高权限）；
      · startup  —— 启动文件夹快捷方式。

    返回 ``{"enabled", "mode", "ok", "errors": [str], "detail": str}``。
    ``enabled=False`` 时移除**全部**方式（服务 + 计划任务 + 启动文件夹）。
    """
    a = (cfg or {}).get("autostart", {}) or {}
    want = bool(a.get("enabled"))
    mode = a.get("mode") or SERVICE_MODE
    delay = int(a.get("delay_sec", 5) or 0)
    errors = []

    if not want:
        removed = []
        if service_installed():                     # 只有确实装了才动 SCM
            ok_svc, msg_svc = remove_service()
            if ok_svc:
                removed.append("Windows 服务")
            else:
                errors.append("服务：%s" % msg_svc)
        r = unregister_both()
        if r.get("task"):
            removed.append("计划任务")
        if r.get("startup"):
            removed.append("启动文件夹")
        detail = "已关闭开机自启" + ("（移除：%s）" % "、".join(removed) if removed else "")
        return {"enabled": False, "mode": mode, "ok": not errors,
                "errors": errors, "detail": detail}

    if mode == SERVICE_MODE:
        svc_ok = False
        try:
            install_service(entry)
            svc_ok = True
            ok_start, msg_start = start_service()
            if not ok_start:
                errors.append("服务启动：%s" % msg_start)
        except Exception as e:
            errors.append("安装服务：%s" % e)
        if svc_ok:
            # 服务负责开局拉起 → 清掉计划任务与启动文件夹，避免重复启动
            for fn in (lambda: remove_startup_shortcut(), lambda: remove_task(),
                       remove_legacy_tasks, _remove_legacy_shortcuts):
                try:
                    fn()
                except Exception:
                    pass
            detail = "已启用开机自启（Windows 服务，开机抢先启动）"
            return {"enabled": True, "mode": mode, "ok": True, "errors": errors,
                    "detail": detail}
        # 服务装不上（例如未取得管理员权限）→ 退回计划任务 + 启动文件夹，别让用户没自启
        r = register_both(entry)
        fallback_ok = bool(r.get("task") or r.get("startup"))
        errors.extend(r.get("errors") or [])
        detail = ("Windows 服务安装失败，已改用计划任务 + 启动文件夹作为备用："
                  + ("、".join(x for x, k in (("计划任务", r.get("task")),
                                              ("启动文件夹", r.get("startup"))) if k)
                     or "未成功"))
        return {"enabled": True, "mode": "task" if fallback_ok else mode,
                "ok": fallback_ok, "errors": errors, "detail": detail}

    if mode == "task":
        try:
            install_task(delay_sec=delay, highest=True, entry=entry)
        except Exception as e:
            return {"enabled": True, "mode": mode, "ok": False, "errors": [str(e)],
                    "detail": "注册计划任务失败：%s" % e}
        remove_service()
        try:
            remove_startup_shortcut()
        except Exception:
            pass
        for fn in (remove_legacy_tasks, _remove_legacy_shortcuts):
            try:
                fn()
            except Exception:
                pass
        return {"enabled": True, "mode": mode, "ok": True, "errors": [],
                "detail": "已启用开机自启（计划任务，最高权限，登录后延迟 %d 秒）" % delay}

    # startup
    try:
        p = install_startup_shortcut(entry=entry)
    except Exception as e:
        return {"enabled": True, "mode": mode, "ok": False, "errors": [str(e)],
                "detail": "创建启动快捷方式失败：%s" % e}
    remove_service()
    for fn in (remove_legacy_tasks, remove_task):
        try:
            fn()
        except Exception:
            pass
    return {"enabled": True, "mode": mode, "ok": True, "errors": [],
            "detail": "已启用开机自启（启动文件夹快捷方式）"}


def describe_full(cfg: dict) -> str:
    """完整的自启状态描述（服务 + 计划任务 + 启动文件夹）。"""
    st = status()
    parts = []
    if st.get("service"):
        info = service_info()
        parts.append("Windows 服务（%s）" % info.get("text", "已安装"))
    if st.get("startup"):
        parts.append("启动文件夹")
    if st.get("task"):
        info = task_info()
        parts.append("计划任务%s" % ("（最高权限）" if info.get("run_level") == 1 else ""))
    if parts:
        return "已启用：" + "、".join(parts)
    if (cfg or {}).get("autostart", {}).get("enabled"):
        return "配置为启用，但系统里未找到自启项（请重新勾选开启）"
    return "未启用"



if __name__ == "__main__":
    print("frozen:", is_frozen())
    print("admin:", is_admin())
    print("launch:", launch_command())
    print("startup dir:", startup_dir())
    print("startup installed:", startup_installed())
    print("task installed:", task_installed())
