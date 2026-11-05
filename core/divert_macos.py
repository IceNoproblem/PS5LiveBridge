from __future__ import annotations

import os
import subprocess
import tempfile
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


def _apply_rules_as_admin(rules: str) -> tuple:
    """
    没 root 时：规则落盘，再用 osascript 弹系统密码框让 pfctl 加载。

    为什么不走 stdin：osascript 的 do shell script 不方便喂标准输入，
    写临时文件再读最稳。用户点取消时 osascript 返回 1。
    """
    tmp = os.path.join(tempfile.gettempdir(), "ps5livebridge-pf.conf")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(rules)
    except Exception as e:
        return -1, "写临时规则文件失败：%s" % e

    # -e 确保 pf 是启用的；已经启用时它会报 "pf already enabled"，忽略即可
    inner = "/sbin/pfctl -e 2>/dev/null; /sbin/pfctl -f %s" % tmp
    script = 'do shell script "%s" with administrator privileges' % inner
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, timeout=180)
    except subprocess.TimeoutExpired:
        return -1, "等待管理员授权超时"
    except Exception as e:
        return -1, "调起授权失败：%s" % e

    out = (r.stdout + r.stderr).decode("utf-8", errors="replace").strip()
    if r.returncode != 0:
        low = out.lower()
        if "cancel" in low or "-128" in low:
            return 1, "用户取消了授权"
        return r.returncode, out
    return 0, out


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
        self.started_at = 0.0
        self.last_error = ""

    def build_rules(self) -> str:
        """生成 pfctl 规则文本。

        只做 rdr，不做 nat，也不碰 lo0。

        这里原来还有两条，2026-11-04 在一个 M1 Pro + UU 主机加速的用户机
        器上实测确认都是错的：

        1. `nat on en0 proto tcp from any to any port 1935 -> <本机> port 1935`
           本意是兜住「加速器从本机用户态 socket 发出的 RTMP」，那是 Windows
           的思路。在 macOS 上它把出向 1935 的源地址改成 <本机>:1935，于是
           本机连自己 192.168.10.22:1935 会变成 <本机>:1935 -> <本机>:1935，
           同一个地址同一个端口自己发给自己，SYN 直接消失。表现为 `nc` 超时、
           PS5 推流无任何响应。清掉 pf 规则后 `nc -zv 192.168.10.22 1935`
           立刻 succeeded，差异非常干净。
        2. `rdr on lo0 proto tcp from any to 127.0.0.1 port 1935`
           会把本机 relay 连 127.0.0.1:1935 的请求绕到局域网地址上，平白多
           一跳，没有任何收益。

        PS5 的包是整包进 en0 的，一条 rdr 就够把连接截到本机。回程由 pf 的
        状态表自动还原源地址，PS5 看到的仍是它连的那个 Twitch IP。
        """
        iface = "en0"
        try:
            r = subprocess.run(["ifconfig"], capture_output=True, timeout=3)
            text = r.stdout.decode()
            lines_txt = text.splitlines()
            for j, l2 in enumerate(lines_txt):
                if "inet " + self.target_ip in l2:
                    for k in range(j - 1, -1, -1):
                        if ":" in lines_txt[k] and not lines_txt[k].startswith("\t"):
                            iface = lines_txt[k].split(":")[0]
                            break
                    break
        except Exception:
            pass

        out = []
        for port in self.ports:
            out.append(
                "rdr on %s proto tcp from any to any port %d -> %s port %d"
                % (iface, port, self.target_ip, port))
        return "\n".join(out) + "\n"

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
        """重新加载 pf 规则。同样可能要走一次授权。"""
        rules = self.build_rules()
        rc, out = _run_pfctl(["-f", "-"], input_str=rules)
        if rc != 0:
            rc, out = _apply_rules_as_admin(rules)
        if rc == 0:
            self._log("规则已更新")
        else:
            logbus.warn("规则更新失败：%s" % (out or "未知原因"), tag="劫持")

    def _log(self, msg: str):
        if self.verbose:
            logbus.info(msg, tag="劫持")

    def available(self) -> bool:
        """系统里有 pfctl 就算可用。

        权限不在这里判断：非 root 时 pfctl -s info 也会失败，
        但那个失败可以用系统密码框解决，不能当成「不支持」。
        """
        return os.path.exists("/sbin/pfctl")

    def start(self):
        """加载 pf 规则，开始劫持。没有 root 时会弹系统密码框。"""
        if self.running:
            return True, ""
        if not self.target_ip:
            return False, "没指定目标 IP"
        if not self.available():
            return False, "系统里找不到 pfctl，端口接管用不了"

        rules = self.build_rules()
        rc, out = _run_pfctl(["-f", "-"], input_str=rules)
        if rc != 0:
            # 多半是没 root。交给系统授权框，不用让用户去开终端
            logbus.step("端口接管需要管理员权限，正在请求授权…", tag="劫持")
            rc, out = _apply_rules_as_admin(rules)
            if rc != 0:
                err = out or "授权失败"
                if "取消" in err:
                    logbus.warn("已取消授权，端口接管没开", tag="劫持")
                else:
                    logbus.error("pfctl 加载规则失败：%s" % err, tag="劫持")
                return False, err

        self._rules_loaded = True
        self.running = True
        self.started_at = time.time()
        self.last_error = ""
        logbus.ok("端口劫持已启动（pfctl）：%s" % ", ".join(str(p) for p in self.ports),
                  tag="劫持")
        return True, ""

    def stop(self):
        """卸载 pf 规则，恢复系统默认规则集。

        以前这里往 pfctl 喂 `anchor "com.ps5livebridge"` 就算卸载了，实际上
        我们从来没往那个 anchor 里装过东西（规则是直接 -f 灌进主规则集的），
        而且非 root 下这条命令本身就执行不了。结果规则一直留在表里，日志却
        写「端口劫持已停止」。

        现在改成重新加载 /etc/pf.conf，把我们灌进去的规则整体换掉。非 root
        会失败，失败就如实说，不再假装成功。
        """
        if not self._rules_loaded:
            self.running = False
            return
        rc, out = _run_pfctl(["-f", "/etc/pf.conf"])
        self._rules_loaded = False
        self.running = False
        self.started_at = 0.0
        if rc == 0:
            logbus.info("端口劫持已停止，pf 规则已恢复系统默认", tag="劫持")
        else:
            self.last_error = "pf 规则没清掉（需要 root）：%s" % (out or "未知原因")
            logbus.warn(
                "端口劫持已停止，但 pf 规则还在表里，要 root 才能清。"
                "如果之后 PS5 连不上，执行一次 sudo pfctl -f /etc/pf.conf",
                tag="劫持")

    def _conn_counts(self) -> dict:
        """数一下 1935/6667 上现在有几条 ESTABLISHED。

        pf 自己的每条规则计数只有 root 能读（pfctl -vs nat），程序常年非 root，
        读不到。所以改用 netstat 数已建立的连接，一样能说明「有没有人连进来」，
        而且不需要任何权限。

        这只反映「当前活着的连接」，不是累计次数。推流期间 PS5 的连接会一直挂着，
        所以够用；推完就断，界面在那之后会回落成 0，这是预期的。
        """
        counts = {p: 0 for p in self.ports}
        try:
            r = subprocess.run(["/usr/sbin/netstat", "-an", "-p", "tcp"],
                               capture_output=True, timeout=3)
            text = r.stdout.decode("utf-8", errors="replace")
        except Exception:
            return counts
        for line in text.splitlines():
            if "ESTABLISHED" not in line:
                continue
            for p in self.ports:
                # netstat 的本地地址写法是 192.168.10.22.1935
                if ("." + str(p) + " ") in line:
                    counts[p] += 1
        return counts

    def snapshot(self) -> dict:
        """结构和 Windows 版（core/divert.py）严格对齐。

        以前这里 ports 返回的是 [1935, 6667] 这种 list，而上层
        orchestrator.hijack_state() 是按 {str(port): {"redirected": n}} 读的，
        list.get() 直接抛 AttributeError，被 except 吞掉之后 ps5_seen 恒为 false，
        连下面看 live.ps5.publishing 的兜底都执行不到。界面上那句「PS5 流量
        还没收到」因此永远不变。
        """
        now = time.time()
        conns = self._conn_counts()
        ports = {}
        total = 0
        for p in self.ports:
            n = conns.get(p, 0)
            total += n
            ports[str(p)] = {
                "redirected": n,
                "idle": -1,
                "dests": [],
            }
        return {
            "running": self.running,
            "available": self.available(),
            "target": self.target_ip,
            "ports": ports,
            "total": total,
            "active_conns": total,
            "uptime": round(now - self.started_at, 1) if self.started_at else 0,
            "exclude": list(self._exclude),
            "error": self.last_error,
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
