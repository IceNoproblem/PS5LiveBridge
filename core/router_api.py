from __future__ import annotations

import json
import urllib.request
import urllib.parse

from . import logbus

PUSH_DOMAINS = [
    "live-push.bilivideo.com",
    "live-push-tx.bilivideo.com",
    "live-push-ks.bilivideo.com",
    "live-push-hs.bilivideo.com",
    "broadcastlv.chat.bilibili.com",
    "contribute.live-video.net",
    "global-contribute.live-video.net",
]


class OpenWrtLUCI:
    """OpenWrt LUCI API 客户端。"""

    def __init__(self, host: str = "192.168.1.1", username: str = "root",
                 password: str = ""):
        self._host = host
        self._username = username
        self._password = password
        self._cookie = ""
        self._token = ""

    def _request(self, path: str, data: dict = None, method: str = "GET") -> dict:
        url = "http://%s/cgi-bin/luci/%s" % (self._host, path)
        headers = {"Content-Type": "application/json"}
        if self._cookie:
            headers["Cookie"] = self._cookie
        try:
            if method == "GET" and data:
                url += "?" + urllib.parse.urlencode(data)
                req = urllib.request.Request(url, headers=headers)
            else:
                body = json.dumps(data).encode() if data else b""
                req = urllib.request.Request(url, data=body, headers=headers,
                                           method=method)
            with urllib.request.urlopen(req, timeout=5) as r:
                result = json.loads(r.read().decode())
                for h in r.headers.get_all("Set-Cookie") or []:
                    if "sysauth" in h:
                        self._cookie = h.split(";")[0]
                return result
        except Exception as e:
            return {"error": str(e)}

    def login(self) -> bool:
        """登录 LUCI。"""
        result = self._request("auth", data={
            "username": self._username,
            "password": self._password,
        }, method="POST")
        if result.get("result") or result.get("token"):
            self._token = result.get("token", "")
            logbus.ok("LUCI 登录成功", tag="路由器")
            return True
        logbus.warn("LUCI 登录失败: %s" % result.get("error", ""), tag="路由器")
        return False

    def set_hosts(self, target_ip: str, domains: list = None) -> bool:
        """配置 hosts（推流域名 → target_ip）。"""
        if not domains:
            domains = PUSH_DOMAINS
        entries = []
        for domain in domains:
            entries.append("%s %s" % (target_ip, domain))
        hosts_content = "\n".join(entries) + "\n"
        result = self._request("sys/fs/write", data={
            "path": "/etc/hosts",
            "content": hosts_content,
        }, method="POST")
        if result.get("result"):
            logbus.ok("路由器 hosts 已配置（%d 个域名 → %s）" % (len(domains), target_ip),
                      tag="路由器")
            return True
        logbus.warn("hosts 配置失败: %s" % result.get("error", ""), tag="路由器")
        return False

    def clear_hosts(self) -> bool:
        """清空自定义 hosts。"""
        result = self._request("sys/fs/write", data={
            "path": "/etc/hosts",
            "content": "",
        }, method="POST")
        return result.get("result", False)

    def get_status(self) -> dict:
        """获取路由器状态。"""
        return self._request("sys/status")


def detect_router(ip: str = "192.168.1.1") -> str:
    """检测路由器类型。返回 "openwrt" / "unknown"。"""
    try:
        req = urllib.request.Request(
            "http://%s/cgi-bin/luci/" % ip,
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=3) as r:
            html = r.read().decode("utf-8", errors="ignore")
        if "luci" in html.lower() or "openwrt" in html.lower():
            return "openwrt"
    except Exception:
        pass
    return "unknown"


def quick_setup(target_ip: str, router_ip: str = "192.168.1.1",
                username: str = "root", password: str = "") -> bool:
    """
    快速配置路由器 hosts。
    自动检测路由器类型，配置推流域名。
    """
    router_type = detect_router(router_ip)
    if router_type != "openwrt":
        logbus.warn("路由器类型不支持（%s），请手动配置 hosts" % router_type, tag="路由器")
        return False
    luci = OpenWrtLUCI(router_ip, username, password)
    if not luci.login():
        return False
    return luci.set_hosts(target_ip)
