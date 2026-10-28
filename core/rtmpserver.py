from __future__ import annotations

import os
import pty
import re
import select
import shutil
import signal
import subprocess
import sys
import threading
import time

from . import logbus, paths


def _pdeathsig():
    """
    Linux：父进程死掉时让 ffmpeg 跟着死（PR_SET_PDEATHSIG=SIGKILL）。
    没有这一层，docker restart / kill 掉 Python 后 ffmpeg 会逃逸成宿主机孤儿，
    继续占着 1935，下一轮启动就报 Address already in use。
    """
    if not sys.platform.startswith("linux"):
        return
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        PR_SET_PDEATHSIG = 1
        libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
    except Exception:
        pass

_FFMPEG_RECV_ARGS = [
    "-y",
    "-fflags", "+genpts+igndts",
    "-probesize", "32",
    "-analyzeduration", "0",
    "-max_delay", "500000",
    "-i", "{input_url}",
    "-c", "copy",
    "-f", "flv",
    "-flvflags", "no_duration_filesize",
    "-flush_packets", "1",
    "{output_url}",
]


class RtmpServer(object):
    """
    FFmpeg 内置 RTMP 服务端。

    用法：
        srv = RtmpServer(rtmp_port=1935)
        srv.start()                         # 启动收流
        srv.set_destination("rtmp://...")   # 设置转发目标
        srv.publishing                      # 是否有推流
        srv.stop()                          # 停止
    """

    def __init__(self, rtmp_port: int = 1935):
        self.rtmp_port = rtmp_port
        self.last_error = ""
        self._proc: subprocess.Popen | None = None
        self._pty_fd: int | None = None
        self._progress_file: str | None = None
        self._lock = threading.Lock()
        self._publishing = False
        self._stream_key = ""
        self._destination = ""
        self._destination_key = ""
        self._monitor_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._started_at = 0.0
        self._bytes_in = 0
        self._bitrate_kbps = 0
        self._last_out_time_ms = 0
        self._last_advance_at = 0.0


    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def publishing(self) -> bool:
        return self._publishing and self.running()

    @property
    def forwarding(self) -> bool:
        """是否已带上 B 站目标（一体化转发中）。"""
        return bool(self._destination) and self.running()

    @property
    def uptime(self) -> float:
        return time.time() - self._started_at if self._started_at else 0

    def snapshot(self) -> dict:
        return {
            "running": self.running(),
            "publishing": self.publishing,
            "port": self.rtmp_port,
            "stream_key": self._stream_key,
            "uptime": round(self.uptime, 1),
            "bitrate_kbps": self._bitrate_kbps,
            "backend": "ffmpeg",
        }


    def set_destination(self, url: str, key: str = "") -> None:
        """
        设置 B 站推流目标。
        ffmpeg -listen 1 只接受一个连接，PS5 占住后没法再被二次拉流，
        所以必须重启 ffmpeg 成「收流+转发」一体化模式。
        PS5 会在端口释放后自动重连（Twitch 客户端有重连逻辑）。
        """
        self._destination = url
        self._destination_key = key
        if self.running():
            self._restart_with_forward()

    def clear_destination(self) -> None:
        """清掉 B 站目标，把 ffmpeg 重启回纯收流模式（停播时用）。"""
        if not self._destination:
            return
        self._destination = ""
        self._destination_key = ""
        if self.running():
            self._restart_with_forward()

    def _restart_with_forward(self):
        """重启 ffmpeg 加入转发目标。"""
        if self._proc:
            self._proc.send_signal(signal.SIGTERM)
            try:
                self._proc.wait(timeout=3)
            except Exception:
                self._proc.kill()
        self.start()

    def _output_url(self) -> str:
        url = self._destination
        if self._destination_key:
            url = url.rstrip("/") + "/" + self._destination_key
        return url


    def start(self) -> bool:
        with self._lock:
            if self.running():
                return True

            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                self.last_error = "找不到 ffmpeg。请安装：brew install ffmpeg"
                return False

            input_url = "rtmp://0.0.0.0:%d/live/stream" % self.rtmp_port

            if self._destination:
                output_url = self._output_url()
                args = [ffmpeg] + [
                    a.format(input_url=input_url, output_url=output_url)
                    for a in _FFMPEG_RECV_ARGS
                ]
                idx = args.index("-i")
                args.insert(idx, "-listen")
                args.insert(idx + 1, "1")
            else:
                args = [
                    ffmpeg, "-y",
                    "-fflags", "+genpts+igndts",
                    "-probesize", "32", "-analyzeduration", "0",
                    "-listen", "1",
                    "-i", input_url,
                    "-c", "copy",
                    "-f", "null", "-",
                ]

            try:
                self._progress_file = os.path.join(
                    paths.DATA_DIR, "rtmp_progress.txt")
                f_idx = len(args)
                for i in range(len(args) - 1, -1, -1):
                    if args[i] == "-f":
                        f_idx = i
                        break
                args_with_progress = (
                    args[:f_idx] + ["-progress", self._progress_file] + args[f_idx:])
                self._proc = subprocess.Popen(
                    args_with_progress,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                    preexec_fn=_pdeathsig,
                )
            except Exception as e:
                self.last_error = "启动 ffmpeg 失败：%s" % str(e)[:120]
                return False

            time.sleep(0.8)
            if self._proc.poll() is not None:
                err = ""
                try:
                    err = (self._proc.stderr.read() or b"").decode("utf-8", "ignore")
                except Exception:
                    pass
                tail = err.strip()[-300:]
                if "Address already in use" in err:
                    self.last_error = (
                        "端口 %d 已被占用（多为上次残留的 ffmpeg 进程）。"
                        "请在服务器上执行：pkill -f 'rtmp://0.0.0.0:%d' 后重试。"
                        % (self.rtmp_port, self.rtmp_port))
                else:
                    self.last_error = "ffmpeg 启动失败：%s" % (tail or "无输出")
                logbus.error(self.last_error, tag="收流")
                return False

            self._publishing = False
            self._last_out_time_ms = 0
            self._last_advance_at = time.time()
            self._started_at = time.time()
            self._stop_event.clear()
            self._monitor_thread = threading.Thread(
                target=self._monitor, daemon=True)
            self._monitor_thread.start()

            mode = "收流+转发" if self._destination else "仅收流"
            logbus.ok("RTMP 服务已启动（%s）端口 %d" % (mode, self.rtmp_port),
                      tag="收流")
            return True

    def stop(self) -> None:
        with self._lock:
            self._stop_event.set()
            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.send_signal(signal.SIGTERM)
                    self._proc.wait(timeout=5)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
            if self._pty_fd is not None:
                try:
                    os.close(self._pty_fd)
                except OSError:
                    pass
                self._pty_fd = None
            self._proc = None
            self._publishing = False
            self._started_at = 0
            logbus.info("RTMP 收流已停止", tag="收流")


    def _monitor(self):
        """
        监控推流状态。
        读 FFmpeg 的 -progress 进度文件，解析 frame/size/bitrate。
        比 stderr 可靠（stderr 在管道模式下是块缓冲）。
        """
        last_size = 0
        while not self._stop_event.is_set():
            time.sleep(1.0)
            if self._stop_event.is_set():
                break

            proc = self._proc
            if not proc or proc.poll() is not None:
                if self._publishing:
                    self._publishing = False
                    logbus.warn("收流进程退出", tag="收流")
                break

            try:
                if not self._progress_file or not os.path.exists(self._progress_file):
                    continue
                content = open(self._progress_file, "r").read()
                if not content:
                    continue

                segments = content.split("progress=")
                last_seg = ""
                for seg in reversed(segments):
                    if "out_time_ms=" in seg or "frame=" in seg:
                        last_seg = seg
                        break

                frame = 0
                bitrate = 0
                out_time_ms = 0
                for line in last_seg.splitlines():
                    line = line.strip()
                    if line.startswith("frame="):
                        try:
                            frame = int(line.split("=")[1])
                        except ValueError:
                            pass
                    elif line.startswith("out_time_ms="):
                        try:
                            out_time_ms = int(line.split("=")[1])
                        except ValueError:
                            pass
                    elif line.startswith("bitrate="):
                        val = line.split("=")[1].replace("kbits/s", "").strip()
                        if val != "N/A":
                            try:
                                bitrate = int(float(val))
                            except ValueError:
                                pass

                self._bitrate_kbps = bitrate

                now = time.time()
                advanced = out_time_ms > self._last_out_time_ms
                if advanced:
                    self._last_out_time_ms = out_time_ms
                    self._last_advance_at = now
                was_publishing = self._publishing
                if frame > 0 or advanced:
                    self._publishing = True
                elif now - self._last_advance_at > 3:
                    self._publishing = False

                if self._publishing and not was_publishing:
                    logbus.ok("PS5 开始推流（已收 %.1f 秒）" % (out_time_ms / 1000.0),
                              tag="收流")
                elif not self._publishing and was_publishing:
                    logbus.warn("推流已结束", tag="收流")

            except Exception:
                pass


    def prepare(self) -> bool:
        """检查 ffmpeg 是否可用。"""
        if not shutil.which("ffmpeg"):
            self.last_error = "找不到 ffmpeg。请安装：brew install ffmpeg"
            return False
        return True

    def stat_xml(self, timeout=1.5) -> str:
        """兼容接口（nginx 状态 XML）。FFmpeg 模式返回简化状态。"""
        s = self.snapshot()
        return ('<?xml version="1.0"?>\n'
                '<rtmp>\n'
                '  <server>\n'
                '    <application name="live">\n'
                '      <stream name="stream">\n'
                '        <publishing>%s</publishing>\n'
                '        <bw_in>%d</bw_in>\n'
                '      </stream>\n'
                '    </application>\n'
                '  </server>\n'
                '</rtmp>\n' % (
                    "true" if s["publishing"] else "false",
                    s["bitrate_kbps"] * 1000,
                ))

    def publisher(self) -> dict:
        """
        兼容 NginxManager.publisher() 接口。
        每次调用时读进度文件，实时解析推流状态。
        """
        self._update_from_progress()
        if self._publishing:
            return {
                "publishing": True,
                "app": "live",
                "name": "stream",
                "stream_key": self._stream_key,
                "bitrate_kbps": self._bitrate_kbps,
                "bw_in": self._bitrate_kbps,
                "width": 0,
                "height": 0,
                "uptime": round(self.uptime, 1),
            }
        return {"publishing": False}

    def _update_from_progress(self):
        """从进度文件解析推流状态（publisher() 调用）。"""
        try:
            if not self._progress_file or not os.path.exists(self._progress_file):
                return
            content = open(self._progress_file, "r").read()
            if not content:
                return

            segments = content.split("progress=")
            last_seg = ""
            for seg in reversed(segments):
                if "out_time_ms=" in seg or "frame=" in seg:
                    last_seg = seg
                    break

            frame = 0
            bitrate = 0
            out_time_ms = 0
            for line in last_seg.splitlines():
                line = line.strip()
                if line.startswith("frame="):
                    try:
                        frame = int(line.split("=")[1])
                    except ValueError:
                        pass
                elif line.startswith("out_time_ms="):
                    try:
                        out_time_ms = int(line.split("=")[1])
                    except ValueError:
                        pass
                elif line.startswith("bitrate="):
                    val = line.split("=")[1].replace("kbits/s", "").strip()
                    if val != "N/A":
                        try:
                            bitrate = int(float(val))
                        except ValueError:
                            pass

            self._bitrate_kbps = bitrate
            now = time.time()
            if out_time_ms > self._last_out_time_ms:
                self._last_out_time_ms = out_time_ms
                self._last_advance_at = now
                self._publishing = True
            elif frame > 0:
                self._publishing = True
            elif now - self._last_advance_at > 3:
                self._publishing = False
            if not self.running():
                self._publishing = False

        except Exception:
            pass
