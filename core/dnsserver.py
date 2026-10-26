# -*- coding: utf-8 -*-
"""
core/dnsserver.py —— 内置 DNS 服务器（纯标准库，无第三方依赖）

只在「路由器重定向做不通」这条分支使用：
    PS5 的 DNS 指向本机 -> 本进程应答 -> 推流域名返回本机 IP，
    其余域名原样转发给上游（必须是加速器 DNS，否则开播握手会绕过加速）。

要点：
    * 同时监听 UDP/53 与 TCP/53。应答被截断时客户端会改用 TCP 重试，
      只做 UDP 会导致部分域名解析失败。
    * 上游可热替换：用户在引导页填了加速器 DNS 之后立刻生效，不用重启。
    * 记录每一次查询，向导靠它判断「PS5 的流量真的到本机了没有」。
"""
from __future__ import annotations

import socket
import struct
import threading
import time
from collections import deque

QTYPE_A = 1
QTYPE_AAAA = 28

# 绝对不能劫持的域名：动了 PS5 就连不上索尼 / 开不了播
NEVER_HIJACK = (
    "api.twitch.tv", "ingest.twitch.tv",
    "playstation.net", "playstation.com", "sony.com",
    "sonyentertainmentnetwork.com",
    "bilibili.com", "bilivideo.com", "hdslb.com",
)

DEFAULT_UPSTREAM = ["223.5.5.5", "119.29.29.29"]


# ──────────────────────────────────────────────────────────────
# 报文解析
# ──────────────────────────────────────────────────────────────
def parse_qname(data: bytes, offset: int):
    """解析域名，返回 (小写域名, 名字之后的位置)。支持压缩指针。"""
    labels = []
    pos = offset
    next_pos = None
    jumps = 0
    total = len(data)
    while True:
        if pos >= total:
            raise ValueError("域名越界")
        length = data[pos]
        if length == 0:
            pos += 1
            if next_pos is None:
                next_pos = pos
            break
        if length & 0xC0 == 0xC0:
            if pos + 1 >= total:
                raise ValueError("指针越界")
            if next_pos is None:
                next_pos = pos + 2
            pointer = ((length & 0x3F) << 8) | data[pos + 1]
            pos = pointer
            jumps += 1
            if jumps > 16:
                raise ValueError("压缩指针循环")
            continue
        labels.append(data[pos + 1:pos + 1 + length])
        pos += 1 + length
    name = b".".join(labels).decode("ascii", "replace").lower()
    return name, (next_pos if next_pos is not None else pos)


def parse_question(data: bytes):
    if len(data) < 12:
        raise ValueError("报文过短")
    qdcount = struct.unpack("!H", data[4:6])[0]
    if qdcount < 1:
        raise ValueError("没有 question")
    qname, pos = parse_qname(data, 12)
    if pos + 4 > len(data):
        raise ValueError("question 越界")
    qtype, qclass = struct.unpack("!HH", data[pos:pos + 4])
    return qname, qtype, qclass, pos + 4


def build_query(domain: str, qtype: int = QTYPE_A, qid: int = 0x4C42) -> bytes:
    header = struct.pack("!HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    qname = b"".join(bytes([len(p)]) + p.encode("ascii")
                     for p in domain.split(".") if p) + b"\x00"
    return header + qname + struct.pack("!HH", qtype, 1)


def parse_answers(data: bytes):
    """
    取出应答里的 A 记录与 CNAME 目标。

    为什么单独处理 CNAME：contribute.live-video.net 这类域名正常应答是
    「只有 CNAME、A 记录在同一条应答的后面或者干脆没有」，
    只读 A 会得到「无 A 记录」，看起来像解析失败，实际只是别名。
    """
    ips = []
    cnames = []
    if len(data) < 12:
        return ips, cnames
    try:
        qd = struct.unpack("!H", data[4:6])[0]
        an = struct.unpack("!H", data[6:8])[0]
    except struct.error:
        return ips, cnames
    pos = 12
    try:
        for _ in range(qd):
            _, pos = parse_qname(data, pos)
            pos += 4
    except Exception:
        return ips, cnames
    for _ in range(an):
        try:
            if data[pos] & 0xC0 == 0xC0:
                pos += 2
            else:
                _, pos = parse_qname(data, pos)
            if pos + 10 > len(data):
                break
            rtype, _rc, _ttl, rdlen = struct.unpack("!HHIH", data[pos:pos + 10])
            pos += 10
            rdata = data[pos:pos + rdlen]
            pos += rdlen
            if rtype == QTYPE_A and rdlen == 4:
                ips.append(socket.inet_ntoa(rdata))
            elif rtype == 5:  # CNAME
                try:
                    name, _ = parse_qname(data, pos - rdlen)
                    if name:
                        cnames.append(name)
                except Exception:
                    pass
        except Exception:
            break
    return ips, cnames


def parse_a_answers(data: bytes) -> list:
    return parse_answers(data)[0]


def build_hijack_response(query: bytes, ip: str) -> bytes:
    qid = query[:2]
    _qname, qtype, _qclass, qend = parse_question(query)
    question = query[12:qend]
    rd = query[2] & 0x01
    flags = 0x8000 | 0x0400 | 0x0080 | (rd << 8)
    answer = b""
    ancount = 0
    if qtype == QTYPE_A:
        answer = (b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 5, 4) + socket.inet_aton(ip))
        ancount = 1
    return qid + struct.pack("!HHHHH", flags, 1, ancount, 0, 0) + question + answer


def build_servfail(query: bytes) -> bytes:
    qid = query[:2]
    rd = query[2] & 0x01
    flags = 0x8000 | 0x0080 | 0x0002 | (rd << 8)
    return qid + struct.pack("!HHHHH", flags, 0, 0, 0, 0)


# ──────────────────────────────────────────────────────────────
# 对外查询工具（检测、诊断都用它）
# ──────────────────────────────────────────────────────────────
def query(server: str, domain: str, qtype: int = QTYPE_A,
          timeout: float = 2.5, port: int = 53, follow_cname: bool = True):
    """
    直接向指定 DNS 发一次真实查询（绕过系统解析器）。
    CNAME 会自动跟一层（推流域名普遍是 CNAME）。
    返回 (ok, ips 或 错误说明)。
    """
    pkt = build_query(domain, qtype)
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        s.sendto(pkt, (server, port))
        resp, _ = s.recvfrom(4096)
    except Exception as e:
        return False, "无响应: %s" % e
    finally:
        try:
            if s:
                s.close()
        except Exception:
            pass

    if len(resp) < 12:
        return False, "应答过短"
    rcode = resp[3] & 0x0F
    if rcode == 3:
        return False, "NXDOMAIN"
    if rcode == 2:
        return False, "SERVFAIL"
    ips, cnames = parse_answers(resp)
    if not ips and cnames and follow_cname:
        return query(server, cnames[0], qtype, timeout, port, follow_cname=False)
    if not ips:
        return False, "无 A 记录(rcode=%d)" % rcode
    return True, ips


# ──────────────────────────────────────────────────────────────
# 统计
# ──────────────────────────────────────────────────────────────
class Stats(object):
    def __init__(self):
        self._lock = threading.Lock()
        self.total = 0
        self.hijacked = 0
        self.forwarded = 0
        self.failed = 0
        self.started_at = time.time()
        self.clients = {}
        self.recent = deque(maxlen=80)
        self.hits = {}

    def record(self, client_ip, qname, hit, ok=True):
        with self._lock:
            self.total += 1
            if hit:
                self.hijacked += 1
                self.hits[qname] = self.hits.get(qname, 0) + 1
            else:
                self.forwarded += 1
            if not ok:
                self.failed += 1
            node = self.clients.setdefault(
                client_ip, {"ip": client_ip, "queries": 0, "hits": 0, "last": 0.0})
            node["queries"] += 1
            node["hits"] += 1 if hit else 0
            node["last"] = time.time()
            self.recent.appendleft({"t": time.time(), "ip": client_ip,
                                    "q": qname, "hit": bool(hit)})

    def snapshot(self):
        with self._lock:
            now = time.time()
            clients = []
            for node in sorted(self.clients.values(), key=lambda n: -n["queries"]):
                clients.append({
                    "ip": node["ip"], "queries": node["queries"],
                    "hits": node["hits"], "idle": round(now - node["last"], 1),
                })
            return {
                "total": self.total, "hijacked": self.hijacked,
                "forwarded": self.forwarded, "failed": self.failed,
                "uptime": round(now - self.started_at, 1),
                "clients": clients[:12],
                "recent": list(self.recent)[:15],
                "hits": dict(sorted(self.hits.items(), key=lambda kv: -kv[1])[:10]),
            }

    def client_hit_from_other(self, local_ips) -> bool:
        """有没有「非本机」地址来查过劫持域名 —— 这是 PS5 真的连上的铁证。"""
        with self._lock:
            for node in self.clients.values():
                if node["hits"] > 0 and node["ip"] not in local_ips \
                        and not node["ip"].startswith("127."):
                    return True
        return False


# ──────────────────────────────────────────────────────────────
# 服务器
# ──────────────────────────────────────────────────────────────
class DnsServer(object):
    def __init__(self, answer_ip, domains=None, upstreams=None, port=53, verbose=False):
        self.answer_ip = answer_ip
        self.domains = set(d.lower().strip(".") for d in (domains or []))
        self._upstreams = list(upstreams or DEFAULT_UPSTREAM)
        self.port = int(port)
        self.verbose = verbose
        self.stats = Stats()
        self.running = False
        self.last_error = ""
        self.bind_host = ""
        self._udp = None
        self._tcp = None
        self._threads = []
        self._up_lock = threading.Lock()

    # ---------- 上游 ----------
    @property
    def upstreams(self):
        with self._up_lock:
            return list(self._upstreams)

    def set_upstreams(self, servers):
        clean = [s for s in (servers or []) if s]
        if not clean:
            return False
        with self._up_lock:
            self._upstreams = clean
        return True

    # ---------- 匹配 ----------
    def is_hijacked(self, qname: str) -> bool:
        qname = qname.rstrip(".").lower()
        for d in NEVER_HIJACK:
            if qname == d or qname.endswith("." + d):
                return False
        if qname in self.domains:
            return True
        for d in self.domains:
            if qname.endswith("." + d):
                return True
        return False

    # ---------- 转发 ----------
    def _forward(self, query_bytes, use_tcp=False):
        for upstream in self.upstreams:
            try:
                if use_tcp:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(3.0)
                    s.connect((upstream, 53))
                    s.sendall(struct.pack("!H", len(query_bytes)) + query_bytes)
                    head = self._recv_exact(s, 2)
                    if not head:
                        s.close()
                        continue
                    want = struct.unpack("!H", head)[0]
                    body = self._recv_exact(s, want)
                    s.close()
                    if body:
                        return body
                else:
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.settimeout(2.0)
                    s.sendto(query_bytes, (upstream, 53))
                    resp, _ = s.recvfrom(4096)
                    s.close()
                    return resp
            except Exception:
                try:
                    s.close()
                except Exception:
                    pass
                continue
        return None

    @staticmethod
    def _recv_exact(sock, count):
        buf = b""
        while len(buf) < count:
            chunk = sock.recv(count - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def resolve(self, data, client_ip, use_tcp=False):
        try:
            qname, _qtype, _qclass, _end = parse_question(data)
        except Exception:
            return None

        if self.is_hijacked(qname):
            self.stats.record(client_ip, qname, True)
            if self.verbose:
                print("  [DNS] 劫持 %-46s <- %s" % (qname, client_ip), flush=True)
            return build_hijack_response(data, self.answer_ip)

        resp = self._forward(data, use_tcp=use_tcp)
        if resp:
            self.stats.record(client_ip, qname, False)
            return resp

        self.stats.record(client_ip, qname, False, ok=False)
        return build_servfail(data)

    # ---------- 监听循环 ----------
    def _serve_udp(self):
        while self.running:
            try:
                data, addr = self._udp.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            except Exception:
                continue
            if not self.running:
                break
            try:
                resp = self.resolve(data, addr[0], use_tcp=False)
                if resp:
                    self._udp.sendto(resp, addr)
            except Exception:
                pass

    def _serve_tcp(self):
        while self.running:
            try:
                conn, addr = self._tcp.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            except Exception:
                continue
            threading.Thread(target=self._handle_tcp, args=(conn, addr), daemon=True).start()

    def _handle_tcp(self, conn, addr):
        try:
            conn.settimeout(5)
            head = self._recv_exact(conn, 2)
            if not head:
                return
            want = struct.unpack("!H", head)[0]
            data = self._recv_exact(conn, want)
            if not data:
                return
            resp = self.resolve(data, addr[0], use_tcp=True)
            if resp:
                conn.sendall(struct.pack("!H", len(resp)) + resp)
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ---------- 启停 ----------
    def bind(self) -> bool:
        """
        先试 0.0.0.0；失败再试只绑定本机 IP。

        为什么要退化：装了加速器时，加速器会建虚拟网卡并在那上面占 53，
        绑 0.0.0.0 会失败，但绑具体网卡 IP 往往还能成。
        """
        hosts = ["0.0.0.0"]
        if self.answer_ip and self.answer_ip != "0.0.0.0":
            hosts.append(self.answer_ip)

        last_err = ""
        for host in hosts:
            udp = None
            try:
                udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                udp.bind((host, self.port))
                udp.settimeout(1.0)
            except OSError as e:
                last_err = "UDP %s:%d 绑定失败: %s" % (host, self.port, e)
                try:
                    if udp:
                        udp.close()
                except Exception:
                    pass
                continue

            tcp = None
            try:
                tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                tcp.bind((host, self.port))
                tcp.listen(64)
                tcp.settimeout(1.0)
            except OSError:
                try:
                    if tcp:
                        tcp.close()
                except Exception:
                    pass
                tcp = None

            self.bind_host = host
            self._udp = udp
            self._tcp = tcp
            return True

        self.last_error = last_err
        return False

    def start(self) -> bool:
        if self.running:
            return True
        if not self.bind():
            return False
        self.running = True
        self._threads = [threading.Thread(target=self._serve_udp, daemon=True, name="dns-udp")]
        if self._tcp is not None:
            self._threads.append(
                threading.Thread(target=self._serve_tcp, daemon=True, name="dns-tcp"))
        for t in self._threads:
            t.start()
        return True

    def stop(self):
        self.running = False
        for sock in (self._udp, self._tcp):
            try:
                if sock:
                    sock.close()
            except Exception:
                pass
        self._udp = None
        self._tcp = None

    def selftest(self):
        """向 127.0.0.1 发一次 A 查询，校验是否返回本机 IP。"""
        domain = sorted(self.domains)[0] if self.domains else "irc.twitch.tv"
        ok, res = query("127.0.0.1", domain, port=self.port)
        if not ok:
            return False, "查询无响应（%s）" % res
        if self.answer_ip in res:
            return True, "%s -> %s" % (domain, self.answer_ip)
        return False, "应答 IP 不符：期望 %s 实际 %s" % (self.answer_ip, ",".join(res))
