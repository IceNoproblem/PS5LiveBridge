from __future__ import annotations

import time

from . import netutil, paths, routermatch, rtmf

_FW_RULES = (
    ("DNS (UDP 53)", "PS5LiveBridge-DNS-UDP"),
    ("DNS (TCP 53)", "PS5LiveBridge-DNS-TCP"),
    ("弹幕 (6667)", "PS5LiveBridge-IRC"),
    ("收流 (1935)", "PS5LiveBridge-RTMP"),
    ("界面 (5000)", "PS5LiveBridge-Web"),
)

# 试绑失败时系统给的错误码，翻成人话。
# 10013 和 10048 一定要分开：前者是系统不让绑（端口被保留），后者是别人占着，
# 解法完全不同。2026-11-14 有个用户卡在这上面，界面上写的是「被占」，其实是 10013。
_BIND_HINTS = {
    10013: "系统不允许绑定这个端口。没有程序在监听，多半是被系统保留了"
           "（装了 Hyper-V / WSL / Docker 的机器会占掉一批端口，每次开机范围还会变）。"
           "管理员命令行依次执行：net stop winnat，重开本程序，再 net start winnat。"
           "要确认就 netsh int ipv4 show excludedportrange protocol=tcp",
    10048: "这个端口已经被别的程序占用。netstat -ano | findstr :端口号 能查到是谁",
    10049: "指定的 IP 不属于本机，绑不了",
    13: "权限不足。低于 1024 的端口需要管理员或 root",
    98: "这个端口已经被别的程序占用（EADDRINUSE，Linux）",
    48: "这个端口已经被别的程序占用（EADDRINUSE，macOS）",
    99: "无法分配地址（EADDRNOTAVAIL）",
}


def _probe_bind(port: int, host: str = "0.0.0.0") -> dict:
    """
    试绑一次这个端口，拿系统给的错误码。

    比「看有没有在听」信息量大得多：端口在听只说明有人占着，
    试绑失败能区分「系统不让绑」和「别人占着」，这两件事的解法完全相反。
    只在端口没在监听时调用，否则会把「自己占着」当成故障报出来。
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Windows 默认允许多个 socket 绑同一个端口，不设独占的话试绑永远成功，
        # 探测就白做了（2026-11-14 实测：端口被占着，bind 照样 ok）。
        # SO_EXCLUSIVEADDRUSE 只有 Windows 有，Linux 默认就是独占。
        excl = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if excl is not None:
            try:
                s.setsockopt(socket.SOL_SOCKET, excl, 1)
            except Exception:
                pass
        s.bind((host, port))
        return {"ok": True, "code": 0, "hint": ""}
    except OSError as e:
        code = getattr(e, "winerror", None) or e.errno or 0
        return {"ok": False, "code": int(code),
                "hint": _BIND_HINTS.get(int(code), str(e)[:120])}
    finally:
        try:
            s.close()
        except Exception:
            pass


def _probe_port(port: int, proto: str = "tcp") -> dict:
    try:
        listening = bool(netutil.port_listening(port, proto))
    except Exception:
        listening = False
    pid, name = 0, ""
    if listening:
        try:
            pid, name = netutil.port_owner(port, proto)
        except Exception:
            pass
    return {"port": port, "listening": listening, "pid": pid, "process": name or ""}


def _firewall() -> dict:
    out = {}
    for label, rule in _FW_RULES:
        try:
            out[label] = bool(netutil.firewall_rule_exists(rule))
        except Exception:
            out[label] = None
    return out


def _ports_overview(orch, caps=None) -> dict:
    """
    这台机器该开的端口有没有都开着。

    443 和 8098 也要看：443 是 HTTPS 中继（Twitch 打不开时多半是它没起），
    8098 是收流端的管理口（本地取流走它）。
    """
    try:
        web_port = int(getattr(orch.nginx, "web_port", 0) or 0)
    except Exception:
        web_port = 0

    # 容器版没有内置 DNS、也不跑 443 中继（它们只在方案二 + 非容器下启动），
    # 把这两个端口列出来只会一直显示「没在听」，白白吓人。
    if caps is None:
        try:
            caps = paths.caps()
        except Exception:
            caps = {}
    if paths.PLATFORM == "docker":
        want = [1935, 6667, 5000]
        if caps.get("builtin_dns"):
            want.insert(0, 53)
    else:
        want = [53, 443, 1935, 6667, 5000]
    if web_port:
        want.append(web_port)

    out = []
    for port in want:
        probe = _probe_port(port, "tcp")
        udp = False
        if port in (53,):
            try:
                udp = bool(netutil.port_listening(port, "udp"))
            except Exception:
                udp = False
        item = {
            "port": port,
            "listening": bool(probe["listening"]) or udp,
            "udp": udp,
            "process": probe["process"],
        }
        # 业务端口没在听时试着绑一下，把系统的错误码带出来。
        # 只看 listening 的话，10013（系统不让绑）和 10048（别人占着）分不出来。
        # 53 不试：低于 1024 的端口在非 root 下必然报权限，会误导。
        if not item["listening"] and port in (1935, 6667, 443):
            b = _probe_bind(port)
            if not b["ok"]:
                item["bind_code"] = b["code"]
                item["bind_hint"] = b["hint"]
        out.append(item)
    return {"items": out}


def _relay_stat(orch) -> dict:
    out = {"running": False, "error": "", "impl": "",
           "bitrate": "", "restarts": 0}
    try:
        r = getattr(orch, "relay", None)
        if r is not None:
            out["running"] = bool(r.running)
            out["error"] = r.last_error or ""
        # 写实际在跑的实现，配置里 auto 看不出真实情况
        out["impl"] = "rtmpf" if getattr(orch, "native_relay", False) else "nginx"
        out["bitrate"] = str(getattr(r, "bitrate", "") or "")
        out["restarts"] = int(getattr(r, "restarts", 0) or 0)
    except Exception:
        pass
    return out


# 常见会干扰本程序的网络相关软件（进程名 → 显示名）
_SEC_PROCS = {
    "360tray.exe": "360 安全卫士",
    "360sd.exe": "360 杀毒",
    "360safe.exe": "360 安全卫士",
    "zhudongfangyu.exe": "360 主动防御",
    "hipstray.exe": "火绒安全",
    "hipsdaemon.exe": "火绒安全服务",
    "usysdiag.exe": "火绒（驱动）",
    "qqpctray.exe": "腾讯电脑管家",
    "qqpcrtp.exe": "腾讯电脑管家",
    "kxetray.exe": "金山毒霸",
    "kxescore.exe": "金山毒霸服务",
    "qqbrowser.exe": "QQ 浏览器",
}


def _env_profile(orch) -> dict:
    """
    判断当前网络环境，用来提醒「这套方案在你的网络下适不适合」。

    重点区分：常规家用路由器 / Windows 共享热点 / 手机热点 / 校园网 / 只有加速器虚拟网卡。
    节目里最常踩的是后三种：PS5 和电脑不在一个广播域，方案三直接失效。
    """
    out = {"kind": "unknown", "level": "ok", "detail": "",
           "lan_ip": "", "gateway": "", "same_subnet": False,
           "virtual_nics": [], "gateways": []}
    try:
        lan_ip = netutil.socket_lan_ip() or ""
        gw = netutil.default_gateway() or ""
        out["lan_ip"] = lan_ip
        out["gateway"] = gw

        # 网关列表 + 虚拟网卡，都用 netutil 现成的枚举（它已经处理了中英文别名）
        gws = []
        try:
            for g in (netutil.get_gateways() or {}).values():
                if g and g not in gws:
                    gws.append(g)
        except Exception:
            pass
        out["gateways"] = gws
        if not gw and gws:
            gw = gws[0]
            out["gateway"] = gw

        virt = []
        try:
            for a in (netutil.list_adapters() or []):
                alias = str(a.get("alias") or "")
                low = alias.lower()
                if a.get("accel") or any(k in low for k in
                                         ("tap", "tun", "wintun", "uu", "迅游",
                                          "xunyou", "clash", "mihomo")):
                    virt.append("%s (%s)" % (alias, a.get("ip")))
        except Exception:
            pass
        out["virtual_nics"] = virt

        same = False
        if lan_ip and gw and lan_ip.count(".") == 3 and gw.count(".") == 3:
            same = lan_ip.rsplit(".", 1)[0] == gw.rsplit(".", 1)[0]
        out["same_subnet"] = same
        last = gw.rsplit(".", 1)[-1] if gw.count(".") == 3 else ""

        if gw == "192.168.137.1":
            out.update(kind="ics", level="ok",
                       detail="Windows 网络共享（ICS），网关 %s" % gw)
        elif gw in ("172.20.10.1",) or lan_ip.startswith("172.20.10."):
            out.update(kind="phone_hotspot", level="err",
                       detail="iPhone 个人热点，网关 %s" % gw)
        elif gw in ("192.168.42.129", "192.168.43.1") or lan_ip.startswith("192.168.43."):
            out.update(kind="phone_hotspot", level="err",
                       detail="安卓手机热点，网关 %s" % gw)
        elif same and last in ("1", "254"):
            out.update(kind="home", level="ok",
                       detail="家用路由器，网关 %s，与本机同网段" % gw)
        elif same:
            out.update(kind="home", level="warn",
                       detail="家用路由器，网关 %s（非 .1/.254，可能是旁路由或二级路由）" % gw)
        elif gws:
            out.update(kind="other_subnet", level="warn",
                       detail="本机 %s 与网关 %s 不在同一网段"
                              % (lan_ip or "?", gw))
        if len(gws) > 1:
            # 加速器虚拟网卡也常带一条默认路由，本身不算问题；
            # 只有网关分散在不同网段时才有选错出口的风险
            subs = {".".join(g.split(".")[:3]) for g in gws if g.count(".") == 3}
            if len(subs) > 1:
                if out["level"] == "ok":
                    out["level"] = "warn"
                out["detail"] = (out["detail"] +
                                 "；检测到 %d 个不同网段的默认网关，注意出口选择"
                                 % len(subs)).strip("；")
    except Exception as e:
        out["detail"] = "环境判定失败：%s" % e
    return out


def _security_scan() -> dict:
    """扫一遍常见安全软件。它们会拦 exe、改 hosts、甚至过滤 netstat 输出。"""
    out = {"found": [], "level": "ok", "detail": ""}
    # 这几家全是 Windows 上的东西，Linux / 容器里没有 tasklist，硬扫只会
    # 得到一句「检测失败」把界面弄脏，直接说清楚不适用。
    if paths.PLATFORM != "windows":
        out["detail"] = "非 Windows 平台，不适用"
        return out
    try:
        rc, txt = netutil.run(["tasklist", "/FO", "CSV", "/NH"], timeout=20)
        low = (txt or "").lower()
        for proc, name in _SEC_PROCS.items():
            if proc in low and name not in out["found"]:
                out["found"].append(name)
        if out["found"]:
            out["level"] = "warn"
            out["detail"] = ("检测到 %s。这类软件可能拦截程序、修改 hosts、"
                             "或让端口检测失灵；装不上/连不通时先把它退掉试一次"
                             % "、".join(out["found"]))
        else:
            out["detail"] = "没检测到常见的安全软件"
    except Exception as e:
        out["detail"] = "安全软件检测失败：%s" % e
    return out


def _accel_procs(orch) -> list:
    """本机在跑的加速器进程（UU / 迅游 之类）。方案三必须有。"""
    try:
        e = orch.env() or {}
        return list(e.get("accel_procs") or [])
    except Exception:
        return []


def _proc_by_name(name: str) -> bool:
    """Linux：扫 /proc/*/comm 找同名进程。精简镜像里没有 pgrep，不能依赖它。"""
    import os
    try:
        pids = os.listdir("/proc")
    except OSError:
        return False
    for d in pids:
        if not d.isdigit():
            continue
        try:
            with open("/proc/%s/comm" % d, "r") as f:
                if f.read().strip() == name:
                    return True
        except OSError:
            continue
    return False


def _runtime_checks(orch, rtmp_port: int) -> dict:
    """程序自身的运行条件：管理员权限 / 收流进程 / 转发进程。"""
    out = {"admin": False, "rtmp_proc": "", "ffmpeg": False}
    # 走 netutil 的统一实现：Windows 用 IsUserAnAdmin，Linux/容器看 geteuid()==0。
    # 以前这里直接调 ctypes.windll，容器里必抛 AttributeError → admin 永远 False，
    # 自检页面顶着一条「不是管理员」红字，还扯 WinDivert，根本对不上容器的情况。
    try:
        out["admin"] = bool(netutil.is_admin())
    except Exception:
        out["admin"] = False
    try:
        _pid, _name = netutil.port_owner(rtmp_port)
        out["rtmp_proc"] = _name or ""
    except Exception:
        pass
    try:
        if paths.PLATFORM == "windows":
            rc, txt = netutil.run(["tasklist", "/FI", "IMAGENAME eq ffmpeg.exe",
                                   "/FO", "CSV", "/NH"], timeout=15)
            out["ffmpeg"] = "ffmpeg.exe" in (txt or "").lower()
        else:
            # 别指望 pgrep：python:3.11-slim 里未必有 procps
            out["ffmpeg"] = _proc_by_name("ffmpeg")
    except Exception:
        pass
    return out


def collect(orch, plan: str = "") -> dict:
    """
    本机侧诊断，只读。

    Twitch 那侧要发网络请求（慢），所以单独走 /api/twitch/session，
    这里只保证在几十毫秒内返回，界面先出本地结论。
    """
    try:
        snap = orch.snapshot()
    except Exception:
        snap = {}

    sv = snap.get("services") or {}
    hj = snap.get("hijack") or {}
    live = snap.get("live") or {}

    try:
        rtmp_port = int(orch.cfg.get("RTMP_PORT", 1935) or 1935)
    except (TypeError, ValueError):
        rtmp_port = 1935
    try:
        irc_port = int(orch.cfg.get("IRC_PORT", 6667) or 6667)
    except (TypeError, ValueError):
        irc_port = 6667

    rtmp = _probe_port(rtmp_port)
    irc = _probe_port(irc_port)

    nginx = sv.get("nginx") or {}
    relay = sv.get("https_relay") or sv.get("relay") or {}
    pub = nginx.get("publisher") or {}
    publishing = bool(pub.get("publishing"))
    nginx_running = bool(nginx.get("running"))

    issues = []

    try:
        caps = paths.caps()
    except Exception:
        caps = {}
    _accel = _accel_procs(orch)
    env = _env_profile(orch)
    try:
        router = routermatch.probe(env.get("gateway") or "")
    except Exception as e:
        router = {"error": "%s: %s" % (type(e).__name__, e)}
    sec = _security_scan()
    checks = _runtime_checks(orch, rtmp_port)

    if paths.PLATFORM == "windows" and not checks["admin"]:
        issues.append({"level": "err",
                       "msg": "程序不是以管理员身份运行。方案三要加载 WinDivert 驱动、"
                              "要改防火墙规则，没有管理员权限会静默失败。"
                              "请退出后右键「以管理员身份运行」"})
    if env.get("detail") and env.get("level") in ("warn", "err"):
        issues.append({"level": env["level"], "msg": env["detail"]})
    if sec.get("level") == "warn" and sec.get("detail"):
        issues.append({"level": "warn", "msg": sec["detail"]})

    # 显示实际在跑的实现：auto 模式下配置里可能还写着旧的 nginx，
    # 而真正收流的已经是 rtmp-forwarder。
    _impl = "rtmpf" if getattr(orch, "native_relay", False) else "nginx"
    _other = "nginx" if _impl == "rtmf" else "rtmf"
    # 「换个收流实现」这句建议只在包里真的带了另一个实现时才成立。
    # 镜像以前只有 nginx，现在也装了 rtmp-forwarder，能不能换看有没有二进制。
    _can_switch = _other == "nginx" or bool(rtmf.find_exe())
    if not rtmp["listening"]:
        if not _can_switch:
            msg = ("收流服务没在监听 %d，PS5 的推流进不来。这个包里没有第二种收流实现，"
                   "先看容器日志里收流为什么没起来" % rtmp_port)
        else:
            msg = ("收流服务没在监听 %d，PS5 的推流进不来。"
                   "去第 5 步把「收流实现」从 %s 换成 %s 再试一次"
                   % (rtmp_port, _impl, _other))
        issues.append({"level": "err", "msg": msg})
    elif not nginx_running:
        issues.append({"level": "err",
                       "msg": "%d 有进程在监听，但收流服务没起来，可能被 %s 占了"
                              % (rtmp_port, rtmp["process"] or "别的进程")})

    # 当前收流实现反复重启 → 建议换另一种（包里只有一种就改看日志）
    _ri = _relay_stat(orch)
    if int(_ri.get("restarts") or 0) >= 3:
        if not _can_switch:
            msg = ("收流实现 %s 已经重启过 %d 次，多半是不稳定。这个包换不了实现，"
                   "去容器日志里看收流报什么错"
                   % (_impl, int(_ri.get("restarts") or 0)))
        else:
            msg = ("收流实现 %s 已经重启过 %d 次，多半是不稳定。去第 5 步换成 %s 试试"
                   % (_impl, int(_ri.get("restarts") or 0), _other))
        issues.append({"level": "warn", "msg": msg})

    if not irc["listening"]:
        issues.append({"level": "warn",
                       "msg": "弹幕通道没在监听 %d，PS5 画面上的弹幕不会出来" % irc_port})

    fw = _firewall()
    missing = [k for k, v in fw.items() if v is False]
    if missing:
        issues.append({"level": "warn",
                       "msg": "防火墙还缺 %d 条放行规则：%s"
                              % (len(missing), "、".join(missing))})


    hijack_mode = hj.get("mode") or ""
    dns_running = bool(hj.get("dns_running"))

    # DNS 端口被别的程序占了（常见：UU 远程的 GameViewerService）
    # 只在「这台机器能跑内置 DNS」时才值得查；容器版默认没有内置 DNS，
    # NAS 上的 53 常年被 dnsmasq/AdGuard 占着，报出来纯属误导。
    if not dns_running and rtmp.get("listening") and caps.get("builtin_dns", True):
        try:
            p53 = _probe_port(53, "udp")
            who = p53.get("process") or ""
            if who and "ps5livebridge" not in who.lower():
                issues.append({
                    "level": "err",
                    "msg": "53 端口被 %s 占着，内置 DNS 起不来。"
                           "关掉它的 DNS/网络优化功能，或退出该程序后重试。" % who,
                })
        except Exception:
            pass

    relay_running = bool(relay.get("running"))
    relay_error = str(relay.get("error") or "").strip()

    if hijack_mode == "dns" and not dns_running:
        live_now = (live.get("state") or "idle") != "idle"
        issues.append({
            "level": "err" if live_now else "warn",
            "msg": ("正在直播但内置 DNS 没在跑，PS5 的域名解析没人答"
                    if live_now else
                    "上次选的是内置 DNS 方案，现在 DNS 服务没开（还没点「开启内置 DNS」）"),
        })
    if relay_error:
        issues.append({"level": "warn", "msg": "443 中继报错：%s" % relay_error[:120]})

    errs = [i for i in issues if i["level"] == "err"]
    warns = [i for i in issues if i["level"] == "warn"]

    # 结论要落到「当前方案 + 现在到哪一步」，不能一句「看起来正常」包打天下
    _ps5 = bool(hj.get("ps5_seen"))
    _dv = bool((hj.get("divert") or {}).get("running"))
    _dns = bool(hj.get("dns_running"))
    # 用户选了什么就以什么为准；没选才按运行状态反推
    if plan in ("hosts", "dns", "divert"):
        _plan = plan
    elif _dns:
        _plan = "dns"
    elif _dv or hijack_mode == "divert":
        _plan = "divert"
    else:
        _plan = "hosts"
    # 容器版只支持方案一（劫持和内置 DNS 都要碰本机网络栈，容器里做不了）。
    # 有人手改 URL 传 plan=divert 也不能给出一条照做会白忙的结论。
    if paths.PLATFORM == "docker":
        _plan = "hosts"

    if errs:
        verdict = errs[0]["msg"]
        level = "err"
    elif warns:
        verdict = warns[0]["msg"]
        level = "warn"
    elif _plan == "divert":
        if not _accel:
            verdict = ("方案三要求加速器装在「这台电脑」上，现在没检测到加速器进程。"
                       "先打开加速器、确认它在给 PS5 加速")
            level = "warn"
        elif not _dv:
            verdict = "加速器在跑了，但流量接管还没开。去「方案选择」里点开启"
            level = "warn"
        elif not _ps5:
            verdict = "本机已就绪（加速器在跑、接管已开），等 PS5 点开播就会自动接住"
            level = "ok"
        else:
            verdict = "方案三正在工作：PS5 的推流已经进到这台电脑了"
            level = "ok"
    elif _plan == "dns":
        if not _dns:
            verdict = ("方案二要先开启内置 DNS（这台电脑临时当 DNS 服务器）。"
                       "去「方案选择」里点「保存代理并开启内置 DNS」")
            level = "warn"
        elif not _ps5:
            verdict = "内置 DNS 已开，等 PS5 那边把 DNS 指到这台电脑"
            level = "ok"
        else:
            verdict = "方案二正在工作：PS5 已经在用这台电脑解析域名"
            level = "ok"
    else:
        _cap = (router or {}).get("hosts") or ""
        _host = "这台 NAS" if paths.PLATFORM == "docker" else "这台电脑"
        if _cap in ("no", "oem"):
            if paths.PLATFORM == "docker":
                verdict = ("这台路由器(%s)改不了 hosts，方案一在它上面走不通。"
                           "换一台支持自定义 hosts 的路由器，或在路由器上用"
                           "「DNS 静态解析」把这些域名指到 %s"
                           % ((router or {}).get("brand") or "未识别型号", _host))
            else:
                verdict = ("你这台路由器(%s)没有自定义 hosts 的功能，方案一走不通，"
                           "建议改用方案二或方案三"
                           % ((router or {}).get("brand") or "未识别型号"))
            level = "warn"
        elif not _ps5:
            if paths.PLATFORM == "docker":
                verdict = ("等 PS5 开播。前提是路由器已经把 Twitch 那几个域名指到%s，"
                           "指没指对可以在第 2 步查一次" % _host)
            else:
                verdict = "等确认路由器的 hosts 已生效（第 2 步检测通过后，PS5 开播即可）"
            level = "ok"
        else:
            verdict = "方案一正在工作：PS5 的流量已经到%s了" % _host
            level = "ok"

    return {
        "at": time.time(),
        "platform": snap.get("platform") or "",
        "version": snap.get("version") or "",
        "lan_ip": snap.get("lan_ip") or "",
        "gateway": env.get("gateway") or "",
        "mode": hijack_mode,
        "local": {
            "rtmp": rtmp,
            "irc": irc,
            "nginx": {"running": bool(nginx.get("running")),
                      "publisher": {"present": publishing,
                                    "name": pub.get("name") or "",
                                    "uptime": int(pub.get("uptime") or 0),
                                    "width": pub.get("width") or 0,
                                    "height": pub.get("height") or 0,
                                    "fps": pub.get("fps") or 0,
                                    "kbps": pub.get("bw_in") or 0}},
            "dns": {"running": dns_running, "mode": hijack_mode},
            "divert": {"running": bool(hj.get("divert", {}).get("running")
                                       if isinstance(hj.get("divert"), dict) else False)},
            "relay": {"running": relay_running, "error": relay_error},
            "firewall": fw,
            "ports": _ports_overview(orch, caps),
            "relay_info": _relay_stat(orch),
            "env": env,
            "security": sec,
            "checks": checks,
            "ps5_seen": bool(hj.get("ps5_seen")),
            "accel": _accel,
            "router": router,
            "relay_443": bool((snap.get("services", {}).get("https_relay") or {})
                              .get("running")),
            "proxy_set": bool((orch.cfg.get("TWITCH_PROXY") or "").strip()),
            "live_state": live.get("state") or "idle",
        },
        "issues": issues,
        "verdict": verdict,
        "level": level,
    }


def _os_evidence(rtmp_port: int = 1935, irc_port: int = 6667) -> dict:
    """
    系统层的证据，专门回答「端口为什么绑不上」。

    这几条 netsh 加起来两百毫秒上下，界面那条自检要几十毫秒返回，所以不背它们，
    只在导出诊断包时跑。
    """
    if paths.PLATFORM != "windows":
        return {}
    import re

    out = {}
    watch = [rtmp_port, irc_port, 53, 5000, 8098]

    # 端口保留段：10013 的唯一解释。落在段里的端口，bind 会被系统拒掉
    for proto in ("tcp", "udp"):
        try:
            rc, txt = netutil.run(["netsh", "int", "ipv4", "show",
                                   "excludedportrange", "protocol=" + proto],
                                  timeout=15)
        except Exception:
            continue
        if rc != 0 or not txt:
            continue
        rows = []
        for line in txt.splitlines():
            m = re.match(r"\s*(\d+)\s+(\d+)\s*$", line)
            if not m:
                continue
            start, end = int(m.group(1)), int(m.group(2))
            row = {"start": start, "end": end}
            covers = [p for p in watch if start <= p <= end]
            if covers:
                row["covers"] = covers
            rows.append(row)
        out["excluded_" + proto] = {
            "count": len(rows),
            "hits": [p for p in watch if any(r["start"] <= p <= r["end"] for r in rows)],
            "rows": rows,
        }

    # 端口代理：1935 被转去别处的可能
    try:
        rc, txt = netutil.run(["netsh", "interface", "portproxy", "show", "all"],
                              timeout=10)
        if rc == 0 and txt:
            out["portproxy"] = [l.strip() for l in txt.splitlines() if l.strip()][:40]
    except Exception:
        pass

    # URL 预留：HTTP.sys 先占了端口，普通 socket 就绑不上
    try:
        rc, txt = netutil.run(["netsh", "http", "show", "urlacl"], timeout=15)
        if rc == 0 and txt:
            hits = []
            for line in txt.splitlines():
                s = line.strip()
                if s.startswith("Reserved URL") or s.startswith("保留的 URL"):
                    if any(":%d/" % p in s or ":%d " % p in s for p in watch):
                        hits.append(s)
            if hits:
                out["urlacl_hits"] = hits[:20]
    except Exception:
        pass

    # 默认路由：网关不对，PS5 的包根本不会经过本机
    try:
        rc, txt = netutil.run(["route", "print", "-4"], timeout=10)
        if rc == 0 and txt:
            defs = []
            for line in txt.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
                    defs.append({"gateway": parts[2], "iface": parts[3],
                                 "metric": parts[4]})
            out["default_routes"] = defs
    except Exception:
        pass

    return out


def _conn_summary(netstat_text: str, port: int) -> list:
    """
    从已经抓好的 netstat 输出里，挑出这个端口上的连接。

    不重新跑 netstat，直接解析现成的文本。有 ESTABLISHED 就说明对面真的连过来了，
    这是判断「PS5 到底有没有走过来」最直接的证据。
    """
    out = []
    tail = ":%d" % port
    for line in (netstat_text or "").splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        if parts[0].upper() not in ("TCP", "TCPV6"):
            continue
        local, remote = parts[1], parts[2]
        if not (local.endswith(tail) or remote.endswith(tail)):
            continue
        out.append({
            "local": local,
            "remote": remote,
            "state": parts[3] if len(parts) > 3 else "",
            "pid": parts[4] if len(parts) > 4 else "",
        })
    return out[:30]


def _dns_snapshot(domains, servers=None, timeout: float = 1.2) -> list:
    """
    每个域名查一遍 A 和 AAAA，把实际拿到的东西列出来。

    2026-11-14 那个「hosts 配了但 PS5 还是直连」的用户，缺的就是这一份：
    只看 hosts 内容不知道有没有命中，得看真实解析结果。
    AAAA 有记录是危险信号，说明 PS5 可能走 IPv6 绕过 hosts。
    """
    from concurrent.futures import ThreadPoolExecutor
    from . import dnsserver

    if servers is None:
        gw = ""
        try:
            gw = netutil.default_gateway()
        except Exception:
            pass
        servers = [gw] if gw else []

    def one(dom):
        row = {"domain": dom, "a": [], "aaaa": [], "error": ""}
        for srv in servers:
            try:
                ok, res = dnsserver.query(srv, dom, qtype=dnsserver.QTYPE_A,
                                          timeout=timeout)
            except Exception as e:
                row["error"] = str(e)[:80]
                continue
            if ok:
                row["a"] = list(res)[:6]
                row["server"] = srv
                break
        for srv in servers:
            try:
                ok6, res6 = dnsserver.query(srv, dom, qtype=dnsserver.QTYPE_AAAA,
                                            timeout=timeout)
            except Exception:
                continue
            if ok6:
                row["aaaa"] = list(res6)[:6]
                break
        if not row["a"] and not row["error"]:
            row["error"] = "查不到 A 记录"
        return row

    items = list(domains or [])
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(6, len(items))) as pool:
        return list(pool.map(one, items))


def _hosts_entries() -> list:
    """本机 hosts 里生效的条目（去掉注释和空行）。"""
    import os
    p = (r"C:\Windows\System32\drivers\etc\hosts"
         if paths.PLATFORM == "windows" else "/etc/hosts")
    out = []
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.split("#", 1)[0].strip()
                if s:
                    out.append(s)
    except Exception:
        return []
    return out[:40]


def _summary_text(info: dict) -> str:
    """
    诊断包里那份人话摘要。用户发问题时可以直接贴这段，不用打开 JSON 找。
    """
    d = info.get("diag") or {}
    ev = d.get("evidence") or {}
    local = d.get("local") or {}
    lines = []
    lines.append("PS5 直播桥 诊断摘要")
    lines.append("时间  %s" % info.get("time", ""))
    lines.append("版本  %s    平台 %s    方案 %s"
                 % (d.get("version") or "?", d.get("platform") or "?", d.get("mode") or "?"))
    lines.append("本机  %s    网关 %s" % (d.get("lan_ip") or "?", d.get("gateway") or "?"))
    lines.append("")

    ports = (local.get("ports") or {}).get("items") or []
    if ports:
        lines.append("端口")
        for it in ports:
            s = "  %-5d %s" % (it.get("port"), "在听" if it.get("listening") else "没听")
            if it.get("process"):
                s += "  (%s)" % it["process"]
            if it.get("bind_code"):
                s += "  试绑失败(%d)" % it["bind_code"]
            lines.append(s)
            if it.get("bind_hint"):
                lines.append("        %s" % str(it["bind_hint"])[:160])
        lines.append("")

    issues = d.get("issues") or []
    if issues:
        lines.append("自检问题 %d 条" % len(issues))
        for i in issues:
            lines.append("  [%s] %s" % (i.get("level") or "?", i.get("msg") or ""))
    else:
        lines.append("自检没有发现问题")
    lines.append("")

    ex = ev.get("excluded_tcp") or {}
    if ex.get("hits"):
        lines.append("!! TCP 端口落在系统保留段里: %s" % ex["hits"])
        for r in ex.get("rows") or []:
            if r.get("covers"):
                lines.append("   %d-%d 覆盖 %s" % (r["start"], r["end"], r["covers"]))
        lines.append("")

    dns_rows = ev.get("dns") or []
    if dns_rows:
        lines.append("域名解析（从本机查）")
        for r in dns_rows:
            lines.append("  %-42s A=%-16s AAAA=%s"
                         % (r.get("domain"), ",".join(r.get("a") or []) or "-",
                            ",".join(r.get("aaaa") or []) or "-"))
        if any(r.get("aaaa") for r in dns_rows):
            lines.append("  注意: 有 AAAA 记录，如果 PS5 开了 IPv6 会绕过 hosts")
        lines.append("")

    conns = ev.get("rtmp_connections") or []
    est = [c for c in conns if "ESTABLISHED" in (c.get("state") or "")]
    lines.append("收流端口连接 %d 条，其中已建立 %d 条" % (len(conns), len(est)))
    for c in est[:6]:
        lines.append("  %s <- %s" % (c.get("local"), c.get("remote")))
    if not est:
        lines.append("  没有已建立的连接。PS5 没有连到本机，先查它是怎么拿到地址的")
    return "\n".join(lines) + "\n"


def _mask_proxy(url: str) -> str:
    """代理地址保留前两段：192.168.*.*:7890。端口留着，排查时要用。"""
    if not url:
        return ""
    scheme, rest = "", url
    if "://" in url:
        scheme, rest = url.split("://", 1)
    hostport = rest.split("/")[0]
    host, _, port = hostport.rpartition(":")
    if not host:
        return url
    parts = host.split(".")
    if len(parts) == 4:
        host = "%s.%s.*.*" % (parts[0], parts[1])
    out = host + (":" + port if port else "")
    return (scheme + "://" + out) if scheme else out


def _mask_config(cfg: dict) -> dict:
    """配置脱敏：代理只留网段，凭据全打码。"""
    secret_keys = ("SESSDATA", "COOKIE", "TOKEN", "SECRET", "PASSWORD", "JCT",
                   "STREAM_KEY", "BILI_JCT", "SESS")
    out = {}
    for k, v in (cfg or {}).items():
        ks = str(k).upper()
        if isinstance(v, str) and "PROXY" in ks:
            out[k] = _mask_proxy(v)
        elif any(s in ks for s in secret_keys):
            out[k] = "***" if v else ""
        else:
            out[k] = v
    return out


def _add_tail(z, path: str, arc: str, lines: int = 2000) -> bool:
    """把文件最后 N 行塞进压缩包。文件不在就跳过。"""
    import os
    if not path or not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = f.readlines()
        z.writestr(arc, "".join(data[-lines:]))
        return True
    except Exception:
        return False


def _find_nginx_log() -> str:
    """nginx 的 error.log：publish 被拒（BadName）会记在这里。"""
    import os
    from . import paths
    for rel in (os.path.join("_internal", "nginx-rtmp", "logs", "error.log"),
                os.path.join("nginx-rtmp", "logs", "error.log"),
                os.path.join("logs", "nginx-error.log")):
        p = os.path.join(paths.BASE_DIR, rel)
        if os.path.exists(p):
            return p
    return ""


def export(orch, diag_data=None) -> dict:
    """打一个诊断包，返回 {ok, name, path}。"""
    import json
    import os
    import time
    import zipfile

    from . import paths, twitch_title

    d = diag_data or collect(orch)
    try:
        snap = orch.snapshot()
    except Exception:
        snap = {}

    # 文件名带上版本和平台，用户发几个包过来时能分清
    stamp = time.strftime("%Y%m%d-%H%M%S")
    ver = str(snap.get("version") or "0").replace(".", "_")
    name = "diag-%s-%s-%s.zip" % (ver, paths.PLATFORM, stamp)
    out_dir = os.path.join(paths.DATA_DIR, "diag")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, name)

    try:
        rtmp_p = int(orch.cfg.get("RTMP_PORT", 1935) or 1935)
    except Exception:
        rtmp_p = 1935
    try:
        irc_p = int(orch.cfg.get("IRC_PORT", 6667) or 6667)
    except Exception:
        irc_p = 6667

    # 系统层证据：保留段、端口代理、URL 预留、默认路由、域名解析、hosts。
    # 几条 netsh 加起来两百毫秒上下，只在打包时跑，界面那条自检不等这些命令。
    ev = {}
    try:
        ev = _os_evidence(rtmp_p, irc_p)
    except Exception:
        ev = {}
    try:
        from .dnscheck import CHECK_DOMAINS
        ev["dns"] = _dns_snapshot(CHECK_DOMAINS)
    except Exception:
        ev["dns"] = []
    try:
        ev["hosts"] = _hosts_entries()
    except Exception:
        ev["hosts"] = []

    # netstat 只跑一次：既进包，也用来提炼收流端的连接
    net_txt = ""
    try:
        rc, out = netutil.run(["netstat", "-ano"] if paths.PLATFORM == "windows"
                              else ["lsof", "-nP", "-i"], timeout=25)
        net_txt = out or ""
    except Exception:
        net_txt = ""
    if net_txt:
        ev["rtmp_connections"] = _conn_summary(net_txt, rtmp_p)
    d["evidence"] = ev

    ch = (orch.cfg.get("TWITCH_CHANNEL") or "").strip()
    px = (orch.cfg.get("TWITCH_PROXY") or "").strip()
    try:
        # 两秒拿不到就算了，诊断包不该卡在这一步
        tw = twitch_title.fetch_session(ch, proxy=px, timeout=2.0)
    except Exception as e:
        tw = {"ok": False, "error": str(e)}

    try:
        from . import logbus
        debug_on = bool(logbus.debug_enabled())
    except Exception:
        debug_on = False

    info = {
        "at": time.time(),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "debug_log": debug_on,
        "diag": d,
        "twitch": tw,
        "snapshot": snap,
    }

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("summary.txt", _summary_text(info))
        z.writestr("info.json", json.dumps(info, ensure_ascii=False, indent=2))
        z.writestr("config.json",
                   json.dumps(_mask_config(orch.cfg), ensure_ascii=False, indent=2))

        log_dir = paths.LOGS_DIR
        tail = 8000 if debug_on else 2000
        _add_tail(z, os.path.join(log_dir, "dns_queries.log"), "dns_queries.log",
                  lines=tail)
        runs = []
        try:
            for fn in os.listdir(log_dir):
                if fn.startswith("run_") and fn.endswith(".log"):
                    runs.append(os.path.join(log_dir, fn))
        except Exception:
            pass
        for p2 in sorted(runs)[-2:]:
            _add_tail(z, p2, "logs/" + os.path.basename(p2), lines=tail)

        ng = _find_nginx_log()
        if ng:
            _add_tail(z, ng, "logs/nginx-error.log", lines=tail)

        for extra in ("rtmf-stderr.log", "divert.log"):
            p3 = os.path.join(log_dir, extra)
            if os.path.exists(p3):
                _add_tail(z, p3, "logs/" + extra, lines=tail)

        if net_txt:
            z.writestr("netstat.txt", net_txt[:300000])

    return {"ok": True, "name": name, "path": path,
            "size": os.path.getsize(path) if os.path.exists(path) else 0}
