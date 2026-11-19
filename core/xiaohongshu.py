"""
小红书推流地址。

网页版 OBS 开播页（xiaohongshu.com/zhibo/obs）就是这么干的：
手机 App 生成 6 位数字，填进去，换回串流密钥。

接口（2026-11-18 从页面 JS 里挖出来并实测）：

    GET /web_api/sns/v1/live/obs/host_auth_info   查有没有 OBS 开播权限
    GET /web_api/sns/v1/live/obs/push_url?code=   用 6 位码换密钥
    GET /web_api/sns/v1/live/obs/living_push_url  正在直播时直接取当前地址

不带 cookie 请求时返回的是「无登录信息」而不是签名错误，说明这一组接口
只校验登录态，不需要 x-s / x-t。所以只要用户粘一次网页 cookie 就能用。

服务器地址是固定的，密钥本身长这样：
    569410816650205483?txSecret=…&txTime=…&vendor=tencent
所以拼出来的完整地址直接交给转发层就行。
"""

from __future__ import annotations

import json
import os
import time

import requests

from . import logbus, paths

API_BASE = "https://www.xiaohongshu.com"
PUSH_SERVER = "rtmp://live-push.xhscdn.com/live"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36")
COOKIE_FILE = "xhs_cookie.json"


def _path() -> str:
    return os.path.join(paths.DATA_DIR, COOKIE_FILE)


def load_cookie() -> str:
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("cookie") or ""
    except Exception:
        return ""


def save_cookie(raw: str) -> str:
    """
    存 cookie。粘进来的可能带 "Cookie:" 前缀，也可能带换行，先洗干净。
    存进 data 目录，不进日志、不回界面。
    """
    s = (raw or "").strip()
    if s.lower().startswith("cookie:"):
        s = s[7:].strip()
    s = " ".join(s.split())
    try:
        os.makedirs(paths.DATA_DIR, exist_ok=True)
        with open(_path(), "w", encoding="utf-8") as f:
            json.dump({"cookie": s, "saved_at": int(time.time())}, f)
    except Exception as e:
        logbus.warn("小红书 cookie 保存失败：%s" % e, tag="小红书")
        return ""
    if s:
        logbus.info("小红书登录信息已保存（%d 个字段）" % len(s.split(";")),
                    tag="小红书")
    return s


def cookie_names(raw: str) -> list:
    out = []
    for seg in (raw or "").split(";"):
        if "=" in seg:
            out.append(seg.split("=", 1)[0].strip())
    return out


def _headers() -> dict:
    return {
        "User-Agent": UA,
        "Referer": API_BASE + "/zhibo/obs",
        "Accept": "application/json, text/plain, */*",
    }


def _call(path: str, params=None) -> dict:
    cookie = load_cookie()
    if not cookie:
        return {"ok": False, "detail": "还没填小红书登录信息"}
    try:
        r = requests.get(API_BASE + path, params=params or {},
                         headers=_headers(), timeout=12,
                         cookies=_parse(cookie))
    except Exception as e:
        return {"ok": False, "detail": "连不上小红书：%s" % e}
    try:
        data = r.json()
    except Exception:
        return {"ok": False, "detail": "小红书返回的不是 JSON（HTTP %d）" % r.status_code}
    if not data.get("success"):
        return {"ok": False,
                "detail": data.get("msg") or "小红书返回失败（code=%s）" % data.get("code")}
    return {"ok": True, "data": data.get("data") or {}}


def _parse(cookie: str):
    out = {}
    for seg in cookie.split(";"):
        if "=" in seg:
            k, v = seg.split("=", 1)
            k = k.strip()
            if k:
                out[k] = v.strip()
    return out


def auth() -> dict:
    """查这个账号能不能用电脑开播。"""
    r = _call("/web_api/sns/v1/live/obs/host_auth_info")
    if not r["ok"]:
        return r
    d = r["data"] or {}
    allow = bool(d.get("liveAuth")) and bool(d.get("allowObs"))
    return {"ok": True, "allow": allow, "raw": d}


def push_url(code: str) -> dict:
    """
    6 位验证码换推流地址。

    返回里优先用 streamKey，那串本身就带 txSecret 鉴权参数。
    接口还给了 backupPushInfo，是备用线路，这里先不用。
    """
    code = str(code or "").strip()
    if not code.isdigit() or len(code) != 6:
        return {"ok": False, "detail": "6 位验证码只填数字，共 6 位"}
    r = _call("/web_api/sns/v1/live/obs/push_url", {"code": code})
    if not r["ok"]:
        return r

    d = r["data"] or {}
    main = d.get("mainPushInfo") or {}
    key = str(main.get("streamKey") or "").strip()
    if not key:
        # 字段名变了就退一步，试着找带 push 的字符串
        for k, v in main.items():
            if isinstance(v, str) and "txSecret" in v:
                key = v
                break
    if not key:
        return {"ok": False,
                "detail": "接口没返回串流密钥，字段：%s" % "、".join(sorted(main))}

    logbus.ok("小红书推流地址已获取", tag="小红书")
    return {"ok": True, "stream_key": key,
            "url": PUSH_SERVER.rstrip("/") + "/" + key.lstrip("/")}


def living_push_url() -> dict:
    """正在直播时，直接拿当前这一场的地址。"""
    r = _call("/web_api/sns/v1/live/obs/living_push_url")
    if not r["ok"]:
        return r
    d = r["data"] or {}
    if not d.get("living"):
        return {"ok": False, "detail": "当前没有在直播"}
    main = d.get("mainPushInfo") or {}
    key = str(main.get("streamKey") or "").strip()
    if not key:
        return {"ok": False, "detail": "在直播，但没拿到密钥"}
    return {"ok": True, "stream_key": key,
            "url": PUSH_SERVER.rstrip("/") + "/" + key.lstrip("/")}
