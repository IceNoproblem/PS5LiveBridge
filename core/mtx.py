"""
MediaMTX 收流端（macOS）。

Windows 用 rtmf，容器用 nginx，这两个在 macOS 上都跑不了。
MediaMTX 是 Go 写的单二进制，自带 RTMP 收流和一个 HTTP API。

这里有个和另外两个实现不同的地方：PS5 推上来的流名带随机后缀
（形如 live_一串数字_一串随机字母），没法写死拉流地址，所以对外多一个
current_path()，让 relay 拿去拼 input_url。API 的 /v3/paths/list
会把 query（?broadcaster=xxx）剥掉，只留路径，正好够用。
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.request

from . import logbus, paths

# MediaMTX 不直接给码率，靠 bytesReceived 前后两次采样差分算。
# 进程里只有一份收流服务，用模块级变量存上次的采样就够了。
_bw_state = {"bytes": 0, "ts": 0.0}

CONFIG_NAME = "mediamtx.yml"


def _exe_candidates():
    """二进制可能在哪：打包后在 _internal/tools，开发时在 tools/macos。"""
    out = []
    for rel in (("tools", "mediamtx"),
                ("tools", "macos", "mediamtx"),
                ("tools", "macos-arm64", "mediamtx")):
        try:
            out.append(paths.resource_path(*rel))
        except Exception:
            pass
    return out


class MediaMtxManager(object):
    """和 nginxmgr / rtmf 保持同一套接口，orchestrator 那边不用改调用方式。"""

    def __init__(self, rtmp_port: int = 1935, api_port: int = 9997):
        self.rtmp_port = int(rtmp_port)
        self.api_port = int(api_port)
        self.last_error = ""
        self.web_port = 0          # MediaMTX 不提供 HTTP-FLV，留空
        self._proc = None
        self._lock = threading.Lock()

    # ---------- 二进制与配置 ----------

    def exe_path(self) -> str:
        for p in _exe_candidates():
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
        return ""

    def _config_path(self) -> str:
        return os.path.join(paths.DATA_DIR, CONFIG_NAME)

    def _write_config(self) -> str:
        """只开 RTMP 和 API，其它协议全关，少开一个端口少一份事。"""
        cfg = (
            "logLevel: info\n"
            "api: yes\n"
            "apiAddress: 127.0.0.1:%d\n"
            "rtsp: no\n"
            "hls: no\n"
            "webrtc: no\n"
            "srt: no\n"
            "playback: no\n"
            # MoQ 默认会去绑 8892/8893。这两个口很容易被别的程序占掉，
            # 占了之后 MediaMTX 启动直接报 "address already in use" 退出，
            # 收流就整个起不来了。我们只用 RTMP，关掉干净。
            "moq: no\n"
            "rtmp: yes\n"
            "rtmpAddress: :%d\n"
            "rtmpEncryption: \"no\"\n"
            # 默认 512 帧（1080p60 约 8 秒）。ffmpeg 重连或者本地播放器
            # 解码跟不上时，缓冲一满 MediaMTX 就开始丢帧，表现为画面卡顿发糊。
            # 抬到 2048 帧（约 34 秒），代价是内存多占一两百兆。
            "writeQueueSize: 2048\n"
            "paths:\n"
            "  all_others:\n"
        ) % (self.api_port, self.rtmp_port)
        p = self._config_path()
        with open(p, "w", encoding="utf-8") as f:
            f.write(cfg)
        return p

    # ---------- 启停 ----------

    def start(self) -> bool:
        with self._lock:
            if self.running():
                return True
            exe = self.exe_path()
            if not exe:
                self.last_error = ("找不到 mediamtx。macOS 版需要 tools/macos/mediamtx，"
                                   "从 bluenviron/mediamtx 的 release 里下 darwin-arm64 那份。")
                logbus.error(self.last_error, tag="收流")
                return False

            try:
                cfg = self._write_config()
            except Exception as e:
                self.last_error = "写配置失败：%s" % e
                return False

            rtmp_log = os.path.join(paths.DATA_DIR, "logs", "mediamtx.log")
            try:
                os.makedirs(os.path.dirname(rtmp_log), exist_ok=True)
                fh = open(rtmp_log, "ab", buffering=0)
            except Exception:
                fh = subprocess.DEVNULL

            try:
                self._proc = subprocess.Popen(
                    [exe, cfg],
                    stdout=fh,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except Exception as e:
                self.last_error = "启动 mediamtx 失败：%s" % e
                logbus.error(self.last_error, tag="收流")
                return False

            # 等端口起来，最多 5 秒
            for _ in range(25):
                time.sleep(0.2)
                if self.running():
                    self.last_error = ""
                    logbus.ok("收流服务已就绪（MediaMTX，RTMP %d / 管理 %d）"
                              % (self.rtmp_port, self.api_port), tag="收流")
                    return True
                if self._proc.poll() is not None:
                    break

            tail = ""
            try:
                with open(rtmp_log, "rb") as f:
                    f.seek(max(0, os.path.getsize(rtmp_log) - 400))
                    tail = f.read().decode("utf-8", errors="replace").strip()
            except Exception:
                pass
            self.last_error = ("mediamtx 没起来，可能 %d 端口被占用。%s"
                               % (self.rtmp_port, tail[-200:]))
            logbus.error(self.last_error, tag="收流")
            self.stop()
            return False

    def stop(self) -> None:
        with self._lock:
            p = self._proc
            self._proc = None
            if p is None:
                return
            try:
                if p.poll() is None:
                    p.terminate()
                    p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    def running(self) -> bool:
        p = self._proc
        if p is None or p.poll() is not None:
            return False
        return bool(self._api("/v3/config/global/get"))

    # ---------- API ----------

    def _api(self, path: str, timeout: float = 2.0) -> dict:
        url = "http://127.0.0.1:%d%s" % (self.api_port, path)
        try:
            # 本机请求，绕开系统代理，否则 Clash 一类会把 127.0.0.1 也代理走
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(url, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            return {}

    def paths(self) -> list:
        d = self._api("/v3/paths/list")
        return d.get("items") or []

    def current_path(self) -> str:
        """当前正在推流的那条路径，没有就返回空串。"""
        for it in self.paths():
            if it.get("ready"):
                return it.get("name") or ""
        return ""

    def publisher(self) -> dict:
        """orchestrator 要的格式，和 nginxmgr / rtmf 的返回对齐。

        MediaMTX 的 API 和 nginx-rtmp 不一样，几个字段要自己凑：
        - 分辨率在 /v3/paths/list 的 tracks2[].codecProps 里
        - 码率没有现成字段，拿 bytesReceived 前后两次差分算
        - 帧率整个 API 都不提供，只能给 0，界面自己决定显不显示
        """
        name = self.current_path()
        if not name:
            _bw_state["bytes"] = 0
            _bw_state["ts"] = 0.0
            return {"publishing": False}

        item = {}
        for it in self.paths():
            if it.get("name") == name:
                item = it
                break

        width = height = 0
        for t in (item.get("tracks2") or []):
            cp = t.get("codecProps") or {}
            if cp.get("width"):
                width = int(cp.get("width") or 0)
                height = int(cp.get("height") or 0)
                break

        # 码率：bytesReceived 差分，单位用 kbps。
        # 界面上是 bw_in/1000 当 Mbps 显示的，和 nginx 的 bw_in 保持同一口径。
        total = int(item.get("bytesReceived") or 0)
        now = time.time()
        bw = 0
        if _bw_state["ts"] and total >= _bw_state["bytes"]:
            dt = now - _bw_state["ts"]
            if dt >= 0.3:
                bw = int((total - _bw_state["bytes"]) * 8 / dt / 1000)
        _bw_state["bytes"] = total
        _bw_state["ts"] = now

        # app/live_xxx → app + live_xxx，界面按这两段显示
        if "/" in name:
            app, stream = name.split("/", 1)
        else:
            app, stream = "app", name
        return {
            "publishing": True,
            "app": app,
            "name": stream,
            "width": width,
            "height": height,
            "fps": 0,
            "bw_in": bw,
        }
