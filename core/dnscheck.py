from __future__ import annotations

from . import dnsserver, netutil

CHECK_DOMAINS = [
    "contribute.live-video.net",
    "global-contribute.live-video.net",
    "ingest.global-contribute.live-video.net",
    "irc.twitch.tv",
    "tmi.twitch.tv",
]

PUBLIC_HINTS = ("不是本机",)


def _looks_fake(ips) -> bool:
    return any(str(ip).startswith(netutil.FAKE_IP_PREFIXES) for ip in ips)


def candidates(include_self: bool = True) -> list:
    """
    列出要问的 DNS 服务器：[{server, label, kind}]
    kind: router / system / self

    为什么不把「本机 IP」单独当一个候选：
        本机 53 上如果跑着内置 DNS，问「本机 IP」等于问内置 DNS，
        会把「路由器没配好」误判成「已经配好了」。
        所以内置 DNS 只以 127.0.0.1 / kind=self 的身份出现，且只在 builtin 模式下检查。
    """
    out = []
    seen = set()

    gw = netutil.default_gateway()
    if gw and gw not in seen:
        seen.add(gw)
        out.append({"server": gw, "label": "路由器 %s" % gw, "kind": "router"})

    for s in netutil.system_dns():
        if s in seen:
            continue
        seen.add(s)
        out.append({"server": s, "label": "系统 DNS %s" % s, "kind": "system"})

    if include_self and "127.0.0.1" not in seen:
        out.append({"server": "127.0.0.1", "label": "本机内置 DNS", "kind": "self"})

    return out


def check_server(server: str, expected_ip: str, domains=None, timeout: float = 2.0) -> dict:
    """问一个服务器要全部域名，返回汇总（域名之间并发，整体不等串行超时）。"""
    from concurrent.futures import ThreadPoolExecutor

    domains = domains or CHECK_DOMAINS

    def one(domain):
        ok, res = dnsserver.query(server, domain, timeout=timeout)
        ips = res if ok else []
        return {
            "domain": domain,
            "ok": bool(ok and expected_ip in ips),
            "ips": ips,
            "err": "" if ok else str(res),
            "fake": netutil_classify_fake(ips),
        }

    with ThreadPoolExecutor(max_workers=min(8, len(domains))) as pool:
        rows = list(pool.map(one, domains))

    hits = sum(1 for r in rows if r["ok"])
    return {
        "server": server,
        "total": len(domains),
        "hits": hits,
        "ok": hits == len(domains),
        "fake_ip": any(r["fake"] for r in rows),
        "rows": rows,
    }


def netutil_classify_fake(ips) -> bool:
    return any(str(ip).startswith(netutil.FAKE_IP_PREFIXES) for ip in ips)


_RANK = {"router": 0, "local": 1, "system": 2, "self": 3, "manual": 4}


def _rank(entry: dict) -> int:
    """判定优先级：路由器 > PS5 直指本机 > 系统 DNS > 本机内置 DNS。"""
    return _RANK.get(entry.get("kind", ""), 9)


def verify(expected_ip: str, target=None, domains=None, mode: str = "router") -> dict:
    """
    总判定。

    mode:   router  —— 只验「路由器 / 系统 DNS」有没有把域名指到本机（第 2 步用）
            builtin —— 只验「本机内置 DNS」是否正常工作（第 2B 步用）
    target: 指定只问某个服务器。None = 按 mode 取候选。
    返回：
        ok          是否判定为重定向成功
        source      生效的那个 DNS（附 label）
        fake_ip     是否存在加速器接管 DNS 的迹象
        results     每个候选服务器的明细
        hint        给用户看的一句话结论
    """
    if not expected_ip:
        return {"ok": False, "source": None, "results": [], "fake_ip": False,
                "hint": "没有探测到本机局域网 IP，无法检测。请检查网络连接。"}

    entries = candidates(include_self=(mode == "builtin"))
    if mode == "builtin":
        entries = [e for e in entries if e["kind"] == "self"]
    else:
        entries = [e for e in entries if e["kind"] != "self"]
    if target:
        entries = [e for e in entries if e["server"] == target] or [
            {"server": target, "label": target, "kind": "manual"}]

    results = []
    winner = None
    fake_any = False

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(entries)))) as pool:
        futures = {pool.submit(check_server, e["server"], expected_ip, domains): e
                   for e in entries}
        for fut in futures:
            entry = futures[fut]
            try:
                r = fut.result(timeout=20)
            except Exception as e:
                r = {"server": entry["server"], "total": 0, "hits": 0, "ok": False,
                     "fake_ip": False, "rows": [], "error": str(e)[:60]}
            r["label"] = entry["label"]
            r["kind"] = entry["kind"]
            results.append(r)
            fake_any = fake_any or r.get("fake_ip", False)
            if r["ok"] and (winner is None or _rank(r) < _rank(winner)):
                winner = r

    results.sort(key=_rank)

    ok = winner is not None
    hint = ""
    if ok:
        kind = winner["kind"]
        if kind == "router":
            hint = "路由器已把推流域名指向本机（%s），配置生效。" % expected_ip
        elif kind == "local":
            hint = "PS5 的 DNS 已直接指向本机（%s），配置生效。" % expected_ip
        elif kind == "self":
            hint = "本机内置 DNS 正在生效，PS5 指向本机即可。"
        else:
            hint = "系统 DNS 已把推流域名指向本机（%s）。" % expected_ip
    else:
        if fake_any:
            hint = ("检测到 DNS 返回 198.18.x.x / 198.19.x.x 这类加速器伪地址："
                    "PS5 的解析已被加速器接管，路由器的 hosts 不会生效。"
                    "请改用「本机内置 DNS」。")
        else:
            hint = "所有来源解析到的都不是本机 %s，重定向尚未生效。" % expected_ip

    return {
        "ok": ok,
        "source": ({"server": winner["server"], "label": winner["label"],
                    "kind": winner["kind"]} if winner else None),
        "fake_ip": fake_any,
        "results": results,
        "expected_ip": expected_ip,
        "hint": hint,
    }


def upstream_health(upstream: str) -> dict:
    """
    验一下上游 DNS 通不通、解析出来什么。
    内置 DNS 模式里，上游必须是加速器 DNS，否则开播握手会绕过加速。
    """
    if not upstream:
        return {"ok": False, "detail": "未配置上游 DNS"}
    ok, res = dnsserver.query(upstream, "api.twitch.tv", timeout=3.0)
    if not ok:
        return {"ok": False, "detail": "上游 %s 查询失败：%s" % (upstream, res)}
    fake = _looks_fake(res)
    return {
        "ok": True,
        "ips": res,
        "fake_ip": fake,
        "accelerated": fake,
        "detail": ("上游 %s 已接管（返回 %s，属加速器伪地址，链路正常）"
                   % (upstream, ",".join(res))) if fake else
                  ("上游 %s 返回公网地址 %s —— 不是加速器 DNS，"
                   "PS5 可能开不了播" % (upstream, ",".join(res))),
    }
