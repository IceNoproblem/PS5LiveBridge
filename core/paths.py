# -*- coding: utf-8 -*-
"""
core/paths.py —— 路径、配置、日志（全项目唯一真相源）

设计原则：
    1. 打包后一切可写文件都落在 exe 同级目录（用户能看见、能备份），
       只有 nginx 例外 —— nginx 打不开中文路径，会被搬到 %LOCALAPPDATA%。
    2. 配置写入用「临时文件 + 替换」，避免断电/崩溃留下半个 JSON。
    3. 所有模块都从这里取路径，禁止各自 os.getcwd()。

两种运行形态（PLATFORM）：
    windows  桌面版。WinDivert 端口劫持，可以替用户改 PS5 网络。
    docker   容器版。加速器必须在路由器上，PS5 保持自动获取；
             容器动不了宿主机的网络栈，所以没有劫持、没有防火墙、没有窗口，
             可写目录由 PS5LB_DATA_DIR 指定（挂卷进来）。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

APP_NAME = "PS5LiveBridge"
APP_TITLE = "PS5 直播桥"
APP_VERSION = "4.6"        # 唯一真相源：发版只改这里，main / 界面 / 更新器都从这读

IS_WINDOWS = sys.platform == "win32"

# 容器判定：显式指定了数据目录，或跑在 Docker 里
IN_CONTAINER = bool(os.environ.get("PS5LB_DATA_DIR")) or os.path.exists("/.dockerenv")

PLATFORM = "docker" if IN_CONTAINER else ("windows" if IS_WINDOWS else "linux")


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def base_dir() -> str:
    """程序主目录（随程序分发的只读资源在这）。"""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


BASE_DIR = base_dir()


def data_dir() -> str:
    """
    可写目录：配置、日志、nginx 临时文件、B 站凭据都落这里。

    桌面版就是 exe 同级目录（用户能看见、能备份）；
    容器版由 PS5LB_DATA_DIR 指定（一般挂 /data），镜像本身保持只读。
    """
    forced = (os.environ.get("PS5LB_DATA_DIR") or "").strip()
    if forced:
        return os.path.abspath(forced)
    return BASE_DIR


DATA_DIR = data_dir()
LOGS_DIR = os.path.join(DATA_DIR, "logs")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

# nginx/ffmpeg 对非 ASCII 路径的容忍度极差，统一搬到这个纯英文目录
if IS_WINDOWS:
    ASCII_SAFE_DIR = os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), APP_NAME)
else:
    ASCII_SAFE_DIR = os.path.join(DATA_DIR, "nginx")


def caps() -> dict:
    """
    当前形态能干什么。界面据此决定显示哪些步骤，不要在前端猜平台。

    divert        端口劫持（WinDivert 驱动，只有 Windows 能跑）
    ps5_net_step  是否需要引导用户去 PS5 填四个数（只有电脑加速器需要）
    builtin_dns   内置 DNS 服务（容器要绑 53 且被路由器指过来才有意义）
    gui           有没有原生窗口
    shutdown      界面上「退出」按钮是否有意义
    """
    docker = PLATFORM == "docker"
    allow_dns = (os.environ.get("PS5LB_ALLOW_DNS") or "").strip().lower() in ("1", "true", "yes")
    return {
        "platform": PLATFORM,
        "divert": IS_WINDOWS,
        "ps5_net_step": not docker,
        "builtin_dns": (not docker) or allow_dns,
        "gui": not docker,
        "shutdown": not docker,
        "firewall": IS_WINDOWS,
    }


def ensure_dirs() -> None:
    for d in (DATA_DIR, LOGS_DIR, ASCII_SAFE_DIR):
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
    "AUTO_UPDATE": True,            # 启动后定期检查新版本（安装仍需在界面点一下）
    "ENABLE_GIFT": True,
    "ENABLE_SC": True,
    "MAX_LOG_ITEMS": 200,
}

_lock = threading.RLock()
_config_cache = None

# ──────────────────────────────────────────────────────────────
# 环境变量覆盖（容器部署用；桌面版不设这些变量就等于没这段代码）
#
# 分两类，界限是「这个值要不要跟容器启动参数绑死」：
#   STRICT  界面没有入口改它（端口、IP、域名表），每次启动都以环境变量为准。
#   SEED    界面能改（分区、标题、输出方式），只在第一次生成 config.json 时
#           当默认值；之后以文件为准 —— 否则用户点完保存、重启又变回去，
#           对小白来说就是「这软件坏了」。
# ──────────────────────────────────────────────────────────────
ENV_STRICT = {
    "WEB_PORT": "PS5LB_WEB_PORT",
    "WEB_HOST": "PS5LB_WEB_HOST",
    "LAN_IP": "PS5LB_LAN_IP",
    "RTMP_PORT": "PS5LB_RTMP_PORT",
    "IRC_PORT": "PS5LB_IRC_PORT",
    "STAT_PORT": "PS5LB_STAT_PORT",
    "HIJACK_MODE": "PS5LB_HIJACK_MODE",
    "HIJACK_DOMAINS": "PS5LB_DOMAINS",
}

ENV_SEED = {
    "OUTPUT_MODE": "PS5LB_OUTPUT_MODE",
    "BILIBILI_ROOM_ID": "PS5LB_ROOM_ID",
    "BILIBILI_AREA_ID": "PS5LB_AREA_ID",
    "BILIBILI_TITLE": "PS5LB_TITLE",
    "AUTO_START_LIVE": "PS5LB_AUTO_START_LIVE",
    "TWITCH_CHANNEL": "PS5LB_CHANNEL",
}

_TRUE = ("1", "true", "yes", "on")


def _env_value(key: str, raw: str):
    """把字符串按默认值的类型转过去，转不动就返回 None（视为没设置）。"""
    default = DEFAULT_CONFIG.get(key)
    raw = raw.strip()
    if not raw:
        return None
    if isinstance(default, bool):
        return raw.lower() in _TRUE
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError:
            return None
    if isinstance(default, list):
        return [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
    return raw


def _apply_env(cfg: dict, keys: dict) -> None:
    for key, env_name in keys.items():
        raw = os.environ.get(env_name)
        if raw is None:
            continue
        val = _env_value(key, raw)
        if val is not None:
            cfg[key] = val


def load_config() -> dict:
    """读配置，缺项用默认值补齐（不落盘，避免频繁 IO）。"""
    global _config_cache
    with _lock:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
        first_run = not os.path.exists(CONFIG_FILE)
        if not first_run:
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
        # 容器部署：SEED 只在首次生成配置时当默认值，STRICT 每次都盖
        if first_run:
            _apply_env(cfg, ENV_SEED)
        _apply_env(cfg, ENV_STRICT)
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
