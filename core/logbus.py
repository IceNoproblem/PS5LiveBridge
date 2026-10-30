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

# 写文件走队列 + 后台线程。
#
# 为什么：log() 会被 asyncio 事件循环里的回调直接调用（比如弹幕回调）。
# 原来每条都 write + flush，等于每条日志做一次磁盘同步 —— 弹幕一密集就把
# 事件循环堵死，Web 接口超时、watchdog 误判断流、ffmpeg 被重启、画面断。
# 改成入队之后，调用方只做一次 list.append，落盘交给后台线程批量做。
_QUEUE = None
_WRITER = None
_QUEUE_LOCK = threading.Lock()

_MAX_LINE = 2000

_TO_STDOUT = paths.PLATFORM == "docker" or \
    (os.environ.get("PS5LB_LOG_STDOUT") or "").strip().lower() in ("1", "true", "yes")

LEVELS = ("debug", "info", "success", "warn", "error", "step")

_DEBUG = threading.Event()


def set_debug(on: bool) -> None:
    if on:
        _DEBUG.set()
    else:
        _DEBUG.clear()


def debug_enabled() -> bool:
    return _DEBUG.is_set()


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


def _writer_loop() -> None:
    """后台线程：从队列取行，攒一批再落盘，减少 flush 次数。"""
    while True:
        try:
            first = _QUEUE.get()
            if first is None:
                break
            batch = [first]
            # 再顺手捞一批，最多 40 行
            for _ in range(39):
                try:
                    nxt = _QUEUE.get_nowait()
                except Exception:
                    break
                if nxt is None:
                    _QUEUE.put(None)
                    break
                batch.append(nxt)
            with _FILE_LOCK:
                fh = _open_file()
                if fh:
                    try:
                        fh.write("".join(batch))
                        fh.flush()
                    except Exception:
                        pass
            if _TO_STDOUT:
                try:
                    sys.stdout.write("".join(batch))
                    sys.stdout.flush()
                except Exception:
                    pass
        except Exception:
            time.sleep(0.2)


def _ensure_writer() -> None:
    global _QUEUE, _WRITER
    if _QUEUE is not None:
        return
    with _QUEUE_LOCK:
        if _QUEUE is not None:
            return
        import queue as _q
        _QUEUE = _q.Queue(maxsize=4000)
        _WRITER = threading.Thread(target=_writer_loop, name="logbus-writer",
                                   daemon=True)
        _WRITER.start()


def log(msg: str, level: str = "info", tag: str = "") -> dict:
    """记一条日志，返回这条记录（前端有时要立即展示）。"""
    if level == "debug" and not _DEBUG.is_set():
        return {}
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
    _ensure_writer()
    try:
        _QUEUE.put_nowait(line)
    except Exception:
        pass          # 队列满了宁可丢日志，也不能阻塞调用线程
    for q in subs:
        try:
            q.put_nowait(item)
        except Exception:
            pass
    return item


def debug(msg: str, **kw) -> dict:
    """调试日志。开关关着时什么都不做。"""
    if not _DEBUG.is_set():
        return {}
    return log(msg, level="debug", **kw)


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
    # 先把队列排空，别丢最后几条
    try:
        if _QUEUE is not None:
            _QUEUE.put(None, timeout=2)
            if _WRITER is not None:
                _WRITER.join(timeout=3)
    except Exception:
        pass
    with _FILE_LOCK:
        if _FILE_HANDLE:
            try:
                _FILE_HANDLE.close()
            except Exception:
                pass
            _FILE_HANDLE = None
