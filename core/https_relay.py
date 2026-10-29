from __future__ import annotations

import socket
import struct
import threading
import time

from . import logbus


def extract_sni(data: bytes) -> str:
    """
    从 TLS ClientHello 中提取 SNI（Server Name Indication）。
    返回域名字符串，失败返回空。
    """
    try:
        if len(data) < 5 or data[0] != 0x16:
            return ""
        if data[5] != 0x01:
            return ""
        pos = 5 + 1 + 3 + 2
        pos += 32
        if pos >= len(data):
            return ""
        session_len = data[pos]
        pos += 1 + session_len
        if pos + 2 > len(data):
            return ""
        cipher_len = struct.unpack("!H", data[pos:pos+2])[0]
        pos += 2 + cipher_len
        if pos >= len(data):
            return ""
        comp_len = data[pos]
        pos += 1 + comp_len
        if pos + 2 > len(data):
            return ""
        ext_total = struct.unpack("!H", data[pos:pos+2])[0]
        pos += 2
        ext_end = pos + ext_total
        while pos + 4 <= ext_end:
            ext_type = struct.unpack("!H", data[pos:pos+2])[0]
            ext_len = struct.unpack("!H", data[pos+2:pos+4])[0]
            if ext_type == 0x0000:
                sni_pos = pos + 4
                if sni_pos + 2 > len(data):
                    return ""
                sni_list_len = struct.unpack("!H", data[sni_pos:sni_pos+2])[0]
                sni_pos += 2
                if sni_pos + 3 > len(data):
                    return ""
                name_type = data[sni_pos]
                if name_type == 0:
                    name_len = struct.unpack("!H", data[sni_pos+1:sni_pos+3])[0]
                    return data[sni_pos+3:sni_pos+3+name_len].decode("ascii", errors="ignore")
            pos += 4 + ext_len
    except Exception:
        pass
    return ""


def read_tls_record(sock, timeout=6.0):
    """
    完整读一条 TLS 握手记录。

    为什么不能只 recv(4096) 一次：ClientHello 可能被拆成多个 TCP 段
    （TLS 1.3 带大扩展时尤其常见），读不全就解析不出 SNI，
    进而转到错误的服务器 —— PS5 那边报的就是 SSL_ERROR_SYSCALL。
    """
    sock.settimeout(timeout)
    buf = b""
    while True:
        if len(buf) >= 5:
            need = 5 + struct.unpack("!H", buf[3:5])[0]
            if len(buf) >= need:
                return buf[:need]
        try:
            chunk = sock.recv(8192)
        except Exception:
            return buf
        if not chunk:
            return buf
        buf += chunk
        if len(buf) > 65536:
            return buf


def resolve_real_ip(domain: str, upstream: str = "223.5.5.5") -> str:
    """
    通过上游 DNS 解析真实 IP（绕过本地劫持）。

    为什么要挨个试：加速器装在路由器上时，路由器 DNS 往往**不解析** Twitch 的
    推流域名（实测「没有 contribute.live-video.net 的 A 记录」）。这时退到
    公网 DNS 才有机会拿到地址；等加速器把本机也纳入加速，第一个上游就能答了。
    """
    import subprocess
    candidates = []
    for d in (upstream, "223.5.5.5", "119.29.29.29", "8.8.8.8"):
        if d and d not in candidates:
            candidates.append(d)

    import re as _re
    for dns in candidates:
        try:
            r = subprocess.run(["nslookup", domain, dns],
                               capture_output=True, timeout=4)
            text = r.stdout.decode(errors="ignore")
            ips = _re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text)
            for ip in ips:
                if ip == dns or ip.startswith(("127.", "0.", "255.")):
                    continue
                return ip
        except Exception:
            continue
    return ""


def parse_proxy(url: str):
    """http://host:port → (host, port)；解析不出返回 ("", 0)。"""
    u = (url or "").strip()
    for pfx in ("http://", "https://", "socks5://"):
        if u.startswith(pfx):
            u = u[len(pfx):]
            break
    u = u.split("/")[0]
    host, _, port = u.rpartition(":")
    if not host:
        return "", 0
    try:
        return host, int(port or 8080)
    except ValueError:
        return "", 0


class HttpsRelay:
    """443 端口 HTTPS 中继。"""

    def __init__(self, port: int = 443, upstream_dns: str = "223.5.5.5",
                 proxy: str = ""):
        self._port = port
        self._upstream_dns = upstream_dns
        self.proxy = (proxy or "").strip()
        self._running = False
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._connections = 0
        self._lock = threading.Lock()

    def start(self) -> bool:
        if self._running:
            return True
        try:
            self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server.bind(("0.0.0.0", self._port))
            self._server.listen(20)
            self._running = True
            self._thread = threading.Thread(target=self._accept_loop, daemon=True, name="https-relay")
            self._thread.start()
            logbus.ok("HTTPS 中继已启动（:%d）" % self._port, tag="中继")
            return True
        except Exception as e:
            logbus.error("HTTPS 中继启动失败：%s" % e, tag="中继")
            return False

    def stop(self) -> None:
        self._running = False
        if self._server:
            try:
                self._server.close()
            except Exception:
                pass
            self._server = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def connections(self) -> int:
        with self._lock:
            return self._connections

    def _accept_loop(self) -> None:
        while self._running and self._server:
            try:
                client, addr = self._server.accept()
            except OSError:
                break
            threading.Thread(target=self._handle_client, args=(client, addr),
                           daemon=True).start()

    def _connect_target(self, sni: str) -> socket.socket:
        """
        连到目标服务器的 443。

        有代理就走 HTTP CONNECT 隧道 —— 关键优势是「由代理去解析域名」，
        正好绕开 contribute.live-video.net 在公网 DNS 上没有 A 记录这个死结；
        没代理才自己解析 IP 直连（那条路在国内基本连不通 Twitch）。
        """
        if self.proxy:
            ph, pp = parse_proxy(self.proxy)
            if ph and pp:
                s = socket.create_connection((ph, pp), timeout=10)
                req = ("CONNECT %s:443 HTTP/1.1\r\nHost: %s:443\r\n\r\n"
                       % (sni, sni))
                s.sendall(req.encode())
                s.settimeout(10)
                buf = b""
                while b"\r\n\r\n" not in buf and len(buf) < 4096:
                    ch = s.recv(512)
                    if not ch:
                        break
                    buf += ch
                first = buf.split(b"\r\n", 1)[0] if buf else b""
                if b" 200" not in first:
                    try:
                        s.close()
                    except Exception:
                        pass
                    raise OSError("代理拒绝 CONNECT：%s" % first[:60].decode("latin-1"))
                return s

        ip = resolve_real_ip(sni, self._upstream_dns)
        if not ip:
            raise OSError("解析不到 %s 的 IP" % sni)
        return socket.create_connection((ip, 443), timeout=8)

    def _handle_client(self, client: socket.socket, addr) -> None:
        try:
            first_data = read_tls_record(client)
            if not first_data:
                client.close()
                return
            sni = extract_sni(first_data)
            try:
                from . import logbus
                logbus.debug("隧道 %s（%d 字节 ClientHello）"
                             % (sni or "无 SNI", len(first_data)), tag="中继")
            except Exception:
                pass
            if not sni:
                try:
                    from . import logbus
                    logbus.warn("读不出 SNI，放弃这条连接（%d 字节）" % len(first_data), tag="中继")
                except Exception:
                    pass
                client.close()
                return

            server = self._connect_target(sni)

            server.sendall(first_data)

            with self._lock:
                self._connections += 1

            self._pipe(client, server)
        except Exception as e:
            try:
                if sni and not any(x in sni for x in ("ttvnw.net", "twitchcdn.net")):
                    from . import logbus
                    logbus.warn("中继转发失败 %s：%s" % (sni, e), tag="中继")
            except Exception:
                pass
        finally:
            try:
                client.close()
            except Exception:
                pass

    def _pipe(self, a: socket.socket, b: socket.socket) -> None:
        """双向转发数据。"""
        def forward(src, dst):
            try:
                while True:
                    data = src.recv(8192)
                    if not data:
                        break
                    dst.sendall(data)
            except Exception:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except Exception:
                    pass

        t1 = threading.Thread(target=forward, args=(a, b), daemon=True)
        t2 = threading.Thread(target=forward, args=(b, a), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        try:
            a.close()
            b.close()
        except Exception:
            pass
