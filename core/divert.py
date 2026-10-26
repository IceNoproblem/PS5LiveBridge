# -*- coding: utf-8 -*-
"""
core/divert.py —— 端口级劫持（WinDivert）

为什么必须有这个模块（这是本项目最关键的一次架构修正）
====================================================
最初的设计是 DNS 劫持：让 PS5 把 DNS 指向本机，把推流域名解析到本机 IP。

这条路在「PS5 需要走游戏加速器」的真实场景下是**结构性失败**的，
不是配置写错了，而是在真机上实测出来的两条互相矛盾的结论：

    1. A 记录答「加速器虚拟网卡 IP」→ 加速器认为这是它自己的流量，直接代推到
       真 Twitch。实测抓到铁证：
           192.168.110.117:57206 -> 35.55.5.15:1935  by uu.exe
       PS5 的流就这样绕过本机，跑到 Twitch 上去了。

    2. A 记录答「本机局域网 IP」→ PS5 在加速器网段里（172.22.x.x/16），
       192.168.110.x 不在同网段，包交给网关（加速器隧道），同样进不了本机。

两条都不通。而且实测还发现：把 PS5 的 DNS 指向本机后，**PS5 根本不来查**
（在加速器场景下 DNS 由加速器接管），`ps5_seen` 一直是 False。

所以正确做法是不碰 DNS，直接在 IP 层改写端口：
    把「发往任意远端 IP:1935 的新连接」改写到本机，让 nginx 收到推流。
    PS5 的网络参数完全按加速器给的填，DNS 照走加速器，零冲突。

关键技术细节（都是真机踩出来的，改动前请先读）
============================================
A. **不能过滤 `ip.DstAddr != 本机IP`**
   去程看这条很合理，但回程包的目的地址恰恰就是本机 IP（因为代理推流的
   uu.exe 就在本机），加了这条等于把回程包全部丢掉，表现为「SYN 改写了，
   但永远收不到 SYN-ACK，客户端一直超时」。
   实测组合矩阵：LAN+out+out / LAN+in+in / LAN+in+out 通过，
   只要 filter 排除了本机目的地址就全部失败。

B. **改写目标用本机局域网 IP，不要用 127.0.0.1**
   改成 127.0.0.1 后包会被当成 martian 丢弃，矩阵里 LO 两种组合全挂。

C. **只认 SYN，不碰既有连接**
   中途打开劫持时，无差别改写会把正在跑的推流连接撕成两半：
   去程进本机、回程还是真 Twitch 发的，Twitch 侧留下半开会话，
   下次开播就报「有其他设备正在向该频道直播」。
   只劫新开的连接，开关劫持随便按都不影响正在进行的推流。

D. **连接表键要带目的端口**
   只用源端口做键时，同一源端口先后连 1935 和 6667 会互相覆盖，
   回程包被还原成错误的源地址，连接当场断掉。

E. **pydivert 4.x 的 API 与 2.x 不同**
   没有 `settimeout()`，超时通过 `recv(timeout=float)` 传；
   `send()` 默认就会重算校验和（`recalculate_checksum=True`）。
   照 2.x 的写法会报 `'WinDivert' object has no attribute 'settimeout'`。
"""
from __future__ import annotations

import threading
import time

try:
    import pydivert
except Exception:                                    # pragma: no cover
    pydivert = None

# pydivert 4.x 用枚举，2.x 用整数；这里统一取值，两个版本都能跑
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
        self._learned = {}          # (src_ip, src_port, dst_port) -> (原始目的IP, 最近活动)
        # 白名单：这些目的 IP 的端口不劫持。
        # 必须放进去的头号对象是「B 站推流服务器」—— 它也用 1935，
        # 不排除就会把 ffmpeg 推到 B 站的那条连接劫回本机，自己推给自己，
        # 表现为 ffmpeg 报 "Error opening output ... I/O error"（真机踩过）。
        self._exclude = set(exclude_ips or ())

        self.stats = dict((p, {"redirected": 0, "orig_dests": set(), "last": 0.0})
                          for p in self.ports)

    # ──────────────────────────────────────────────
    # filter
    # ──────────────────────────────────────────────
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

    # ──────────────────────────────────────────────
    # 主循环
    # ──────────────────────────────────────────────
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
                    # 无论改没改，都必须把包放回去，否则连接直接断
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

        # ── 去程：发往远端推流/弹幕端口的新连接 ──
        if dst_port in self.ports and dst_ip not in local_ips:
            if dst_ip in self._exclude:
                return              # 白名单（B 站推流服务器等）：一个字都不许动
            key = (src_ip, src_port, dst_port)
            is_syn = bool(tcp.syn and not tcp.ack)
            with self._lock:
                known = self._learned.get(key)
                if is_syn:
                    # 新连接：记下真实目的地，再把目的改成自己
                    self._learned[key] = (dst_ip, now)
                    first = known is None
                elif known:
                    self._learned[key] = (known[0], now)
                    first = False
                else:
                    if tcp.fin or tcp.rst:
                        self._learned.pop(key, None)
                    return          # 没见过的既有连接：原样放行，绝不撕它
            p.dst_addr = self.target_ip
            if first:
                self._bump(dst_port, dst_ip, src_ip, local_ips)
                self._log("接下新连接 %s:%d（原本发往 %s:%d）"
                          % (src_ip, src_port, dst_ip, dst_port))
            if tcp.fin or tcp.rst:
                with self._lock:
                    self._learned.pop(key, None)
            return

        # ── 回程：本机服务的应答，把源地址还原成客户端期待的那个远端 IP ──
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
            # 加速器/本机代理发起的连接，源地址就是本机，不算「外部客户端」
            if src_ip not in local_ips:
                st["orig_dests"].add(src_ip)
            st["orig_dests"].add(orig_dst)

    def _log(self, msg):
        if self.verbose:
            print("  [DIVERT] " + msg, flush=True)

    # ──────────────────────────────────────────────
    # 白名单
    # ──────────────────────────────────────────────
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

    # ──────────────────────────────────────────────
    # 启停
    # ──────────────────────────────────────────────
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
        # 等它真的抓起来 —— WinDivertOpen 失败只在线程里知道
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

    # ──────────────────────────────────────────────
    # 给界面看的快照
    # ──────────────────────────────────────────────
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
