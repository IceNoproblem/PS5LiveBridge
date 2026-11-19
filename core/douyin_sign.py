"""
抖音的两种签名。

抖音对外的接口有两道签名，算法不一样，别混用：

· **a_bogus**：HTTP 接口用（取房间号），SM3 + RC4 + 变体 Base64，
  结果是一百七十来个字符的长串。
· **WS signature**：WebSocket 握手用，同样 RC4 + 变体 Base64，
  但只输出 16 个字符。

两者都是纯算法，不依赖 JS 引擎或浏览器，所以容器里也能跑。
参考实现是 `jwwsjlm/douyinLive`（MIT），这里是照着算法重写的 Python 版，
并且用官方 SM3 测试向量核对过。
"""

from __future__ import annotations

import base64
import hashlib
import random
import time

M32 = 0xFFFFFFFF

# 变体 Base64 编码表，下标就是用途，别改
TABLES = {
    "s3": "ckdp1h4ZKsUB80/Mfvw36XIgR25+WQAlEi7NLboqYTOPuzmFjJnryx9HVGDaStCe",
    "s4": "Dkdpgh2ZmsQB80/MfvV36XI1R45-WUAlEixNLwoqYTOPuzKFjJnry79HbGcaStCe",
    # WebSocket 签名用的表，和 a_bogus 的不是同一张
    "ws": "Dkdpgh4ZKsQB80/Mfvw36XI1R25+WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe",
}
_STD = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


# ── SM3 ────────────────────────────────────────────────
def _rotl(x: int, n: int) -> int:
    n &= 31
    return ((x << n) | (x >> (32 - n))) & M32


def _p0(x: int) -> int:
    return x ^ _rotl(x, 9) ^ _rotl(x, 17)


def _p1(x: int) -> int:
    return x ^ _rotl(x, 15) ^ _rotl(x, 23)


def sm3(data: bytes) -> bytes:
    """国密 SM3，输出 32 字节。"""
    reg = [0x7380166F, 0x4914B2B9, 0x172442D7, 0xDA8A0600,
           0xA96F30BC, 0x163138AA, 0xE38DEE4D, 0xB0FB0E4E]
    buf = bytearray(data)
    bitlen = len(data) * 8
    buf.append(0x80)
    while len(buf) % 64 != 56:
        buf.append(0)
    buf += bitlen.to_bytes(8, "big")

    for off in range(0, len(buf), 64):
        blk = buf[off:off + 64]
        w = [int.from_bytes(blk[i * 4:i * 4 + 4], "big") for i in range(16)]
        for j in range(16, 68):
            w.append(_p1(w[j - 16] ^ w[j - 9] ^ _rotl(w[j - 3], 15))
                     ^ _rotl(w[j - 13], 7) ^ w[j - 6])
        w1 = [w[j] ^ w[j + 4] for j in range(64)]
        a, b, c, d, e, f, g, h = reg
        for j in range(64):
            tj = 0x79CC4519 if j < 16 else 0x7A879D8A
            ss1 = _rotl((_rotl(a, 12) + e + _rotl(tj, j)) & M32, 7)
            ss2 = ss1 ^ _rotl(a, 12)
            if j < 16:
                ff = a ^ b ^ c
                gg = e ^ f ^ g
            else:
                ff = (a & b) | (a & c) | (b & c)
                gg = (e & f) | (~e & g & M32)
            tt1 = (ff + d + ss2 + w1[j]) & M32
            tt2 = (gg + h + ss1 + w[j]) & M32
            d, c, b, a = c, _rotl(b, 9), a, tt1
            h, g, f, e = g, _rotl(f, 19), e, _p0(tt2)
        reg = [x ^ y for x, y in zip(reg, [a, b, c, d, e, f, g, h])]
    return b"".join(x.to_bytes(4, "big") for x in reg)


# ── 公用小工具 ──────────────────────────────────────────
def rc4(key: bytes, data: bytes) -> bytes:
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 0xFF
        s[i], s[j] = s[j], s[i]
    out = bytearray()
    i = j = 0
    for byte in data:
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out.append(byte ^ s[(s[i] + s[j]) & 0xFF])
    return bytes(out)


def b64(data: bytes, table: str) -> str:
    """变体 Base64，不带补位（补位由调用方决定）。"""
    raw = base64.b64encode(data).decode().rstrip("=")
    return raw.translate(str.maketrans(_STD, TABLES[table]))


def _be32(n: int):
    n &= M32
    return [(n >> 24) & 255, (n >> 16) & 255, (n >> 8) & 255, n & 255]


def _mix(rand: int, opt0: int, opt1: int) -> bytes:
    b1 = rand & 255
    b2 = (rand >> 8) & 255
    return bytes([(b1 & 170) | (opt0 & 85), (b1 & 85) | (opt0 & 170),
                  (b2 & 170) | (opt1 & 85), (b2 & 85) | (opt1 & 170)])


# ── a_bogus（HTTP 用） ──────────────────────────────────
def ab_sign(params: str, user_agent: str) -> str:
    """
    给查询串算 a_bogus。

    params 必须是**实际发出去的那一串参数**（顺序也不能变），
    UA 也必须和请求头里的一致，否则服务端算出来对不上，返回空响应。
    """
    env = b"1920|1080|1920|1040|0|30|0|0|1872|92|1920|1040|1857|92|1|24|Win32"
    st = int(time.time() * 1000) & M32
    et = (st + 100) & M32

    url_hash = sm3(sm3((params + "cus").encode()))
    cus_hash = sm3(sm3(b"cus"))
    ua_hash = sm3(b64(rc4(bytes([0, 1, 14]), user_agent.encode()), "s3").encode())

    b = {8: 3, 10: et, 16: st, 18: 44}
    stb = _be32(st)
    b[20], b[21], b[22], b[23] = stb
    b[24] = (st >> 32) & 255
    b[25] = (st >> 40) & 255
    b[26] = b[27] = b[28] = b[29] = 0          # arguments[0] = 0
    b[30] = 0                                   # arguments[1] = 1 的高位
    b[31] = 1
    b[32] = b[33] = 0                            # arguments[1] 的大端前两字节
    a2 = _be32(14)                               # arguments[2] = 14
    b[34], b[35], b[36], b[37] = a2
    b[38], b[39] = url_hash[21], url_hash[22]
    b[40], b[41] = cus_hash[21], cus_hash[22]
    b[42], b[43] = ua_hash[23], ua_hash[24]
    etb = _be32(et)
    b[44], b[45], b[46], b[47] = etb
    b[48] = 3
    b[49] = (et >> 32) & 255
    b[50] = (et >> 40) & 255
    pid = _be32(110624)                          # pageId
    b[51] = 110624
    b[52], b[53], b[54], b[55] = pid
    b[56] = 6383                                 # aid
    b[57] = 6383 & 255
    b[58] = (6383 >> 8) & 255
    b[59] = (6383 >> 16) & 255
    b[60] = (6383 >> 24) & 255
    b[64] = len(env)
    b[65] = len(env) & 255
    b[66] = (len(env) >> 8) & 255
    b[69] = b[70] = b[71] = 0
    b[72] = 0
    for k in (18, 20, 26, 30, 38, 40, 42, 21, 27, 31, 35, 39, 41, 43, 22, 28,
              32, 36, 23, 29, 33, 37, 44, 45, 46, 47, 48, 49, 50, 24, 25, 52,
              53, 54, 55, 57, 58, 59, 60, 65, 66, 70, 71):
        b[72] ^= b[k]

    order = [18, 20, 52, 26, 30, 34, 58, 38, 40, 53, 42, 21, 27, 54, 55, 31,
             35, 57, 39, 41, 43, 22, 28, 32, 60, 36, 23, 29, 33, 37, 44, 45,
             59, 46, 47, 48, 49, 50, 24, 25, 65, 66, 70, 71]
    bb = bytes(b[k] & 255 for k in order) + env + bytes([b[72] & 255])

    prefix = (_mix(int(0.123456789 * 10000), 3, 45)
              + _mix(int(0.987654321 * 10000), 1, 0)
              + _mix(int(0.555555555 * 10000), 1, 5))
    return b64(prefix + rc4(b"\x79", bb), "s4") + "="


# ── WebSocket 握手签名 ──────────────────────────────────
def x_ms_stub(room_id: str, uid: str) -> str:
    """
    握手签名的输入。

    这 13 个字段和顺序是抖音网页自己写死在 `websocketKey` 里的，
    从直播间页面就能读到，改版时以页面为准。
    """
    joined = ",".join([
        "live_id=1", "aid=6383", "version_code=180800",
        "webcast_sdk_version=1.0.15", "room_id=" + room_id,
        "sub_room_id=", "sub_channel_id=", "did_rule=3",
        "user_unique_id=" + uid, "device_platform=web",
        "device_type=", "ac=", "identity=audience",
    ])
    return hashlib.md5(joined.encode()).hexdigest()


def ws_signature(stub_hex: str, counter: int = 0, keep_rand: bool = True) -> str:
    """由 X-MS-STUB 算 16 字符握手签名。"""
    stub_md5 = hashlib.md5(bytes.fromhex(stub_hex)).digest()
    empty = hashlib.md5(hashlib.md5(b"").digest()).digest()
    pr = random.randint(0, 254)
    kr = random.randint(0, 254)

    p = bytearray(10)
    p[0] = counter & 0x3F
    p[1] = 0
    p[2] = 1
    p[3] = 14
    p[4] = empty[14]
    p[5] = empty[15]
    p[6] = stub_md5[14]
    p[7] = stub_md5[15]
    p[8] = pr
    acc = 0
    for i in range(9):
        acc ^= p[i]
    p[9] = acc

    flags = 0x40 | (0x10 if keep_rand else 0)
    return b64(bytes([flags, kr]) + rc4(bytes([kr]), bytes(p)), "ws")
