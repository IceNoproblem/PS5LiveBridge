# -*- coding: utf-8 -*-
"""
core/paths.py —— 路径、配置、日志（全项目唯一真相源）

设计原则：
    1. 打包后一切可写文件都落在 exe 同级目录（用户能看见、能备份），
       只有 nginx 例外 —— nginx 打不开中文路径，会被搬到 %LOCALAPPDATA%。
    2. 配置写入用「临时文件 + 替换」，避免断电/崩溃留下半个 JSON。
    3. 所有模块都从这里取路径，禁止各自 os.getcwd()。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

APP_NAME = "PS5LiveBridge"
APP_TITLE = "PS5 直播桥"

IS_WINDOWS = sys.platform == "win32"

# nginx/ffmpeg 对非 ASCII 路径的容忍度极差，统一搬到这个纯英文目录
ASCII_SAFE_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), APP_NAME
) if IS_WINDOWS else os.path.join(os.path.expanduser("~"), "." + APP_NAME.lower())


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def base_dir() -> str:
    """程序主目录（只读资源 + 用户配置都在这）。"""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


BASE_DIR = base_dir()
LOGS_DIR = os.path.join(BASE_DIR, "logs")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")


def ensure_dirs() -> None:
    for d in (LOGS_DIR, ASCII_SAFE_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass


def resource_path(*parts: str) -> str:
    """
    取随程序分发的只读资源。

    PyInstaller 单目录模式下资源在 _internal/，onefile 模式会解到 sys._MEIPASS。
    两种都覆盖，源码运行时回退到项目根。
    """
    candidates = []
    if hasattr(sys, "_MEIPASS"):
        candidates.append(os.path.join(sys._MEIPASS, *parts))
    candidates.append(os.path.join(BASE_DIR, *parts))
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", *parts))
    for c in candidates:
        c = os.path.abspath(c)
        if os.path.exists(c):
            return c
    return os.path.abspath(candidates[-1])


# ──────────────────────────────────────────────────────────────
# 配置
# ──────────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    # Web 控制台
    "WEB_PORT": 5000,
    "WEB_HOST": "127.0.0.1",

    # 网络
    "LAN_IP": "",                  # 答复给 PS5 的本机 IP，空=自动探测
    "LOCAL_IP_FORCE": "",          # 用户手动指定（自动识别不对时）

    # DNS 重定向
    "HIJACK_DOMAINS": [
        "contribute.live-video.net",
        "global-contribute.live-video.net",
        "live.twitch.tv",
        "irc.twitch.tv",
        "tmi.twitch.tv",
    ],
    "DNS_UPSTREAM": "",             # 内置 DNS 的上游，空=自动（优先加速器 DNS）
    "DNS_PORT": 53,

    # 劫持方式
    #
    # divert = 端口级劫持（WinDivert），**配合加速器时的唯一可行方案**
    #          不碰 DNS，PS5 网络参数照加速器给的填，直接改写发往远端 1935 的连接
    # dns    = 路由器/内置 DNS 重定向，只在「PS5 不经过加速器」时用
    #
    # 为什么默认 divert：真机实测证明，PS5 一旦经加速器（UU 等），
    # DNS 方案无论 A 记录答哪个 IP 都是死路（详见 core/divert.py 顶部）。
    "HIJACK_MODE": "divert",
    "DIVERT_PORTS": [1935, 6667],   # 推流端口 + 弹幕（IRC）端口

    # 加速器 / PS5 网络
    "ACCELERATOR": "",              # 记录的加速器名字，仅用于文案
    "PS5_NET": {                    # 引导页让用户照抄加速器的四项
        "ip": "", "mask": "", "gateway": "", "dns": "",
    },
    "DNS_MODE": "router",           # router=路由器重定向 / builtin=内置 DNS

    # B 站
    "BILIBILI_ROOM_ID": 0,
    "BILIBILI_UID": 0,
    "BILIBILI_UNAME": "",
    "BILIBILI_AREA_ID": "236",      # 单机游戏 → 主机游戏（实查 2026-10：236 是主机游戏，237 是怀旧游戏）
    "BILIBILI_TITLE": "",           # 空=沿用 PS5/Twitch 标题

    # 推流
    "RTMP_PORT": 1935,
    "IRC_PORT": 6667,
    "STAT_PORT": 8080,
    "TWITCH_CHANNEL": "",           # 空=用 PS5 上报的频道名
    "RELAY_VIDEO_BITRATE": 0,       # 0=不限制，纯 copy

    # 输出方式
    #   direct = 软件收流后自己转推到 B 站，并替你开播（默认）
    #   local  = 软件只收流，给出一个本地地址，你自己在 OBS 等软件里拉
    "OUTPUT_MODE": "direct",

    # 行为
    "AUTO_START_LIVE": True,        # PS5 一推流就自动开播
    "AUTO_RELAY": True,             # 自动把截到的流转发到 B 站
    "ENABLE_GIFT": True,
    "ENABLE_SC": True,
    "MAX_LOG_ITEMS": 200,
}

_lock = threading.RLock()
_config_cache = None


def load_config() -> dict:
    """读配置，缺项用默认值补齐（不落盘，避免频繁 IO）。"""
    global _config_cache
    with _lock:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    for k, v in loaded.items():
                        if k in DEFAULT_CONFIG:
                            if isinstance(DEFAULT_CONFIG[k], dict) and isinstance(v, dict):
                                cfg[k].update(v)
                            else:
                                cfg[k] = v
            except Exception:
                pass
        _config_cache = cfg
        return cfg


def save_config(patch: dict | None = None) -> dict:
    """合并保存。返回保存后的完整配置。"""
    global _config_cache
    with _lock:
        cfg = _config_cache or load_config()
        if patch:
            for k, v in patch.items():
                if k not in DEFAULT_CONFIG:
                    continue
                if isinstance(DEFAULT_CONFIG[k], dict) and isinstance(v, dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        cfg["_saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        tmp = CONFIG_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CONFIG_FILE)
        except Exception:
            pass
        _config_cache = cfg
        return cfg


# ──────────────────────────────────────────────────────────────
# 进度（向导做到第几步了，重启后能接着来）
# ──────────────────────────────────────────────────────────────
PROGRESS_FILE = os.path.join(LOGS_DIR, "progress.json")


def load_progress() -> dict:
    default = {"step": 1, "dns_verified": False, "dns_mode": "", "lan_ip": ""}
    try:
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            default.update(data)
    except Exception:
        pass
    return default


def save_progress(data: dict) -> dict:
    cur = load_progress()
    cur.update(data or {})
    tmp = PROGRESS_FILE + ".tmp"
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
        os.replace(tmp, PROGRESS_FILE)
    except Exception:
        pass
    return cur
