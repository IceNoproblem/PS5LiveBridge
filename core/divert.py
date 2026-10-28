from __future__ import annotations

import threading
import time

try:
    import pydivert
except Exception:
    pydivert = None

try:
    _LAYER_NETWORK = pydivert.Layer.NETWORK
except Exception:
    _LAYER_NETWORK = 0


class PortInterceptor(object):
    """
    把发往指定端口的 TCP 新连接，改写到本机。

    用法：
        ic = PortInterceptor(ports=(1935, 6667))
        ok, err = ic.start()      # 起后台线程，1 秒内返回是否真的抓起来了
        ...
        ic.stop()

    为什么 start() 要等 1 秒：WinDivertOpen 失败（没管理员权限、驱动没起来、
    杀软拦截）时不会抛异常到调用方，只有线程里自己知道。等 1 秒是为了把
    「没抓起来」如实回报给界面，而不是显示绿灯骗用户。
    """

    def __init__(self, ports=(1935, 6667), target_ip: str = "",
                 verbose: bool = False, idle_sweep: float = 120.0,
                 exclude_ips=None):
        self.ports = tuple(int(p) for p in ports)
        self.target_ip = target_ip
        self.verbose = verbose
        self.idle_sweep = idle_sweep

        self.running = False
        self.last_error = ""
        self.started_at = 0.0

        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._learned = {}
        self._exclude = set(exclude_ips or ())

        self.stats = dict((p, {"redirected": 0, "orig_dests": set(), "last": 0.0})
                          for p in self.ports)

    def build_filter(self) -> str:
        """
        一条 filter 覆盖所有端口、两个方向。

        合并成一条而不是每端口开一个 handle：WinDivert 每次 WinDivertOpen
        都会尝试启动驱动服务，如果服务的启动类型被改成 DISABLED（实测遇到过），
        第二次 StartService 报 [WinError 1058]，最后只有第一个端口生效。
        """
        parts = []
        for p in self.ports:
            parts.append("tcp.DstPort == %d" % p)
            parts.append("tcp.SrcPort == %d" % p)
        return "tcp and (" + " or ".join(parts) + ")"

    def _loop(self):
        local_ips = _local_ips()
        filt = self.build_filter()
        try:
            with pydivert.WinDivert(filt, layer=_LAYER_NETWORK) as w:
                self.running = True
                self.started_at = time.time()
                self._log("端口劫持已启动：%s -> 本机 %s"
                          % ("/".join(str(p) for p in self.ports), self.target_ip))
                last_sweep = time.time()

                while not self._stop.is_set():
                    try:
                        p = w.recv(timeout=0.4)
                    except TimeoutError:
                        p = None
                    except OSError as e:
                        self.last_error = "读取数据包失败：%s" % e
                        break
                    if p is None:
                        continue

                    try:
                        now = time.time()
                        if now - last_sweep > 30:
                            with self._lock:
                                for k in [k for k, v in self._learned.items()
                                          if now - v[1] > self.idle_sweep]:
                                    self._learned.pop(k, None)
                            last_sweep = now
                        self._handle(p, local_ips, now)
                    except Exception as e:
                        self._log("处理数据包异常：%s" % e)
                    try:
                        w.send(p)
                    except Exception as e:
                        self._log("回投数据包失败：%s" % e)
        except Exception as e:
            self.last_error = _explain_open_error(e)
            self._log(self.last_error)
        finally:
            self.running = False

    def _handle(self, p, local_ips, now):
        tcp = p.tcp
        if tcp is None:
            return
        dst_port, src_port = p.dst_port, p.src_port
        src_ip, dst_ip = str(p.src_addr), str(p.dst_addr)

        if dst_port in self.ports and dst_ip not in local_ips:
            if dst_ip in self._exclude:
                return
            key = (src_ip, src_port, dst_port)
            is_syn = bool(tcp.syn and not tcp.ack)
            with self._lock:
                known = self._learned.get(key)
                if is_syn:
                    self._learned[key] = (dst_ip, now)
                    first = known is None
                elif known:
                    self._learned[key] = (known[0], now)
                    first = False
                else:
                    if tcp.fin or tcp.rst:
                        self._learned.pop(key, None)
                    return
            p.dst_addr = self.target_ip
            if first:
                self._bump(dst_port, dst_ip, src_ip, local_ips)
                self._log("接下新连接 %s:%d（原本发往 %s:%d）"
                          % (src_ip, src_port, dst_ip, dst_port))
            if tcp.fin or tcp.rst:
                with self._lock:
                    self._learned.pop(key, None)
            return

        if src_port in self.ports and src_ip == self.target_ip:
            key = (dst_ip, dst_port, src_port)
            with self._lock:
                orig = self._learned.get(key)
                if orig:
                    if tcp.fin or tcp.rst:
                        self._learned.pop(key, None)
                    else:
                        self._learned[key] = (orig[0], now)
            if orig:
                p.src_addr = orig[0]

    def _bump(self, port, orig_dst, src_ip, local_ips):
        with self._lock:
            st = self.stats.setdefault(port, {"redirected": 0, "orig_dests": set(), "last": 0.0})
            st["redirected"] += 1
            st["last"] = time.time()
            if src_ip not in local_ips:
                st["orig_dests"].add(src_ip)
            st["orig_dests"].add(orig_dst)

    def _log(self, msg):
        if self.verbose:
            print("  [DIVERT] " + msg, flush=True)

    def set_exclude(self, ips, replace: bool = False) -> list:
        """
        设置「不劫持」的目的 IP。返回当前完整白名单。

        典型用法：拿到 B 站推流地址后，把该域名解析出的所有 A 记录塞进来。
        不做这一步的后果是真机实测过的：B 站推流服务器也用 1935，
        ffmpeg 推流会被自己的劫持吃回本机，报 I/O error，直播永远开不起来。
        """
        with self._lock:
            if replace:
                self._exclude.clear()
            for ip in (ips or ()):
                ip = str(ip or "").strip()
                if ip:
                    self._exclude.add(ip)
            return sorted(self._exclude)

    def get_exclude(self) -> list:
        with self._lock:
            return sorted(self._exclude)

    def available(self) -> bool:
        return pydivert is not None

    def start(self):
        """
        返回 (ok, error)。

        ok=False 时 error 是给用户看的人话，不是异常字符串。
        """
        if pydivert is None:
            return False, ("缺少 pydivert 组件（WinDivert 的 Python 绑定）。"
                           "请执行：pip install pydivert")
        if not self.target_ip:
            return False, "拿不到本机局域网 IP，无法确定把流量转到哪里"
        if self.running:
            return True, ""
        self.last_error = ""
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="divert", daemon=True)
        self._thread.start()
        for _ in range(30):
            if self.running:
                return True, ""
            if self.last_error:
                return False, self.last_error
            time.sleep(0.1)
        return False, self.last_error or "端口劫持启动超时（驱动无响应，可能被杀毒软件拦截）"

    def stop(self):
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=3.0)
        self.running = False
        self._thread = None
        with self._lock:
            self._learned.clear()

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            ports = {}
            total = 0
            for p, st in self.stats.items():
                total += st["redirected"]
                ports[str(p)] = {
                    "redirected": st["redirected"],
                    "idle": round(now - st["last"], 1) if st["last"] else -1,
                    "dests": sorted(st["orig_dests"])[:6],
                }
            active = len(self._learned)
        return {
            "running": bool(self.running),
            "available": self.available(),
            "target": self.target_ip,
            "filter": self.build_filter(),
            "ports": ports,
            "total": total,
            "active_conns": active,
            "uptime": round(now - self.started_at, 1) if self.started_at else 0,
            "exclude": self.get_exclude(),
            "error": self.last_error,
        }


def _local_ips() -> set:
    """本机所有 IPv4（判断「这个目的地址是不是本机自己」要用）。"""
    ips = {"127.0.0.1"}
    try:
        from . import netutil
        for it in netutil.list_adapters():
            if it.get("ip"):
                ips.add(it["ip"])
    except Exception:
        pass
    try:
        import socket
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    return ips


def _explain_open_error(exc) -> str:
    """把 WinDivert 的报错翻译成用户能动手的人话。"""
    text = str(exc)
    low = text.lower()
    if "1058" in text or "disabled" in low:
        return ("WinDivert 驱动服务被禁用了，端口劫持起不来。"
                "请在服务管理器里把「WinDivert」启动类型改回「手动」，然后重试。")
    if "5" == text.strip() or "denied" in low or "10013" in text:
        return "权限不足：端口劫持必须以管理员身份运行本程序。"
    if "1275" in text or "driver" in low:
        return "WinDivert 驱动被系统或杀毒软件拦截，请把本程序加入白名单。"
    return "端口劫持启动失败：%s" % text


def pick_target_ip(lan_ip: str, accel_ip: str = "") -> str:
    """
    选「把流量转到哪个 IP」。

    必须用本机局域网 IP，不能用 127.0.0.1 —— 实测改成 127.0.0.1 后
    包会被当成 martian 丢掉，握手永远不完成（组合矩阵 LO 两种全挂）。

    加速器网卡 IP 也不行：那是加速器隧道自己的地址，把包改到那儿
    等于又交给加速器转发，白忙一场。
    """
    return lan_ip or accel_ip or ""


def pyk_available() -> bool:
    """pydivert 是否可用（界面据此决定要不要显示端口劫持这一项）。"""
    return pydivert is not None


import sys as _sys

if _sys.platform == "darwin":
    from .divert_macos import (PfPortInterceptor as PortInterceptor,
                                pick_target_ip, pf_available as pyk_available)
