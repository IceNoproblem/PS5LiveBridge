from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque

from . import amf, logbus, paths

DEFAULT_WEB_PORT = 8098
BANDWIDTH_WINDOW = 3.0
POLL_INTERVAL = 1.0


def _bases():
    bases = [paths.BASE_DIR]
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        bases.append(meipass)
    if getattr(sys, "frozen", False):
        try:
            bases.append(os.path.dirname(os.path.abspath(sys.executable)))
        except Exception:
            pass
    else:
        bases.append(os.getcwd())

    out = []
    for b in list(bases):
        out.append(b)
        out.append(os.path.join(b, "tools"))
        out.append(os.path.join(b, "_internal"))
        out.append(os.path.join(b, "_internal", "tools"))

    seen, uniq = set(), []
    for d in out:
        d = os.path.abspath(d)
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    return uniq


def _candidates():
    out = []
    for base in _bases():
        for name in ("rtmf.exe", "rtmp-forwarder.exe", "rtmf", "rtmp-forwarder"):
            out.append(os.path.join(base, name))
    return out


def find_exe() -> str:
    for p in _candidates():
        if os.path.exists(p):
            return p
    return ""


def _http(method: str, url: str, body=None, timeout: float = 3.0):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw.decode("utf-8", "ignore")) if raw else {}


def split_rtmp_url(addr: str, key: str):
    """
    把 B 站的 (地址, 密钥) 拆成 rtmp-forwarder 要的 (host:port, app, stream)。

    B 站给的形式是：
        addr = rtmp://live-push.bilivideo.com/live-broadcast
        key  = ?streamkey=xxxxx
    拼起来的完整地址里，app 是 live-broadcast，stream 是 ?streamkey=xxxxx。
    """
    a = (addr or "").strip()
    k = (key or "").strip()
    host, app = "", ""

    if a.startswith("rtmp://") or a.startswith("rtmps://"):
        rest = a.split("://", 1)[1]
        rest = rest.split("?", 1)[0]
        parts = rest.strip("/").split("/")
        host = parts[0] if parts else ""
        app = "/".join(parts[1:]) if len(parts) > 1 else ""
    else:
        parts = a.strip("/").split("/")
        host = parts[0] if parts else ""
        app = "/".join(parts[1:]) if len(parts) > 1 else ""

    if host and ":" not in host:
        host = host + ":1935"
    if k.startswith("/"):
        k = k[1:]
    return host, app, k


class RtmpForwarderManager(object):
    """
    rtmp-forwarder 的管理壳，接口与 NginxManager 对齐。

    它一个进程同时干两件事：接住 PS5 的推流、把流转发到 B 站。
    状态靠 HTTP 接口拿，码率靠字节数差分算 —— 对方没直接给码率。
    """

    def __init__(self, rtmp_port: int = 1935, web_port: int = 0):
        self.exe = find_exe()
        self.rtmp_port = int(rtmp_port or 1935)
        self.web_port = int(web_port or os.environ.get("PS5LB_RTMPF_PORT")
                            or DEFAULT_WEB_PORT)
        self.base = "http://127.0.0.1:%d" % self.web_port
        self.cfg_path = os.path.join(paths.DATA_DIR, "rtmpf.json")

        self.proc = None
        self.last_error = ""
        self._lock = threading.RLock()
        self._snap = {}
        self._meta = {}
        self._samples = deque()
        self._bw = 0
        self._started_at = 0.0
        self._dest = None
        self._stop = threading.Event()
        self._thread = None

    # ── 进程 ──

    def running(self) -> bool:
        p = self.proc
        return bool(p is not None and p.poll() is None)

    @property
    def forwarding(self) -> bool:
        with self._lock:
            return bool(self._snap.get("stream_key")) and self._dest is not None

    @property
    def uptime(self) -> float:
        return (time.time() - self._started_at) if self._started_at else 0.0

    def start(self) -> bool:
        if self.running():
            return True
        if not self.exe:
            self.last_error = "找不到 rtmpf.exe"
            return False

        os.makedirs(paths.DATA_DIR, exist_ok=True)
        self._write_config([])
        args = [self.exe,
                "-l", "0.0.0.0:%d" % self.rtmp_port,
                "-w", "127.0.0.1:%d" % self.web_port,
                "-c", self.cfg_path]
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | \
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            self.proc = subprocess.Popen(args, cwd=paths.BASE_DIR,
                                         stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL,
                                         creationflags=flags)
        except Exception as e:
            self.last_error = "启动失败：%s" % e
            return False

        for _ in range(40):
            if self.proc.poll() is not None:
                self.last_error = "进程启动后立即退出（端口 %d 被占？）" % self.rtmp_port
                return False
            try:
                _http("GET", self.base + "/api/stream-info", timeout=1.0)
                break
            except Exception:
                time.sleep(0.25)
        else:
            self.last_error = "接口没响应（端口 %d）" % self.web_port
            return False

        self._started_at = time.time()
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll_loop,
                                        name="rtmpf-poll", daemon=True)
        self._thread.start()
        logbus.ok("收流服务已就绪（rtmp-forwarder，RTMP %d / 管理 %d）"
                  % (self.rtmp_port, self.web_port), tag="收流")
        return True

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None
        p = self.proc
        self.proc = None
        self._started_at = 0.0
        if p is None:
            return
        try:
            p.terminate()
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    # ── 配置与转发 ──

    def _write_config(self, forwarders):
        cfg = {
            "server": {"listen_addr": "0.0.0.0:%d" % self.rtmp_port,
                       "app": None, "stream_key": None},
            "forwarders": forwarders,
            "relay_addr": "",
            "relay_enabled": False,
            "web": {"addr": "127.0.0.1:%d" % self.web_port,
                    "username": None, "password": None},
            "log_level": "info",
        }
        try:
            with open(self.cfg_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=1)
        except Exception as e:
            logbus.warn("写 rtmpf 配置失败：%s" % e, tag="收流")

    def set_destination(self, addr: str, key: str) -> bool:
        host, app, stream = split_rtmp_url(addr, key)
        if not host or not stream:
            self.last_error = "推流地址解析不出来：%s / %s" % (addr, key[:8] + "...")
            return False
        body = {
            "forwarders": [{"addr": host, "app": app or None,
                            "stream": stream, "enabled": True}],
            "relay_addr": "",
            "relay_enabled": False,
        }
        try:
            _http("POST", self.base + "/api/config", body, timeout=8.0)
        except Exception as e:
            self.last_error = "下发转发配置失败：%s" % e
            return False
        with self._lock:
            self._dest = host
        logbus.info("转发目标已下发：%s/%s" % (host, app or "-"), tag="转发")
        return True

    def clear_destination(self) -> None:
        try:
            _http("POST", self.base + "/api/config",
                  {"forwarders": [], "relay_addr": "", "relay_enabled": False},
                  timeout=5.0)
        except Exception:
            pass
        with self._lock:
            self._dest = None

    # ── 状态 ──

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception as e:
                logbus.debug("轮询 rtmpf 失败：%s" % e, tag="收流")
            self._stop.wait(POLL_INTERVAL)

    def _poll_once(self) -> None:
        info = _http("GET", self.base + "/api/stream-info", timeout=2.0)
        now = time.time()
        key = info.get("stream_key") or ""
        total = int(info.get("bytes_total") or 0)

        meta = {}
        raw = info.get("metadata")
        if raw:
            try:
                import base64
                meta = amf.decode_metadata(base64.b64decode(raw))
            except Exception:
                meta = {}

        with self._lock:
            if key:
                self._samples.append((now, total))
                while self._samples and now - self._samples[0][0] > BANDWIDTH_WINDOW:
                    self._samples.popleft()
                if len(self._samples) >= 2:
                    t0, b0 = self._samples[0]
                    t1, b1 = self._samples[-1]
                    dt = t1 - t0
                    if dt > 0.2 and b1 >= b0:
                        self._bw = int((b1 - b0) * 8 / dt // 1000)
                self._meta = meta
            else:
                self._samples.clear()
                self._bw = 0
                self._meta = {}

            self._snap = {
                "publishing": bool(key),
                "name": key,
                "app": info.get("app_name") or "",
                "tc_url": info.get("tc_url") or "",
                "bytes_total": total,
            }

    def publisher(self) -> dict:
        with self._lock:
            snap = dict(self._snap)
            meta = dict(self._meta)
            bw = self._bw
        publishing = bool(snap.get("publishing"))
        return {
            "publishing": publishing,
            "name": snap.get("name") or "",
            "app": snap.get("app") or "app",
            "width": amf.pick(meta, "width") if publishing else 0,
            "height": amf.pick(meta, "height") if publishing else 0,
            "fps": amf.pick(meta, "framerate", "fps") if publishing else 0,
            "bw_in": bw if publishing else 0,
            "uptime": self.uptime if publishing else 0.0,
            "bytes_total": snap.get("bytes_total") or 0,
        }

    def stat_xml(self, timeout: float = 1.5) -> str:
        return ""
