"""
网关探测：访问路由器后台网页，取标题，猜它是什么路由器、能不能改 hosts。

为什么值得做：三个方案里只有方案一（路由器 hosts）需要路由器支持自定义 hosts，
而支持与否直接决定用户该走哪条路。与其让人自己去后台翻半天，不如直接告诉他一
"你这台是小米 AX3000，没有 hosts，建议走方案二或三"。
"""
from __future__ import annotations

import re
import threading
import time
import urllib.request

from . import logbus, paths

_CACHE = {"at": 0.0, "key": "", "val": None, "ttl": 0.0}
_LOCK = threading.Lock()
TTL = 300.0
# 失败也缓存，但短一些：一次超时不该让后面每次打开自检页都再等一轮
FAIL_TTL = 60.0

# (关键词, 品牌, 型号提示, hosts 能力)
# hosts: "yes" 后台通常能改 / "hard" 有但很麻烦 / "no" 基本没有 / "oem" 光猫类
RULES = [
    ("openwrt", "OpenWrt", "", "yes"),
    ("luci", "OpenWrt", "", "yes"),
    ("asuswrt-merlin", "华硕 · 梅林固件", "", "yes"),
    ("merlin", "华硕 · 梅林固件", "", "yes"),
    ("asus", "华硕", "", "yes"),
    ("rt-ax", "华硕", "RT-AX 系列", "yes"),
    ("rt-ac", "华硕", "RT-AC 系列", "yes"),
    ("tuf-ax", "华硕", "TUF 系列", "yes"),
    ("netgear", "网件 NETGEAR", "", "hard"),
    ("nighthawk", "网件 夜鹰", "", "hard"),
    ("orbi", "网件 Orbi", "", "hard"),
    ("linksys", "领势 Linksys", "", "hard"),
    ("velop", "领势 Velop", "", "hard"),
    ("xiaomi", "小米", "", "hard"),
    ("miwifi", "小米", "", "hard"),
    ("小米路由器", "小米", "", "hard"),
    ("redmi", "红米", "", "hard"),
    ("honor", "荣耀", "", "hard"),
    ("huawei", "华为", "", "hard"),
    ("华为", "华为", "", "hard"),
    ("ws5200", "华为", "WS5200", "hard"),
    ("hg8245", "华为光猫", "HG8245", "oem"),
    ("echolife", "华为光猫", "", "oem"),
    ("tp-link", "TP-Link", "", "no"),
    ("tplink", "TP-Link", "", "no"),
    ("archer", "TP-Link Archer", "", "no"),
    ("mercury", "水星 MERCURY", "", "no"),
    ("tenda", "腾达 Tenda", "", "no"),
    ("netcore", "磊科 netcore", "", "no"),
    ("fast", "迅捷 FAST", "", "no"),
    ("h3c", "H3C 华三", "", "no"),
    ("magic", "H3C Magic", "", "no"),
    ("ruijie", "锐捷 Ruijie", "", "no"),
    ("reyee", "锐捷睿易", "", "no"),
    ("zte", "中兴 ZTE", "", "no"),
    ("中兴", "中兴 ZTE", "", "no"),
    ("ikuai", "爱快 iKuai", "", "yes"),
    ("爱快", "爱快 iKuai", "", "yes"),
    ("phicomm", "斐讯", "", "no"),
    ("hiwifi", "极路由", "", "no"),
    ("jdcloud", "京东云", "", "no"),
    ("unifi", "Ubiquiti UniFi", "", "hard"),
    ("ubiquiti", "Ubiquiti", "", "hard"),
    ("pgy", "蒲公英", "", "no"),
    ("tplogin", "TP-Link", "", "no"),
    ("miwifi.com", "小米", "", "hard"),
    ("routerlogin", "网件 NETGEAR", "", "hard"),
    ("router.asus.com", "华硕", "", "yes"),
    # 光猫 / 运营商
    ("天翼", "电信光猫", "", "oem"),
    ("e8-c", "电信光猫", "", "oem"),
    ("epon", "光猫 (EPON)", "", "oem"),
    ("gpon", "光猫 (GPON)", "", "oem"),
    ("家庭网关", "运营商光猫", "", "oem"),
    ("智能网关", "运营商网关", "", "oem"),
    ("中国移动", "移动光猫", "", "oem"),
    ("中国联通", "联通光猫", "", "oem"),
    ("中国电信", "电信光猫", "", "oem"),
]

ADVICE = {
    "yes": "后台一般能改 hosts，走方案一最省事（配一次长期有效，PS5 什么都不用改）",
    "hard": "能改但要开 SSH 或刷固件，普通人不好弄。建议走方案二（内置 DNS）或方案三（流量接管）",
    "no": "这类家用路由器基本没有 hosts 功能。建议走方案二（内置 DNS）或方案三（流量接管）",
    "oem": "运营商光猫后台功能被锁，改不了 hosts。建议走方案二或方案三",
    "": "没认出来是什么型号，按方案二或方案三走更稳",
}

# 容器版做不了方案二/三（要碰本机网络栈），只能靠路由器 hosts 这一条路，
# 所以给的建议必须换一套，不能照着桌面版的话术把人往死路上带。
ADVICE_DOCKER = {
    "yes": "后台能改 hosts，容器版正好靠这个，把域名指到这台 NAS 就行",
    "hard": "能改但要开 SSH 或刷固件，不好弄。容器版只有路由器这条路，"
            "搞不定就得换台支持自定义 hosts 的路由器",
    "no": "这类路由器没有 hosts 功能，容器版走不通。换台支持自定义 hosts 的路由器，"
          "或在路由器上用「DNS 静态解析」把域名指到这台 NAS",
    "oem": "运营商光猫后台被锁，改不了 hosts。容器版走不通。"
           "换台路由器，或在光猫后面接一台能改 hosts 的路由器",
    "": "没认出来是什么型号。容器版要靠路由器 hosts，先确认后台能不能加自定义解析"
        "（有些型号叫「DNS 静态解析」或「本地域名」）",
}


def advice_for(cap: str) -> str:
    """按当前形态给建议：桌面版可以换方案，容器版只能修路由器。"""
    if paths.PLATFORM == "docker":
        return ADVICE_DOCKER.get(cap, ADVICE_DOCKER[""])
    return ADVICE.get(cap, ADVICE[""])


def _fetch_title(ip: str, https: bool = False, timeout: float = 1.8) -> str:
    scheme = "https" if https else "http"
    url = "%s://%s/" % (scheme, ip)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "text/html,*/*",
    })
    ctx = None
    if https:
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        raw = r.read(20000)
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            text = raw.decode(enc, "ignore")
            break
        except Exception:
            continue
    else:
        return ""
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    if not m:
        return ""
    return re.sub(r"\s+", " ", m.group(1)).strip()[:120]


def probe(gateway: str, force: bool = False) -> dict:
    """
    探测网关。返回 {reachable, title, brand, model, hosts, advice}。
    结果缓存 5 分钟 —— 每次诊断都去连一次路由器太慢。
    """
    out = {"reachable": False, "title": "", "brand": "", "model": "",
           "hosts": "", "advice": advice_for(""), "gateway": gateway}
    if not gateway:
        return out

    with _LOCK:
        if (not force and _CACHE["val"] is not None
                and _CACHE["key"] == gateway
                and time.time() - _CACHE["at"] < (_CACHE["ttl"] or TTL)):
            return dict(_CACHE["val"])

    title = ""
    err = ""
    for https in (False, True):
        try:
            title = _fetch_title(gateway, https=https)
            if title:
                break
        except Exception as e:
            err = str(e)
            continue

    if title:
        out["reachable"] = True
        out["title"] = title
        low = title.lower()
        for kw, brand, model, cap in RULES:
            if kw and kw.lower() in low:
                out["brand"] = brand
                out["model"] = model
                out["hosts"] = cap
                out["advice"] = advice_for(cap)
                break
        if not out["brand"]:
            out["advice"] = "后台页面能打开（%s），但型号没认出来。%s" % (
                title, advice_for(""))
    else:
        if paths.PLATFORM == "docker":
            out["advice"] = ("访问网关页面没拿到标题，可能后台只认 HTTPS 或需要登录。"
                             "容器版要靠路由器 hosts，先去路由器后台找「hosts」"
                             "或「DNS 静态解析」设置")
        else:
            out["advice"] = ("访问网关页面没拿到标题，可能后台只认 HTTPS 或需要登录。"
                             "按方案二或方案三走更稳")
        if err:
            out["error"] = err

    # 连通的缓存 5 分钟；连不通的也缓存 1 分钟 —— 一次超时不该让后面每次
    # 打开自检页都再等一轮（诊断接口是页面一加载就调的）。
    with _LOCK:
        _CACHE.update(at=time.time(), key=gateway, val=dict(out),
                      ttl=TTL if out["reachable"] else FAIL_TTL)
    return out
