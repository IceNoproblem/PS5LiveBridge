"""
斗鱼弹幕。

协议要点（2026-11-18 实测，网上老资料里的 4 字节包头已经不认了）：

· 地址 `wss://danmuproxy.douyu.com:8501-8506`，任一端口都行，连不上换下一个
· 包头 12 字节：长度(4) + 长度(4) + 689(2) + 0(2)，那个长度是「总长减 4」
· 连上直接发 loginreq 和 joingroup，不等服务端回 loginres
· 心跳 45 秒一次 `mrkl`；服务端主动发 `pingreq` 也要马上回 `mrkl`
· 正文是 `key@=value/` 的文本，value 里的 `/` 编码成 `@S`，`@` 编码成 `@A`
· 靓号（6657 这种）要先换成真实房间号，否则能收到 loginres 但一条弹幕都没有
· TLS 默认参数会被拒（SSLV3_ALERT_HANDSHAKE_FAILURE），要锁 TLS1.2 并放宽 SECLEVEL

消息统一成和 B 站一样的结构，交给 orchestrator 的 _on_danmaku 处理。
"""

from __future__ import annotations

import asyncio
import re
import ssl
import struct
import time

import aiohttp

from . import logbus

PORTS = (8504, 8503, 8502, 8501, 8506, 8505)
HOST = "danmuproxy.douyu.com"
HEARTBEAT_SEC = 45
MAX_FRAME = 1 << 20        # 单帧超过 1MB 当异常，直接丢
RETRY_MAX = 30             # 重连最长等 30 秒

# 斗鱼表情写成 [emotdy106] 这种，PS5 的 IRC 画面显示出来是一串乱码般的方括号
_EMOT_RE = re.compile(r"\[emot[a-z0-9]+\]", re.I)


def _ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    except Exception:
        pass
    try:
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
    except Exception:
        pass
    return ctx


def pack(text: str) -> bytes:
    """12 字节头 + 正文 + \\0。长度字段填「总长减 4」。"""
    body = text.encode("utf-8") + b"\0"
    n = len(body) + 8
    return struct.pack("<IIhh", n, n, 689, 0) + body


def split_frames(buf: bytes):
    """
    按 12 字节头切包。

    切不出来就把缓冲清空：斗鱼偶尔会塞进来一个不认识的帧，
    留着它以后所有帧都对不上，缓冲会一路涨到把进程撑爆。
    """
    out = []
    while len(buf) >= 12:
        n = struct.unpack_from("<I", buf, 0)[0]
        if n < 9 or n > MAX_FRAME or 4 + n > len(buf):
            return out, b""
        out.append(buf[12:4 + n - 1].decode("utf-8", "ignore"))
        buf = buf[4 + n:]
    return out, buf


def unescape(s: str) -> str:
    """value 里的转义，一次替换完，别让 @AS 被连替两遍。"""
    return re.sub(r"@([AS])", lambda m: "/" if m.group(1) == "S" else "@", s)


def parse_stt(text: str) -> dict:
    out = {}
    for part in text.split("/"):
        if "@=" not in part:
            continue
        k, v = part.split("@=", 1)
        out[k] = unescape(v)
    return out


def to_item(fields: dict):
    """斗鱼的消息揉成和 B 站一样的结构，不认识就返回 None。"""
    t = fields.get("type") or ""
    if t == "chatmsg":
        txt = _EMOT_RE.sub("", fields.get("txt") or "").strip()
        if not txt:
            return None
        return {"kind": "danmaku", "user": fields.get("nn") or "",
                "text": txt, "num": 1, "price": 0}
    if t == "dgb":
        cnt = fields.get("gfcnt") or "1"
        # 2026-11-19 抓真实帧确认：名字就在 gfn（陪伴印章、粉丝荧光棒），
        # gs 恒为 "0"，gfid 是数字编号。以前不认 gfn 才全显示成「礼物」。
        name = (fields.get("gfn") or "").strip()
        if not name or set(name) <= {"0"}:
            name = (fields.get("gs") or "").strip()
        if not name or set(name) <= {"0"}:
            name = "礼物"
        try:
            num = int(cnt)
        except Exception:
            num = 1
        return {"kind": "gift", "user": fields.get("nn") or "",
                "text": "%s x%d" % (name, num), "num": num, "price": 0}
    return None


async def resolve_room(sess, room: str) -> str:
    """
    靓号换真实房间号。

    6657 是靓号，直接拿去 loginreq 能收到 loginres，但一条弹幕都没有。
    真实号在移动页的页面数据里，取不到就用原值，别把连接卡死。
    """
    rid = str(room or "").strip()
    if not rid.isdigit():
        return rid
    try:
        async with sess.get("https://m.douyu.com/%s" % rid,
                            headers={"User-Agent": "Mozilla/5.0"},
                            timeout=aiohttp.ClientTimeout(total=8)) as r:
            html = await r.text(errors="ignore")
    except Exception as e:
        logbus.debug("斗鱼房间号解析失败，按原值用：%s" % e, tag="弹幕")
        return rid
    m = re.search(r'\$ROOM\.room_id\s*=\s*["\']?([1-9]\d*)', html)
    if m:
        return m.group(1)
    m = re.search(r'"rid"\s*:\s*"?([1-9]\d*)"?', html)
    if m:
        return m.group(1)
    return rid


class DouyuDanmaku(object):
    """斗鱼弹幕客户端，接口和 B 站那个对齐。"""

    def __init__(self, on_message=None, on_state=None):
        self.on_message = on_message
        self.on_state = on_state
        self.room_id = ""
        self.real_room = ""
        self.connected = False
        self.running = False
        self.count = 0
        self.last_at = 0.0
        self.last_error = ""
        self._task = None
        self._hb = None
        self._ws = None
        self._retry = 0
        self.retry = 0          # 连续失败次数，界面显示
        self.retry_wait = 0     # 距下次重连还有几秒
        self._port = 0

    # ── 生命周期 ──

    async def start(self, room) -> bool:
        await self.stop()
        self.room_id = str(room or "").strip()
        if not self.room_id:
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
        if self.on_state:
            try:
                self.on_state(state)
            except Exception:
                pass

    # ── 连接 ──

    async def _loop(self):
        async with aiohttp.ClientSession() as sess:
            self.real_room = await resolve_room(sess, self.room_id)
            if self.real_room and self.real_room != self.room_id:
                logbus.info("斗鱼房间 %s 对应真实房间号 %s"
                            % (self.room_id, self.real_room), tag="弹幕")
            while self.running:
                try:
                    if not await self._connect_once(sess):
                        raise RuntimeError(self.last_error or "连接失败")
                    await self._receive()
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    if self.running:
                        logbus.warn("斗鱼弹幕连接中断：%s" % e, tag="弹幕")
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

    async def _connect_once(self, sess) -> bool:
        # 端口轮换：斗鱼这几个端口在部分网络下总有连不上的，失败了换下一个
        ports = list(PORTS)
        if self._port:
            ports.remove(self._port)
            ports.insert(0, self._port)
        for port in ports:
            if not self.running:
                return False
            url = "wss://%s:%d/" % (HOST, port)
            try:
                self._ws = await asyncio.wait_for(
                    sess.ws_connect(url, heartbeat=None, ssl=_ssl_ctx()),
                    timeout=10)
            except Exception as e:
                self.last_error = "端口 %d 连不上：%s" % (port, e)
                continue
            self._port = port
            break
        else:
            return False

        room = self.real_room or self.room_id
        try:
            await self._ws.send_bytes(pack("type@=loginreq/roomid@=%s/" % room))
            await self._ws.send_bytes(
                pack("type@=joingroup/rid@=%s/gid@=-9999/" % room))
        except Exception as e:
            self.last_error = "登录包发不出去：%s" % e
            return False

        self.connected = True
        self._retry = 0
        self.retry = 0
        self.retry_wait = 0
        self._emit("open")
        logbus.ok("斗鱼弹幕已连接（房间 %s，端口 %d）" % (room, port), tag="弹幕")
        self._hb = asyncio.ensure_future(self._heartbeat())
        return True

    async def _heartbeat(self):
        while self.running and self.connected:
            await asyncio.sleep(HEARTBEAT_SEC)
            try:
                await self._ws.send_bytes(pack("type@=mrkl/"))
            except Exception:
                return

    async def _receive(self):
        buf = b""
        while self.running and self._ws is not None:
            try:
                m = await asyncio.wait_for(self._ws.receive(), timeout=HEARTBEAT_SEC + 30)
            except asyncio.TimeoutError:
                raise RuntimeError("太久没收到任何数据")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                raise RuntimeError("收包失败：%s" % e)

            if m.type == aiohttp.WSMsgType.BINARY:
                buf += m.data
                if len(buf) > 4 * MAX_FRAME:
                    buf = b""
                frames, buf = split_frames(buf)
                for f in frames:
                    await self._dispatch(f)
            elif m.type in (aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR):
                raise RuntimeError("服务端关闭连接")

    async def _dispatch(self, raw: str):
        fields = parse_stt(raw)
        t = fields.get("type") or ""
        if t == "pingreq":
            # 服务端点名要心跳，等 45 秒的定时器就太晚了
            try:
                await self._ws.send_bytes(pack("type@=mrkl/"))
            except Exception:
                pass
            return
        if t == "loginres":
            err = fields.get("error") or "0"
            if err not in ("0", ""):
                logbus.warn("斗鱼登录返回错误码 %s" % err, tag="弹幕")
            return

        item = to_item(fields)
        if not item:
            return
        self.count += 1
        self.last_at = time.time()
        if self.on_message:
            try:
                self.on_message(item)
            except Exception:
                pass
