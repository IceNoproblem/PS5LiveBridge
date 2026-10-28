# -*- coding: utf-8 -*-
"""
core/bili.py —— B 站直播能力（扫码登录 / 开播 / 关播 / 分区 / 发弹幕）

来源与取舍：
    开播签名走「哔哩哔哩直播姬」的 appkey（公开且长期稳定），
    这是业内绕开网页端人脸验证的标准做法，bililivetool 与 PSLinkB 都在用。
    没有它，startLive 会直接返回 60024 要求人脸认证。

对外只暴露同步方法（内部用 requests），因为调用方是 Flask 线程和编排器线程，
不是事件循环 —— 免得再把 asyncio 搅进来。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.parse

import requests

from . import logbus, paths

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

BASE_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "zh-CN,zh;q=0.9",
    "user-agent": UA,
    "referer": "https://live.bilibili.com/",
    "origin": "https://live.bilibili.com",
}

# 直播姬客户端 headers —— startLive / stopLive 必须用这套，
# 混进 web headers（referer/origin/Chrome UA）会被当网页请求触发人脸验证。
APP_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "bili-live/4.0.0 Chrome/108.0.0.0 Safari/537.36",
    "content-type": "application/x-www-form-urlencoded",
    "x-requested-with": "tv.danmaku.bili",
    "env": "prod",
}

# 直播姬 appkey / appsec（公开值，所有第三方开播工具都用这一对）
APP_KEY = "aae92bc66f3edfab"
APP_SEC = "af125a0d5279fd576c1b4418a3e8276d"

# Electron 桌面端通道（web_electron_link）——不触发人脸验证的概率更高
ELECTRON_KEY = "aa1e74ee4874176e"
ELECTRON_SEC = "54e6a9a31b911cd5fc0daa66ebf94bc4"
ELECTRON_BUILD = "1001017006"

# 凭据落在可写目录：桌面版是 exe 同级，容器里是挂进来的 /data（重启不丢登录）
CRED_FILE = os.path.join(paths.DATA_DIR, "bili_credential.json")

# 开播失败码 → 人话
START_LIVE_ERRORS = {
    -101: "登录已过期，请重新扫码",
    -400: "请求参数错误",
    -403: "无权限开播（可能被限制直播）",
    -500: "B 站服务异常，稍后再试",
    60004: "直播间不存在或不属于该账号",
    60019: "直播分区不可用，换一个分区试试",
    60024: "需要人脸验证（本程序走直播姬通道，出现此项说明账号被强制要求）",
    60043: "需要人脸验证，请到 B 站 App 完成认证",
    60009: "直播被封禁",
    60056: "推流地址获取失败，请重试",
    10030: "分区未选择",
}

# wbi 打乱表
_MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


def _err(code, msg="") -> dict:
    return {"ok": False, "code": code, "msg": msg or START_LIVE_ERRORS.get(code, "未知错误")}


class Bilibili(object):
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(BASE_HEADERS)
        self.cookies = {}
        self.uid = 0
        self.uname = ""
        self.room_id = 0
        self._wbi_keys = ("", "")
        self._wbi_at = 0.0
        self._partitions = {}
        self.load_credential()

    # ──────────────────────────────────────────────
    # 凭据持久化
    # ──────────────────────────────────────────────
    def load_credential(self) -> bool:
        try:
            if not os.path.exists(CRED_FILE):
                return False
            with open(CRED_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return False
            ck = data.get("cookies") or {}
            if ck.get("SESSDATA"):
                self.cookies = ck
                self.uid = int(data.get("uid") or 0)
                self.uname = data.get("uname") or ""
                self.room_id = int(data.get("room_id") or 0)
                logbus.info("已载入上次的登录状态：%s" % (self.uname or "（未命名）"), tag="B站")
                return True
        except Exception as e:
            logbus.warn("读登录凭据失败：%s" % e, tag="B站")
        return False

    def save_credential(self) -> None:
        try:
            os.makedirs(os.path.dirname(CRED_FILE), exist_ok=True)
            payload = {
                "cookies": self.cookies,
                "uid": self.uid,
                "uname": self.uname,
                "room_id": self.room_id,
                "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            tmp = CRED_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CRED_FILE)
            try:
                os.chmod(CRED_FILE, 0o600)
            except Exception:
                pass
        except Exception as e:
            logbus.warn("保存登录凭据失败：%s" % e, tag="B站")

    def logout(self) -> None:
        self.cookies = {}
        self.uid = 0
        self.uname = ""
        self.room_id = 0
        try:
            if os.path.exists(CRED_FILE):
                os.remove(CRED_FILE)
        except Exception:
            pass

    @property
    def logged_in(self) -> bool:
        return bool(self.cookies.get("SESSDATA"))

    @property
    def csrf(self) -> str:
        return self.cookies.get("bili_jct", "")

    # ──────────────────────────────────────────────
    # 底层请求
    # ──────────────────────────────────────────────
    def _request(self, method, url, params=None, data=None, timeout=12) -> dict:
        try:
            if method.upper() == "GET":
                r = self.session.get(url, params=params, data=data,
                                     cookies=self.cookies, timeout=timeout)
            else:
                r = self.session.post(url, params=params, data=data,
                                      cookies=self.cookies, timeout=timeout)
            try:
                return r.json()
            except ValueError:
                return {"code": -1, "msg": "返回非 JSON：%s" % r.text[:120]}
        except Exception as e:
            return {"code": -1, "msg": "网络错误：%s" % str(e)[:120]}

    def _app_request(self, method, url, params=None, data=None, timeout=12) -> dict:
        """直播姬客户端通道请求：用 APP_HEADERS，不带 web referer/origin。"""
        headers = dict(APP_HEADERS)
        try:
            if method.upper() == "GET":
                r = requests.get(url, params=params, data=data,
                                 cookies=self.cookies, headers=headers,
                                 timeout=timeout)
            else:
                r = requests.post(url, params=params, data=data,
                                  cookies=self.cookies, headers=headers,
                                  timeout=timeout)
            try:
                return r.json()
            except ValueError:
                return {"code": -1, "msg": "返回非 JSON：%s" % r.text[:120]}
        except Exception as e:
            return {"code": -1, "msg": "网络错误：%s" % str(e)[:120]}

    def _appsign(self, params: dict) -> dict:
        """直播姬 APP 签名：排序 → urlencode → md5(query+secret)。"""
        params = dict(params)
        params["appkey"] = APP_KEY
        params = dict(sorted(params.items()))
        query = urllib.parse.urlencode(params)
        params["sign"] = hashlib.md5((query + APP_SEC).encode()).hexdigest()
        return params

    def _electronsign(self, params: dict) -> dict:
        """Electron 桌面端签名（不同 appkey/secret）。"""
        params = dict(params)
        params["appkey"] = ELECTRON_KEY
        params = dict(sorted(params.items()))
        query = urllib.parse.urlencode(params)
        params["sign"] = hashlib.md5((query + ELECTRON_SEC).encode()).hexdigest()
        return params

    # ──────────────────────────────────────────────
    # wbi 签名（弹幕信息、发弹幕需要）
    # ──────────────────────────────────────────────
    def _refresh_wbi(self, force=False) -> bool:
        if not force and self._wbi_keys[0] and time.time() - self._wbi_at < 1800:
            return True
        d = self._request("GET", "https://api.bilibili.com/x/web-interface/nav")
        try:
            wbi = d["data"]["wbi_img"]
            img = wbi["img_url"].rsplit("/", 1)[1].split(".")[0]
            sub = wbi["sub_url"].rsplit("/", 1)[1].split(".")[0]
            self._wbi_keys = (img, sub)
            self._wbi_at = time.time()
            return True
        except Exception:
            return False

    def _wbi_sign(self, params: dict) -> dict:
        if not self._refresh_wbi():
            return dict(params)
        img, sub = self._wbi_keys
        orig = img + sub
        mixin = "".join(orig[i] for i in _MIXIN_TAB if i < len(orig))[:32]
        params = dict(params)
        params["wts"] = round(time.time())
        params = dict(sorted(params.items()))
        params = {k: "".join(c for c in str(v) if c not in "!'()*")
                  for k, v in params.items()}
        query = urllib.parse.urlencode(params)
        params["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
        return params

    # ──────────────────────────────────────────────
    # 扫码登录
    # ──────────────────────────────────────────────
    def qr_generate(self) -> dict:
        d = self._request("GET", "https://passport.bilibili.com/x/passport-login/web/qrcode/generate")
        if d.get("code") == 0 and d.get("data"):
            return {"ok": True, "key": d["data"]["qrcode_key"], "url": d["data"]["url"]}
        return _err(d.get("code", -1), d.get("message") or d.get("msg") or "获取二维码失败")

    def qr_poll(self, key: str) -> dict:
        """
        返回 status: waiting / scanned / expired / success
        success 时顺手把 cookie、直播间号、昵称都填好。
        """
        try:
            r = self.session.get(
                "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
                params={"qrcode_key": key}, timeout=12)
            d = r.json()
        except Exception as e:
            return {"status": "error", "msg": str(e)[:100]}

        code = (d.get("data") or {}).get("code", -1)
        if code == 86090:
            return {"status": "scanned"}
        if code == 86038:
            return {"status": "expired"}
        if code != 0:
            return {"status": "waiting"}

        # 登录成功：把 cookie 收集起来（poll 的 Set-Cookie 是唯一来源）
        ck = r.cookies.get_dict()
        if not ck.get("SESSDATA"):
            for c in self.session.cookies:
                if c.name in ("SESSDATA", "bili_jct", "DedeUserID"):
                    ck[c.name] = c.value
        self.cookies = ck
        self.uid = int(ck.get("DedeUserID") or 0)

        # 补 buvid3（缺了会被风控拦）
        if not ck.get("buvid3"):
            spi = self._request("GET", "https://api.bilibili.com/x/frontend/finger/spi")
            if spi.get("code") == 0:
                self.cookies["buvid3"] = (spi.get("data") or {}).get("b_3", "")
                self.cookies["buvid4"] = (spi.get("data") or {}).get("b_4", "")

        nav = self._request("GET", "https://api.bilibili.com/x/web-interface/nav")
        if nav.get("code") == 0 and nav.get("data", {}).get("isLogin"):
            self.uname = nav["data"].get("uname", "")
            self.uid = int(nav["data"].get("mid") or self.uid)

        room = self.room_id_by_uid(self.uid)
        if room:
            self.room_id = room
        self.save_credential()
        logbus.ok("B 站登录成功：%s（直播间 %s）" % (self.uname or self.uid, self.room_id), tag="B站")
        return {
            "status": "success",
            "uid": self.uid,
            "uname": self.uname,
            "room_id": self.room_id,
        }

    def verify_login(self) -> bool:
        if not self.logged_in:
            return False
        d = self._request("GET", "https://api.bilibili.com/x/web-interface/nav")
        if d.get("code") == 0 and d.get("data", {}).get("isLogin"):
            self.uname = d["data"].get("uname", self.uname)
            self.uid = int(d["data"].get("mid") or self.uid)
            if not self.room_id:
                self.room_id = self.room_id_by_uid(self.uid) or 0
            return True
        return False

    def room_id_by_uid(self, uid: int) -> int:
        if not uid:
            return 0
        d = self._request(
            "GET", "https://api.live.bilibili.com/room/v2/Room/room_id_by_uid",
            params={"uid": uid})
        if d.get("code") == 0:
            return int((d.get("data") or {}).get("room_id") or 0)
        return 0

    def room_info(self) -> dict:
        if not self.room_id:
            return {}
        d = self._request("GET", "https://api.live.bilibili.com/room/v1/Room/get_info",
                          params={"room_id": self.room_id})
        if d.get("code") == 0:
            return d.get("data") or {}
        return {}

    # ──────────────────────────────────────────────
    # 分区
    # ──────────────────────────────────────────────
    def partitions(self) -> dict:
        if self._partitions:
            return self._partitions
        d = self._request("GET", "https://api.live.bilibili.com/room/v1/Area/getList",
                          params={"show_pinyin": 1})
        out = {}
        if d.get("code") == 0:
            for p in d.get("data") or []:
                subs = {s.get("name"): str(s.get("id")) for s in p.get("list") or []}
                out[p.get("name")] = subs
            self._partitions = out
        return out

    def area_id_by_name(self, parent: str, sub: str) -> str:
        return (self.partitions().get(parent) or {}).get(sub, "")

    def update_room(self, title: str = None, area_id: str = None) -> dict:
        if not self.logged_in or not self.room_id:
            return _err(-101)
        data = {"room_id": self.room_id, "platform": "pc_link",
                "csrf_token": self.csrf, "csrf": self.csrf}
        if title is not None:
            data["title"] = title
        if area_id is not None:
            data["area_id"] = area_id
        d = self._request("POST", "https://api.live.bilibili.com/room/v1/Room/update", data=data)
        if d.get("code") == 0:
            return {"ok": True}
        return _err(d.get("code", -1), d.get("message") or d.get("msg"))

    # ──────────────────────────────────────────────
    # 开播 / 关播
    # ──────────────────────────────────────────────
    def start_live(self, area_id: str = "236", title: str = "") -> dict:
        """
        开播成功返回：
            {ok, rtmp_addr, rtmp_code, srt_addr, srt_code}
        失败返回 {ok: False, code, msg, face_url?}
        """
        if not self.logged_in:
            return _err(-101, "未登录")
        if not self.room_id:
            self.room_id = self.room_id_by_uid(self.uid) or 0
        if not self.room_id:
            return _err(-1, "拿不到直播间号，请重新扫码登录")

        if title:
            r = self.update_room(title=title)
            if not r["ok"]:
                logbus.warn("改直播间标题失败（不影响开播）：%s" % r.get("msg"), tag="B站")

        # 1) 取服务器时间戳
        now = self._request("GET", "https://api.bilibili.com/x/report/click/now")
        if now.get("code") != 0:
            return _err(-1, "取服务器时间失败：%s" % now.get("msg"))
        ts = (now.get("data") or {}).get("now") or int(time.time())

        # 2) 取直播姬版本号（不带这个会被判客户端过期）
        ver = self._app_request(
            "GET",
            "https://api.live.bilibili.com/xlive/app-blink/v1/liveVersionInfo/getHomePageLiveVersion",
            params=self._appsign({"system_version": 2, "ts": ts}))
        if ver.get("code") != 0:
            return _err(ver.get("code", -1), "取直播姬版本失败：%s" % ver.get("msg"))
        vdata = ver.get("data") or {}

        # 3) 开播——双通道：先 Electron（不触发人脸概率高），不行再直播姬
        # 关键：60024/60043（人脸）不换通道，直接返回等验证；只有其他错误才降级
        for channel in ("electron", "livehime"):
            if channel == "electron":
                payload = {
                    "room_id": self.room_id,
                    "platform": "web_electron_link",
                    "area_v2": area_id or "236",
                    "csrf": self.csrf,
                    "csrf_token": self.csrf,
                    "build": ELECTRON_BUILD,
                    "ts": ts,
                }
                d = self._app_request("POST", "https://api.live.bilibili.com/room/v1/Room/startLive",
                                      data=self._electronsign(payload))
            else:
                payload = {
                    "room_id": self.room_id,
                    "platform": "pc_link",
                    "area_v2": area_id or "236",
                    "backup_stream": "0",
                    "csrf_token": self.csrf,
                    "csrf": self.csrf,
                    "build": vdata.get("build", 0),
                    "version": vdata.get("curr_version", ""),
                    "ts": ts,
                }
                d = self._app_request("POST", "https://api.live.bilibili.com/room/v1/Room/startLive",
                                      data=self._appsign(payload))

            code = d.get("code", -1)
            if code == 0:
                logbus.info("开播成功（通道：%s）" % channel, tag="B站")
                break
            # 人脸验证 → 直接返回，不换通道（换通道没用，验证是账号级的）
            if code in (60024, 60043):
                break
            # 其他错误 → 换通道降级
            if channel == "electron":
                logbus.info("Electron 通道失败（%s），降级直播姬…" % code, tag="B站")
                continue
            break

        if code != 0:
            msg = START_LIVE_ERRORS.get(code) or d.get("message") or d.get("msg") or "开播失败"
            logbus.error("B 站开播失败（%s）：%s" % (code, msg), tag="B站")
            face_url = ""
            if code == 60024:
                face_url = ((d.get("data") or {}).get("qr") or "")
            elif code == 60043:
                uid = self.uid or ""
                face_url = ("https://www.bilibili.com/blackboard/live/face-auth-middle.html"
                            "?source_event=live&source=live&uid=%s" % uid)
            return {"ok": False, "code": code, "msg": msg, "face_url": face_url}

        data = d.get("data") or {}
        rtmp = data.get("rtmp") or {}
        addr = rtmp.get("addr", "")
        key = rtmp.get("code", "")

        # RTMP 备用线路 + SRT
        alt = {"addr": "", "code": ""}
        srt = {"addr": "", "code": ""}
        for p in data.get("protocols") or []:
            proto = p.get("protocol")
            if proto == "rtmp" and not alt["addr"] and p.get("addr") and p.get("code"):
                alt = {"addr": p["addr"], "code": p["code"]}
            elif proto == "srt" and not srt["addr"] and p.get("addr") and p.get("code"):
                srt = {"addr": p["addr"], "code": p["code"]}

        if not addr or not key:
            return _err(-1, "B 站没给推流地址（可能分区不可用）")

        logbus.ok("B 站已开播，推流地址已获取", tag="B站")
        return {
            "ok": True,
            "room_id": self.room_id,
            "rtmp_addr": addr,
            "rtmp_code": key,
            "rtmp_alt_addr": alt["addr"],
            "rtmp_alt_code": alt["code"],
            "srt_addr": srt["addr"],
            "srt_code": srt["code"],
        }

    def stop_live(self) -> dict:
        if not self.logged_in or not self.room_id:
            return _err(-101)
        d = self._app_request("POST", "https://api.live.bilibili.com/room/v1/Room/stopLive",
                              data=self._appsign({"room_id": self.room_id, "platform": "pc_link",
                                                  "csrf_token": self.csrf, "csrf": self.csrf}))
        if d.get("code") == 0:
            logbus.info("B 站已关播", tag="B站")
            return {"ok": True}
        return _err(d.get("code", -1), d.get("message") or d.get("msg") or "关播失败")

    def check_face_status(self) -> str:
        """
        查询人脸验证状态。
        返回 "verified" / "pending" / "error:xxx"
        用 IsUserIdentityAuth 接口，带 face_auth_code=60024。
        """
        if not self.logged_in or not self.room_id:
            return "error:未登录"
        d = self._app_request(
            "POST",
            "https://api.live.bilibili.com/xlive/app-blink/v1/preLive/IsUserIdentityAuth",
            data=self._appsign({
                "room_id": self.room_id,
                "face_auth_code": "60024",
                "csrf_token": self.csrf,
                "csrf": self.csrf,
                "visit_id": "",
            }))
        if d.get("code") != 0:
            return "error:%s" % (d.get("message") or d.get("msg") or "查询失败")
        data = d.get("data") or {}
        if data.get("is_identified") is True:
            return "verified"
        return "pending"

    def live_status(self) -> int:
        """
        0 未开播 / 1 直播中 / 2 轮播
        用来确认「推流真的到 B 站了」——这是开播成功的唯一硬证据。
        """
        info = self.room_info()
        try:
            return int(info.get("live_status", 0))
        except Exception:
            return 0

    # ──────────────────────────────────────────────
    # 弹幕
    # ──────────────────────────────────────────────
    def danmu_info(self) -> dict:
        params = self._wbi_sign({"id": self.room_id, "type": 0})
        d = self._request(
            "GET", "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo",
            params=params)
        if d.get("code") == 0:
            return d.get("data") or {}
        if d.get("code") == -352:
            self._refresh_wbi(force=True)
        logbus.warn("获取弹幕服务器失败：%s %s" % (d.get("code"), d.get("message")), tag="B站")
        return {}

    def send_danmu(self, text: str) -> dict:
        if not self.logged_in:
            return _err(-101, "未登录")
        params = self._wbi_sign({"web_location": 444.8})
        data = {
            "color": 16777215, "fontsize": 25, "mode": 1, "bubble": 0,
            "msg": text, "csrf_token": self.csrf, "csrf": self.csrf,
            "roomid": self.room_id, "rnd": int(time.time()),
        }
        d = self._request("POST", "https://api.live.bilibili.com/msg/send",
                          params=params, data=data)
        code = d.get("code", -1)
        if code == 0:
            return {"ok": True}
        mapping = {1003212: "超出长度限制", -101: "未登录", -400: "参数错误",
                   10031: "发送太频繁", 10030: "该房间禁止弹幕"}
        return _err(code, mapping.get(code) or d.get("message") or "发送失败")


# ──────────────────────────────────────────────────────────────
# 二维码图片
# ──────────────────────────────────────────────────────────────
def qr_png_data_url(text: str, box: int = 8) -> str:
    """把登录链接画成 PNG 的 data URL，前端直接 <img src>。"""
    try:
        import base64
        import io

        import qrcode
        qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M,
                           box_size=box, border=2)
        qr.add_data(text)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#0f1115", back_color="#ffffff")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as e:
        logbus.warn("生成二维码失败：%s" % e, tag="B站")
        return ""
