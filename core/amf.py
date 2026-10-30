from __future__ import annotations

import struct

MAX_DEPTH = 8


class _Reader(object):
    def __init__(self, data: bytes):
        self.d = data
        self.i = 0

    def eof(self) -> bool:
        return self.i >= len(self.d)

    def u8(self) -> int:
        v = self.d[self.i]
        self.i += 1
        return v

    def u16(self) -> int:
        v = struct.unpack(">H", self.d[self.i:self.i + 2])[0]
        self.i += 2
        return v

    def u32(self) -> int:
        v = struct.unpack(">I", self.d[self.i:self.i + 4])[0]
        self.i += 4
        return v

    def f64(self) -> float:
        v = struct.unpack(">d", self.d[self.i:self.i + 8])[0]
        self.i += 8
        return v

    def utf8(self, n: int) -> str:
        v = self.d[self.i:self.i + n].decode("utf-8", "replace")
        self.i += n
        return v


def _value(r: _Reader, depth: int = 0):
    """读一个 AMF0 值。只处理 metadata 里会出现的类型。"""
    if depth > MAX_DEPTH or r.eof():
        return None
    t = r.u8()

    if t == 0x00:
        return r.f64()
    if t == 0x01:
        return r.u8() != 0
    if t == 0x02:
        return r.utf8(r.u16())
    if t == 0x03:
        return _object(r, depth, has_count=False)
    if t == 0x08:
        return _object(r, depth, has_count=True)
    if t in (0x05, 0x06):
        return None
    if t == 0x0A:
        n = r.u32()
        return [_value(r, depth + 1) for _ in range(max(0, min(n, 256)))]
    if t == 0x0B:
        return r.f64()  # Date：毫秒数，后面还有 2 字节时区
    if t == 0x0C:
        return r.utf8(r.u32())
    if t == 0x0F:
        n = r.u32()
        return r.utf8(min(n, 1 << 20))
    return None


def _object(r: _Reader, depth: int, has_count: bool) -> dict:
    if has_count:
        r.u32()
    out = {}
    for _ in range(256):
        if r.eof():
            break
        klen = r.u16()
        if klen == 0:
            # 空键 + 0x09 = end marker
            if not r.eof() and r.d[r.i] == 0x09:
                r.i += 1
            break
        key = r.utf8(klen)
        out[key] = _value(r, depth + 1)
    return out


def decode_metadata(data: bytes) -> dict:
    """
    解 RTMP 的 onMetaData。

    实际抓到的字节长这样（三层）：
        0x02 "onMetaData"         ← 或者先来一个 "@setDataFrame"
        0x08 <count> <k/v ...>
    不同推流器前缀层数不一样，所以循环剥到 dict 为止。
    """
    if not data:
        return {}
    r = _Reader(data)
    for _ in range(4):
        v = _value(r)
        if isinstance(v, dict):
            return v
        if v is None and r.eof():
            break
    return {}


def pick(meta: dict, *keys, default=0):
    """从 metadata 里取第一个能转成数字的键。"""
    for k in keys:
        if k in meta:
            try:
                return int(round(float(meta[k])))
            except (TypeError, ValueError):
                continue
    return default
