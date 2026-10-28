from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time

from . import logbus


def find_ffmpeg() -> str:
    """检测 FFmpeg 二进制路径。"""
    env = os.environ.get("FFMPEG_PATH") or os.environ.get("PS5LB_FFMPEG")
    if env and os.path.isfile(env):
        return env

    from . import paths
    for name in ("ffmpeg.exe", "ffmpeg"):
        for sub in (("tools",), ("tools", "ffmpeg"), ("_internal", "tools"), ()):
            try:
                p = paths.resource_path(*(sub + (name,)))
            except Exception:
                continue
            if p and os.path.isfile(p):
                return p

    for name in ["ffmpeg", "ffmpeg.exe"]:
        path = shutil.which(name)
        if path:
            return path
    return ""


class FfmpegManager:
    """FFmpeg 进程管理器。"""

    def __init__(self, max_restarts: int = 3):
        self._process: subprocess.Popen | None = None
        self._max_restarts = max_restarts
        self._restart_count = 0
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_output = 0.0
        self._on_drop = None
        self._cmd_args: list = []
        self._stderr_tail: list = []

    def start(self, args: list, on_drop=None) -> bool:
        """
        启动 FFmpeg。
        args: 命令行参数（不含 ffmpeg 本身）
        on_drop: 崩溃回调（reason: str）
        """
        ffmpeg_bin = find_ffmpeg()
        if not ffmpeg_bin:
            logbus.error("找不到 FFmpeg，转发无法启动", tag="FFmpeg")
            return False

        self._cmd_args = args
        self._on_drop = on_drop
        self._restart_count = 0
        self._running = True

        if not self._spawn(ffmpeg_bin, args):
            return False

        self._thread = threading.Thread(target=self._monitor, daemon=True,
                                       name="ffmpeg-monitor")
        self._thread.start()
        return True

    def _spawn(self, ffmpeg_bin: str, args: list) -> bool:
        """启动 FFmpeg 进程。"""
        try:
            cmd = [ffmpeg_bin] + args
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            self._last_output = time.time()
            logbus.info("FFmpeg 已启动（PID %d）" % self._process.pid, tag="FFmpeg")
            return True
        except Exception as e:
            logbus.error("FFmpeg 启动失败：%s" % e, tag="FFmpeg")
            return False

    def _monitor(self) -> None:
        """监控进程健康状态。"""
        while self._running and self._process:
            time.sleep(2)
            try:
                ret = self._process.poll()
                if ret is not None:
                    stderr = ""
                    try:
                        stderr = self._process.stderr.read().decode(errors="ignore")[-200:]
                    except Exception:
                        pass
                    logbus.warn("FFmpeg 退出（code=%d）：%s" % (ret, stderr[-100:]), tag="FFmpeg")
                    if self._running and self._restart_count < self._max_restarts:
                        self._restart_count += 1
                        logbus.info("自动重启 FFmpeg（第 %d 次）" % self._restart_count,
                                   tag="FFmpeg")
                        time.sleep(1)
                        ffmpeg_bin = find_ffmpeg()
                        if ffmpeg_bin and self._spawn(ffmpeg_bin, self._cmd_args):
                            continue
                    self._running = False
                    if self._on_drop:
                        self._on_drop("FFmpeg 崩溃（code=%d）" % ret)
                    return
                if time.time() - self._last_output > 30:
                    logbus.warn("FFmpeg 30 秒无输出，可能卡住", tag="FFmpeg")
                    self._last_output = time.time()
            except Exception:
                pass

    def stop(self) -> None:
        """停止 FFmpeg（含子进程）。"""
        self._running = False
        with self._lock:
            if self._process:
                try:
                    if os.name == 'nt':
                        subprocess.run(["taskkill", "/F", "/T", "/PID",
                                       str(self._process.pid)],
                                      capture_output=True, timeout=5)
                    else:
                        os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)
                except Exception:
                    try:
                        self._process.kill()
                    except Exception:
                        pass
                self._process = None

    @property
    def is_running(self) -> bool:
        return self._running and self._process is not None

    @property
    def pid(self) -> int:
        return self._process.pid if self._process else 0

    @property
    def restarts(self) -> int:
        return self._restart_count

    @property
    def last_error(self) -> str:
        return self._stderr_tail[-1] if self._stderr_tail else ""
