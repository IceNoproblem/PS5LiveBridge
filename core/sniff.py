from __future__ import annotations

import threading
import time

try:
    import pydivert
except Exception:
    pydivert = None

try:
    from .divert import _LAYER_NETWORK
except Exception:
    _LAYER_NETWORK = 0

try:
    from . import netutil
except Exception:
    netutil = None

TWITCH_ALT_PORTS = range(2805, 2900)


def _local_ips() -> set:
    ips = {"127.0.0.1"}
    if netutil is None:
        return ips
    try:
        ips.add(netutil.socket_lan_ip())
    except Exception:
        pass
    try:
        import socket
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    return ips


def _explain(exc) -> str:
    t = str(exc)
    if "1450" in t or "系统资源不足" in t:
        return ("系统资源不足（错误 1450）。重启电脑后再试，"
                "或者退出杀毒软件。")
    if "5" == t.strip() or "denied" in t.lower():
        return "权限不足：嗅探需要以管理员身份运行。"
    if "1058" in t or "disabled" in t.lower():
        return "WinDivert 驱动被禁用了，去服务管理器改回「手动」再试。"
    return "嗅探起不来：%s" % t[:120]


class PortSniffer(object):
    """
    只读捕获 PS5 发起的所有 TCP 连接，看它到底连了哪些端口。

    为什么需要它：Twitch 的推流除了 1935，还有 2805-2899 一段备用端口。
    以前只监听 1935，万一 PS5 回落备用端口，我们既收不到流，
    也不知道它去了哪里 —— 只能看用户「开播了但没画面」，然后在黑暗里猜。

    实现上必须把包原样送回去（w.send），不然就变成拦包了。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

        self.running = False
        self.error = ""
        self.ps5_ip = ""
        self.started_at = 0.0
        self.duration = 30
        self.hits = {}
        self.total = 0
        self._local = set()

    def start(self, ps5_ip: str = "", seconds: int = 30):
        if pydivert is None:
            return False, "缺少 pydivert 组件，无法嗅探"
        if self.running:
            return True, ""
        if self._thread is not None and self._thread.is_alive():
            return self.running, self.error
        self.ps5_ip = (ps5_ip or "").strip()
        try:
            self.duration = max(5, min(120, int(seconds)))
        except (TypeError, ValueError):
            self.duration = 30
        with self._lock:
            self.hits = {}
            self.total = 0
        self.error = ""
        self._stop.clear()
        self._local = _local_ips()
        self._thread = threading.Thread(target=self._loop, name="sniff", daemon=True)
        self._thread.start()

        for _ in range(30):
            if self.running:
                return True, ""
            if self.error:
                return False, self.error
            time.sleep(0.1)
        return False, self.error or "嗅探启动超时"

    def stop(self):
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=3.0)
        self.running = False
        self._thread = None

    def _loop(self):
        filt = "tcp.Syn and tcp.Ack == 0"
        if self.ps5_ip:
            filt = "ip.SrcAddr == %s and (%s)" % (self.ps5_ip, filt)
        try:
            with pydivert.WinDivert(filt, layer=_LAYER_NETWORK) as w:
                self.running = True
                self.started_at = time.time()
                while not self._stop.is_set():
                    if time.time() - self.started_at >= self.duration:
                        break
                    try:
                        p = w.recv(timeout=0.4)
                    except TimeoutError:
                        continue
                    except OSError as e:
                        self.error = "读取数据包失败：%s" % e
                        break
                    if p is None:
                        continue
                    try:
                        w.send(p)
                    except Exception:
                        pass
                    try:
                        self._record(p)
                    except Exception:
                        pass
        except Exception as e:
            self.error = _explain(e)
        finally:
            self.running = False

    def _record(self, p):
        tcp = getattr(p, "tcp", None)
        if tcp is None:
            return
        try:
            src = str(p.src_addr)
            dst = str(p.dst_addr)
            port = int(p.dst_port)
        except Exception:
            return

        now = time.time()
        with self._lock:
            if not self.ps5_ip:
                self.ps5_ip = src
            self.total += 1
            seq = self.total
            h = self.hits.get(port)
            if h is None:
                h = {"port": port, "count": 0, "dst": [], "first": now,
                     "last": now, "local": False, "remote": False}
                self.hits[port] = h
            h["count"] += 1
            h["last"] = now
            is_local = dst in self._local
            h["local"] = h["local"] or is_local
            if not is_local:
                h["remote"] = True
                if dst not in h["dst"] and len(h["dst"]) < 3:
                    h["dst"].append(dst)

        if seq <= 200:
            try:
                from . import logbus
                logbus.debug("SYN %s:%d -> %s:%d%s" % (
                    src, int(getattr(p, "src_port", 0) or 0), dst, port,
                    "（本机）" if is_local else ""), tag="嗅探")
            except Exception:
                pass

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            ports = []
            for h in sorted(self.hits.values(), key=lambda x: x["port"]):
                ports.append({
                    "port": h["port"],
                    "count": h["count"],
                    "dst": list(h["dst"]),
                    "local": h["local"],
                    "remote": h["remote"],
                    "alt": h["port"] in TWITCH_ALT_PORTS,
                })
            elapsed = (now - self.started_at) if self.started_at else 0
            remain = max(0, int(self.duration - elapsed)) if self.running else 0
            total = self.total
            ip = self.ps5_ip

        alt = [p for p in ports if p["alt"]]
        remote_alt = [p for p in alt if p["remote"]]
        if self.error:
            level, verdict = "err", self.error
        elif not ports:
            level = "warn"
            verdict = ("这 %d 秒内没抓到任何连接。PS5 是不是没在开播？"
                       "或者这台电脑不在它的路径上。" % self.duration)
        elif remote_alt:
            level = "err"
            verdict = ("PS5 往 %s 这些备用端口发起了连接，而且不是连本机。"
                       "这些包直接去了公网，我们收不到 —— 开播会卡在这一步。"
                       % "、".join(str(p["port"]) for p in remote_alt))
        elif alt:
            level = "ok"
            verdict = "PS5 试过备用端口，但都连到了本机，没问题。"
        else:
            has_1935 = any(p["port"] == 1935 and p["local"] for p in ports)
            if has_1935:
                level, verdict = "ok", "PS5 的推流连接正常打到本机 1935。"
            else:
                level = "warn"
                verdict = ("抓到了连接，但没有一条打到本机 1935。"
                           "检查 DNS / hosts 是不是没指对。")

        return {
            "running": bool(self.running),
            "error": self.error,
            "ps5_ip": ip,
            "duration": self.duration,
            "remain": remain,
            "total": total,
            "ports": ports,
            "level": level,
            "verdict": verdict,
        }


_SNIFFER = None
_LOCK = threading.Lock()


def get_sniffer() -> PortSniffer:
    global _SNIFFER
    with _LOCK:
        if _SNIFFER is None:
            _SNIFFER = PortSniffer()
        return _SNIFFER


def guess_ps5_ip(orch) -> str:
    """猜 PS5 的 IP：先看谁在查本机 DNS，再看劫持学到的源地址。"""
    try:
        dns = getattr(orch, "dns", None)
        if dns is not None and getattr(dns, "running", False):
            local = _local_ips()
            stats = getattr(dns, "stats", None)
            if stats is not None:
                best, best_n = "", -1
                for ip, c in (getattr(stats, "clients", {}) or {}).items():
                    if ip in local:
                        continue
                    n = c.get("queries", 0) if isinstance(c, dict) else 0
                    if n > best_n:
                        best, best_n = ip, n
                if best:
                    return best
    except Exception:
        pass

    try:
        dv = getattr(orch, "divert", None)
        if dv is not None:
            for key in (getattr(dv, "_learned", {}) or {}):
                if isinstance(key, tuple) and key and isinstance(key[0], str):
                    return key[0]
    except Exception:
        pass
    return ""
