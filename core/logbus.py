from __future__ import annotations

import os
import sys
import threading
import time
from collections import deque

from . import paths

_LOCK = threading.RLock()
_BUFFER: deque = deque(maxlen=400)
_SUBSCRIBERS: list = []
_FILE_LOCK = threading.Lock()
_FILE_HANDLE = None

_MAX_LINE = 2000

_TO_STDOUT = paths.PLATFORM == "docker" or \
    (os.environ.get("PS5LB_LOG_STDOUT") or "").strip().lower() in ("1", "true", "yes")

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
    now = time.time()
    item = {
        "t": "%s.%03d" % (time.strftime("%H:%M:%S", time.localtime(now)),
                          int(now * 1000) % 1000),
        "ts": now,
        "level": level if level in LEVELS else "info",
        "tag": tag,
        "msg": str(msg)[:_MAX_LINE],
    }
    with _LOCK:
        _BUFFER.append(item)
        subs = list(_SUBSCRIBERS)
    stamp = "%s %s" % (time.strftime("%Y-%m-%d", time.localtime(now)), item["t"])
    line = "%s [%s]%s %s\n" % (
        stamp, item["level"].upper().ljust(7),
        (" " + tag) if tag else "", item["msg"])
    with _FILE_LOCK:
        fh = _open_file()
        if fh:
            try:
                fh.write(line)
                fh.flush()
            except Exception:
                pass
    for q in subs:
        try:
            q.put_nowait(item)
        except Exception:
            pass
    if _TO_STDOUT:
        try:
            sys.stdout.write(line)
            sys.stdout.flush()
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
