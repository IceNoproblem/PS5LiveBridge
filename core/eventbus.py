from __future__ import annotations

import enum
import threading
import time
from collections import deque
from dataclasses import dataclass, field


class EventType(enum.Enum):
    PS5_PUBLISH = "ps5_publish"
    PS5_UNPUBLISH = "ps5_unpublish"

    BILI_START_OK = "bili_start_ok"
    BILI_START_FAIL = "bili_start_fail"
    BILI_STOP_OK = "bili_stop_ok"
    BILI_STOP_FAIL = "bili_stop_fail"
    BILI_FACE_AUTH = "bili_face_auth"
    BILI_FACE_VERIFIED = "bili_face_verified"

    STREAM_CONFIRMED = "stream_confirmed"
    STREAM_TIMEOUT = "stream_timeout"
    STREAM_DROPPED = "stream_dropped"

    SHUTDOWN = "shutdown"
    TICK = "tick"

    STATE_CHANGED = "state_changed"
    TITLE_SYNCED = "title_synced"
    DANMAKU_THROTTLED = "danmaku_throttled"


@dataclass
class Event:
    type: EventType
    data: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class EffectType(enum.Enum):
    BILI_START_LIVE = "bili_start_live"
    BILI_STOP_LIVE = "bili_stop_live"
    FFMPEG_START = "ffmpeg_start"
    FFMPEG_STOP = "ffmpeg_stop"
    DANMAKU_START = "danmaku_start"
    DANMAKU_STOP = "danmaku_stop"
    NOTIFY_PS5 = "notify_ps5"
    LOG = "log"
    CLEANUP = "cleanup"


@dataclass
class Effect:
    type: EffectType
    data: dict = field(default_factory=dict)


class EventBus:
    """事件总线。"""

    def __init__(self, max_queue: int = 100):
        self._event_handlers: dict = {}
        self._effect_handlers: dict = {}
        self._event_queue: deque = deque(maxlen=max_queue)
        self._effect_queue: deque = deque(maxlen=max_queue)
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None

    def on_event(self, event_type: EventType, handler) -> None:
        """注册事件处理器。"""
        with self._lock:
            self._event_handlers.setdefault(event_type, []).append(handler)

    def on_effect(self, effect_type: EffectType, handler) -> None:
        """注册副作用处理器。"""
        with self._lock:
            self._effect_handlers.setdefault(effect_type, []).append(handler)

    def emit_event(self, event: Event) -> None:
        """发送事件。"""
        with self._lock:
            self._event_queue.append(event)
        self._process_event(event)

    def emit_effect(self, effect: Effect) -> None:
        """发送副作用。"""
        with self._lock:
            self._effect_queue.append(effect)
        self._process_effect(effect)

    def _process_event(self, event: Event) -> None:
        handlers = self._event_handlers.get(event.type, [])
        for fn in handlers:
            try:
                fn(event)
            except Exception as e:
                print("[EventBus] 事件处理错误 (%s): %s" % (event.type, e))

    def _process_effect(self, effect: Effect) -> None:
        handlers = self._effect_handlers.get(effect.type, [])
        for fn in handlers:
            try:
                fn(effect)
            except Exception as e:
                print("[EventBus] 副作用执行错误 (%s): %s" % (effect.type, e))

    def recent_events(self, count: int = 20) -> list:
        """获取最近事件（调试用）。"""
        with self._lock:
            return list(self._event_queue)[-count:]

    def recent_effects(self, count: int = 20) -> list:
        """获取最近副作用（调试用）。"""
        with self._lock:
            return list(self._effect_queue)[-count:]

    @property
    def event_count(self) -> int:
        with self._lock:
            return len(self._event_queue)

    @property
    def effect_count(self) -> int:
        with self._lock:
            return len(self._effect_queue)
