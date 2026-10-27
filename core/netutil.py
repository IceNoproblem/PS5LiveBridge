# -*- coding: utf-8 -*-
"""
core/netutil.py —— 本机网络环境（纯标准库）

回答四个问题：
    1. 本机该用哪个 IP 答复 PS5？（避开 VPN / 虚拟网卡 / 加速器 fake-ip 段）
    2. 加速器在不在跑？它的虚拟网卡 IP / DNS 是多少？
    3. 端口占没占？谁占的？
    4. Windows 防火墙放行了没有？（不放行 = PS5 永远连不上，最高频失败原因）

所有 Windows 调用都做了平台判断，非 Windows 会安全降级 ——
这样开发机（macOS）能跑全流程自测。
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import subprocess
import sys
import threading
import time

IS_WINDOWS = sys.platform == "win32"

# 虚拟网卡关键字：选答复 IP 时要减分
VIRTUAL_HINTS = (
    "vethernet", "vmware", "virtualbox", "hyper-v", "tap", "tun", "tailscale",
    "zerotier", "wireguard", "clash", "mihomo", "meta", "docker", "wsl",
    "loopback", "npcap", "radmin", "hamachi", "openvpn", "softether",
    "npcap loopback", "utun", "awdl", "llw", "bridge",
)

# 加速器虚拟网卡关键字：这些网卡上的 IP 才是 PS5 真正的网关
ACCEL_ADAPTER_HINTS = (
    "uu", "netease", "网易", "xunyou", "迅游", "leigod", "雷神", "biubiu",
    "qiyou", "奇游", "accel", "booster", "wtfast", "exitlag",
)

ACCEL_PROCESS_HINTS = (
    "uubooster", "uu.exe", "neteaseuu", "uu_booster", "uugamebooster",
    "xunyou", "xyacc", "leigod", "biubiu", "qiyou", "xyqy",
    "wtfast", "exitlag", "gameraccelerator",
)

# 加速器做 DNS 分流时的 fake-ip 段
FAKE_IP_PREFIXES = ("198.18.", "198.19.")

_NON_ROUTABLE_PREFIXES = ("127.", "169.254.", "0.", "255.", "224.", "239.")


# ──────────────────────────────────────────────────────────────
# 命令执行
# ──────────────────────────────────────────────────────────────
def run(cmd, timeout=8, env=None):
    """
    执行命令返回 (returncode, 文本)。

    中文 Windows 上 netsh / tasklist / PowerShell 往管道里写的是 GBK 字节。
    以前无脑按 UTF-8 解，网卡别名「以太网」会变成一串乱码，选 IP 直接选错。
    这里先严格 UTF-8，失败退系统 ANSI 代码页。
    """
    try:
        kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        if IS_WINDOWS:
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        p = subprocess.run(cmd, **kwargs)
        raw = p.stdout or b""
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            encoding = "mbcs" if IS_WINDOWS else "utf-8"
            try:
                text = raw.decode(encoding, "replace")
            except Exception:
                text = raw.decode("latin-1", "replace")
        return p.returncode, text
    except Exception as e:
        return -1, str(e)


def powershell(script, timeout=12):
    """跑一段 PowerShell。前置 OutputEncoding，否则输出会被转成 GBK。"""
    prologue = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                "$OutputEncoding=[Text.Encoding]::UTF8;")
    return run(["powershell", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command", prologue + script],
               timeout=timeout)


# ──────────────────────────────────────────────────────────────
# 权限
# ──────────────────────────────────────────────────────────────
def is_admin() -> bool:
    if not IS_WINDOWS:
        try:
            return os.geteuid() == 0
        except AttributeError:
            return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def elevate(args=None, exe=None) -> bool:
    """以管理员身份重启自己。成功返回 True（调用方须立即退出）。"""
    if not IS_WINDOWS:
        return False
    try:
        import ctypes
        exe = exe or sys.executable
        cmd = subprocess.list2cmdline([str(a) for a in (args or [])])
        ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, cmd, None, 1)
        return ret > 32
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────
# 子进程生命周期（Windows Job 对象）
# ──────────────────────────────────────────────────────────────
def add_kill_on_close_job() -> bool:
    """
    把自己放进一个「关闭即杀」的 Job 对象：本进程无论怎么死（含任务管理器强杀、
    崩溃、拔电），nginx / ffmpeg 这些子进程都会跟着被系统收掉。

    为什么必须有：实测在 Win11 上强杀主程序后，nginx 会留下 2 个进程死占 1935，
    重启软件虽然能自动清理，但那只是第二手补救 —— 用户看到的是「端口被占用」；
    而且积攒下来就是一桌面僵尸（接手这台机器时就有 7 个）。

    非 Windows 直接返回 False，作业控制交给 systemd / launchd。
    """
    if not IS_WINDOWS:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                        ("WriteOperationCount", ctypes.c_ulonglong),
                        ("OtherOperationCount", ctypes.c_ulonglong),
                        ("ReadTransferCount", ctypes.c_ulonglong),
                        ("WriteTransferCount", ctypes.c_ulonglong),
                        ("OtherTransferCount", ctypes.c_ulonglong)]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                        ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        extended_limit_information = 9

        # ⚠️ 必须显式声明原型：ctypes 默认按 32 位 int 处理返回值，
        # 在 x64 上 HANDLE 是被截断的，AssignProcessToJobObject 会返回
        # 0 且 GetLastError=6（句柄无效）。这个坑只在真机上才看得见。
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetCurrentProcess.argtypes = []

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return False
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            job, extended_limit_information, ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            return False
        # 自己已在别人的 Job 里时（SSH 会话就是），Win8+ 允许嵌套，失败也不致命
        if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
            return False
        globals()["_JOB_HANDLE"] = job      # 持有句柄，进程退出时关闭即触发清理
        return True
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────
# 降权启动（浏览器必须用普通权限开）
# ──────────────────────────────────────────────────────────────
class _WinProc(object):
    """CreateProcessWithTokenW 拿到的是句柄不是 Popen，这里补一个 poll()。"""

    def __init__(self, pid, handle):
        self.pid = pid
        self._handle = handle

    def poll(self):
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(self._handle, ctypes.byref(code)):
                return 0
            STILL_ACTIVE = 259
            return None if code.value == STILL_ACTIVE else int(code.value)
        except Exception:
            return 0


def run_unelevated(args, cwd=None):
    """
    从管理员进程里以「普通用户身份」启动程序，返回带 poll() 的对象。

    为什么必须有：本软件要提权（防火墙、WinDivert 驱动），但 Edge / Chrome
    以管理员身份启动时有自我保护——进程起来一两秒就自动退出，界面啥都没显示。
    表现就是「启动后闪退」，而轨迹里一切正常（窗口那一步之后一秒正常退出）。

    做法：拿当前进程的 LinkedToken（同一个用户的未提权令牌），用
    CreateProcessWithTokenW 以它启动。失败就退回普通 Popen。
    """
    args = [str(a) for a in args]
    if not IS_WINDOWS:
        try:
            return subprocess.Popen(args, cwd=cwd)
        except Exception:
            return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

        TOKEN_QUERY = 0x0008
        TOKEN_DUPLICATE = 0x0002
        TokenLinkedToken = 19
        LOGON_WITH_PROFILE = 1

        class STARTUPINFOW(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
                ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
                ("hStdError", wintypes.HANDLE),
            ]

        class PROCESS_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD),
            ]

        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                              ctypes.POINTER(wintypes.HANDLE)]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD)]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.CreateProcessWithTokenW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPWSTR,
            wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
            ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION)]
        advapi32.CreateProcessWithTokenW.restype = wintypes.BOOL

        h_tok = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
                kernel32.GetCurrentProcess(), TOKEN_QUERY | TOKEN_DUPLICATE,
                ctypes.byref(h_tok)):
            raise OSError("OpenProcessToken failed")
        try:
            linked = wintypes.HANDLE()
            n = wintypes.DWORD()
            if not advapi32.GetTokenInformation(
                    h_tok, TokenLinkedToken, ctypes.byref(linked),
                    ctypes.sizeof(linked), ctypes.byref(n)):
                raise OSError("no linked token")
        finally:
            kernel32.CloseHandle(h_tok)

        si = STARTUPINFOW()
        si.cb = ctypes.sizeof(STARTUPINFOW)
        pi = PROCESS_INFORMATION()
        ok = advapi32.CreateProcessWithTokenW(
            linked, LOGON_WITH_PROFILE, args[0],
            subprocess.list2cmdline(args), 0, None, cwd,
            ctypes.byref(si), ctypes.byref(pi))
        kernel32.CloseHandle(linked)
        if not ok:
            raise OSError("CreateProcessWithTokenW failed: %d" % ctypes.get_last_error())
        kernel32.CloseHandle(pi.hThread)
        return _WinProc(pi.dwProcessId, pi.hProcess)
    except Exception:
        try:
            return subprocess.Popen(args, cwd=cwd)
        except Exception:
            return None


# ──────────────────────────────────────────────────────────────
# IP
# ──────────────────────────────────────────────────────────────
# ──────────────────────────────────────────────────────────────
# 结果缓存
#
# 为什么必须有：这些探测每项都要起一次 PowerShell（冷启动 1~3 秒），
# 而界面每 1.5 秒就要一次快照。实测不缓存时 /api/state 要 35 秒才返回，
# 界面看起来就是「卡死」。网关、网卡、DNS 这类信息几十秒内不会变，缓存完全够用。
# ──────────────────────────────────────────────────────────────
_MEM = {}
_MEM_LOCK = threading.Lock()


def _cached(key: str, ttl: float, fn):
    now = time.time()
    with _MEM_LOCK:
        hit = _MEM.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _MEM_LOCK:
        _MEM[key] = (now, val)
    return val


def clear_cache(prefix: str = "") -> None:
    """清缓存。注意清 adapter/gateway 会退化成每次查 PowerShell。"""
    with _MEM_LOCK:
        for k in list(_MEM):
            if k.startswith(prefix):
                _MEM.pop(k, None)


def _is_private_ipv4(ip: str) -> bool:
    if not ip or not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip):
        return False
    if any(ip.startswith(p) for p in _NON_ROUTABLE_PREFIXES):
        return False
    if any(ip.startswith(p) for p in FAKE_IP_PREFIXES):
        return False
    try:
        return ipaddress.IPv4Address(ip).is_private
    except Exception:
        return False


def socket_lan_ip() -> str:
    """用 UDP 路由技巧拿默认出口 IP（不发包）。"""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1.0)
        s.connect(("223.5.5.5", 53))
        return s.getsockname()[0]
    except Exception:
        return ""
    finally:
        try:
            if s:
                s.close()
        except Exception:
            pass


def list_adapters() -> list:
    """带 30 秒缓存（派发到 _list_adapters）。"""
    return _cached("adapters", 30, _list_adapters)


def _list_adapters() -> list:
    """
    返回 [{alias, ip, gateway, score}]，按可信度降序。

    score：真实有线/无线网卡高分；虚拟网卡重罚；加速器网卡单列标记。
    """
    items = []
    if IS_WINDOWS:
        rc, out = powershell(
            "Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
            "Where-Object { $_.IPAddress -ne '127.0.0.1' } | "
            "ForEach-Object { $_.InterfaceAlias + '|' + $_.IPAddress }"
        )
        if rc == 0:
            for line in out.splitlines():
                line = line.strip()
                if "|" not in line:
                    continue
                alias, ip = line.rsplit("|", 1)
                items.append({"alias": alias.strip(), "ip": ip.strip()})
    else:
        rc, out = run(["ifconfig"], timeout=5)
        if "inet " not in out and "inet addr" not in out:
            # 精简镜像里没有 net-tools，退到 iproute2；两者格式不同，下面的
            # 正则同时兼容（ifconfig: "eth0: ..." / ip: "2: eth0: <...>"）
            rc, out = run(["ip", "-4", "addr"], timeout=5)
        cur = ""
        for line in out.splitlines():
            m = re.match(r"^\s*(?:\d+:\s*)?([A-Za-z][\w.@-]*):", line)
            if m:
                cur = m.group(1)
                continue
            m2 = re.search(r"inet\s+(?:addr:)?(\d+\.\d+\.\d+\.\d+)", line)
            if m2:
                items.append({"alias": cur, "ip": m2.group(1)})

    gateways = get_gateways()
    result = []
    for it in items:
        ip = it["ip"]
        if not _is_private_ipv4(ip):
            continue
        alias = it["alias"]
        low = alias.lower()
        score = 0
        is_accel = any(h in low for h in ACCEL_ADAPTER_HINTS)
        if is_accel:
            score -= 40          # 加速器网卡可以当网关，但不该拿来答复 DNS
        if any(h in low for h in VIRTUAL_HINTS) and not is_accel:
            score -= 100
        if any(h in low for h in ("以太网", "ethernet", "wlan", "wi-fi", "无线", "eth")):
            score += 50
        if ip.startswith("192.168."):
            score += 20
        elif ip.startswith("10."):
            score += 10
        result.append({
            "alias": alias,
            "ip": ip,
            "gateway": gateways.get(ip, ""),
            "accel": is_accel,
            "score": score,
        })

    if not result:
        ip = socket_lan_ip()
        if ip:
            result.append({"alias": "auto", "ip": ip, "gateway": "", "accel": False, "score": 0})

    result.sort(key=lambda x: -x["score"])
    return result


def get_lan_ip() -> str:
    """答复 PS5 用的本机 IP。没有可用网卡时返回空串。"""
    for it in list_adapters():
        if not it["accel"]:
            return it["ip"]
    items = list_adapters()
    return items[0]["ip"] if items else socket_lan_ip()


def get_gateways() -> dict:
    """带 60 秒缓存。网关不会变。"""
    return _cached("gateways", 60, _get_gateways)


def _get_gateways() -> dict:
    """
    {"本机IP": 默认网关}

    为什么不用 Get-NetIPConfiguration：那条命令会去查 DHCP 租约、协议绑定等一堆东西，
    在虚拟机里实测要 10~30 秒。改成 Get-NetIPAddress + Get-NetRoute 两条轻量命令，
    一条 PowerShell 内完成关联，耗时降到 1 秒级。
    """
    mapping = {}
    if IS_WINDOWS:
        rc, out = powershell(
            "$ips=@{}; "
            "Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
            "ForEach-Object { $ips[$_.InterfaceAlias] = $_.IPAddress }; "
            "Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | "
            "ForEach-Object { $ips[$_.InterfaceAlias] + '|' + $_.NextHop }"
        )
        if rc == 0:
            for line in out.splitlines():
                line = line.strip()
                if "|" not in line:
                    continue
                ip, gw = line.rsplit("|", 1)
                ip, gw = ip.strip(), gw.strip()
                if ip and re.match(r"^\d+\.\d+\.\d+\.\d+$", gw):
                    mapping[ip] = gw
    else:
        rc, out = run(["netstat", "-rn"], timeout=5)
        gws = re.findall(r"default\s+(\d+\.\d+\.\d+\.\d+)", out)
        ip = socket_lan_ip()
        if gws and ip:
            mapping[ip] = gws[0]
    return mapping


def default_gateway() -> str:
    """当前默认网关（PS5 视角的「路由器」通常就是它）。"""
    gws = list(get_gateways().values())
    return gws[0] if gws else ""


def accelerator_gateway_ip() -> str:
    """加速器虚拟网卡上的 IP —— PS5 被加速时的真实网关。"""
    for it in list_adapters():
        if it["accel"]:
            return it["ip"]
    return ""


def accelerator_dns() -> list:
    """带 10 秒缓存（加速器可能刚启动，不能缓存太久）。"""
    return _cached("acceldns", 10, _accelerator_dns)


def _accelerator_dns() -> list:
    """
    探测加速器下发给客户端的 DNS（通常是 198.18.x.x）。

    这是内置 DNS 模式最关键的一环：我们接管了 53，必须把非劫持域名
    转发给「加速器 DNS」，否则 PS5 握手阶段会拿到公网 IP、绕过加速链路，
    表现为「无法连接索尼服务器 / 开播失败」。
    """
    found = []
    if IS_WINDOWS:
        rc, out = powershell(
            "Get-DnsClientServerAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
            "ForEach-Object { $_.InterfaceAlias + '|' + ($_.ServerAddresses -join ',') }"
        )
        if rc == 0:
            for line in out.splitlines():
                line = line.strip()
                if "|" not in line:
                    continue
                alias, servers = line.rsplit("|", 1)
                low = alias.lower()
                if not any(h in low for h in ACCEL_ADAPTER_HINTS + ("tun", "tap", "meta")):
                    continue
                for s in servers.split(","):
                    s = s.strip()
                    if re.match(r"^\d+\.\d+\.\d+\.\d+$", s) and s not in found:
                        found.append(s)
    # 从本机网卡 DNS 里捞 fake-ip 段（有些加速器改的是主网卡 DNS）
    for s in system_dns():
        if any(s.startswith(p) for p in FAKE_IP_PREFIXES) and s not in found:
            found.append(s)
    return found


def system_dns() -> list:
    """带 30 秒缓存。"""
    return _cached("sysdns", 30, _system_dns)


def _system_dns() -> list:
    """系统当前 DNS（跳过回环与本机地址，避免自环）。"""
    servers = []
    if IS_WINDOWS:
        rc, out = powershell(
            "Get-DnsClientServerAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
            "ForEach-Object { $_.ServerAddresses }"
        )
        if rc == 0:
            for line in out.splitlines():
                line = line.strip()
                if re.match(r"^\d+\.\d+\.\d+\.\d+$", line):
                    servers.append(line)
    else:
        try:
            with open("/etc/resolv.conf", "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if line.startswith("nameserver"):
                        servers.append(line.split()[1])
        except Exception:
            pass

    local = {socket_lan_ip(), accelerator_gateway_ip()}
    cleaned = []
    for s in servers:
        if s.startswith("127.") or s in local or s in cleaned:
            continue
        cleaned.append(s)
    return cleaned


def detect_accelerator() -> list:
    """带 10 秒缓存（tasklist 每次几百毫秒，界面轮询受不了）。"""
    return _cached("accel", 10, _detect_accelerator)


def _detect_accelerator() -> list:
    """本机在跑的加速器进程名列表（粗测，用来给用户确认）。"""
    hits = []
    if IS_WINDOWS:
        rc, out = run(["tasklist", "/FO", "CSV", "/NH"], timeout=12)
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith('"'):
                continue
            name = line.split('","')[0].strip('"')
            low = name.lower()
            if any(h in low for h in ACCEL_PROCESS_HINTS) and name not in hits:
                hits.append(name)
    else:
        rc, out = run(["ps", "-Ao", "comm"], timeout=8)
        for line in out.splitlines():
            low = line.strip().lower()
            if any(h in low for h in ACCEL_PROCESS_HINTS) and low not in hits:
                hits.append(low)
    return hits


def classify_dns(ip: str) -> str:
    """loopback / tun / linklocal / lan / public / none"""
    if not ip:
        return "none"
    if ip.startswith("127.") or ip == "::1":
        return "loopback"
    if any(ip.startswith(p) for p in FAKE_IP_PREFIXES):
        return "tun"
    if ip.startswith("169.254."):
        return "linklocal"
    if _is_private_ipv4(ip):
        return "lan"
    return "public"


# ──────────────────────────────────────────────────────────────
# 端口
# ──────────────────────────────────────────────────────────────
def port_free(port: int, proto: str = "tcp", host: str = "0.0.0.0") -> bool:
    stype = socket.SOCK_DGRAM if proto == "udp" else socket.SOCK_STREAM
    s = None
    try:
        s = socket.socket(socket.AF_INET, stype)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if proto == "tcp":
            # Windows 上 SO_REUSEADDR 语义不同，别用它抢端口
            try:
                s.bind((host, port))
            except OSError:
                if host != "0.0.0.0":
                    raise
                s.bind(("", port))
        else:
            s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        try:
            if s:
                s.close()
        except Exception:
            pass


def port_listening(port: int, proto: str = "tcp") -> bool:
    """只看 LISTENING，不发起连接（避免污染 PS5 连接日志）。"""
    if IS_WINDOWS:
        rc, out = run(["netstat", "-ano", "-p", proto.upper()], timeout=8)
        needle = ":%d" % port
        for line in out.splitlines():
            if needle in line and "LISTENING" in line.upper():
                return True
        return False
    # Linux 先读 /proc/net/tcp：不依赖任何外部命令，容器里最可靠。
    # （镜像里没有 lsof 是常态，早先的版本在这里会误判成「端口没在听」，
    #   于是又起一组 nginx，两组抢 1935。）
    if sys.platform.startswith("linux") and proto == "tcp":
        found = _proc_net_listening(port)
        if found is not None:
            return found
    rc, out = run(["lsof", "-nP", "-i%s:%d" % (proto.upper(), port), "-sTCP:LISTEN"], timeout=8)
    return "(LISTEN)" in out


def _proc_net_listening(port: int):
    """返回 True/False；读不到 /proc 时返回 None（交给调用方换办法）。"""
    seen = False
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path, "r") as f:
                next(f, None)
                for line in f:
                    parts = line.split()
                    if len(parts) < 4:
                        continue
                    if parts[3] != "0A":        # 0A = TCP_LISTEN
                        continue
                    seen = True
                    try:
                        if int(parts[1].rsplit(":", 1)[1], 16) == port:
                            return True
                    except (IndexError, ValueError):
                        continue
        except Exception:
            continue
    return False if seen else None


def port_owner(port: int, proto: str = "tcp"):
    """返回 (pid, 进程名)，拿不到给 (0, '')。"""
    if not IS_WINDOWS:
        rc, out = run(["lsof", "-nP", "-i%s:%d" % (proto.upper(), port)], timeout=8)
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) > 1 and parts[1].isdigit():
                return int(parts[1]), parts[0]
        return 0, ""

    rc, out = run(["netstat", "-ano", "-p", proto.upper()], timeout=8)
    pid = 0
    needle = ":%d" % port
    for line in out.splitlines():
        if needle not in line:
            continue
        tail = line.split()[-1]
        if tail.isdigit() and tail != "0":
            pid = int(tail)
            break
    if not pid:
        return 0, ""
    rc2, out2 = run(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"])
    for line in out2.splitlines():
        line = line.strip()
        if line.startswith('"'):
            name = line.split('","')[0].strip('"')
            if name and not name.upper().startswith("INFO"):
                return pid, name
    return pid, ""


def kill_pid(pid: int) -> bool:
    if not pid:
        return False
    if not IS_WINDOWS:
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
            return True
        except Exception:
            try:
                os.kill(pid, signal.SIGKILL)
                return True
            except Exception:
                return False
    rc, _ = run(["taskkill", "/PID", str(pid), "/F"], timeout=10)
    return rc == 0


# ──────────────────────────────────────────────────────────────
# Windows 防火墙
# ──────────────────────────────────────────────────────────────
FIREWALL_RULES = [
    ("PS5LiveBridge-DNS-UDP", "UDP", 53),
    ("PS5LiveBridge-DNS-TCP", "TCP", 53),
    ("PS5LiveBridge-Web", "TCP", 5000),
    ("PS5LiveBridge-IRC", "TCP", 6667),
    ("PS5LiveBridge-RTMP", "TCP", 1935),
    ("PS5LiveBridge-Stat", "TCP", 8080),
]


def firewall_rule_exists(name: str) -> bool:
    if not IS_WINDOWS:
        return True
    rc, out = run(["netsh", "advfirewall", "firewall", "show", "rule", "name=%s" % name])
    return rc == 0 and name in out


def ensure_firewall(ports=None) -> tuple:
    """幂等放行。返回 (全部成功, [消息])。"""
    if not IS_WINDOWS:
        return True, ["非 Windows 平台，跳过防火墙"]
    rules = [
        ("PS5LiveBridge-DNS-UDP", "UDP", 53),
        ("PS5LiveBridge-DNS-TCP", "TCP", 53),
        ("PS5LiveBridge-Web", "TCP", (ports or {}).get("web", 5000)),
        ("PS5LiveBridge-IRC", "TCP", (ports or {}).get("irc", 6667)),
        ("PS5LiveBridge-RTMP", "TCP", (ports or {}).get("rtmp", 1935)),
        ("PS5LiveBridge-Stat", "TCP", (ports or {}).get("stat", 8080)),
    ]
    msgs = []
    all_ok = True
    for name, proto, port in rules:
        if firewall_rule_exists(name):
            msgs.append("已放行 %s (%s %d)" % (name, proto, port))
            continue
        rc, out = run([
            "netsh", "advfirewall", "firewall", "add", "rule",
            "name=%s" % name, "dir=in", "action=allow",
            "protocol=%s" % proto, "localport=%d" % port,
            "profile=any", "enable=yes",
        ], timeout=15)
        if rc == 0:
            msgs.append("已放行 %s (%s %d)" % (name, proto, port))
        else:
            all_ok = False
            msgs.append("放行失败 %s (%s %d): %s" % (name, proto, port, out.strip()[:100]))
    return all_ok, msgs


# ──────────────────────────────────────────────────────────────
# 探测
# ──────────────────────────────────────────────────────────────
def probe_tcp(host: str, port: int, timeout: float = 1.5) -> bool:
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        try:
            if s:
                s.close()
        except Exception:
            pass


def probe_http(url: str, timeout: float = 3.0) -> tuple:
    """
    只用标准库。必须绕过系统代理 —— 这类用户普遍在跑 Clash/mihomo，
    urllib 会读 http_proxy，127.0.0.1 的请求被代理转发回来 502，
    健康检查就会误判成「服务挂了」。
    """
    import urllib.error
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ps5bridge-check"})
        with opener.open(req, timeout=timeout) as resp:
            return (200 <= resp.status < 400), resp.status
    except urllib.error.HTTPError as e:
        return False, e.code
    except Exception as e:
        return False, str(e)[:80]


def path_warnings(path: str) -> list:
    warns = []
    try:
        path.encode("ascii")
    except UnicodeEncodeError:
        warns.append("程序路径含中文：%s" % path)
    if " " in path:
        warns.append("程序路径含空格：%s" % path)
    if len(path) > 90:
        warns.append("程序路径过长（%d 字符）" % len(path))
    return warns
