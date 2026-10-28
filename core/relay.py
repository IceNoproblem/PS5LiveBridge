from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time

from . import logbus, paths


def find_ffmpeg() -> str:
    """
    统一走 ffmpeg_mgr 的探测（它会看 FFMPEG_PATH 环境变量、
    随包 tools/、系统 PATH，比这里原来那套更全）。
    """
    try:
        from .ffmpeg_mgr import find_ffmpeg as _find
        p = _find()
        if p:
            return p
    except Exception:
        pass
    for name in ("ffmpeg.exe", "ffmpeg"):
        p = paths.resource_path("tools", name)
        if os.path.exists(p):
            return p
        p2 = paths.resource_path(name)
        if os.path.exists(p2):
            return p2
    return shutil.which("ffmpeg") or "ffmpeg"


class Relay(object):
    def __init__(self):
        self.proc = None
        self.input_url = ""
        self.output_url = ""
        self.started_at = 0.0
        self.restarts = 0
        self.last_error = ""
        self.ffmpeg = find_ffmpeg()
        self._lock = threading.RLock()
        self._tail = []
        self._tail_lock = threading.Lock()
        self._watch = None
        self._watch_gen = 0
        self._stopping = False
        self.on_drop = None

    @property
    def running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    @property
    def uptime(self) -> int:
        return int(time.time() - self.started_at) if self.running else 0

    def tail(self, n: int = 20) -> list:
        with self._tail_lock:
            return self._tail[-n:]

    def _push_tail(self, line: str):
        line = line.strip()
        if not line:
            return
        with self._tail_lock:
            self._tail.append(line[:300])
            if len(self._tail) > 200:
                del self._tail[:100]

    def start(self, input_url: str, output_url: str) -> bool:
        with self._lock:
            self.stop(_internal=True)
            self._stopping = False
            self.input_url = input_url
            self.output_url = output_url
            self.started_at = time.time()
            return self._spawn()

    def _spawn(self) -> bool:
        args = [
            self.ffmpeg,
            "-hide_banner", "-loglevel", "warning",
            "-fflags", "+genpts",
            "-rtmp_live", "live",
            "-i", self.input_url,
            "-c", "copy",
            "-f", "flv",
            "-flvflags", "no_duration_filesize",
            "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
            self.output_url,
        ]
        startup = {}
        if os.name == "nt":
            startup["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = subprocess.SW_HIDE
            startup["startupinfo"] = si

        try:
            self.proc = subprocess.Popen(
                args, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                **startup)
        except Exception as e:
            self.last_error = "启动 ffmpeg 失败：%s（路径：%s）" % (e, self.ffmpeg)
            logbus.error(self.last_error, tag="转发")
            return False

        logbus.ok("开始把 PS5 画面转发到 B 站", tag="转发")
        threading.Thread(target=self._read_stderr, args=(self.proc,), daemon=True).start()
        self._watch = None
        return True

    def _read_stderr(self, proc):
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "ignore").strip()
                if not line:
                    continue
                self._push_tail(line)
                low = line.lower()
                if any(k in low for k in ("error", "failed", "refused", "broken pipe",
                                          "unable", "invalid")):
                    self.last_error = line[:200]
                    logbus.warn("转发告警：%s" % line[:200], tag="转发")
        except Exception:
            pass

    def start_watchdog(self, on_drop=None, max_restarts=8):
        self.on_drop = on_drop
        self.restarts = 0
        self._watch_gen += 1
        self._watchdog_loop(on_drop, max_restarts, self._watch_gen)

    def _watchdog_loop(self, on_drop, max_restarts, gen):
        def loop():
            while True:
                time.sleep(2)
                if gen != self._watch_gen:
                    return
                if self._stopping:
                    return
                if not self.proc:
                    return
                if self.proc.poll() is None:
                    continue
                code = self.proc.returncode
                if self._stopping:
                    return
                self.restarts += 1
                logbus.warn("转发进程退出（code=%s），第 %d 次重连" % (code, self.restarts),
                            tag="转发")
                if self.restarts > max_restarts:
                    logbus.error("转发连续失败 %d 次，已停止重试：%s"
                                 % (self.restarts, self.last_error or "未知原因"), tag="转发")
                    if on_drop:
                        try:
                            on_drop(self.last_error or "转发中断")
                        except Exception:
                            pass
                    return
                time.sleep(min(10, 2 * self.restarts))
                if self._stopping:
                    return
                if not self._spawn():
                    if on_drop:
                        try:
                            on_drop(self.last_error)
                        except Exception:
                            pass
                    return
        self._watch = threading.Thread(target=loop, name="relay-watchdog", daemon=True)
        self._watch.start()

    def stop(self, _internal: bool = False) -> None:
        was_running = self.running
        self._stopping = True
        self._watch_gen += 1
        p = self.proc
        self.proc = None
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=4)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        if not _internal:
            self.started_at = 0.0
            if was_running:
                logbus.info("已停止转发", tag="转发")
