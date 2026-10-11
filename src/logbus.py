# -*- coding: utf-8 -*-
"""日志中枢：写入文件 + 内存环形缓冲 + 推送给订阅者（界面）。

线程模型：引擎在后台线程调用 add()，订阅者回调只做「塞进 queue」，
不直接操作 tkinter 控件。

日志落盘在「我的文档\\AutoLogin\\logs\\autologin.log」（见 paths.data_dir()），
并且**只保留最近 `RETENTION_HOURS` 小时**的内容：启动时清一次，运行期间每隔
`MAINTAIN_INTERVAL_SEC` 再清一次，超过保留期的行会被直接丢弃（顺带清理老版本
留下的 `.1` 备份文件）。
"""
from __future__ import annotations

import collections
import os
import re
import threading
import time
from datetime import datetime, timedelta

from paths import data_dir

DEFAULT_LOG_FILE = os.path.join(data_dir(), "logs", "autologin.log")

RETENTION_HOURS = 24            # 日志只保留最近 24 小时
MAINTAIN_INTERVAL_SEC = 600     # 运行期间清理超期日志的最小间隔（秒）

_TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s?(.*)$")


class LogBus:
    def __init__(self, log_file: str = DEFAULT_LOG_FILE,
                 max_bytes: int = 2 * 1024 * 1024, buffer_size: int = 2000,
                 retention_hours: float = RETENTION_HOURS):
        self.log_file = log_file
        self.max_bytes = max_bytes
        self.retention_hours = float(retention_hours)
        self.maintain_interval_sec = MAINTAIN_INTERVAL_SEC
        self._last_maintain = 0.0
        self._lock = threading.RLock()
        self._buf: collections.deque = collections.deque(maxlen=buffer_size)
        self._subs: list = []
        self._seq = 0

    # ---------- 订阅 ----------
    def subscribe(self, cb) -> None:
        with self._lock:
            if cb not in self._subs:
                self._subs.append(cb)

    def unsubscribe(self, cb) -> None:
        with self._lock:
            if cb in self._subs:
                self._subs.remove(cb)

    # ---------- 写入 ----------
    def add(self, msg, level: str = "info") -> dict:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        text = "[%s] %s" % (ts, msg)
        with self._lock:
            self._seq += 1
            rec = {"seq": self._seq, "ts": ts, "level": level, "text": text}
            self._buf.append(rec)
            subs = list(self._subs)
            self._write_file(text)
            self._maybe_maintain()
        for cb in subs:
            try:
                cb(rec)
            except Exception:
                pass
        return rec

    def info(self, msg):
        return self.add(msg, "info")

    def warn(self, msg):
        return self.add(msg, "warn")

    def error(self, msg):
        return self.add(msg, "error")

    def success(self, msg):
        return self.add(msg, "success")

    def _write_file(self, text: str) -> None:
        try:
            d = os.path.dirname(self.log_file)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except Exception:
            pass

    # ---------- 保留策略：只留最近 RETENTION_HOURS 小时 ----------
    def _target_files(self) -> list:
        """需要维护的日志文件：当前文件 + 老版本可能留下的 `.1` 备份。"""
        return [self.log_file, self.log_file + ".1"]

    @staticmethod
    def _line_time(line: str):
        m = _TS_RE.match(line)
        if not m:
            return None
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None

    def prune_old(self, now=None) -> int:
        """删除早于保留期的日志行，返回被丢弃的行数。

        没有时间戳的行（极少）一律保留，避免误删非本程序写入的内容。
        """
        now = now or datetime.now()
        cutoff = now - timedelta(hours=self.retention_hours)
        removed_total = 0
        for path in self._target_files():
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.read().splitlines()
            except Exception:
                continue
            kept = []
            removed = 0
            for ln in lines:
                if not ln.strip():
                    continue
                t = self._line_time(ln)
                if t is not None and t < cutoff:
                    removed += 1
                    continue
                kept.append(ln)
            if not removed:
                continue
            removed_total += removed
            try:
                with open(path, "w", encoding="utf-8") as f:
                    if kept:
                        f.write("\n".join(kept) + "\n")
            except Exception:
                pass
        return removed_total

    def _maybe_maintain(self, now: float | None = None) -> None:
        """按间隔节流地执行清理；另外文件超过 max_bytes 时也顺手清一次。"""
        now = time.time() if now is None else now
        due = (now - self._last_maintain) >= self.maintain_interval_sec
        try:
            oversized = os.path.getsize(self.log_file) > self.max_bytes
        except Exception:
            oversized = False
        if not (due or oversized):
            return
        self._last_maintain = now
        try:
            self.prune_old()
        except Exception:
            pass

    # ---------- 读取 ----------
    def records(self) -> list:
        with self._lock:
            return list(self._buf)

    def records_tail(self, n: int) -> list:
        with self._lock:
            return list(self._buf)[-n:] if n > 0 else []

    def text(self) -> str:
        return "\n".join(r["text"] for r in self.records())

    def count(self) -> int:
        with self._lock:
            return len(self._buf)

    # ---------- 清除 ----------
    def clear(self) -> None:
        with self._lock:
            self._buf.clear()
            try:
                d = os.path.dirname(self.log_file)
                if d:
                    os.makedirs(d, exist_ok=True)
                with open(self.log_file, "w", encoding="utf-8") as f:
                    f.write("")
            except Exception:
                pass
            subs = list(self._subs)
        evt = {"seq": 0, "ts": "", "level": "clear", "text": ""}
        for cb in subs:
            try:
                cb(evt)
            except Exception:
                pass

    # ---------- 启动时把历史日志读进内存 ----------
    def preload(self, max_lines: int = 800) -> int:
        # 启动先按保留期清一次，避免把过老的内容读进来
        try:
            self.prune_old()
        except Exception:
            pass
        self._last_maintain = time.time()
        if not os.path.exists(self.log_file):
            return 0
        try:
            with open(self.log_file, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.read().splitlines()
        except Exception:
            return 0
        lines = [ln for ln in lines if ln.strip()][-max_lines:]
        with self._lock:
            for ln in lines:
                m = _TS_RE.match(ln)
                ts = m.group(1) if m else ""
                rec = {"seq": self._seq, "ts": ts, "level": "info", "text": ln}
                self._seq += 1
                self._buf.append(rec)
        return len(lines)


if __name__ == "__main__":
    bus = LogBus()
    seen = []
    bus.subscribe(lambda r: seen.append(r))
    bus.info("hello")
    bus.success("world")
    bus.clear()
    print("records:", len(bus.records()), "seen:", len(seen))
