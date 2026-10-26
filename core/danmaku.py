# -*- coding: utf-8 -*-
"""
core/danmaku.py —— B 站直播弹幕 WebSocket 客户端

职责单一：连上 B 站弹幕服务器，把「弹幕 / 礼物 / 舰长 / 醒目留言」解出来，
交给上层（编排器）决定要不要回注到 PS5 画面。

协议细节：
    * 包头 16 字节：total(4) header(4) ver(2) op(4) seq(4)，大端。
    * ver=0 明文 JSON，ver=1 心跳，ver=2 zlib 压缩，ver=3 brotli 压缩。
      压缩包解开后是一串完整的小包，要递归解。
    * 认证 op=7，心跳 op=2 每 30 秒，服务端通知 op=5。
"""
from __future__ import annotations

import asyncio
import json
import struct
import time
import zlib

from . import logbus

HEADER = 16
VER_PLAIN = 0
VER_HEARTBEAT = 1
VER_ZLIB = 2
VER_BROTLI = 3

OP_HEARTBEAT = 2
OP_HEARTBEAT_REPLY = 3
OP_MESSAGE = 5
OP_AUTH = 7
OP_AUTH_REPLY = 8


def pack(op: int, body: bytes = b"", ver: int = VER_PLAIN) -> bytes:
    return struct.pack(">IHHII", HEADER + len(body), HEADER, ver, op, 1) + body


def unpack(data: bytes) -> list:
    out = []
    off = 0
    while off + HEADER <= len(data):
        total, hlen, ver, op, _seq = struct.unpack_from(">IHHII", data, off)
        if total < HEADER or off + total > len(data):
            break
        out.append((op, ver, data[off + hlen:off + total]))
        off += total
    return out


def decode(ver: int, body: bytes) -> list:
    """压缩包递归展开成 JSON 字典列表。"""
    out = []
    try:
        if ver == VER_ZLIB:
            for op, v, b in unpack(zlib.decompress(body)):
                out.extend(decode(v, b))
        elif ver == VER_BROTLI:
            try:
                import brotli
                for op, v, b in unpack(brotli.decompress(body)):
                    out.extend(decode(v, b))
            except ImportError:
                logbus.warn("缺少 brotli，无法解压部分弹幕消息", tag="弹幕")
        elif ver in (VER_PLAIN, VER_HEARTBEAT):
            if body:
                out.append(json.loads(body.decode("utf-8", "ignore")))
    except Exception:
        pass
    return out


def parse_message(msg: dict):
    """
    把一条 B 站消息规整成统一结构：
        {kind: danmaku|gift|guard|sc, user, text, num, price, raw}
    不认识的消息返回 None。
    """
    cmd = msg.get("cmd") or ""
    if cmd.startswith("DANMU_MSG"):
        try:
            info = msg["info"]
            text = info[1]
            user = (info[2][1] if len(info) > 2 and isinstance(info[2], list)
                    and len(info[2]) > 1 else "")
            if not user:
                return None
            return {"kind": "danmaku", "user": user, "text": text, "num": 1, "price": 0}
        except Exception:
            return None

    if cmd == "SEND_GIFT":
        d = msg.get("data") or {}
        return {
            "kind": "gift",
            "user": d.get("uname") or "",
            "text": "%s x%s" % (d.get("giftName") or "礼物", d.get("num") or 1),
            "num": int(d.get("num") or 1),
            "price": int(d.get("total_coin") or 0),
            "coin": d.get("coin_type") or "gold",
        }

    if cmd == "GUARD_BUY":
        d = msg.get("data") or {}
        level = int(d.get("guard_level") or 3)
        name = {1: "总督", 2: "提督", 3: "舰长"}.get(level, "舰长")
        return {
            "kind": "guard", "user": d.get("username") or "",
            "text": "开通了%s x%s" % (name, d.get("num") or 1),
            "num": int(d.get("num") or 1), "price": 0,
        }

    if cmd == "SUPER_CHAT_MESSAGE":
        d = msg.get("data") or {}
        ui = d.get("user_info") or {}
        return {
            "kind": "sc", "user": ui.get("uname") or "",
            "text": d.get("message") or "",
            "num": 1, "price": int(d.get("price") or 0),
        }
    return None


class DanmakuClient(object):
    """不自带事件循环，由 services.py 持有。"""

    def __init__(self, bili, on_message=None, on_state=None):
        self.bili = bili
        self.on_message = on_message      # callable(dict) —— 同步回调
        self.on_state = on_state          # callable(str) —— connected/closed/error
        self.room_id = 0
        self.connected = False
        self.running = False
        self._ws = None
        self._session = None
        self._task = None
        self._hb = None
        self._retry = 0
        self.count = 0
        self.last_at = 0.0

    # ---------- 连接 ----------
    async def start(self, room_id: int) -> bool:
        await self.stop()
        self.room_id = room_id
        self.running = True
        self._retry = 0
        self._task = asyncio.ensure_future(self._loop())
        return True

    async def stop(self):
        self.running = False
        for t in (self._hb, self._task):
            if t:
                t.cancel()
        self._hb = None
        self._task = None
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._ws = None
        await self._close_session()
        if self.connected:
            self.connected = False
            self._emit_state("closed")

    async def _close_session(self):
        sess, self._session = self._session, None
        if sess:
            try:
                await sess.close()
            except Exception:
                pass

    def _emit_state(self, s: str):
        if self.on_state:
            try:
                self.on_state(s)
            except Exception:
                pass

    async def _loop(self):
        while self.running:
            try:
                ok = await self._connect_once()
                if not ok:
                    raise RuntimeError("连接失败")
                await self._receive()
            except asyncio.CancelledError:
                return
            except Exception as e:
                if self.running:
                    logbus.warn("弹幕连接中断：%s" % e, tag="弹幕")
            finally:
                self.connected = False
                if self._hb:
                    self._hb.cancel()
                    self._hb = None
                if self._ws:
                    try:
                        await self._ws.close()
                    except Exception:
                        pass
                    self._ws = None
                await self._close_session()

            if not self.running:
                return
            self._retry += 1
            delay = min(30, 3 * self._retry)
            logbus.info("弹幕 %d 秒后重连…" % delay, tag="弹幕")
            await asyncio.sleep(delay)

    async def _connect_once(self) -> bool:
        import aiohttp

        info = await asyncio.get_event_loop().run_in_executor(None, self.bili.danmu_info)
        if not info:
            return False
        token = info.get("token") or ""
        hosts = info.get("host_list") or []
        if not hosts:
            return False
        host = hosts[0]
        url = "wss://%s:%s/sub" % (host.get("host"), host.get("wss_port") or 443)

        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
            "Origin": "https://live.bilibili.com",
            "Referer": "https://live.bilibili.com/%d" % self.room_id,
        }
        session = aiohttp.ClientSession()
        self._session = session
        # 弹幕有冷场期，接收超时必须关掉，否则十几秒没消息就被判超时、不停重连。
        # 新旧 aiohttp 的写法不一样，按版本分支，别指望用户装的都是最新版。
        ws_kw = {"headers": headers, "heartbeat": None}
        if hasattr(aiohttp, "ClientWSTimeout"):
            ws_kw["timeout"] = aiohttp.ClientWSTimeout(ws_receive=None)
        else:
            ws_kw["receive_timeout"] = None
        try:
            self._ws = await session.ws_connect(url, **ws_kw)
        except Exception as e:
            await session.close()
            logbus.warn("连不上弹幕服务器 %s：%s" % (url, e), tag="弹幕")
            return False

        auth = {
            "uid": int(self.bili.uid or 0),
            "roomid": int(self.room_id),
            "protover": 3,
            "platform": "web",
            "type": 2,
            "key": token,
        }
        if self.bili.cookies.get("buvid3"):
            auth["buvid"] = self.bili.cookies["buvid3"]
        await self._ws.send_bytes(pack(OP_AUTH, json.dumps(auth).encode()))

        self.connected = True
        self._retry = 0
        self._emit_state("connected")
        logbus.ok("弹幕通道已连上（房间 %d）" % self.room_id, tag="弹幕")
        self._hb = asyncio.ensure_future(self._heartbeat())
        return True

    async def _heartbeat(self):
        while self.running and self._ws and not self._ws.closed:
            try:
                await self._ws.send_bytes(pack(OP_HEARTBEAT, b"[object Object]"))
            except Exception:
                return
            await asyncio.sleep(30)

    async def _receive(self):
        import aiohttp
        while self.running and self._ws and not self._ws.closed:
            try:
                frame = await self._ws.receive(timeout=None)
            except Exception:
                return
            if frame.type == aiohttp.WSMsgType.BINARY:
                for op, ver, body in unpack(frame.data):
                    if op in (OP_MESSAGE, OP_HEARTBEAT_REPLY):
                        for msg in decode(ver, body):
                            self._dispatch(msg)
            elif frame.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR,
                                aiohttp.WSMsgType.CLOSE):
                return

    def _dispatch(self, msg: dict):
        if msg.get("cmd") == "DANMU_MSG":
            pass
        item = parse_message(msg)
        if not item:
            return
        self.count += 1
        item["t"] = time.strftime("%H:%M:%S")
        self.last_at = time.time()
        if self.on_message:
            try:
                self.on_message(item)
            except Exception:
                pass
