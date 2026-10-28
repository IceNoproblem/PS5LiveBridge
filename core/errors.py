from __future__ import annotations


class AppError(Exception):
    """基础错误类。"""
    def __init__(self, message: str = ""):
        self.message = message
        super().__init__(message)


class BiliAPIError(AppError):
    """B 站 API 错误。Display: "BiliAPI (StartLive, code=4): 没有权限" """
    def __init__(self, operation: str, code: int, message: str = ""):
        self.operation = operation
        self.code = code
        self.message = message or "未知错误"
        super().__init__(
            "BiliAPI (%s, code=%d): %s" % (operation, code, self.message))

    @property
    def is_face_auth(self) -> bool:
        """是否是人脸验证错误。"""
        return self.code in (60024, 60043)

    @property
    def is_not_logged_in(self) -> bool:
        return self.code == -101


class FfmpegError(AppError):
    """FFmpeg 退出/崩溃错误。"""
    def __init__(self, exit_code: int = 0, stderr_tail: str = ""):
        self.exit_code = exit_code
        self.stderr_tail = stderr_tail[-200:] if stderr_tail else ""
        super().__init__(
            "FFmpeg 退出（code=%d）%s" % (
                exit_code,
                (": " + self.stderr_tail) if self.stderr_tail else ""))

    @property
    def is_network(self) -> bool:
        """是否是网络错误（RTMP 连接失败等）。"""
        s = self.stderr_tail.lower()
        return any(kw in s for kw in ("connection", "network", "timeout", "refused"))

    @property
    def is_codec(self) -> bool:
        return "codec" in self.stderr_tail.lower() or "encoder" in self.stderr_tail.lower()


class NetworkError(AppError):
    """网络错误。"""
    def __init__(self, message: str = "", host: str = ""):
        self.host = host
        super().__init__(
            "网络错误%s: %s" % ((" (%s)" % host) if host else "", message))


class StateError(AppError):
    """状态机非法操作。"""
    def __init__(self, current: str, target: str, message: str = ""):
        self.current = current
        self.target = target
        super().__init__(
            "状态转换被拒绝: %s → %s%s" % (
                current, target, (": " + message) if message else ""))


class ConfigError(AppError):
    """配置错误。"""
    pass


def bili_api(op: str, code: int, msg: str = "") -> BiliAPIError:
    return BiliAPIError(op, code, msg)


def ffmpeg(exit_code: int = 0, stderr: str = "") -> FfmpegError:
    return FfmpegError(exit_code, stderr)


def network(msg: str = "", host: str = "") -> NetworkError:
    return NetworkError(msg, host)


def state(current: str, target: str, msg: str = "") -> StateError:
    return StateError(current, target, msg)
