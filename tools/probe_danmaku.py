"""
弹幕协议探测工具。

用途：接一个平台的弹幕服务器，把收到的每一帧原样打出来。逆向新平台、
或者平台偷偷改了协议时，用浏览器 F12 抓到的首包跟这里对比，能很快定位差在哪。

用法：
  # 斗鱼，自动依次试 8504/8506/8501，用的是 STT 文本包
  python tools/probe_danmaku.py douyu 288016

  # 手动指定地址和首包（从浏览器 F12 里抄）
  python tools/probe_danmaku.py raw wss://xxx/danmu \\
      --send "type@=loginreq/roomid@=123/" --frame stt

  # 只看能不能连上，不等待
  python tools/probe_danmaku.py douyu 288016 --timeout 3

依赖 aiohttp（项目 requirements 里已有）。
"""

from __future__ import annotations

import argparse
import asyncio
import ssl
import struct
import sys
import time

try:
    import aiohttp
except ImportError:
    sys.exit("需要 aiohttp：pip install aiohttp")


# ── 斗鱼 STT 包 ────────────────────────────────────────
# 4 字节小端长度（含这 4 字节和结尾的 \0）+ 正文 + \0


def stt_pack(text: str) -> bytes:
    body = text.encode("utf-8") + b"\0"
    return struct.pack("<I", len(body) + 4) + body


def stt_split(buf: bytes):
    out = []
    while len(buf) >= 4:
        n = struct.unpack("<I", buf[:4])[0]
        if n < 5 or n > len(buf):
            break
        out.append(buf[4:n - 1].decode("utf-8", "ignore"))
        buf = buf[n:]
    return out, buf


def hexdump(data: bytes, limit: int = 96) -> str:
    chunk = data[:limit]
    head = " ".join("%02x" % b for b in chunk)
    if len(data) > limit:
        head += " ...(共 %d 字节)" % len(data)
    return head


def tls_context():
    """
    斗鱼的弹幕端口对 TLS 很挑：默认上下文会被拒（SSLV3_ALERT_HANDSHAKE_FAILURE），
    固定成 TLS1.2 + SECLEVEL=1 才握得上。这里统一按这个来。
    """
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


PRESETS = {
    # 斗鱼：先试 wss，端口从新到旧
    "douyu": {
        "urls": ["wss://danmuproxy.douyu.com:%d/" % p for p in (8504, 8506, 8501)],
        "frame": "stt",
        "send": ["type@=loginreq/roomid@={room}/",
                 "type@=joingroup/rid@={room}/gid@=-9999/"],
        "heartbeat": "type@=mrkl/",
    },
}


async def probe_url(sess, url, frame, sends, heartbeat, timeout, quiet):
    try:
        ws = await asyncio.wait_for(sess.ws_connect(url, heartbeat=None),
                                    timeout=min(10, timeout + 3))
    except Exception as e:
        print("  连不上：%s %s" % (type(e).__name__, str(e)[:110]))
        return 0

    print("  已连上 %s" % url)
    for s in sends:
        if frame == "stt":
            await ws.send_bytes(stt_pack(s))
        else:
            await ws.send_str(s)
    print("  已发 %d 个包" % len(sends))

    buf = b""
    frames = 0
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            m = await asyncio.wait_for(ws.receive(), timeout=3)
        except asyncio.TimeoutError:
            if heartbeat:
                await ws.send_bytes(stt_pack(heartbeat))
            continue
        if m.type == aiohttp.WSMsgType.BINARY:
            frames += 1
            buf += m.data
            if frame == "stt":
                msgs, buf = stt_split(buf)
                for s in msgs:
                    print("  <- %s" % s[:200])
            elif not quiet:
                print("  <- %s" % hexdump(m.data))
        elif m.type == aiohttp.WSMsgType.TEXT:
            frames += 1
            print("  <- 文本 %s" % str(m.data)[:200])
        elif m.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR):
            print("  连接被关（type=%s）" % m.type)
            break

    try:
        await ws.close()
    except Exception:
        pass
    print("  共收到 %d 帧" % frames)
    return frames


async def main(args):
    if args.platform == "raw":
        if not args.url:
            sys.exit("raw 模式要给地址：python tools/probe_danmaku.py raw wss://...")
        urls = [args.url]
        frame = args.frame
        sends = [args.send] if args.send else []
        heartbeat = args.heartbeat
    else:
        preset = PRESETS[args.platform]
        urls = preset["urls"]
        frame = preset["frame"]
        sends = [s.format(room=args.room) for s in preset["send"]]
        heartbeat = preset["heartbeat"]
        if not args.room:
            sys.exit("要给房间号")

    conn = aiohttp.TCPConnector(ssl=tls_context(), limit=1)
    async with aiohttp.ClientSession(connector=conn,
                                     headers={"User-Agent": "Mozilla/5.0"}) as sess:
        for u in urls:
            n = await probe_url(sess, u, frame, sends, heartbeat,
                                args.timeout, args.quiet)
            if n:
                break
        else:
            print("所有地址都没收到数据")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="弹幕协议探测")
    ap.add_argument("platform", choices=["douyu", "raw"])
    ap.add_argument("room", nargs="?", default="")
    ap.add_argument("--url", default="", help="raw 模式的地址")
    ap.add_argument("--send", action="append", default=[], help="要发的包，可重复")
    ap.add_argument("--heartbeat", default="", help="空闲时发的心跳包")
    ap.add_argument("--frame", default="stt", choices=["stt", "text"],
                    help="包格式：stt 是斗鱼那种带长度头的，text 是普通文本帧")
    ap.add_argument("--timeout", type=float, default=15, help="每个地址收多久")
    ap.add_argument("--quiet", action="store_true", help="不打印二进制帧的十六进制")
    asyncio.run(main(ap.parse_args()))
