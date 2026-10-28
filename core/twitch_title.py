from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request

_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
_GQL_URL = "https://gql.twitch.tv/gql"

_PROXY_CANDIDATES = ("http://127.0.0.1:7890", "http://127.0.0.1:7891",
                     "http://127.0.0.1:10809", "http://127.0.0.1:8080")


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


def _proxy_list() -> list:
    """按优先级给出要试的代理：显式配置 > 实际在监听的常见端口 > 直连。"""
    env = (os.environ.get("PS5LB_TWITCH_PROXY") or "").strip()
    out = []
    if env:
        out.append(env)
    for c in _PROXY_CANDIDATES:
        if c != env and _port_open(c):
            out.append(c)
    out.append("")
    return out


def _post_gql(query: str, variables: dict, proxy: str, timeout: float) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(_GQL_URL, data=body, headers={
        "Client-ID": _CLIENT_ID,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    })
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


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
