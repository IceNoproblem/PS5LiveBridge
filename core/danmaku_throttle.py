from __future__ import annotations

import threading
import time
from collections import deque


class DanmakuThrottle:
    """弹幕限速队列。"""

    def __init__(self, interval: float = 1.5, max_queue: int = 50):
        self._queue: deque = deque()
        self._lock = threading.Lock()
        self._interval = interval
        self._max_queue = max_queue
        self._last_send = 0.0
        self._running = False
        self._thread: threading.Thread | None = None
        self._sender = None

    def set_sender(self, fn) -> None:
        """设置实际发送函数：fn(user, text) -> bool"""
        self._sender = fn

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="danmaku-throttle")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        with self._lock:
            self._queue.clear()

    def push(self, user: str, text: str, priority: int = 0) -> bool:
        """
        推入弹幕队列。
        priority: 0=普通弹幕, 1=礼物/SC（优先发）
        返回 True=已入队，False=队列满被丢弃。
        """
        item = (priority, time.time(), user, text)
        with self._lock:
            if len(self._queue) >= self._max_queue:
                for i, (p, t, u, txt) in enumerate(self._queue):
                    if p == 0:
                        del self._queue[i]
                        break
                else:
                    self._queue.popleft()
            self._queue.append(item)
            if priority > 0:
                self._queue = deque(sorted(self._queue, key=lambda x: -x[0]))
        return True

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._queue)

    @property
    def is_running(self) -> bool:
        return self._running

    def _loop(self) -> None:
        while self._running:
            item = None
            with self._lock:
                if self._queue:
                    item = self._queue.popleft()
            if item is None:
                time.sleep(0.1)
                continue
            _, _, user, text = item
            elapsed = time.time() - self._last_send
            if elapsed < self._interval:
                time.sleep(self._interval - elapsed)
            if self._sender:
                try:
                    self._sender(user, text)
                except Exception:
                    pass
            self._last_send = time.time()
