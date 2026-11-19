"""
快手弹幕探针。

用途：在真实直播间上把协议定下来。跑法：

    python tools/ks_probe.py https://live.kuaishou.com/u/w2995686376
    python tools/ks_probe.py w2995686376

它做四件事，每一步都打印原始返回，方便对着改：

1. 开一个会话，先访问房间页，让快手下发 did（自己造的 did 会被风控拦）
2. livedetail 查主播名、是否在播、liveStreamId
3. 在播就取 websocketinfo（wss 地址 + token）
4. 连上去，发 CSWebEnterRoom，把收到的前几帧按字节打出来

第 4 步的帧格式还没定（见 docs/弹幕协议-快手.md），所以这里只是把
「发什么、收到什么」原样摊开，等看到真实字节再定封装。
"""

import asyncio
import json
import re
import sys
from urllib.parse import quote

import aiohttp

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36")
BASE = "https://live.kuaishou.com"


def parse_target(raw: str) -> str:
    """从链接或纯 id 里取 principalId。"""
    raw = (raw or "").strip()
    m = re.search(r"kuaishou\.com/u/([A-Za-z0-9_-]+)", raw)
    if m:
        return m.group(1)
    return raw.split("?")[0].strip("/")


# ── 极简 protobuf ────────────────────────────────────────
def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def pb_str(field: int, s: str) -> bytes:
    data = s.encode("utf-8")
    return varint((field << 3) | 2) + varint(len(data)) + data


def pb_fields(buf: bytes):
    """拆字段，返回 [(field, wire, value)]，value 是 bytes 或 int。"""
    out, i, n = [], 0, len(buf)
    while i < n:
        key, i = _read_varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            v, i = _read_varint(buf, i)
            out.append((field, wire, v))
        elif wire == 2:
            ln, i = _read_varint(buf, i)
            out.append((field, wire, buf[i:i + ln]))
            i += ln
        elif wire == 5:
            out.append((field, wire, buf[i:i + 4]))
            i += 4
        elif wire == 1:
            out.append((field, wire, buf[i:i + 8]))
            i += 8
        else:
            break
    return out


def _read_varint(buf: bytes, i: int):
    shift = result = 0
    while i < len(buf):
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7
    return result, i


def enter_room(live_stream_id: str, token: str, page_id: str) -> bytes:
    """CSWebEnterRoom：token=1 liveStreamId=2 pageId=7。"""
    return (pb_str(1, token) + pb_str(2, live_stream_id) + pb_str(7, page_id))


def hexdump(buf: bytes, limit: int = 96) -> str:
    head = buf[:limit]
    parts = " ".join("%02x" % b for b in head)
    tail = "" if len(buf) <= limit else " …共 %d 字节" % len(buf)
    return parts + tail


async def main(raw: str):
    pid = parse_target(raw)
    print("principalId =", pid)

    async with aiohttp.ClientSession(headers={"User-Agent": UA}) as sess:
        # 1. 访问房间页，拿服务端下发的 did
        async with sess.get("%s/u/%s" % (BASE, pid)) as r:
            await r.read()
        cookies = "; ".join("%s=%s" % (c.key, c.value) for c in sess.cookie_jar)
        print("cookie: %s" % (cookies[:90] + ("…" if len(cookies) > 90 else "")))

        h = {"kpf": "PC_WEB", "Accept": "application/json",
             "Referer": "%s/u/%s" % (BASE, pid)}

        # 2. 房间详情
        url = "%s/live_api/liveroom/livedetail?principalId=%s" % (BASE, quote(pid))
        detail = await (await sess.get(url, headers=h)).json()
        d = detail.get("data") or {}
        au = d.get("author") or {}
        ls = d.get("liveStream") or {}
        print("\nlivedetail: result=%s 主播=%s living=%s" % (
            d.get("result"), au.get("name"), au.get("living")))
        print("  liveStreamId =", ls.get("id"), "| 标题 =", ls.get("caption") or "（空）")
        if not au.get("living"):
            print("\n这个号现在没在播，快手不会下发 wss 地址，先开播再来。")
            return

        # 3. wss 地址和 token
        url = "%s/live_api/liveroom/websocketinfo?liveStreamId=%s" % (
            BASE, quote(ls.get("id") or ""))
        info = await (await sess.get(url, headers=h)).json()
        print("\nwebsocketinfo:", json.dumps(info, ensure_ascii=False)[:300])
        i = info.get("data") or {}
        urls = i.get("websocketUrls") or []
        token = i.get("token") or ""
        page_id = i.get("pageId") or ""
        if not urls:
            print("没拿到 wss 地址，字段是：%s" % list(i))
            return

        # 4. 连上去，发 enter，收前几帧
        print("\n连接 %s" % urls[0][:80])
        async with sess.ws_connect(urls[0], headers={"User-Agent": UA}) as ws:
            await ws.send_bytes(enter_room(ls.get("id") or "", token, page_id))
            print("已发 CSWebEnterRoom（裸 protobuf，%d 字节）" % len(
                enter_room(ls.get("id") or "", token, page_id)))
            for _ in range(3):
                try:
                    msg = await ws.receive(timeout=8)
                except asyncio.TimeoutError:
                    print("  8 秒没收到东西")
                    break
                if msg.type == aiohttp.WSMsgType.BINARY:
                    print("  收到 %d 字节: %s" % (len(msg.data), hexdump(msg.data)))
                else:
                    print("  收到", msg.type, str(msg.data)[:80])
                    break


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python tools/ks_probe.py <直播间链接或 principalId>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
