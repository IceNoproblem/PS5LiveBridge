from __future__ import annotations

import time

from . import netutil

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


def collect(orch) -> dict:
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
    relay = sv.get("relay") or {}
    pub = nginx.get("publisher") or {}
    publishing = bool(pub.get("publishing"))
    nginx_running = bool(nginx.get("running"))

    issues = []

    if not rtmp["listening"]:
        issues.append({"level": "err",
                       "msg": "收流服务没在监听 %d，PS5 的推流进不来" % rtmp_port})
    elif not nginx_running:
        issues.append({"level": "err",
                       "msg": "%d 有进程在监听，但收流服务没起来，可能被 %s 占了"
                              % (rtmp_port, rtmp["process"] or "别的进程")})

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
    if errs:
        verdict = errs[0]["msg"]
        level = "err"
    elif warns:
        verdict = warns[0]["msg"]
        level = "warn"
    else:
        verdict = "本机这一侧看起来正常"
        level = "ok"

    return {
        "at": time.time(),
        "platform": snap.get("platform") or "",
        "version": snap.get("version") or "",
        "lan_ip": snap.get("lan_ip") or "",
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

        try:
            rc, out = netutil.run(["netstat", "-ano"] if paths.PLATFORM == "windows"
                                  else ["lsof", "-nP", "-i"], timeout=10)
            if out:
                z.writestr("netstat.txt", out[:300000])
        except Exception:
            pass

    return {"ok": True, "name": name, "path": path,
            "size": os.path.getsize(path) if os.path.exists(path) else 0}
