from __future__ import annotations

import json
import os
import sys
import threading
import time

APP_NAME = "PS5LiveBridge"
APP_TITLE = "PS5 直播桥"
APP_VERSION = "5.1.0"

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"

IN_CONTAINER = bool(os.environ.get("PS5LB_DATA_DIR")) or os.path.exists("/.dockerenv")

if IN_CONTAINER:
    PLATFORM = "docker"
elif IS_WINDOWS:
    PLATFORM = "windows"
elif IS_MACOS:
    PLATFORM = "macos"
else:
    PLATFORM = "linux"


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
        "divert": IS_WINDOWS or IS_MACOS,
        "ps5_net_step": IS_WINDOWS or IS_MACOS,
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


DEFAULT_CONFIG = {
    "WEB_PORT": 5000,
    "WEB_HOST": "127.0.0.1",

    "LAN_IP": "",
    "LOCAL_IP_FORCE": "",

    "HIJACK_DOMAINS": [
        "live-video.net",

        "twitch.tv",

        "ttvnw.net",
        "twitchcdn.net",

    ],
    "DNS_UPSTREAM": "",
    "DNS_PORT": 53,

    "HIJACK_MODE": "divert",
    "DIVERT_PORTS": [1935, 6667],

    "ACCELERATOR": "",
    "PS5_NET": {
        "ip": "", "mask": "", "gateway": "", "dns": "",
    },
    "DNS_MODE": "router",

    "BILIBILI_ROOM_ID": 0,
    "BILIBILI_UID": 0,
    "BILIBILI_UNAME": "",
    "BILIBILI_AREA_ID": "236",
    "BILIBILI_TITLE": "",

    "RTMP_PORT": 1935,
    "IRC_PORT": 6667,
    "STAT_PORT": 8080,
    "TWITCH_CHANNEL": "",
    "TWITCH_PROXY": "",

    # 收流实现：Windows 用 rtmp-forwarder（单进程、省内存）；
    # 容器和 Linux 继续用 nginx-rtmp —— rtmpf 的二进制没有 Linux 版，
    # 容器里找不到会直接起不来收流。
    "RTMP_IMPL": "nginx" if PLATFORM in ("docker", "linux") else "rtmpf",

    # ── 画面叠加（要重编码，默认关闭）──
    "OVERLAY_TEXT": "",
    "OVERLAY_FONT_SIZE": 42,
    "OVERLAY_COLOR": "#ffffff",
    "OVERLAY_ALPHA": 0.9,
    "OVERLAY_BG_ALPHA": 0.0,
    "OVERLAY_POS": "top-left",
    "OVERLAY_X": 0.04,
    "OVERLAY_Y": 0.04,

    # 叠加烧给谁：B 站那一路 / 本地取流那一路，可以都要
    "OVERLAY_TO_BILI": True,
    "OVERLAY_TO_LOCAL": True,


    "DEBUG_LOG": False,
    "RELAY_VIDEO_BITRATE": 0,

    "OUTPUT_MODE": "direct",

    "PUSH_BILI": True,
    "LOCAL_STREAM": True,

    "AUTO_START_LIVE": True,
    "AUTO_RELAY": True,
    "AUTO_UPDATE": True,
    "ENABLE_GIFT": True,
    "ENABLE_SC": True,
    "MAX_LOG_ITEMS": 200,
}

_lock = threading.RLock()
_config_cache = None

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
    "TWITCH_PROXY": "PS5LB_TWITCH_PROXY",
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
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
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
                            elif k == "HIJACK_DOMAINS" and isinstance(v, list):
                                merged = list(v)
                                for d in DEFAULT_CONFIG[k]:
                                    if d not in merged:
                                        merged.append(d)
                                cfg[k] = merged
                            else:
                                cfg[k] = v
            except Exception:
                pass
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
