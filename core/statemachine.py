from __future__ import annotations

import enum
import threading
import time


class LiveState(enum.Enum):
    IDLE = "idle"
    PS5_STREAMING = "waiting"
    LIVE_PREPARING = "starting"
    WAITING_FACE_AUTH = "face_verify"
    RELAYING = "relaying"
    LIVE = "live"
    LOCAL = "local"
    ERROR = "error"


_BY_STR = {s.value: s for s in LiveState}


def to_state(value) -> LiveState:
    """字符串（或枚举）→ 枚举。认不出就当 IDLE。"""
    if isinstance(value, LiveState):
        return value
    return _BY_STR.get(str(value or "").strip().lower(), LiveState.IDLE)


_TRANSITIONS: dict[LiveState, set[LiveState]] = {
    LiveState.IDLE: {
        LiveState.PS5_STREAMING,
        LiveState.LIVE_PREPARING,
        LiveState.ERROR,
    },
    LiveState.PS5_STREAMING: {
        LiveState.LIVE_PREPARING,
        LiveState.LOCAL,
        LiveState.IDLE,
        LiveState.ERROR,
    },
    LiveState.LIVE_PREPARING: {
        LiveState.WAITING_FACE_AUTH,
        LiveState.RELAYING,
        LiveState.LIVE,
        LiveState.PS5_STREAMING,
        LiveState.IDLE,
        LiveState.ERROR,
    },
    LiveState.WAITING_FACE_AUTH: {
        LiveState.LIVE_PREPARING,
        LiveState.RELAYING,
        LiveState.IDLE,
        LiveState.ERROR,
    },
    LiveState.RELAYING: {
        LiveState.LIVE,
        LiveState.IDLE,
        LiveState.ERROR,
    },
    LiveState.LIVE: {
        LiveState.IDLE,
        LiveState.ERROR,
    },
    LiveState.LOCAL: {
        LiveState.IDLE,
        LiveState.ERROR,
    },
    LiveState.ERROR: {
        LiveState.IDLE,
    },
}


class LiveStateMachine:
    """线程安全的状态机。"""

    def __init__(self, initial: LiveState = LiveState.IDLE):
        self._state = initial
        self._lock = threading.Lock()
        self._since = 0.0
        self._listeners: list = []

    @property
    def state(self) -> LiveState:
        with self._lock:
            return self._state

    @property
    def since(self) -> float:
        with self._lock:
            return self._since

    @property
    def elapsed(self) -> float:
        with self._lock:
            return time.time() - self._since if self._since else 0.0

    def can_transition(self, target: LiveState) -> bool:
        with self._lock:
            return target in _TRANSITIONS.get(self._state, set())

    def transition(self, target: LiveState, force: bool = False) -> bool:
        """
        尝试转换到目标状态。
        返回 True=成功，False=非法转换（被拒绝）。
        force=True 时跳过检查（用于异常恢复）。
        """
        with self._lock:
            old = self._state
            if not force and target not in _TRANSITIONS.get(old, set()):
                return False
            self._state = target
            self._since = time.time()
        for cb in self._listeners:
            try:
                cb(old, target)
            except Exception:
                pass
        return True

    def on_transition(self, callback) -> None:
        """注册状态转换回调：callback(old_state, new_state)"""
        self._listeners.append(callback)

    @property
    def is_live(self) -> bool:
        return self.state == LiveState.LIVE

    @property
    def is_active(self) -> bool:
        """是否在任何活跃状态（非 IDLE）。"""
        return self.state != LiveState.IDLE

    @property
    def is_working(self) -> bool:
        """是否在「正在开播」状态（准备中/等人脸）。"""
        return self.state in (LiveState.LIVE_PREPARING, LiveState.WAITING_FACE_AUTH)


class ClientType(enum.Enum):
    """B 站开播通道——关播必须配对使用同一通道。"""
    ELECTRON = "electron"
    LIVEHIME = "livehime"


class LiveContext:
    """直播上下文：状态机 + 配对信息 + 运行时数据。"""

    def __init__(self):
        self.sm = LiveStateMachine()
        self.client: ClientType | None = None
        self.room_id: int = 0
        self.rtmp_addr: str = ""
        self.rtmp_code: str = ""
        self.face_url: str = ""
        self.error: str = ""
        self.ps5_app: str = ""
        self.ps5_name: str = ""
        self.start_time: float = 0.0

    def reset(self):
        self.sm.transition(LiveState.IDLE, force=True)
        self.client = None
        self.rtmp_addr = ""
        self.rtmp_code = ""
        self.face_url = ""
        self.error = ""
        self.ps5_app = ""
        self.ps5_name = ""
        self.start_time = 0.0

    def to_dict(self) -> dict:
        return {
            "state": self.sm.state.value,
            "client": self.client.value if self.client else None,
            "room_id": self.room_id,
            "rtmp_tail": self.rtmp_code[-6:] if self.rtmp_code else "",
            "face_url": self.face_url,
            "error": self.error,
            "ps5_app": self.ps5_app,
            "ps5_name": self.ps5_name,
            "since": self.sm.since,
        }
