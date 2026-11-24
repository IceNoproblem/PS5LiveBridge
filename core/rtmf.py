from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque

from . import amf, logbus, netutil, paths

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
    """
    找收流端二进制。

    容器镜像里它装在 /usr/local/bin，所以除了程序自己的目录和 tools，
    还要扫 PATH。这里不用 shutil.which：它要求文件已经有执行位，
    而解压或拷进容器时恰恰最容易丢这个位，丢了也要能找回来补上。
    """
    cands = list(_candidates())
    _names = (("rtmf.exe", "rtmp-forwarder.exe") if os.name == "nt"
              else ("rtmf", "rtmp-forwarder"))
    for d in (os.environ.get("PATH") or "").split(os.pathsep):
        d = (d or "").strip().strip('"')
        if not d:
            continue
        for n in _names:
            cands.append(os.path.join(d, n))

    for p in cands:
        if not os.path.isfile(p):
            continue
        if os.name != "nt" and not os.access(p, os.X_OK):
            try:
                os.chmod(p, os.stat(p).st_mode | 0o111)
            except Exception:
                continue
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


def split_full_url(url: str):
    """
    把一整条推流地址拆成 rtmp-forwarder 要的 (host:port, app, stream)。

    用户从平台后台粘过来的是完整地址，形如
        rtmp://push-rtmp.douyin.com/live/xxxx?expire=1700000000
    最后一段是推流码（可能带查询串），前面几段是 app。
    只有 host 加一段时（rtmp://host/key），app 留空。
    """
    u = (url or "").strip()
    if not u:
        return "", "", ""
    if "://" in u:
        u = u.split("://", 1)[1]
    parts = u.strip("/").split("/")
    if not parts or not parts[0]:
        return "", "", ""
    host = parts[0]
    if ":" not in host:
        host += ":1935"
    rest = parts[1:]
    if not rest:
        return host, "", ""
    return host, "/".join(rest[:-1]), rest[-1]


def _explain_exit(rtmp_port: int, web_port: int, tail: str) -> str:
    """
    把 rtmf 的退出原因翻译成人能看懂的话。

    以前这里只写「进程启动后立即退出（端口 X 被占？）」，可实际日志里
    往往写着 Windows 错误码。10013 是权限拒绝，不是被别人占着；
    10048 才是真的地址在用。两种情况解法完全不同，不能混在一句里。
    """
    if "10013" in tail:
        return ("收流端绑定 %d 失败，Windows 返回 10013（权限拒绝）。"
                "这个端口没有程序在监听，多半是被系统保留了：装了 Hyper-V、WSL、"
                "Docker Desktop 的机器会占掉一批端口，开机后每次分配的段还可能不一样。"
                "用管理员命令行按顺序执行：net stop winnat，然后重开本程序，"
                "起来之后 net start winnat。还不行就跑 "
                "netsh int ipv4 show excludedportrange protocol=tcp "
                "看 %d 是否落在保留段里。" % (rtmp_port, rtmp_port))
    if "10048" in tail:
        return ("收流端口 %d 已经被别的程序占用（10048）。"
                "netstat -ano | findstr :%d 能看到是谁，结束它再重试。"
                % (rtmp_port, rtmp_port))
    if tail:
        return "进程启动后立即退出（端口 %d）：%s" % (rtmp_port, tail)
    return "进程启动后立即退出（端口 %d），没有任何输出" % rtmp_port


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
        self._logfh = None
        self.last_error = ""
        # HTTP-FLV 口撞了系统保留段会被换掉，记下原因给界面看
        self.web_port_note = ""
        self._lock = threading.RLock()
        # 停掉的 Popen 挂这儿不就地析构：Python 3.13 的 Popen.__del__
        # 会递归爆栈，在请求线程里触发会直接带走整个进程。
        self._retired = []
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

    def _stderr_tail(self, limit: int = 3) -> str:
        """取 rtmf-stderr.log 末尾几条关键行，用来解释秒退原因。"""
        path = os.path.join(paths.LOGS_DIR, "rtmf-stderr.log")
        try:
            with open(path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 8192))
                text = f.read().decode("utf-8", "replace")
        except Exception:
            return ""
        hits = []
        for line in text.splitlines():
            s = line.strip()
            if not s or s.startswith("=====") or s.startswith("note:"):
                continue
            if any(k in s for k in ("Error", "code:", "panic")):
                hits.append(s)
        return " | ".join(hits[-limit:])[:320]

    def start(self) -> bool:
        if self.running():
            return True
        # 上一次启动失败留下的死进程先收进退休列表，别让下一次赋值时析构它
        if self.proc is not None and self.proc.poll() is not None:
            netutil.disarm_popen(self.proc)
            self._retired.append(self.proc)
            self.proc = None
        if not self.exe:
            self.last_error = "找不到 rtmpf.exe"
            return False

        # 起之前先清场。
        # 上次进程没退干净时，它还抱着 PS5 的 publisher 会话：新进程 bind 不上
        # 1935，旧进程继续拒掉 PS5 的每一次重推，PS5 就报「有其他设备正在向
        # 同一频道播放」。原来这里什么都不查，直接 Popen，于是「启动成功」
        # 其实连到了旧实例的管理口，状态全对不上。
        for _port, _label in ((self.rtmp_port, "RTMP"), (self.web_port, "管理")):
            guard = netutil.free_port_for_receiver(_port)
            if not guard["ok"]:
                self.last_error = guard["detail"]
                if _port == self.rtmp_port:
                    self.last_error += ("。PS5 推流必须走这个端口，"
                                        "先关掉占用它的程序（别的直播软件、虚拟机、Docker）再试")
                logbus.error(self.last_error, tag="收流")
                return False
            if guard["killed"]:
                logbus.warn("%s端口 %d：%s" % (_label, _port, guard["detail"]), tag="收流")

        os.makedirs(paths.DATA_DIR, exist_ok=True)
        # HTTP-FLV 口只服务于「本地取流」，被占就让一个，别把收流整个搞挂
        # 先看上次用的是哪个口，能沿用就沿用，别让重启把本地取流地址改掉
        if not os.environ.get("PS5LB_RTMPF_PORT"):
            _saved = netutil.recall_port("rtmf_web")
            if _saved and _saved != self.web_port:
                self.web_port = _saved
                self.base = "http://127.0.0.1:%d" % self.web_port
        _pick = netutil.pick_port(self.web_port)
        if _pick["port"] and _pick["port"] != self.web_port:
            self.web_port_note = _pick["reason"]
            self.web_port = _pick["port"]
            self.base = "http://127.0.0.1:%d" % self.web_port
            logbus.warn(_pick["reason"], tag="收流")
        else:
            self.web_port_note = ""
        if not os.environ.get("PS5LB_RTMPF_PORT"):
            netutil.remember_port("rtmf_web", self.web_port)
        self._write_config([])
        args = [self.exe,
                "-l", "0.0.0.0:%d" % self.rtmp_port,
                # HTTP-FLV 口也要给局域网：本地取流是给 OBS 用的，
                # OBS 经常在另一台机器上。原来绑 127.0.0.1，导致
                # http://<本机局域网IP>:8098/... 连不上（2026-11-01 实测）。
                "-w", "0.0.0.0:%d" % self.web_port,
                "-c", self.cfg_path]
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | \
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            log_path = os.path.join(paths.LOGS_DIR, "rtmf-stderr.log")
            try:
                os.makedirs(paths.LOGS_DIR, exist_ok=True)
                self._logfh = open(log_path, "ab", buffering=0)
                self._logfh.write(("\n===== %s start =====\n"
                                   % time.strftime("%Y-%m-%d %H:%M:%S")).encode("utf-8"))
            except Exception:
                self._logfh = None
            self.proc = subprocess.Popen(args, cwd=paths.BASE_DIR,
                                         stdout=self._logfh or subprocess.DEVNULL,
                                         stderr=subprocess.STDOUT,
                                         creationflags=flags)
        except Exception as e:
            self.last_error = "启动失败：%s" % e
            return False

        for _ in range(40):
            if self.proc.poll() is not None:
                self.last_error = _explain_exit(self.rtmp_port, self.web_port,
                                                self._stderr_tail())
                return False
            try:
                _http("GET", self.base + "/api/stream-info", timeout=1.0)
                break
            except Exception:
                time.sleep(0.25)
        else:
            self.last_error = "接口没响应（端口 %d）" % self.web_port
            return False

        # 接口能通不等于「这个进程起的」。管理口可能还是上一次残留实例的，
        # 那种情况下界面显示收流正常，实际收流的却是没人管的旧进程。
        owner, _owner_name = netutil.port_owner(self.web_port)
        if owner and self.proc.pid and owner != self.proc.pid:
            self.last_error = ("管理端口 %d 属于另一个进程（PID %d），不是刚启动的这个"
                               "（PID %d），收流端可能没起来"
                               % (self.web_port, owner, self.proc.pid))
            logbus.error(self.last_error, tag="收流")
            self.stop()
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
        self._started_at = 0.0
        p = self.proc
        if p is not None:
            try:
                if p.poll() is None:
                    p.terminate()
                    p.wait(timeout=5)
            except Exception:
                pass
            # terminate 对 Windows 上的 Go 程序经常不生效。确认一次，
            # 还活着就连进程树一起强杀。留一个僵尸在 1935 上抱着
            # publisher 会话，PS5 再推就被拒，这就是那句
            # 「有其他设备正在向同一频道播放」的直接来源。
            if p.poll() is None:
                logbus.warn("收流进程（PID %d）没响应终止请求，强制结束" % p.pid, tag="收流")
                netutil.kill_pid_tree(p.pid)
                try:
                    p.wait(timeout=3)
                except Exception:
                    pass
            if p.poll() is None:
                logbus.error("收流进程（PID %d）结束不掉，下次启动会按端口自动清理" % p.pid,
                             tag="收流")
        # 这里无论死没死都放掉引用。原先是先置 None 再 terminate，
        # 杀失败就成了「进程还在、程序认为它没了」的幽灵。现在由 start()
        # 里的端口清场兜底，引用的执念反而会让 running() 永远为真。
        if p is not None:
            netutil.disarm_popen(p)
            self._retired.append(p)
        self.proc = None
        fh = getattr(self, "_logfh", None)
        if fh:
            try:
                fh.close()
            except Exception:
                pass
            self._logfh = None

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
        return self._push_forwarders([(host, app, stream)])

    def set_destinations(self, urls) -> bool:
        """
        一次下发多个转发目标。

        rtmp-forwarder 自己把一份流转发到每个目标，不额外占 CPU，
        所以多平台推流在这条路上是零拷贝的。地址是用户粘过来的完整形式。
        """
        rows = []
        for u in urls or ():
            host, app, stream = split_full_url(u)
            if not host or not stream:
                self.last_error = "推流地址解析不出来：%s" % ((u or "")[:48] + "...")
                return False
            rows.append((host, app, stream))
        return self._push_forwarders(rows)

    def _push_forwarders(self, rows) -> bool:
        fw = [{"addr": h, "app": a or None, "stream": s, "enabled": True}
              for h, a, s in rows]
        body = {"forwarders": fw, "relay_addr": "", "relay_enabled": False}
        try:
            _http("POST", self.base + "/api/config", body, timeout=8.0)
        except Exception as e:
            self.last_error = "下发转发配置失败：%s" % e
            return False
        with self._lock:
            self._dest = fw[0]["addr"] if fw else None
        if fw:
            logbus.info("转发目标 %d 个：%s"
                        % (len(fw), "、".join(x["addr"] for x in fw)), tag="转发")
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
