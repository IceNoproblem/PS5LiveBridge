from __future__ import annotations

import time

from . import netutil, paths, routermatch

_FW_RULES = (
    ("DNS (UDP 53)", "PS5LiveBridge-DNS-UDP"),
    ("DNS (TCP 53)", "PS5LiveBridge-DNS-TCP"),
    ("弹幕 (6667)", "PS5LiveBridge-IRC"),
    ("收流 (1935)", "PS5LiveBridge-RTMP"),
    ("界面 (5000)", "PS5LiveBridge-Web"),
)


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
        out.append({
            "port": port,
            "listening": bool(probe["listening"]) or udp,
            "udp": udp,
            "process": probe["process"],
        })
    return {"items": out}


def _relay_stat(orch) -> dict:
    out = {"running": False, "error": "", "impl": "",
           "bitrate": "", "restarts": 0}
    try:
        r = getattr(orch, "relay", None)
        if r is not None:
            out["running"] = bool(r.running)
            out["error"] = r.last_error or ""
        out["impl"] = orch.cfg.get("RTMP_IMPL", "")
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
    """程序自身的运行条件：管理员权限 / 收流进程 / 编码进程。"""
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

    _impl = str(orch.cfg.get("RTMP_IMPL") or "rtmf").lower()
    _other = "nginx" if _impl == "rtmf" else "rtmf"
    # 容器里只有 nginx 能跑（rtmf 没有 Linux 二进制，换成它直接起不来），
    # 所以「换个收流实现」这句建议只能在桌面版说。
    _only_nginx = paths.PLATFORM == "docker"
    if not rtmp["listening"]:
        if _only_nginx:
            msg = ("收流服务没在监听 %d，PS5 的推流进不来。容器版只有 nginx 一种收流实现，"
                   "先看容器日志里 nginx 为什么没起来" % rtmp_port)
        else:
            msg = ("收流服务没在监听 %d，PS5 的推流进不来。"
                   "去第 5 步把「收流实现」从 %s 换成 %s 再试一次"
                   % (rtmp_port, _impl, _other))
        issues.append({"level": "err", "msg": msg})
    elif not nginx_running:
        issues.append({"level": "err",
                       "msg": "%d 有进程在监听，但收流服务没起来，可能被 %s 占了"
                              % (rtmp_port, rtmp["process"] or "别的进程")})

    # 当前收流实现反复重启 → 建议换另一种（容器版没有第二种，改看日志）
    _ri = _relay_stat(orch)
    if int(_ri.get("restarts") or 0) >= 3:
        if _only_nginx:
            msg = ("收流实现 %s 已经重启过 %d 次，多半是不稳定。容器版换不了实现，"
                   "去容器日志里看 nginx 报什么错"
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

    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = "diag-%s.zip" % stamp
    out_dir = os.path.join(paths.DATA_DIR, "diag")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, name)

    d = diag_data or collect(orch)
    try:
        snap = orch.snapshot()
    except Exception:
        snap = {}
    ch = (orch.cfg.get("TWITCH_CHANNEL") or "").strip()
    px = (orch.cfg.get("TWITCH_PROXY") or "").strip()
    try:
        tw = twitch_title.fetch_session(ch, proxy=px, timeout=8.0)
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

        try:
            rc, out = netutil.run(["netstat", "-ano"] if paths.PLATFORM == "windows"
                                  else ["lsof", "-nP", "-i"], timeout=25)
            if out:
                z.writestr("netstat.txt", out[:300000])
        except Exception:
            pass

    return {"ok": True, "name": name, "path": path,
            "size": os.path.getsize(path) if os.path.exists(path) else 0}
