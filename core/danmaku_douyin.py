"""
抖音弹幕。

和斗鱼不一样，抖音这道门上有两把锁：

· 要先把 web_rid（直播间链接后面那串数字）换成内部 room_id，这个接口要
  a_bogus 签名；
· WebSocket 握手要 signature，还要带 ttwid 这颗 cookie，缺一个都是
  200 而不给升级。

两把锁都是纯算法（见 douyin_sign.py），不需要登录，也不需要浏览器。

匿名身份下昵称会被打码（例如「茶***」），弹幕正文是完整的。想让昵称完整，
在界面里填一次抖音网页的 cookie 就行：接口和握手都换成登录态，
cookie 存在 data 目录，不写日志、不回界面。

消息统一成和 B 站一样的结构，交给 orchestrator 的 _on_danmaku 处理。
"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import random
import re
import ssl
import time
from urllib.parse import quote

import aiohttp

from . import douyin_sign as sign
from . import logbus, paths

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36")
HOME = "https://live.douyin.com/"
ENTER = "https://live.douyin.com/webcast/room/web/enter/"
WS_URL = "wss://webcast100-ws-web-lf.douyin.com/webcast/im/push/v2/"
COOKIE_FILE = "douyin_cookie.json"
HB_FRAME = b"\x3a\x02hb"          # PushFrame{payload_type:"hb"}
HB_SEC = 10
MAX_FRAME = 8 << 20               # 单帧上限，解压后也按这个卡
RETRY_MAX = 30
OFFLINE_WAIT = 30                 # 没开播时的重试间隔


# ── protobuf 小解码器 ───────────────────────────────────
# 只解我们关心的字段，不引第三方 protobuf 库：省依赖，也免得版本打架
def pb_fields(buf: bytes):
    """返回 [(字段号, 线型, 值)]，0 型给 int，2 型给 bytes。"""
    out = []
    i = 0
    n = len(buf)
    while i < n:
        key = 0
        shift = 0
        while i < n:
            b = buf[i]
            i += 1
            key |= (b & 0x7F) << shift
            shift += 7
            if not b & 0x80:
                break
        field, wire = key >> 3, key & 7
        if wire == 0:
            v = 0
            shift = 0
            while i < n:
                b = buf[i]
                i += 1
                v |= (b & 0x7F) << shift
                shift += 7
                if not b & 0x80:
                    break
            out.append((field, 0, v))
        elif wire == 2:
            ln = 0
            shift = 0
            while i < n:
                b = buf[i]
                i += 1
                ln |= (b & 0x7F) << shift
                shift += 7
                if not b & 0x80:
                    break
            out.append((field, 2, buf[i:i + ln]))
            i += ln
        elif wire == 5:
            out.append((field, 5, buf[i:i + 4]))
            i += 4
        elif wire == 1:
            out.append((field, 1, buf[i:i + 8]))
            i += 8
        else:
            break
    return out


def pb_bytes(buf: bytes, field: int) -> bytes:
    for f, w, v in pb_fields(buf):
        if f == field and w == 2:
            return v
    return b""


def pb_int(buf: bytes, field: int, default: int = 0) -> int:
    for f, w, v in pb_fields(buf):
        if f == field and w == 0:
            return v
    return default


def pb_str(buf: bytes, field: int, default: str = "") -> str:
    v = pb_bytes(buf, field)
    return v.decode("utf-8", "ignore") if v else default


def pb_varint_field(field: int, n: int) -> bytes:
    out = bytearray()
    key = field << 3
    while True:
        b = key & 0x7F
        key >>= 7
        if key:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def pb_bytes_field(field: int, data: bytes) -> bytes:
    out = bytearray()
    key = (field << 3) | 2
    while True:
        b = key & 0x7F
        key >>= 7
        if key:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    ln = len(data)
    while True:
        b = ln & 0x7F
        ln >>= 7
        if ln:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out) + data


# ── 登录信息（可选） ────────────────────────────────────
def _cookie_path() -> str:
    return os.path.join(paths.DATA_DIR, COOKIE_FILE)


def load_cookie() -> str:
    try:
        with open(_cookie_path(), "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("cookie") or ""
    except Exception:
        return ""


def save_cookie(raw) -> str:
    """
    存抖音网页的 cookie。

    粘进来的可能带 "Cookie:" 前缀、可能带换行，先洗干净再存。
    只落在 data 目录，不进日志、不回界面，也不进配置文件。
    """
    s = (raw or "").strip()
    if s.lower().startswith("cookie:"):
        s = s[7:].strip()
    s = " ".join(s.split())
    try:
        os.makedirs(paths.DATA_DIR, exist_ok=True)
        with open(_cookie_path(), "w", encoding="utf-8") as f:
            json.dump({"cookie": s, "saved_at": int(time.time())}, f)
        try:
            os.chmod(_cookie_path(), 0o600)
        except Exception:
            pass
    except Exception as e:
        logbus.warn("抖音登录信息保存失败：%s" % e, tag="弹幕")
        return ""
    if s:
        logbus.info("抖音登录信息已保存（%d 个字段）" % len(s.split(";")),
                    tag="弹幕")
    return s


def cookie_names(raw: str) -> list:
    out = []
    for seg in (raw or "").split(";"):
        if "=" in seg:
            out.append(seg.split("=", 1)[0].strip())
    return [x for x in out if x]


# ── 房间号解析 ──────────────────────────────────────────
_RID_RE = re.compile(r"(?:live\.douyin\.com/)?(\d{6,})")
# 昵称字段：抖音 User 消息里 nickname 在 3，早期资料写 2 都不对，
# 这里两个都试，取到哪个算哪个
_NICK_FIELDS = (3, 2)


def parse_room(text: str) -> str:
    """用户填的可能是纯数字，也可能是整条直播间链接。"""
    s = (text or "").strip()
    if not s:
        return ""
    m = _RID_RE.search(s)
    return m.group(1) if m else s


def _nickname(user: bytes) -> str:
    for f in _NICK_FIELDS:
        v = pb_str(user, f)
        if v:
            return v
    return ""


def to_item(method: str, body: bytes):
    """把抖音的业务消息揉成和 B 站一样的结构，不认识的返回 None。"""
    if method == "WebcastChatMessage":
        text = pb_str(body, 3).strip()
        if not text:
            return None
        return {"kind": "danmaku", "user": _nickname(pb_bytes(body, 2)),
                "text": text, "num": 1, "price": 0}

    if method == "WebcastEmojiChatMessage":
        # 表情弹幕：正文在 default_content，emoji_content 是图片，没有文字就跳过
        text = pb_str(body, 5).strip()
        if not text:
            return None
        return {"kind": "danmaku", "user": _nickname(pb_bytes(body, 2)),
                "text": text, "num": 1, "price": 0}

    if method == "WebcastGiftMessage":
        num = pb_int(body, 5, 1) or 1     # repeat_count
        name = _gift_name(pb_bytes(body, 15), body)
        return {"kind": "gift", "user": _nickname(pb_bytes(body, 7)),
                "text": "%s x%d" % (name or "礼物", num), "num": num,
                "price": 0}
    return None


def _gift_name(gift: bytes, body: bytes = b"") -> str:
    """
    礼物名字。

    已知字段是 name(16) 和 describe(2)，但抖音改过这层编号，实测见过两者都空、
    最后只显示「礼物」的情况。所以改成打分挑：在所有字符串字段里找最像礼物名的，
    礼物子消息找不到就看外层。字段编号命中只是加分，不是唯一依据。
    2026-11-19 加固，抖音礼物帧仍稀有，没拿到真实样本反复校验。
    """
    def score(s: str, field: int) -> int:
        if not s or len(s) > 24 or not s.isprintable():
            return -1
        if s.lower().startswith(("http", "rtmp")) or s.startswith("//"):
            return -1
        if s.isdigit() or set(s) <= {"0"}:
            return -1
        n = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")
        v = 1 + (3 if n else 0) + (2 if 2 <= len(s) <= 12 else 0)
        if field in (16, 2):
            v += 4
        elif field in (3, 4, 5, 6, 7):
            v += 1
        return v

    best, best_score = "", 0
    for source, bonus in ((gift, 0), (body, -3)):
        for f, w, raw in pb_fields(source):
            if w != 2 or f in (1, 15, 21):   # 这几个是图片地址
                continue
            s = raw.decode("utf-8", "ignore").strip()
            if s.startswith("送") and len(s) > 1 and f == 2:
                s = s[1:]
            sc = score(s, f)
            if sc > 0:
                sc += bonus
                if sc > best_score:
                    best, best_score = s, sc
    return best


class DouyinDanmaku(object):
    """抖音弹幕客户端，接口和斗鱼那个对齐。"""

    def __init__(self, on_message=None, on_state=None):
        self.on_message = on_message
        self.on_state = on_state
        self.web_rid = ""          # 用户填的那个
        self.real_room = ""        # 内部 room_id
        self.anchor = ""           # 主播昵称，日志里好认
        self.connected = False
        self.running = False
        self.count = 0
        self.last_at = 0.0
        self.last_error = ""
        self.state = "idle"
        self._task = None
        self._hb = None
        self._ws = None
        self._sess = None
        self._cookie = ""
        self.user_cookie = ""
        self.logged_in = False
        self._retry = 0
        self.retry = 0          # 连续失败次数，界面显示
        self.retry_wait = 0     # 距下次重连还有几秒

    # ── 生命周期 ──

    async def start(self, room) -> bool:
        await self.stop()
        self.web_rid = parse_room(room)
        if not self.web_rid:
            return False
        self.running = True
        self._retry = 0
        self.retry = 0
        self.retry_wait = 0
        self.last_error = ""
        self._task = asyncio.ensure_future(self._loop())
        return True

    async def stop(self):
        self.running = False
        for t in (self._hb, self._task):
            if t:
                t.cancel()
        self._hb = None
        self._task = None
        await self._close_ws()
        if self._sess is not None:
            try:
                await self._sess.close()
            except Exception:
                pass
            self._sess = None
        if self.connected:
            self.connected = False
            self._emit("closed")

    async def _close_ws(self):
        ws, self._ws = self._ws, None
        if ws:
            try:
                await ws.close()
            except Exception:
                pass

    def _emit(self, state: str):
        self.state = state
        if self.on_state:
            try:
                self.on_state(state)
            except Exception:
                pass

    # ── 主循环 ──

    async def _loop(self):
        self._emit("connecting")
        # 每轮重连都重新读一次，界面上刚保存的登录信息不用重启程序
        self.user_cookie = load_cookie()
        self._cookie = ""
        self.logged_in = bool(self.user_cookie)
        self._sess = aiohttp.ClientSession()
        try:
            await self._ensure_cookie()
            while self.running:
                try:
                    if not await self._resolve():
                        # 没开播不是错误，隔一会儿再看
                        await asyncio.sleep(OFFLINE_WAIT)
                        continue
                    await self._connect()
                    await self._receive()
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    if self.running:
                        self.last_error = str(e)
                        logbus.warn("抖音弹幕连接中断：%s" % e, tag="弹幕")
                finally:
                    self.connected = False
                    if self._hb:
                        self._hb.cancel()
                        self._hb = None
                    await self._close_ws()

                if not self.running:
                    return
                self._retry += 1
                self.retry = self._retry
                self.retry_wait = min(RETRY_MAX, 2 ** min(self._retry, 5))
                self._emit("reconnecting")
                await asyncio.sleep(self.retry_wait)
                self.retry_wait = 0
        except asyncio.CancelledError:
            pass
        finally:
            self._emit("closed")

    async def _ensure_cookie(self):
        """
        备好这颗 cookie。

        匿名时只需要 ttwid：没有它握手会被拒（返回 200 不给升级），
        而且这颗有效期很长，取一次就够。用户填了登录信息就以他的为主，
        缺 ttwid 再补一颗。
        """
        if self._cookie:
            return
        jar = {}
        try:
            async with self._sess.get(HOME, headers={"User-Agent": UA},
                                      timeout=aiohttp.ClientTimeout(total=12)) as r:
                await r.read()
            for c in self._sess.cookie_jar:
                jar[c.key] = c.value
        except Exception as e:
            logbus.warn("抖音首页打不开，昵称和房间信息可能取不到：%s" % e, tag="弹幕")

        for seg in (self.user_cookie or "").split(";"):
            if "=" in seg:
                k, v = seg.split("=", 1)
                k = k.strip()
                if k:
                    jar[k] = v.strip()

        self._cookie = "; ".join("%s=%s" % (k, v) for k, v in jar.items())
        if self.user_cookie:
            logbus.ok("抖音使用登录态（%d 个字段）" % len(jar), tag="弹幕")
        else:
            logbus.debug("抖音匿名连接（%d 个字段）" % len(jar), tag="弹幕")

    def _enter_params(self) -> str:
        return ("aid=6383&app_name=douyin_web&live_id=1&device_platform=web"
                "&language=zh-CN&enter_from=link_share&cookie_enabled=true"
                "&screen_width=1920&screen_height=1080&browser_language=zh-CN"
                "&browser_platform=Win32&browser_name=Chrome&browser_version=133.0.0.0"
                "&os_name=Windows&os_version=10&web_rid=%s&room_id_str="
                "&enter_source=&is_need_double_stream=false&insert_task_id="
                "&live_reason=&msToken=" % self.web_rid)

    async def _resolve(self) -> bool:
        """web_rid → 内部 room_id。顺便看一眼有没有开播。"""
        params = self._enter_params()
        url = ENTER + "?" + params + "&a_bogus=" + quote(
            sign.ab_sign(params, UA), safe="")
        async with self._sess.get(url, headers={
            "User-Agent": UA, "Cookie": self._cookie,
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "identity",
            "Referer": HOME + self.web_rid,
        }, timeout=aiohttp.ClientTimeout(total=12)) as r:
            body = await r.text()
        if not body.strip():
            raise RuntimeError("房间信息接口返回空（签名或 UA 被拒）")
        try:
            d = json.loads(body)
        except Exception:
            raise RuntimeError("房间信息不是 JSON，可能被风控拦了")
        rooms = (d.get("data") or {}).get("data") or []
        if not rooms:
            self._emit("offline")
            logbus.warn("抖音直播间 %s 不存在或已下播" % self.web_rid, tag="弹幕")
            return False
        rm = rooms[0]
        rid = str(rm.get("id_str") or "")
        if not rid:
            return False
        status = rm.get("status")
        self.real_room = rid
        self.anchor = ((rm.get("owner") or {}).get("nickname") or "").strip()
        if status not in (2, None):
            self._emit("offline")
            logbus.info("抖音「%s」还没开播，等它开" % (self.anchor or self.web_rid),
                        tag="弹幕")
            return False
        return True

    def _ws_query(self, uid: str) -> str:
        now = int(time.time() * 1000)
        ext = ("internal_src:dim|wss_push_room_id:%s|wss_push_did:%s"
               "|first_req_ms:%d|fetch_time:%d|seq:1|wss_info:0-%d-0-0"
               "|wrds_v:7382620942951772256" % (self.real_room, uid, now, now, now))
        sig = sign.ws_signature(sign.x_ms_stub(self.real_room, uid))
        parts = [
            "app_name=douyin_web", "version_code=180800",
            "webcast_sdk_version=1.0.15", "update_version_code=1.0.15",
            "compress=gzip", "device_platform=web", "cookie_enabled=true",
            "screen_width=1920", "screen_height=1080",
            "browser_language=zh-CN", "browser_platform=Win32",
            "browser_name=Mozilla",
            "browser_version=" + quote(UA.split("Mozilla/", 1)[1]),
            "browser_online=true", "tz_name=Asia/Shanghai",
            "cursor=", "internal_ext=" + quote(ext, safe=":|-"),
            "host=https://live.douyin.com", "aid=6383", "live_id=1",
            "did_rule=3", "endpoint=live_pc", "support_wrds=1",
            "user_unique_id=" + uid, "im_path=/webcast/im/fetch/",
            "identity=audience", "need_persist_msg_count=0",
            "insert_task_id=", "live_reason=", "room_id=" + self.real_room,
            "heartbeatDuration=0", "signature=" + sig,
        ]
        return "&".join(parts)

    async def _connect(self):
        uid = str(random.randint(10 ** 18, 10 ** 19 - 1))
        try:
            self._ws = await asyncio.wait_for(self._sess.ws_connect(
                WS_URL + "?" + self._ws_query(uid),
                headers={"User-Agent": UA, "Cookie": self._cookie,
                         "Origin": "https://live.douyin.com", "Referer": HOME},
                ssl=ssl.create_default_context(),
                heartbeat=None, max_msg_size=0,
            ), timeout=12)
        except Exception as e:
            self.last_error = "握手失败：%s" % e
            raise RuntimeError(self.last_error)

        self.connected = True
        self._retry = 0
        self.retry = 0
        self.retry_wait = 0
        self.last_error = ""
        self._emit("open")
        logbus.ok("抖音弹幕已连接（%s%s）"
                  % (self.anchor or "直播间", " " + self.web_rid),
                  tag="弹幕")
        self._hb = asyncio.ensure_future(self._heartbeat())

    async def _heartbeat(self):
        while self.running and self.connected:
            await asyncio.sleep(HB_SEC)
            try:
                await self._ws.send_bytes(HB_FRAME)
            except Exception:
                return

    async def _receive(self):
        while self.running and self._ws is not None:
            try:
                m = await asyncio.wait_for(self._ws.receive(), timeout=HB_SEC + 30)
            except asyncio.TimeoutError:
                raise RuntimeError("太久没收到任何数据")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                raise RuntimeError("收包失败：%s" % e)

            if m.type == aiohttp.WSMsgType.BINARY:
                await self._on_frame(m.data)
            elif m.type in (aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR):
                raise RuntimeError("服务端关闭连接")

    async def _on_frame(self, frame: bytes):
        # 每条带 im-internal_ext 的帧都要回 ACK，不回服务端会慢慢断流
        logid = 0
        ext = b""
        for f, w, v in pb_fields(frame):
            if f == 2 and w == 0:
                logid = v
            elif f == 5 and w == 2 and pb_str(v, 1) == "im-internal_ext":
                ext = pb_str(v, 2).encode()
        if logid and ext:
            try:
                await self._ws.send_bytes(pb_varint_field(2, logid)
                                          + pb_bytes_field(7, b"ack")
                                          + pb_bytes_field(8, ext))
            except Exception:
                pass

        if pb_str(frame, 7) != "msg":
            return
        payload = pb_bytes(frame, 8)
        if not payload or len(payload) > MAX_FRAME:
            return
        try:
            payload = gzip.decompress(payload)
        except Exception:
            pass
        if len(payload) > MAX_FRAME * 4:
            return

        for f, w, v in pb_fields(payload):
            if f != 1 or w != 2:
                continue
            method = pb_str(v, 1)
            body = pb_bytes(v, 2)
            if not body:
                continue
            item = to_item(method, body)
            if not item:
                continue
            self.count += 1
            self.last_at = time.time()
            if self.on_message:
                try:
                    self.on_message(item)
                except Exception:
                    pass
