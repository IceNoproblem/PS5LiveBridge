from __future__ import annotations

import atexit
import signal
import sys
import threading


def _emit(msg: str) -> None:
    """
    输出清理进度。

    优先走 logbus（UTF-8 文件，桌面版和容器都能看）；
    退化到 stdout 时必须能安全编码 —— Windows 控制台默认 GBK，
    直接 print("✓") 会抛 UnicodeEncodeError 把整条清理链打断
    （实测：退出时关播没执行，就是这个原因）。
    """
    try:
        from . import logbus
        logbus.info(msg, tag="退出")
        return
    except Exception:
        pass
    try:
        safe = msg.encode("ascii", "replace").decode("ascii")
        sys.stdout.write("[SHUTDOWN] %s\n" % safe)
        sys.stdout.flush()
    except Exception:
        pass


class ShutdownManager:
    """优雅退出管理器。"""

    def __init__(self):
        self._callbacks: list = []
        self._lock = threading.Lock()
        self._shutting_down = False
        self._done = threading.Event()

    def register(self, name: str, fn, priority: int = 0) -> None:
        """
        注册清理回调。priority 越小越先执行。
        建议：关播(priority=0) → 停 FFmpeg(priority=10) → 停弹幕(priority=20) → 其他(priority=30)
        """
        with self._lock:
            self._callbacks.append((name, fn, priority))
            self._callbacks.sort(key=lambda x: x[2])

    def install_signal_handlers(self) -> None:
        """安装信号处理器（SIGTERM / SIGINT）。"""
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        atexit.register(self._atexit)

    def _handle_signal(self, signum, frame):
        name = signal.Signals(signum).name if hasattr(signal, 'Signals') else str(signum)
        self.shutdown(reason="收到信号 %s" % name)

    def _atexit(self):
        if not self._shutting_down:
            self.shutdown(reason="程序退出")

    def shutdown(self, reason: str = "", timeout: float = 10.0) -> None:
        """执行清理回调。幂等——多次调用只执行一次。"""
        with self._lock:
            if self._shutting_down:
                return
            self._shutting_down = True

        _emit("开始清理（%s）" % (reason or "退出"))
        for name, fn, priority in self._callbacks:
            try:
                fn()
                _emit("完成 %s" % name)
            except Exception as e:
                _emit("失败 %s: %s" % (name, e))
        self._done.set()
        _emit("清理完成")

    @property
    def is_shutting_down(self) -> bool:
        return self._shutting_down

    def wait(self, timeout: float = None) -> bool:
        """等待清理完成。"""
        return self._done.wait(timeout)


shutdown_mgr = ShutdownManager()
