from __future__ import annotations

import json
import os
import socket
import time
from datetime import datetime, timezone
import urllib.error
import urllib.request

_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
_GQL_URL = "https://gql.twitch.tv/gql"

PROBE_QUERY = "query{ __typename }"

_PROXY_CANDIDATES = ("http://127.0.0.1:7890", "http://127.0.0.1:7897",
                     "http://127.0.0.1:7891", "http://127.0.0.1:7892",
                     "http://127.0.0.1:10809", "http://127.0.0.1:10808",
                     "http://127.0.0.1:2080", "http://127.0.0.1:8080")

_LOCAL_CACHE = {"ts": 0.0, "list": []}
_LOCAL_TTL = 60.0


def parse_broadcaster_id(stream_key: str) -> str:
    """
    从 Twitch 推流密钥里抠 broadcaster_id。
        live_1348515979_DGbH2bro...  →  1348515979
    不是这个格式就返回空。
    """
    s = (stream_key or "").strip()
    if not s.startswith("live_"):
        return ""
    rest = s[len("live_"):]
    head = rest.split("_")[0] if rest else ""
    return head if head.isdigit() else ""


def _port_open(url: str, timeout: float = 0.4) -> bool:
    """
    代理端口是不是真的在监听。

    为什么要这一步：Windows 上大多数用户没装 Clash/mihomo，
    如果挨个试 4 个候选端口、每个超时 6 秒，光等代理就 24 秒 ——
    开播后标题要半天才出来。先探端口，没开就直接跳过。
    """
    try:
        rest = url.split("://", 1)[-1].split("/")[0]
        host, _, port = rest.rpartition(":")
        if not host or not port:
            return False
        with socket.create_connection((host, int(port)), timeout):
            return True
    except Exception:
        return False


def _local_proxies() -> list:
    """
    扫本机常见代理端口，结果缓存 60 秒。

    用户留空代理时的默认行为：不指定地址，但本机要是跑着 Clash/mihomo，
    就直接用上它。缓存是为了不让每次标题同步都重新扫 8 个端口。
    """
    now = time.time()
    if now - _LOCAL_CACHE["ts"] < _LOCAL_TTL:
        return list(_LOCAL_CACHE["list"])
    found = [c for c in _PROXY_CANDIDATES if _port_open(c)]
    _LOCAL_CACHE["ts"] = now
    _LOCAL_CACHE["list"] = found
    return list(found)


def _proxy_list() -> list:
    """
    要依次尝试的通道：显式配置 > 本机现成的代理 > 直连。
    最后那个空串表示直连，保证无论如何都有退路。
    """
    env = (os.environ.get("PS5LB_TWITCH_PROXY") or "").strip()
    if env:
        return [env, ""]
    return _local_proxies() + [""]


def detect_local_proxy() -> str:
    """留空代理时用：返回本机第一个可用的代理地址，没有就返回空串。"""
    found = _local_proxies()
    return found[0] if found else ""


def _brief(exc) -> str:
    """把异常压成一句人话。"""
    t = str(exc).lower()
    if "timed out" in t or "timeout" in t:
        return "连接超时"
    if "refused" in t or "10061" in t:
        return "端口拒绝连接"
    if "407" in t:
        return "代理需要认证"
    if "getaddrinfo" in t or "name or service" in t:
        return "域名解析失败"
    if "tunnel" in t or "ssl" in t:
        return "代理建立隧道失败"
    return str(exc)[:60]


def check_one(proxy: str = "", timeout: float = 6.0) -> dict:
    """测一条通道能不能连到 Twitch。proxy="" 表示直连。"""
    t0 = time.time()
    try:
        _post_gql(PROBE_QUERY, {}, proxy, timeout)
        return {"proxy": proxy, "ok": True,
                "ms": int((time.time() - t0) * 1000), "error": ""}
    except Exception as e:
        return {"proxy": proxy, "ok": False,
                "ms": int((time.time() - t0) * 1000), "error": _brief(e)}


def check(proxy: str = "", timeout: float = 6.0) -> dict:
    """
    界面「代理检测」按钮用。

    proxy 有值 → 只测这一条，好使就行。
    proxy 为空 → 直连 + 本机代理都测一遍，然后告诉用户到底要不要填。
    """
    px = (proxy or "").strip()

    if px:
        one = check_one(px, timeout)
        return {
            "mode": "explicit",
            "direct": None,
            "local": [],
            "chosen": px if one["ok"] else "",
            "result": one,
            "verdict": "proxy_ok" if one["ok"] else "proxy_bad",
            "advice": ("这个代理能连上 Twitch，就用它。"
                       if one["ok"] else
                       "这个代理连不上 Twitch（%s）。检查地址、端口，"
                       "以及代理本身有没有开。" % one["error"]),
        }

    direct = check_one("", timeout)
    if direct["ok"]:
        return {
            "mode": "auto", "direct": direct, "local": [], "chosen": "",
            "result": direct, "verdict": "direct_ok",
            "advice": "本机直连就能到 Twitch，这一栏留空就行。",
        }

    locals_ = _local_proxies()
    tested = [check_one(c, timeout) for c in locals_]
    good = [t for t in tested if t["ok"]]

    if good:
        return {
            "mode": "auto", "direct": direct, "local": tested,
            "chosen": good[0]["proxy"], "result": good[0],
            "verdict": "local_ok",
            "advice": ("直连到不了 Twitch，但本机有现成的代理（%s）能用，"
                       "留空程序会自动走它。" % good[0]["proxy"]),
        }

    detail = ("，".join("%s %s" % (t["proxy"].split("//")[-1], t["error"])
                       for t in tested) if tested else "本机没发现代理端口")
    return {
        "mode": "auto", "direct": direct, "local": tested, "chosen": "",
        "result": direct, "verdict": "none",
        "advice": ("直连到不了 Twitch（%s），本机也没找到能用的代理（%s）。"
                   "这一栏必须填一个能上外网的代理地址。"
                   % (direct["error"], detail)),
    }


def _post_gql(query: str, variables: dict, proxy: str, timeout: float) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    handlers = [urllib.request.ProxyHandler(
        {"http": proxy, "https": proxy} if proxy else {})]
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(_GQL_URL, data=body, headers={
        "Client-ID": _CLIENT_ID,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    })
    t0 = time.time()
    with opener.open(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", "ignore"))
    try:
        from . import logbus
        logbus.debug("GQL %dms 经 %s" % (
            int((time.time() - t0) * 1000), proxy or "直连"), tag="Twitch")
    except Exception:
        pass
    return data


def fetch_live_title(broadcaster_id: str, proxy: str = None,
                     timeout: float = 8.0) -> str:
    """按 broadcaster_id 查标题。失败返回空串。"""
    bid = (broadcaster_id or "").strip()
    if not bid:
        return ""
    query = "query($id:ID!){ user(id:$id){ lastBroadcast { title } } }"
    proxies = [proxy] if proxy is not None else _proxy_list()
    for px in proxies:
        try:
            payload = _post_gql(query, {"id": bid}, px, timeout)
            title = (((payload.get("data") or {}).get("user") or {})
                     .get("lastBroadcast") or {}).get("title") or ""
            title = title.strip()
            if title:
                return title
        except (urllib.error.URLError, urllib.error.HTTPError,
                TimeoutError, OSError, ValueError, KeyError):
            continue
    return ""


SESSION_QUERY = """
query($login:String!){
  user(login:$login){
    id
    stream { id createdAt viewersCount }
    lastBroadcast { startedAt title }
    videos(first:1){ edges { node { id } } }
  }
}
"""


def _parse_ts(text: str):
    """ISO8601（带 Z 和小数秒）→ 时间戳，失败返回 0。"""
    t = (text or "").strip()
    if not t:
        return 0.0
    try:
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        return datetime.fromisoformat(t).timestamp()
    except Exception:
        return 0.0


def fetch_session(channel: str = "", proxy: str = "",
                  timeout: float = 8.0) -> dict:
    """
    查频道当前在不在播、上一场是什么时候。

    这是判断「PS5 说其他设备正在播放」到底是不是 Twitch 那侧问题的主要依据：
    stream 非空 = Twitch 认为还在播；空着但刚播过 = 可能留了旧会话。
    """
    ch = (channel or "").strip().lstrip("@").lower()
    base = {
        "channel": ch, "ok": False, "proxy": "", "ms": 0,
        "is_live": False, "stream_created_at": "", "viewers": 0,
        "last_broadcast": {"started_at": "", "title": "", "ts": 0.0},
        "vod_count": 0, "verdict": "unknown", "error": "", "advice": "",
    }
    if not ch:
        base["error"] = "没填 Twitch 频道名"
        base["advice"] = "在第 4 步填上频道名，这里才能查 Twitch 那侧的状态。"
        return base

    proxies = [proxy] if proxy else _proxy_list()
    last_err = ""
    for px in proxies:
        t0 = time.time()
        try:
            payload = _post_gql(SESSION_QUERY, {"login": ch}, px, timeout)
        except Exception as e:
            last_err = _brief(e)
            continue

        user = (payload.get("data") or {}).get("user")
        if not user:
            base.update(ok=True, ms=int((time.time() - t0) * 1000), proxy=px,
                        error="Twitch 上没有这个频道",
                        advice="检查第 4 步的频道名拼写。")
            return base

        st = user.get("stream") or {}
        lb = user.get("lastBroadcast") or {}
        vids = ((user.get("videos") or {}).get("edges")) or []
        started = lb.get("startedAt") or ""
        ts = _parse_ts(started)
        now = time.time()

        if st:
            verdict, advice = "live", "Twitch 显示这个频道正在直播。这时候 PS5 再点开播，必然被拦。"
        elif ts and (now - ts) < 3 * 3600:
            verdict = "stale"
            advice = ("这个频道 %d 分钟前刚播过，现在不在播。如果 PS5 说「其他设备正在播放」，"
                      "先去 twitch.tv/dashboard 结束上一次直播，或者重置推流密钥。"
                      % int((now - ts) / 60))
        else:
            verdict, advice = "offline", "这个频道当前没在直播，状态正常。"

        base.update(
            ok=True, proxy=px, ms=int((time.time() - t0) * 1000),
            is_live=bool(st),
            stream_created_at=(st.get("createdAt") or "") if st else "",
            viewers=int((st.get("viewersCount") or 0)) if st else 0,
            last_broadcast={"started_at": started, "title": lb.get("title") or "",
                            "ts": ts},
            vod_count=len(vids),
            verdict=verdict, advice=advice, error="",
        )
        return base

    base["error"] = last_err or "连不上 Twitch"
    base["advice"] = "检查代理是否可用（第 1 步/第 4 步的「检测」按钮）。"
    return base


def fetch_stream_title(stream_key: str, proxy: str = None) -> str:
    """一步到位：推流密钥 → 标题。"""
    return fetch_live_title(parse_broadcaster_id(stream_key), proxy=proxy)


def fetch_twitch_title(channel: str, proxy: str = None,
                       timeout: float = 8.0) -> str:
    """备用：按频道名（login）查当前直播标题。"""
    ch = (channel or "").strip().lstrip("@").lower()
    if not ch:
        return ""
    query = "query($login:String!){ user(login:$login){ stream { title } } }"
    proxies = [proxy] if proxy is not None else _proxy_list()
    for px in proxies:
        try:
            payload = _post_gql(query, {"login": ch}, px, timeout)
            title = (((payload.get("data") or {}).get("user") or {})
                     .get("stream") or {}).get("title") or ""
            title = title.strip()
            if title:
                return title
        except (urllib.error.URLError, urllib.error.HTTPError,
                TimeoutError, OSError, ValueError, KeyError):
            continue
    return ""
