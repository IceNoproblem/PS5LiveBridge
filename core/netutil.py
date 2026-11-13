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

VIRTUAL_HINTS = (
    "vethernet", "vmware", "virtualbox", "hyper-v", "tap", "tun", "tailscale",
    "zerotier", "wireguard", "clash", "mihomo", "meta", "docker", "wsl",
    "loopback", "npcap", "radmin", "hamachi", "openvpn", "softether",
    "npcap loopback", "utun", "awdl", "llw", "bridge",
)

ACCEL_ADAPTER_HINTS = (
    "uu", "netease", "网易", "xunyou", "迅游", "leigod", "雷神", "biubiu",
    "qiyou", "奇游", "accel", "booster", "wtfast", "exitlag",
)

ACCEL_PROCESS_HINTS = (
    "uubooster", "uu.exe", "neteaseuu", "uu_booster", "uugamebooster",
    "xunyou", "xyacc", "leigod", "biubiu", "qiyou", "xyqy",
    "wtfast", "exitlag", "gameraccelerator",
)

FAKE_IP_PREFIXES = ("198.18.", "198.19.")

_NON_ROUTABLE_PREFIXES = ("127.", "169.254.", "0.", "255.", "224.", "239.")


_PROC_JANITOR = []


def _patch_popen_del() -> None:
    """
    给 subprocess.Popen.__del__ 套一层异常保护。

    实测这台机器的 Python 3.13.12 在 Popen.__del__ 里会抛 RecursionError。
    多数时候它只是被 "Exception ignored" 吞掉，看着人畜无害；但在请求
    线程里触发过一次，直接把整个 Flask 进程带走了（「修复收流」第一版
    就是这么崩的，页面显示连接被拒）。

    与其在每个 new Popen 的地方各防一遍，不如在源头兜住：析构阶段
    出什么异常都不应该影响程序本身。这是针对解释器缺陷的兜底，
    不改变任何正常行为。
    """
    try:
        import subprocess as _sp
        orig = _sp.Popen.__del__
        if getattr(orig, "_ps5lb_safe", False):
            return

        def safe_del(self, *a, **kw):
            try:
                orig(self, *a, **kw)
            except BaseException:
                pass

        safe_del._ps5lb_safe = True
        _sp.Popen.__del__ = safe_del
    except Exception:
        pass


_patch_popen_del()


def disarm_popen(proc) -> None:
    """
    拆掉 Popen 的析构逻辑。

    CPython 的 Popen.__del__ 第一行就是 `if not self._child_created: return`，
    把这个标志置 False，析构就成了空操作。实测 Python 3.13.12 的 __del__
    会抛 RecursionError，在请求线程里触发时能连带把整个 Flask 进程带走
    （「修复收流」第一版就是这么崩的）。

    调用前必须已经 wait 过子进程，否则子进程会变僵尸。
    """
    if proc is None:
        return
    try:
        proc._child_created = False
    except Exception:
        pass


def _retire(proc) -> None:
    """用完的 Popen 挂起来，别就地析构。"""
    disarm_popen(proc)
    try:
        _PROC_JANITOR.append(proc)
        if len(_PROC_JANITOR) > 300:
            del _PROC_JANITOR[:150]
    except Exception:
        pass


def run(cmd, timeout=8, env=None, cwd=None):
    """
    执行命令返回 (returncode, 文本)。

    中文 Windows 上 netsh / tasklist / PowerShell 往管道里写的是 GBK 字节。
    以前无脑按 UTF-8 解，网卡别名「以太网」会变成一串乱码，选 IP 直接选错。
    这里先严格 UTF-8，失败退系统 ANSI 代码页。
    """
    try:
        kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if IS_WINDOWS:
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if env is not None:
            kwargs["env"] = env
        if cwd:
            kwargs["cwd"] = cwd
        # 不用 subprocess.run：它内部的 Popen 在函数返回时就被析构，
        # 正好撞上上面说的 __del__ 递归问题。
        # finally 里一定要 retire：communicate 抛异常时也不能漏掉，
        # 漏掉的那个会在下一次创建进程时被 GC，然后原地炸掉。
        p = None
        try:
            p = subprocess.Popen(cmd, **kwargs)
            try:
                raw, _ = p.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    p.kill()
                except Exception:
                    pass
                raw, _ = p.communicate()
            rc = p.returncode
        finally:
            if p is not None:
                _retire(p)
        raw = raw or b""
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            encoding = "mbcs" if IS_WINDOWS else "utf-8"
            try:
                text = raw.decode(encoding, "replace")
            except Exception:
                text = raw.decode("latin-1", "replace")
        return rc, text
    except Exception as e:
        return -1, str(e)


def powershell(script, timeout=12):
    """跑一段 PowerShell。前置 OutputEncoding，否则输出会被转成 GBK。"""
    prologue = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                "$OutputEncoding=[Text.Encoding]::UTF8;")
    return run(["powershell", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command", prologue + script],
               timeout=timeout)


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
        # 允许「重启」时新进程带着 CREATE_BREAKAWAY_FROM_JOB 脱离这个 Job，
        # 否则主程序一退出，刚拉起来的新进程会被一起收掉，重启永远失败。
        JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x0800
        extended_limit_information = 9

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
        info.BasicLimitInformation.LimitFlags = (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                                                 | JOB_OBJECT_LIMIT_BREAKAWAY_OK)
        ok = kernel32.SetInformationJobObject(
            job, extended_limit_information, ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            return False
        if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
            return False
        globals()["_JOB_HANDLE"] = job
        return True
    except Exception:
        return False


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
    """拿物理网卡的局域网 IP。

    优先用网卡枚举：list_adapters() 会给虚拟网卡重罚，代理软件开 TUN 模式
    时 utun 的 192.0.0.1 会排在最末，而 en0 的真实地址排第一。
    单靠 UDP 路由技巧不行 —— 流量被 TUN 接走之后 getsockname() 拿到的是
    隧道地址（192.0.0.1），于是界面会报「本机 IP 与网关不在同一网段」。
    """
    try:
        for a in list_adapters():
            ip = (a.get("ip") or "").strip()
            if not ip:
                continue
            if ip.startswith(("127.", "169.254.", "192.0.0.", "0.")):
                continue
            return ip
    except Exception:
        pass

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
            score -= 40
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
        gw = ""
        try:
            with open("/proc/net/route", "r") as f:
                next(f, None)
                for line in f:
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    dest, gateway = parts[1], parts[2]
                    if dest == "00000000" and gateway != "00000000":
                        raw = int(gateway, 16)
                        gw = ".".join(str(b) for b in raw.to_bytes(4, "little"))
                        break
        except Exception:
            pass
        if not gw:
            rc, out = run(["netstat", "-rn"], timeout=5)
            gws = re.findall(r"default\s+(\d+\.\d+\.\d+\.\d+)", out)
            gw = gws[0] if gws else ""
        ip = socket_lan_ip()
        if gw and ip:
            mapping[ip] = gw
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


def port_free(port: int, proto: str = "tcp", host: str = "0.0.0.0") -> bool:
    stype = socket.SOCK_DGRAM if proto == "udp" else socket.SOCK_STREAM
    s = None
    try:
        s = socket.socket(socket.AF_INET, stype)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if proto == "tcp":
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


def _win_tcp_listeners():
    """
    用 IP Helper API 直接读内核的 TCP 监听表，返回 {端口: PID}。

    为什么要绕开 netstat：实测有机器（装了 360 之类）上 netstat 的输出被
    过滤，`0.0.0.0:1935 LISTENING` 整行消失，只剩几条状态错乱的残行，
    于是程序误判「端口没监听」，把排查方向带偏。

    返回 None 表示 API 不可用（非 Windows / 调用失败），交给调用方兜底。
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None

    class MIB_TCPROW_OWNER_PID(ctypes.Structure):
        _fields_ = [
            ("dwState", wintypes.DWORD),
            ("dwLocalAddr", wintypes.DWORD),
            ("dwLocalPort", wintypes.DWORD),
            ("dwRemoteAddr", wintypes.DWORD),
            ("dwRemotePort", wintypes.DWORD),
            ("dwOwningPid", wintypes.DWORD),
        ]

    AF_INET = 2
    TCP_TABLE_OWNER_PID_LISTENER = 3
    NO_ERROR = 0
    ERROR_INSUFFICIENT_BUFFER = 122

    try:
        iphlpapi = ctypes.windll.iphlpapi
        get = iphlpapi.GetExtendedTcpTable
    except Exception:
        return None

    size = wintypes.DWORD(0)
    ret = get(None, ctypes.byref(size), False, AF_INET,
              TCP_TABLE_OWNER_PID_LISTENER, 0)
    if ret not in (NO_ERROR, ERROR_INSUFFICIENT_BUFFER) or not size.value:
        return None

    buf = ctypes.create_string_buffer(size.value)
    ret = get(buf, ctypes.byref(size), False, AF_INET,
              TCP_TABLE_OWNER_PID_LISTENER, 0)
    if ret != NO_ERROR:
        return None

    count = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD)).contents.value
    offset = ctypes.sizeof(wintypes.DWORD)
    rows = ctypes.cast(ctypes.addressof(buf) + offset,
                       ctypes.POINTER(MIB_TCPROW_OWNER_PID))
    out = {}
    for i in range(count):
        row = rows[i]
        raw = int(row.dwLocalPort) & 0xFFFF
        port = ((raw & 0xFF) << 8) | ((raw >> 8) & 0xFF)   # 网络序转主机序
        out[port] = int(row.dwOwningPid)
    return out


def port_listening(port: int, proto: str = "tcp") -> bool:
    """只看 LISTENING，不发起连接（避免污染 PS5 连接日志）。"""
    if IS_WINDOWS:
        if proto.lower() == "tcp":
            m = _win_tcp_listeners()
            if m is not None:
                return int(port) in m
        rc, out = run(["netstat", "-ano", "-p", proto.upper()], timeout=20)
        needle = ":%d" % port
        for line in out.splitlines():
            if needle in line and "LISTENING" in line.upper():
                return True
        return False
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
                    if parts[3] != "0A":
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


def _pid_of_socket_inode(inode: str) -> int:
    """Linux：按 socket inode 反查持有它的 pid。只扫得动本 PID 命名空间。"""
    import os as _os
    try:
        pids = _os.listdir("/proc")
    except OSError:
        return 0
    needle = "socket:[%s]" % inode
    for d in pids:
        if not d.isdigit():
            continue
        fd_dir = "/proc/%s/fd" % d
        try:
            fds = _os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                if _os.readlink(_os.path.join(fd_dir, fd)) == needle:
                    return int(d)
            except OSError:
                continue
    return 0


def _proc_net_owner(port: int, proto: str = "tcp"):
    """
    Linux 下拿端口占用者：/proc/net/tcp 查 inode → /proc/*/fd 反查 pid → /proc/<pid>/comm 拿进程名。

    为什么要自己解析而不用 lsof：python:3.11-slim 这类精简镜像里根本没装 lsof
    （容器版实测），一调就报「命令不存在」，端口在听也只会显示「未识别到」。
    读不到 /proc 时返回 None，交给调用方换别的办法。
    """
    if not sys.platform.startswith("linux") or proto.lower() != "tcp":
        return None
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            fh = open(path, "r")
        except OSError:
            continue
        try:
            next(fh, None)
            for line in fh:
                parts = line.split()
                # 列：sl local rem st tx:rx tr:tm retrnsmt uid timeout inode
                if len(parts) < 10 or parts[3] != "0A":
                    continue
                try:
                    if int(parts[1].rsplit(":", 1)[1], 16) != int(port):
                        continue
                except (IndexError, ValueError):
                    continue
                inode = parts[9]
                pid = _pid_of_socket_inode(inode)
                if not pid:
                    return 0, ""
                name = ""
                try:
                    with open("/proc/%d/comm" % pid, "r") as cf:
                        name = cf.read().strip()
                except OSError:
                    pass
                return pid, name
        except Exception:
            continue
        finally:
            try:
                fh.close()
            except Exception:
                pass
    return None


def port_owner(port: int, proto: str = "tcp"):
    """返回 (pid, 进程名)，拿不到给 (0, '')。"""
    if not IS_WINDOWS:
        mine = _proc_net_owner(port, proto)
        if mine:
            return mine
        rc, out = run(["lsof", "-nP", "-i%s:%d" % (proto.upper(), port)], timeout=8)
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) > 1 and parts[1].isdigit():
                return int(parts[1]), parts[0]
        return 0, ""

    pid = 0
    m = _win_tcp_listeners()
    if m is not None:
        pid = int(m.get(int(port)) or 0)
    if not pid:
        rc, out = run(["netstat", "-ano", "-p", proto.upper()], timeout=20)
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


def kill_pid_tree(pid: int) -> bool:
    """
    连子进程一起杀。

    收流端会有派生进程：nginx 带 worker，ffmpeg 有时挂在别处。
    只杀父进程会留下孤儿继续占着 1935，那正是「有其他设备正在向同一频道
    播放」的来源之一：旧进程抱着 publisher 会话不放，PS5 再推就被拒。
    """
    if not pid:
        return False
    if not IS_WINDOWS:
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
        for _ in range(20):
            time.sleep(0.15)
            if not pid_alive(pid):
                return True
        try:
            os.kill(pid, signal.SIGKILL)
            return True
        except Exception:
            return False
    rc, _ = run(["taskkill", "/F", "/T", "/PID", str(pid)], timeout=10)
    return rc == 0


def pid_alive(pid: int) -> bool:
    if not pid:
        return False
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False
    rc, out = run(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
                  timeout=10)
    return rc == 0 and ('"%d"' % pid) in out


# 收流端会用到的进程名。启动前发现这些占着端口才动手清；
# 别的程序占着就一个字都不碰，明确报错让用户自己处理。
RECEIVER_PROC_NAMES = (
    "rtmf", "rtmp-forwarder", "rtmp_forwarder",
    "nginx", "mediamtx",
    "ffmpeg",
)


def free_port_for_receiver(port: int, names=None, wait: float = 5.0) -> dict:
    """
    收流端口启动前的清场。

    为什么必须有这个：rtmf / mediamtx 的 start() 原来只看自己的进程退没退，
    完全不管 1935 上是不是还坐着一个上次留下的同款进程。结果是新进程 bind
    失败、旧进程继续抱着 PS5 的 publisher 会话，PS5 一重推就被拒，报
    「有其他设备正在向同一频道播放」。用户只能靠重启电脑解决。

    只清自己家的进程（rtmf/nginx/mediamtx/ffmpeg），别人的进程一律不动。

    返回 {"ok": bool, "killed": [pid], "detail": str}
    """
    if not port:
        return {"ok": True, "killed": [], "detail": ""}
    names = tuple(n.lower() for n in (names or RECEIVER_PROC_NAMES))
    killed = []
    last_pid, last_name = 0, ""
    deadline = time.time() + max(1.0, wait)

    while True:
        if not port_listening(port):
            if killed:
                msg = "已清掉上次残留的收流进程（%s）" % "、".join(str(p) for p in killed)
                return {"ok": True, "killed": killed, "detail": msg}
            return {"ok": True, "killed": [], "detail": ""}

        pid, name = port_owner(port)
        last_pid, last_name = pid, name
        if not pid:
            return {"ok": False, "killed": killed,
                    "detail": "端口 %d 被占用，但查不到是哪个进程（可能需要管理员权限）" % port}

        low = (name or "").lower()
        if not any(n in low for n in names):
            return {"ok": False, "killed": killed,
                    "detail": "端口 %d 被 %s（PID %d）占着，那不是直播桥的进程，没有动它"
                              % (port, name or "未知程序", pid)}

        if pid != os.getpid() and pid not in killed:
            killed.append(pid)
            kill_pid_tree(pid)

        if time.time() > deadline:
            return {"ok": False, "killed": killed,
                    "detail": "端口 %d 上的 %s（PID %d）清不掉，可能需要用管理员身份启动，"
                              "或者有安全软件在拦" % (port, last_name or "进程", last_pid)}
        time.sleep(0.25)


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
