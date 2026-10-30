from __future__ import annotations

import os
import threading
import time

from . import paths

# Windows 自带的中文字体。drawtext 不指定 fontfile 会用默认字体，
# 中文会变成方块，所以必须显式给。
FONT_CANDIDATES = (
    # TTF 优先。TTC 是字体集合，drawtext 解析它更容易出问题
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simsun.ttc",
    # macOS，开发机上跑 unit test 用
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    # Linux / Docker 容器。Debian 装 fonts-noto-cjk 之后是这几个路径，
    # 容器版不装字体包的话这里全是空的，烧字会找不到 fontfile。
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
)

SLOTS = 3  # 弹幕槽位数。降数量是为了减轻 drawtext 的重载压力
FIXED_BYTES = 512  # 每个文本文件的固定字节数，原因见 _write_text


def pick_font() -> str:
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    return ""


def _hex_to_rgb(h: str):
    h = (h or "#ffffff").lstrip("#").strip()
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        h = "ffffff"
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return 255, 255, 255


def _ff_path(p: str) -> str:
    """ffmpeg 滤镜里的路径：反斜杠换正斜杠，冒号要转义。"""
    return p.replace("\\", "/").replace(":", "\\:")


class Overlay(object):
    """
    把悬浮文字和弹幕渲染成 drawtext 滤镜，交给 ffmpeg 烧进画面。

    为什么从 ASS 改成 drawtext：
    ASS 的样式能力强，但 ffmpeg 的 ass 滤镜只在启动时读一次文件，之后不重读。
    结果是开播之后再收到的弹幕永远不显示 —— 这是实测踩到的坑。

    但 drawtext 的 reload 在 Windows 上会让 ffmpeg 反复崩，所以这里
    不用 reload：文字只在开播时读一次，改了文字靠重启转码生效。
    代价是每条弹幕要一个独立的 drawtext 滤镜，所以预分配固定槽位。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._dir = paths.DATA_DIR
        self._font = pick_font()
        try:
            self.write()
        except Exception:
            pass
        self._start_timer()

    # ── 弹幕队列 ──

    # ── 落盘 ──

    def _write_text(self, name: str, content: str) -> None:
        """
        写入定长文件 —— 这是整个功能能不能稳的关键。

        ffmpeg 的 drawtext 用 av_file_map() 把 textfile mmap 进内存
        （PROT_READ|PROT_WRITE）。文件大小一旦变化，那块映射就失效，
        ffmpeg 直接 0xC0000005 崩溃。ffmpeg 官方邮件列表的原话是
        "drawtext 不适合这个场景"。

        和实测完全吻合：悬浮文字长度固定所以一直没事；
        弹幕每条长短不一，一来弹幕就崩。

        对策：每个槽位写死 FIXED_BYTES 字节，不足补空格，超了安全截断。
        文件大小永远不变，mmap 就不会被打穿。
        """
        p = os.path.join(self._dir, name)
        try:
            os.makedirs(self._dir, exist_ok=True)
            b = (content or "").encode("utf-8")
            if len(b) > FIXED_BYTES - 1:
                b = b[:FIXED_BYTES - 1]
                # 不能把多字节字符切成半个
                while b and (b[-1] & 0xC0) == 0x80:
                    b = b[:-1]
                if b and b[-1] >= 0xC0:
                    b = b[:-1]
            b = b + b" " * (FIXED_BYTES - len(b))
            with open(p, "wb") as f:
                f.write(b)
        except Exception as e:
            from . import logbus
            logbus.warn("写叠加文本失败 %s：%s" % (name, e), tag="叠加")

    def write(self, cfg: dict = None) -> None:
        """
        把当前状态落成 txt 文件，开播时由 drawtext 读一次。

        drawtext 的 textfile 不能是空文件（滤镜会报错），
        所以没有内容时写一个空格占位。
        """
        cfg = cfg or paths.load_config()

        st = (cfg.get("OVERLAY_TEXT") or "").strip()
        self._write_text("ov_title.txt", st if st else " ")


    # ── 滤镜链 ──

    def filter_chain(self, cfg: dict = None) -> str:
        """拼 drawtext 滤镜链。返回空串表示不需要叠加。"""
        cfg = cfg or paths.load_config()
        if not self._font:
            from . import logbus
            logbus.warn("找不到中文字体，叠加会显示成方块", tag="叠加")
            return ""

        parts = []
        font = _ff_path(self._font)

        # 悬浮文字
        st = (cfg.get("OVERLAY_TEXT") or "").strip()
        if st:
            size = int(cfg.get("OVERLAY_FONT_SIZE", 42) or 42)
            r, g, b = _hex_to_rgb(cfg.get("OVERLAY_COLOR", "#ffffff"))
            alpha = float(cfg.get("OVERLAY_ALPHA", 0.9) or 0.9)
            bg_a = float(cfg.get("OVERLAY_BG_ALPHA", 0) or 0)
            x = float(cfg.get("OVERLAY_X", 0.04) or 0.04)
            y = float(cfg.get("OVERLAY_Y", 0.04) or 0.04)
            p = _ff_path(os.path.join(self._dir, "ov_title.txt"))
            # 注意：这里刻意不加 reload。
            #
            # drawtext 的 reload 在 Windows 上会让 ffmpeg 反复崩
            # （0xC0000005 访问冲突 / 0xC0000374 堆损坏），实测
            # reload=1 / 15 / 60 都一样，只要开着reload，播几分钟必崩。
            # ffmpeg 官方也说 drawtext 不适合做动态文字。
            #
            # 现在改成「文字内容变了就重启转码」（1~2 秒），
            # 平时完全不碰文件，可以长期稳定。
            box = ""
            if bg_a > 0.02:
                box = ":box=1:boxcolor=black@%.2f:boxborderw=10" % bg_a
            parts.append(
                "drawtext=fontfile='%s':textfile='%s':"
                "fontsize=%d:fontcolor=0x%02X%02X%02X@%.2f:"
                "x=w*%.4f:y=h*%.4f%s"
                % (font, p, size, r, g, b, alpha, x, y, box)
            )

        return ",".join(parts)

    # ── 定时落盘 ──

    def _start_timer(self) -> None:
        """
        每 2 秒把当前文字落一次盘。

        弹幕叠加下线后没有"内容变化"的判断依据了（原来靠 _dirty 标记），
        直接定时写一次即可 —— 文件大小固定，ffmpeg 不 reload，不会崩。
        """
        def loop():
            while True:
                time.sleep(2.0)
                try:
                    cfg = paths.load_config()
                    self.write(cfg)
                except Exception:
                    pass

        threading.Thread(target=loop, name="overlay-io", daemon=True).start()

    # ── 杂项 ──

    def clear(self) -> None:
        self.write()

    @property
    def count(self) -> int:
        """弹幕叠加已下线，没有队列了，永远是 0。"""
        return 0

    @property
    def font(self) -> str:
        return self._font
