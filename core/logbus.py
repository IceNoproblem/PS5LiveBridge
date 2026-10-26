# -*- coding: utf-8 -*-
"""
core/logbus.py —— 日志总线

一个进程里跑着 Web、DNS、nginx 监控、B站连接、转发器，日志必须能汇到一处：
    1. 写文件（出问题后能翻）
    2. 进内存环形缓冲（前端「运行日志」直接读，不用 SSE 也能工作）
    3. 推给订阅者（SSE 实时刷）

不做日志聚合的话，用户报「灯不绿」时我们只能猜。
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque

from . import paths

_LOCK = threading.RLock()
_BUFFER: deque = deque(maxlen=400)
_SUBSCRIBERS: list = []          # list[queue.Queue]
_FILE_LOCK = threading.Lock()
_FILE_HANDLE = None

_MAX_LINE = 2000

LEVELS = ("debug", "info", "success", "warn", "error", "step")


def _open_file():
    global _FILE_HANDLE
    if _FILE_HANDLE is not None:
        return _FILE_HANDLE
    try:
        paths.ensure_dirs()
        name = "run_%s.log" % time.strftime("%Y%m%d")
        fh = open(os.path.join(paths.LOGS_DIR, name), "a", encoding="utf-8")
        _FILE_HANDLE = fh
        return fh
    except Exception:
        return None


def log(msg: str, level: str = "info", tag: str = "") -> dict:
    """记一条日志，返回这条记录（前端有时要立即展示）。"""
    item = {
        "t": time.strftime("%H:%M:%S"),
        "ts": time.time(),
        "level": level if level in LEVELS else "info",
        "tag": tag,
        "msg": str(msg)[:_MAX_LINE],
    }
    with _LOCK:
        _BUFFER.append(item)
        subs = list(_SUBSCRIBERS)
    # 文件
    with _FILE_LOCK:
        fh = _open_file()
        if fh:
            try:
                fh.write("%s [%s]%s %s\n" % (
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                    item["level"].upper(),
                    (" " + tag) if tag else "",
                    item["msg"],
                ))
                fh.flush()
            except Exception:
                pass
    # 订阅者
    for q in subs:
        try:
            q.put_nowait(item)
        except Exception:
            pass
    return item


def info(msg: str, **kw) -> dict:
    return log(msg, "info", **kw)


def ok(msg: str, **kw) -> dict:
    return log(msg, "success", **kw)


def warn(msg: str, **kw) -> dict:
    return log(msg, "warn", **kw)


def error(msg: str, **kw) -> dict:
    return log(msg, "error", **kw)


def step(msg: str, **kw) -> dict:
    return log(msg, "step", **kw)


def recent(n: int = 100) -> list:
    with _LOCK:
        return list(_BUFFER)[-n:]


def subscribe() -> object:
    import queue
    q = queue.Queue(maxsize=500)
    with _LOCK:
        _SUBSCRIBERS.append(q)
    return q


def unsubscribe(q) -> None:
    with _LOCK:
        try:
            _SUBSCRIBERS.remove(q)
        except ValueError:
            pass


def close() -> None:
    global _FILE_HANDLE
    with _FILE_LOCK:
        if _FILE_HANDLE:
            try:
                _FILE_HANDLE.close()
            except Exception:
                pass
            _FILE_HANDLE = None
