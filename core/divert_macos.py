from __future__ import annotations

import os
import subprocess
import threading
import time

from . import logbus, paths

ANCHOR = "com.ps5livebridge"
PF_CONF_TAG = "PS5LiveBridge"


def _run_pfctl(args: list, input_str: str = None) -> tuple:
    """跑 pfctl，返回 (returncode, stdout+stderr)。"""
    cmd = ["/sbin/pfctl"] + args
    try:
        r = subprocess.run(cmd, input=input_str.encode() if input_str else None,
                           capture_output=True, timeout=10)
        out = (r.stdout + r.stderr).decode("utf-8", errors="replace").strip()
        return r.returncode, out
    except FileNotFoundError:
        return -1, "找不到 pfctl（/sbin/pfctl）"
    except subprocess.TimeoutExpired:
        return -1, "pfctl 超时"
    except Exception as e:
        return -1, str(e)[:120]


def _local_ips() -> set:
    """取本机所有 IPv4 地址。"""
    ips = set()
    try:
        import socket
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
        ips.add("127.0.0.1")
    except Exception:
        pass
    return ips


class PfPortInterceptor(object):
    """macOS 版端口劫持：pfctl rdr 规则。"""

    def __init__(self, ports=(1935, 6667), target_ip: str = "",
                 verbose: bool = False):
        self.ports = tuple(int(p) for p in ports)
        self.target_ip = target_ip
        self.verbose = verbose
        self.running = False
        self._rules_loaded = False
        self._exclude: list = []
        self._lock = threading.Lock()
        self.counts = {p: 0 for p in self.ports}

    def build_rules(self) -> str:
        """生成 pfctl 规则文本。
        两种规则：
        1. rdr on <if> —— 拦入站流量（PS5 直连本机时）
        2. pass out route-to lo0 —— 拦出站流量（UU 加速器发出的 RTMP）
        """
        iface = "en0"
        try:
            import subprocess
            r = subprocess.run(["ifconfig"], capture_output=True, timeout=3)
            text = r.stdout.decode()
            for line in text.splitlines():
                if "inet " + self.target_ip in line:
                    lines = text.splitlines()
                    for j, l2 in enumerate(lines):
                        if "inet " + self.target_ip in l2:
                            for k in range(j-1, -1, -1):
                                if ":" in lines[k] and not lines[k].startswith("\t"):
                                    iface = lines[k].split(":")[0]
                                    break
                            break
                    break
        except Exception:
            pass

        lines = []
        for port in self.ports:
            lines.append(
                "rdr on lo0 proto tcp from any to 127.0.0.1 port %d -> %s port %d"
                % (port, self.target_ip, port))
            lines.append(
                "rdr on %s proto tcp from any to any port %d -> %s port %d"
                % (iface, port, self.target_ip, port))
            lines.append(
                "nat on %s proto tcp from any to any port %d -> %s port %d"
                % (iface, port, self.target_ip, port))
        return "\n".join(lines) + "\n"

    def set_exclude(self, ips, replace: bool = False) -> list:
        """设置排除白名单（这些 IP 的流量不被劫持）。"""
        with self._lock:
            if replace:
                self._exclude = list(ips)
            else:
                for ip in ips:
                    if ip not in self._exclude:
                        self._exclude.append(ip)
            if self._rules_loaded:
                self._reload_rules()
        return self._exclude

    def get_exclude(self) -> list:
        return list(self._exclude)

    def _reload_rules(self):
        """重新加载 pf 规则。"""
        rules = self.build_rules()
        _run_pfctl(["-f", "-"], input_str=rules)
        self._log("规则已更新")

    def _log(self, msg: str):
        if self.verbose:
            logbus.info(msg, tag="劫持")

    def available(self) -> bool:
        """检查 pfctl 是否可用且有权限。"""
        if not os.path.exists("/sbin/pfctl"):
            return False
        rc, out = _run_pfctl(["-s", "info"])
        return rc == 0

    def start(self):
        """加载 pf 规则，开始劫持。"""
        if self.running:
            return True, ""
        if not self.target_ip:
            return False, "没指定目标 IP"
        if not self.available():
            return False, "pfctl 不可用（需要 root 权限）"

        rules = self.build_rules()
        rc, out = _run_pfctl(["-f", "-"], input_str=rules)
        if rc != 0:
            err = "pfctl 加载规则失败：%s" % out
            logbus.error(err, tag="劫持")
            return False, err

        self._rules_loaded = True
        self.running = True
        logbus.ok("端口劫持已启动（pfctl）：%s" % ", ".join(str(p) for p in self.ports),
                  tag="劫持")
        return True, ""

    def stop(self):
        """卸载 pf 规则。"""
        if not self._rules_loaded:
            self.running = False
            return
        _run_pfctl(["-f", "-"], input_str='anchor "%s"\n' % ANCHOR)
        self._rules_loaded = False
        self.running = False
        logbus.info("端口劫持已停止", tag="劫持")

    def snapshot(self) -> dict:
        return {
            "running": self.running,
            "ports": list(self.ports),
            "target": self.target_ip,
            "exclude": list(self._exclude),
            "counts": dict(self.counts),
            "backend": "pfctl",
        }


def _explain_open_error(exc) -> str:
    """把 pfctl 的错误翻译成人话。"""
    s = str(exc).lower()
    if "permission" in s or "operation not permitted" in s:
        return "需要管理员权限。请用 sudo 运行，或在系统设置里授权。"
    if "not found" in s:
        return "找不到 pfctl。这是 macOS 系统工具，不应该缺失。"
    return str(exc)[:200]


def pick_target_ip(lan_ip: str, accel_ip: str = "") -> str:
    """
    选劫持目标 IP。
    Windows 版会考虑加速器网段；macOS 版目前只用本机 IP。
    """
    return lan_ip


def pf_available() -> bool:
    """pfctl 是否可用。"""
    return os.path.exists("/sbin/pfctl")
