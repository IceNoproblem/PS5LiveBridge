# -*- coding: utf-8 -*-
"""
阿冰没问题（Icenoproblem）PS5 哔哩哔哩 直播系统 V3.0
Windows 10 原生运行版本（无需Docker）
新增：B站扫码登录，彻底解决风控问题
优化：直播间历史记录、主播名显示、一键清空
修复：滚动条显示、房间ID显示不一致
增强：IRC协议完整实现，支持PS5更多命令
新增：RTMP推流支持，显示推流码、编码格式、码率、分辨率、帧率
"""

import os
import sys

# 修复 Windows 控制台编码问题
if sys.platform == 'win32':
    import io as sys_io
    sys.stdout = sys_io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = sys_io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
    import locale
    try:
        locale.setlocale(locale.LC_ALL, 'zh_CN.UTF-8')
    except:
        pass

import asyncio
import logging
import json
import time
import struct
import zlib
import threading
import random
import io
import base64
import hashlib
import urllib.parse
from typing import Dict, Set
from collections import deque
from datetime import datetime

import warnings
warnings.filterwarnings('ignore')

import requests
import aiohttp
from flask import Flask, jsonify, request, render_template_string

try:
    import qrcode
    HAS_QRCODE = True
except ImportError:
    HAS_QRCODE = False

# RTMP 推流状态
RTMP_STATUS = {
    "active": False,
    "stream_key": "",
    "encoding": "",
    "bitrate": 0,
    "resolution": "",
    "fps": 0,
    "last_update": 0
}

# ==================== 全局配置 ====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
COOKIE_FILE = os.path.join(BASE_DIR, "bili_cookies.json")

DEFAULT_CONFIG = {
    "WEB_PORT": 5000,
    "BILIBILI_ROOM_ID": 943565,
    "TWITCH_CHANNEL": "icenoproblem",
    "IRC_HOST": "0.0.0.0",
    "IRC_PORT": 6667,
    "MAX_SEEN_DANMAKU": 1000,
    "MAX_SEEN_GIFT": 500,
    "HEARTBEAT_TIMEOUT": 18000,  # 5 小时 = 5 * 3600 = 18000 秒
    "USER_AGENT": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "ENABLE_GIFT": True,
    "MAX_LOG_ITEMS": 50,
    "BILIBILI_SESSDATA": "",
    "BILIBILI_BILI_JCT": "",
    "BILIBILI_UID": 0,
    "BILIBILI_UNAME": "",
    "RECONNECT_DELAY": 5,
    "ROOM_HISTORY": []  # 直播间历史记录 [{"room_id": 123, "room_title": "主播名", "timestamp": 123456}]
}

CONFIG = DEFAULT_CONFIG.copy()

# ==================== 全局状态 ====================
ACTIVE_CONNECTIONS: Set = set()
IRC_RUNNING = False
WS_RUNNING = False
DANMAKU_COUNT = 0
GIFT_COUNT = 0
GUARD_COUNT = 0
SC_COUNT = 0
NEED_RECONNECT = False  # 标记是否需要重新连接（房间ID改变时）
NEW_ROOM_ID = None  # 新的房间ID
LOGIN_STATE = {
    "qr_key": "",
    "qr_url": "",
    "qr_img_b64": "",
    "status": "idle",   # idle / waiting / success / expired / scanned
    "uname": "",
    "uid": 0,
    "expire_at": 0,
    "poll_active": False
}

recent_danmaku_log = deque(maxlen=500)
recent_gift_log = deque(maxlen=500)   # 包含 gift / guard / sc 三种类型
GUARD_COUNT = 0
SC_COUNT = 0

# ==================== 日志 ====================
os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)

logger = logging.getLogger("ps5-danmaku")
logger.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

ch = logging.StreamHandler(sys.stdout)
ch.setFormatter(formatter)
logger.addHandler(ch)

log_file = os.path.join(BASE_DIR, "logs", f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
fh = logging.FileHandler(log_file, encoding='utf-8')
fh.setFormatter(formatter)
logger.addHandler(fh)

# ===== Web日志队列（用于在Web界面显示） =====
web_log_queue = deque(maxlen=100)  # 保存最近100条日志


def _add_web_log(level: str, msg: str):
    """添加日志到Web队列"""
    now = datetime.now()
    web_log_queue.appendleft({
        "time": now.strftime("%H:%M:%S"),
        "level": level,
        "msg": msg
    })

for noisy in ['urllib3', 'requests', 'flask', 'werkzeug', 'aiohttp']:
    logging.getLogger(noisy).setLevel(logging.CRITICAL)


# ==================== 配置管理 ====================
def load_config():
    global CONFIG
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                for k, v in loaded.items():
                    if k in DEFAULT_CONFIG:
                        CONFIG[k] = v
        else:
            save_config()
    except Exception as e:
        logger.error(f"加载配置失败: {e}，使用默认配置")
        save_config()
    # 尝试从cookie文件加载登录信息
    _load_cookies_to_config()
    logger.info(f"配置已加载 | 房间: {CONFIG['BILIBILI_ROOM_ID']} | 礼物: {'启用' if CONFIG['ENABLE_GIFT'] else '禁用'}")
    _add_web_log("success", f"程序已启动，监听直播间: {CONFIG['BILIBILI_ROOM_ID']}")
    if CONFIG.get("BILIBILI_UNAME"):
        logger.info(f"已登录账号: {CONFIG['BILIBILI_UNAME']} (uid={CONFIG['BILIBILI_UID']})")
        _add_web_log("success", f"已登录账号: {CONFIG['BILIBILI_UNAME']}")


def _load_cookies_to_config():
    """从cookie文件加载登录态到CONFIG"""
    global CONFIG
    try:
        if os.path.exists(COOKIE_FILE):
            with open(COOKIE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            CONFIG["BILIBILI_SESSDATA"] = data.get("SESSDATA", "")
            CONFIG["BILIBILI_BILI_JCT"] = data.get("bili_jct", "")
            CONFIG["BILIBILI_UID"] = data.get("uid", 0)
            CONFIG["BILIBILI_UNAME"] = data.get("uname", "")
    except Exception as e:
        logger.debug(f"加载cookie文件失败: {e}")


def _save_cookies(sessdata: str, bili_jct: str, uid: int, uname: str):
    """保存登录Cookie到文件"""
    try:
        data = {
            "SESSDATA": sessdata,
            "bili_jct": bili_jct,
            "uid": uid,
            "uname": uname,
            "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        with open(COOKIE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        CONFIG["BILIBILI_SESSDATA"] = sessdata
        CONFIG["BILIBILI_BILI_JCT"] = bili_jct
        CONFIG["BILIBILI_UID"] = uid
        CONFIG["BILIBILI_UNAME"] = uname
        logger.info(f"Cookie已保存: {uname} (uid={uid})")
    except Exception as e:
        logger.error(f"保存Cookie失败: {e}")


def save_config(new_config=None):
    global CONFIG
    INT_KEYS = {"BILIBILI_ROOM_ID", "IRC_PORT", "WEB_PORT", "MAX_SEEN_DANMAKU",
                "MAX_SEEN_GIFT", "HEARTBEAT_TIMEOUT", "MAX_LOG_ITEMS", "RECONNECT_DELAY"}
    if new_config:
        for k, v in new_config.items():
            if k not in DEFAULT_CONFIG:
                continue
            if k in INT_KEYS:
                try:
                    CONFIG[k] = int(v)
                except:
                    CONFIG[k] = DEFAULT_CONFIG[k]
            elif k == "ENABLE_GIFT":
                CONFIG[k] = str(v).lower() in ("true", "1", "yes", "on")
            else:
                CONFIG[k] = str(v).strip() if isinstance(v, str) else v
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(CONFIG, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logger.error(f"保存配置失败: {e}")


async def get_room_info(room_id: int) -> dict:
    """获取直播间信息（主播名字）"""
    try:
        url = f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={room_id}"
        headers = {
            "User-Agent": CONFIG["USER_AGENT"],
            "Referer": f"https://live.bilibili.com/{room_id}",
        }

        cookies = {}
        sessdata = CONFIG.get("BILIBILI_SESSDATA", "")
        if sessdata:
            cookies["SESSDATA"] = sessdata
            bili_jct = CONFIG.get("BILIBILI_BILI_JCT", "")
            if bili_jct:
                cookies["bili_jct"] = bili_jct

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, cookies=cookies,
                                   timeout=aiohttp.ClientTimeout(total=10), ssl=False) as resp:
                text = await resp.text()
                if not text:
                    return {"room_id": room_id, "room_title": f"直播间{room_id}"}

                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    return {"room_id": room_id, "room_title": f"直播间{room_id}"}

                if data.get("code") == 0:
                    room_data = data.get("data", {})
                    room_title = room_data.get("title", "")
                    uname = room_data.get("uname", "")
                    # 优先显示主播名，其次标题
                    display_name = uname if uname else (room_title if room_title else f"直播间{room_id}")
                    return {
                        "room_id": room_id,
                        "room_title": display_name,
                        "uname": uname,
                        "title": room_title
                    }
                else:
                    return {"room_id": room_id, "room_title": f"直播间{room_id}"}
    except Exception as e:
        logger.error(f"获取直播间信息失败: {e}")
        return {"room_id": room_id, "room_title": f"直播间{room_id}"}


def add_room_to_history(room_id: int, room_title: str = None):
    """添加直播间到历史记录"""
    global CONFIG
    try:
        room_id = int(room_id)

        # 获取历史记录
        history = CONFIG.get("ROOM_HISTORY", [])
        if not isinstance(history, list):
            history = []

        # 移除已存在的相同房间ID（去重）
        history = [h for h in history if h.get("room_id") != room_id]

        # 添加新的房间记录到开头
        new_record = {
            "room_id": room_id,
            "room_title": room_title or f"直播间{room_id}",
            "timestamp": int(time.time())
        }
        history.insert(0, new_record)

        # 最多保留20条历史记录
        if len(history) > 20:
            history = history[:20]

        CONFIG["ROOM_HISTORY"] = history

        # 立即保存到配置文件
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(CONFIG, f, ensure_ascii=False, indent=4)

        logger.info(f"直播间 {room_id} 已添加到历史记录: {room_title}")
    except Exception as e:
        logger.error(f"添加房间到历史记录失败: {e}")


# ==================== 扫码登录 ====================
def _gen_qr_b64(url: str) -> str:
    """生成二维码图片并返回base64字符串"""
    if not HAS_QRCODE:
        return ""
    try:
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=8,
            border=2,
        )
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        b64 = base64.b64encode(buf.getvalue()).decode('ascii')
        return f"data:image/png;base64,{b64}"
    except Exception as e:
        logger.error(f"生成二维码失败: {e}")
        return ""


def qr_generate() -> dict:
    """请求B站生成二维码key"""
    try:
        headers = {
            "User-Agent": CONFIG["USER_AGENT"],
            "Referer": "https://www.bilibili.com/",
        }
        r = requests.get(
            "https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
            headers=headers, timeout=10, verify=False
        )
        d = r.json()
        if d.get("code") == 0:
            return {
                "key": d["data"]["qrcode_key"],
                "url": d["data"]["url"]
            }
    except Exception as e:
        logger.error(f"获取二维码失败: {e}")
    return {}


def qr_poll(qr_key: str) -> dict:
    """轮询二维码状态
    返回 {status: 'waiting'/'scanned'/'success'/'expired', cookies: {}}
    """
    try:
        headers = {
            "User-Agent": CONFIG["USER_AGENT"],
            "Referer": "https://www.bilibili.com/",
        }
        r = requests.get(
            f"https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key={qr_key}",
            headers=headers, timeout=10, verify=False
        )
        d = r.json()
        code = d.get("data", {}).get("code", -1)
        # 86101 = 未扫码, 86090 = 已扫码未确认, 0 = 成功, 86038 = 已过期
        if code == 0:
            # 登录成功，从响应Cookie拿SESSDATA
            cookies = r.cookies.get_dict()
            refresh_token = d["data"].get("refresh_token", "")
            return {"status": "success", "cookies": cookies, "refresh_token": refresh_token}
        elif code == 86090:
            return {"status": "scanned"}
        elif code == 86038:
            return {"status": "expired"}
        else:
            return {"status": "waiting"}
    except Exception as e:
        logger.error(f"轮询二维码失败: {e}")
        return {"status": "error"}


def _fetch_user_info(sessdata: str) -> dict:
    """登录成功后获取用户信息"""
    try:
        headers = {
            "User-Agent": CONFIG["USER_AGENT"],
            "Cookie": f"SESSDATA={sessdata}",
            "Referer": "https://www.bilibili.com/",
        }
        r = requests.get(
            "https://api.bilibili.com/x/web-interface/nav",
            headers=headers, timeout=10, verify=False
        )
        d = r.json()
        if d.get("code") == 0:
            return {
                "uid": d["data"].get("mid", 0),
                "uname": d["data"].get("uname", ""),
                "face": d["data"].get("face", "")
            }
    except Exception as e:
        logger.debug(f"获取用户信息失败: {e}")
    return {}


def qr_login_thread():
    """后台线程：持续轮询二维码状态直到成功/过期"""
    global LOGIN_STATE
    key = LOGIN_STATE["qr_key"]
    LOGIN_STATE["poll_active"] = True
    deadline = time.time() + 180  # 3分钟有效期

    while time.time() < deadline and LOGIN_STATE["poll_active"]:
        time.sleep(2)
        result = qr_poll(key)
        status = result["status"]

        if status == "success":
            cookies = result.get("cookies", {})
            sessdata = cookies.get("SESSDATA", "")
            bili_jct = cookies.get("bili_jct", "")
            # 获取用户信息
            user_info = _fetch_user_info(sessdata)
            uid = user_info.get("uid", 0)
            uname = user_info.get("uname", "未知用户")
            _save_cookies(sessdata, bili_jct, uid, uname)
            LOGIN_STATE["status"] = "success"
            LOGIN_STATE["uname"] = uname
            LOGIN_STATE["uid"] = uid
            LOGIN_STATE["poll_active"] = False
            logger.info(f"✅ 扫码登录成功: {uname} (uid={uid})")
            break
        elif status == "scanned":
            LOGIN_STATE["status"] = "scanned"
            logger.info("二维码已扫描，等待确认...")
        elif status == "expired":
            LOGIN_STATE["status"] = "expired"
            LOGIN_STATE["poll_active"] = False
            logger.warning("二维码已过期")
            break
        else:
            # waiting / error 继续等
            pass

    if LOGIN_STATE["status"] not in ("success", "expired"):
        LOGIN_STATE["status"] = "expired"
    LOGIN_STATE["poll_active"] = False


def logout_bili():
    """退出登录（清除Cookie文件）"""
    global CONFIG
    try:
        if os.path.exists(COOKIE_FILE):
            os.remove(COOKIE_FILE)
        CONFIG["BILIBILI_SESSDATA"] = ""
        CONFIG["BILIBILI_BILI_JCT"] = ""
        CONFIG["BILIBILI_UID"] = 0
        CONFIG["BILIBILI_UNAME"] = ""
        LOGIN_STATE["status"] = "idle"
        LOGIN_STATE["uname"] = ""
        LOGIN_STATE["uid"] = 0
        logger.info("已退出B站账号")
        return True
    except Exception as e:
        logger.error(f"退出登录失败: {e}")
        return False


# ==================== WBI签名机制 ====================
WBI_INIT_URL = 'https://api.bilibili.com/x/web-interface/nav'
WBI_KEY_INDEX_TABLE = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13
]


class WBISigner:
    """WBI签名器，用于绕过B站风控"""
    
    def __init__(self):
        self._wbi_key = ''
        self._last_refresh_time = None
        self._refresh_future = None
    
    def need_refresh(self):
        """检查是否需要刷新WBI key"""
        if not self._wbi_key:
            return True
        if not self._last_refresh_time:
            return True
        # 每12小时刷新一次
        return (datetime.now() - self._last_refresh_time).total_seconds() > 12 * 3600
    
    async def get_wbi_key(self, session: aiohttp.ClientSession):
        """获取WBI key"""
        if not self.need_refresh():
            return self._wbi_key
        
        # 如果正在刷新，等待刷新完成
        if self._refresh_future is not None:
            await self._refresh_future
            return self._wbi_key
        
        # 开始刷新
        self._refresh_future = asyncio.create_task(self._do_refresh(session))
        await self._refresh_future
        self._refresh_future = None
        return self._wbi_key
    
    async def _do_refresh(self, session: aiohttp.ClientSession):
        """执行刷新WBI key"""
        try:
            async with session.get(
                WBI_INIT_URL,
                headers={'User-Agent': CONFIG["USER_AGENT"]},
                skip_auto_headers=["Accept-Encoding"],
                ssl=False
            ) as res:
                if res.status != 200:
                    logger.warning(f"获取WBI key失败: status={res.status}")
                    return
                
                data = await res.json()
                
                # 解析wbi key
                wbi_img = data.get('data', {}).get('wbi_img', {})
                img_key = wbi_img.get('img_url', '').rpartition('/')[2].partition('.')[0]
                sub_key = wbi_img.get('sub_url', '').rpartition('/')[2].partition('.')[0]
                
                # 按WBI_KEY_INDEX_TABLE重组key
                shuffled_key = img_key + sub_key
                wbi_key = []
                for index in WBI_KEY_INDEX_TABLE:
                    if index < len(shuffled_key):
                        wbi_key.append(shuffled_key[index])
                
                self._wbi_key = ''.join(wbi_key)
                self._last_refresh_time = datetime.now()
                logger.info(f"WBI key刷新成功: {self._wbi_key[:10]}...")
                
        except Exception as e:
            logger.error(f"刷新WBI key失败: {e}")
    
    def add_wbi_sign(self, params: dict):
        """添加WBI签名"""
        if not self._wbi_key:
            return params
        
        # 添加时间戳
        params_to_sign = {**params, 'wts': str(int(datetime.now().timestamp()))}
        
        # 按key字典序排序
        params_to_sign = {
            key: params_to_sign[key]
            for key in sorted(params_to_sign.keys())
        }
        
        # 过滤特殊字符
        for key, value in params_to_sign.items():
            value = ''.join(ch for ch in str(value) if ch not in "!'()*")
            params_to_sign[key] = value
        
        # 计算MD5
        str_to_sign = urllib.parse.urlencode(params_to_sign) + self._wbi_key
        w_rid = hashlib.md5(str_to_sign.encode('utf-8')).hexdigest()
        
        return {
            **params,
            'wts': params_to_sign['wts'],
            'w_rid': w_rid
        }


# 创建全局WBI签名器
wbi_signer = WBISigner()


# ==================== B站 WebSocket 协议 ====================
WS_HEADER_SIZE = 16
WS_OP_HEARTBEAT = 2
WS_OP_HEARTBEAT_REPLY = 3
WS_OP_MESSAGE = 5
WS_OP_USER_AUTH = 7
WS_OP_CONNECT_SUCCESS = 8

WS_VER_PLAIN = 0
WS_VER_HEARTBEAT = 1
WS_VER_ZLIB = 2
WS_VER_BROTLI = 3


def pack_ws_message(op: int, body: bytes = b'', ver: int = WS_VER_PLAIN) -> bytes:
    total = WS_HEADER_SIZE + len(body)
    header = struct.pack('>IHHII', total, WS_HEADER_SIZE, ver, op, 1)
    return header + body


def unpack_ws_messages(data: bytes) -> list:
    messages = []
    offset = 0
    while offset < len(data):
        if offset + WS_HEADER_SIZE > len(data):
            break
        total, header_size, ver, op, seq = struct.unpack_from('>IHHII', data, offset)
        if total < WS_HEADER_SIZE or offset + total > len(data):
            break
        body = data[offset + header_size: offset + total]
        messages.append((op, ver, body))
        offset += total
    return messages


def decode_ws_body(ver: int, body: bytes) -> list:
    results = []
    try:
        if ver == WS_VER_ZLIB:
            body = zlib.decompress(body)
            for op, v, b in unpack_ws_messages(body):
                results.extend(decode_ws_body(v, b))
        elif ver == WS_VER_BROTLI:
            try:
                import brotli
                body = brotli.decompress(body)
                for op, v, b in unpack_ws_messages(body):
                    results.extend(decode_ws_body(v, b))
            except ImportError:
                logger.warning("brotli未安装，跳过brotli消息")
        elif ver in (WS_VER_PLAIN, WS_VER_HEARTBEAT):
            if body:
                results.append(json.loads(body.decode('utf-8', errors='ignore')))
    except Exception as e:
        logger.debug(f"解码消息失败: {e}")
    return results


async def get_danmaku_server_info(room_id: int, sessdata: str = '') -> dict:
    """
    获取弹幕服务器信息（带WBI签名）
    参考blivedm的实现
    """
    try:
        # 第一步：获取WBI key
        async with aiohttp.ClientSession(skip_auto_headers=["Accept-Encoding"]) as wbi_session:
            await wbi_signer.get_wbi_key(wbi_session)
        
        # 第二步：构建请求参数并添加WBI签名
        params = wbi_signer.add_wbi_sign({
            'id': room_id,
            'type': 0
        })
        
        headers = {
            "User-Agent": CONFIG["USER_AGENT"],
            "Referer": f"https://live.bilibili.com/{room_id}",
            "Origin": "https://live.bilibili.com",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        }
        
        # 构建Cookie
        cookies = {}
        if sessdata:
            cookies["SESSDATA"] = sessdata
            bili_jct = CONFIG.get("BILIBILI_BILI_JCT", "")
        if bili_jct:
            cookies["bili_jct"] = bili_jct
        cookies["DedeUserID"] = str(CONFIG.get("BILIBILI_UID", 0))
        cookies["innersign"] = "0"
        
        # 第三步：发送请求获取弹幕服务器信息
        url = "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo"
        
        async with aiohttp.ClientSession(skip_auto_headers=["Accept-Encoding"]) as api_session:
            async with api_session.get(url, headers=headers, cookies=cookies, params=params,
                                       timeout=aiohttp.ClientTimeout(total=15), ssl=False) as resp:
                if resp.status != 200:
                    logger.warning(f"获取弹幕服务器信息失败: status={resp.status}")
                    return {}
                
                # 获取原始响应文本
                text = await resp.text()
                if not text:
                    logger.warning(f"getDanmuInfo 返回空内容")
                    return {}

                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    logger.warning(f"getDanmuInfo 返回非JSON内容: {text[:200]}")
                    return {}

                if data.get("code") == 0:
                    return data["data"]
                else:
                    logger.warning(f"getDanmuInfo 返回错误: code={data.get('code')} msg={data.get('message', '')}")
                    # 风控处理
                    if data.get("code") == -352:
                        # WBI签名错误，重置WBI key并重试
                        wbi_signer._wbi_key = ''
                        wbi_signer._last_refresh_time = None
                        logger.warning("⚠️ WBI签名错误(-352)，已重置WBI key，下次连接将刷新")
    except Exception as e:
        logger.error(f"获取弹幕服务器信息失败: {e}")
    return {}


async def get_real_room_id(room_id: int) -> int:
    # 同样优化风控
    url = f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={room_id}"
    headers = {
        "User-Agent": CONFIG["USER_AGENT"],
        "Referer": f"https://live.bilibili.com/{room_id}",
        "Origin": "https://live.bilibili.com",
        "Accept": "application/json, text/plain, */*",
    }
    
    # 尝试使用登录态
    cookies = {}
    sessdata = CONFIG.get("BILIBILI_SESSDATA", "")
    if sessdata:
        cookies["SESSDATA"] = sessdata
        bili_jct = CONFIG.get("BILIBILI_BILI_JCT", "")
        if bili_jct:
            cookies["bili_jct"] = bili_jct
    
    try:
        # 禁用自动解压，手动处理
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, cookies=cookies,
                                   timeout=aiohttp.ClientTimeout(total=15), ssl=False,
                                   skip_auto_headers=["Accept-Encoding"]) as resp:
                text = await resp.text()
                if not text:
                    return room_id

                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    logger.warning(f"获取房间信息返回非JSON: {text[:200]}")
                    return room_id

                if data.get("code") == 0:
                    real_id = data["data"].get("room_id", room_id)
                    logger.info(f"房间 {room_id} 真实ID: {real_id}")
                    return real_id
                elif data.get("code") == -352:
                    logger.warning("获取房间信息时触发风控(-352)，建议扫码登录")
    except Exception as e:
        logger.error(f"获取真实房间ID失败: {e}")
    return room_id


# ==================== RTMP 推流状态 ====================
def update_rtmp_status(**kwargs):
    """更新RTMP推流状态"""
    global RTMP_STATUS
    logger.debug(f'update_rtmp_status: 更新前 = {RTMP_STATUS}')
    logger.debug(f'update_rtmp_status: 更新参数 = {kwargs}')
    RTMP_STATUS.update(kwargs)
    RTMP_STATUS["last_update"] = int(time.time())
    logger.debug(f'update_rtmp_status: 更新后 = {RTMP_STATUS}')


def get_rtmp_status() -> dict:
    """获取RTMP推流状态"""
    global RTMP_STATUS
    return RTMP_STATUS.copy()


def reset_rtmp_status():
    """重置RTMP推流状态"""
    global RTMP_STATUS
    RTMP_STATUS = {
        "active": False,
        "stream_key": "",
        "encoding": "",
        "bitrate": 0,
        "resolution": "",
        "fps": 0,
        "last_update": 0
    }


# ==================== IRC 服务端 ====================
class IRCClient:
    def __init__(self, reader, writer, server):
        self.reader = reader
        self.writer = writer
        self.server = server
        self.nick = ""
        self.peername = writer.get_extra_info("peername")
        self.last_active = time.time()
        self.auto_joined = False
        self.is_alive = True
        ACTIVE_CONNECTIONS.add(str(self.peername))
        logger.info(f"PS5 连接建立: {self.peername}")

    def check_alive(self) -> bool:
        if not self.writer or self.writer.is_closing():
            self._mark_dead()
            return False
        if time.time() - self.last_active > CONFIG["HEARTBEAT_TIMEOUT"]:
            logger.warning(f"PS5({self.peername}) 心跳超时，断开连接")
            self._mark_dead()
            return False
        return True

    def _mark_dead(self):
        self.is_alive = False
        ACTIVE_CONNECTIONS.discard(str(self.peername))

    async def send_safe(self, data: str):
        if not self.check_alive():
            return
        if not data.endswith("\r\n"):
            data += "\r\n"
        try:
            self.writer.write(data.encode('utf-8'))
            await self.writer.drain()
            self.last_active = time.time()
        except Exception as e:
            logger.error(f"发送数据到 PS5({self.peername}) 失败: {e}")
            self._mark_dead()

    async def auto_join_channel(self):
        if self.auto_joined or not self.check_alive():
            return
        target = f"#{CONFIG['TWITCH_CHANNEL']}"
        self.server.clients[target] = self
        await self.send_safe(f":{self.nick}!{self.nick}@tmi.twitch.tv JOIN {target}")
        await self.send_safe(f":tmi.twitch.tv 353 {self.nick} = {target} :{self.nick}")
        await self.send_safe(f":tmi.twitch.tv 366 {self.nick} {target} :End of /NAMES list")
        self.auto_joined = True
        logger.info(f"PS5({self.peername}) 已加入频道 {target}")

    async def handle_line(self, line: str):
        if not line or not self.check_alive():
            return
        parts = line.split()
        if not parts:
            return
        cmd = parts[0].upper()
        self.last_active = time.time()

        if cmd == "NICK" and len(parts) >= 2:
            self.nick = parts[1]
            logger.info(f"PS5({self.peername}) 昵称: {self.nick}")
            await self.auto_join_channel()
        elif cmd == "USER":
            await self.send_safe(f":tmi.twitch.tv 001 {self.nick} :Welcome to the Twitch IRC Server!")
            await self.send_safe(f":tmi.twitch.tv 002 {self.nick} :Your host is tmi.twitch.tv")
            await self.send_safe(f":tmi.twitch.tv 003 {self.nick} :This server is rather new")
            await self.send_safe(f":tmi.twitch.tv 004 {self.nick} tmi.twitch.tv -")
            await self.send_safe(f":tmi.twitch.tv 375 {self.nick} :-")
            await self.send_safe(f":tmi.twitch.tv 372 {self.nick} :You are in a maze of twisty passages, all alike.")
            await self.send_safe(f":tmi.twitch.tv 376 {self.nick} :>")
        elif cmd == "PING":
            ping_arg = parts[1] if len(parts) >= 2 else "tmi.twitch.tv"
            await self.send_safe(f"PONG :{ping_arg}")
        elif cmd == "JOIN":
            if len(parts) >= 2:
                chan = parts[1]
                await self.send_safe(f":{self.nick}!{self.nick}@tmi.twitch.tv JOIN {chan}")
                await self.send_safe(f":tmi.twitch.tv 353 {self.nick} = {chan} :{self.nick}")
                await self.send_safe(f":tmi.twitch.tv 366 {self.nick} {chan} :End of /NAMES list")
        elif cmd == "CAP":
            # 处理IRCv3能力协商
            # CAP LS : 列出服务器支持的能力
            # CAP REQ : 请求特定能力
            # CAP END : 结束协商
            if len(parts) >= 2:
                subcmd = parts[1].upper()
                if subcmd == "LS":
                    await self.send_safe(f":tmi.twitch.tv CAP * LS :twitch.tv/tags twitch.tv/commands twitch.tv/membership")
                elif subcmd == "REQ":
                    # 客户端请求某些能力，我们全部ACK
                    capabilities = ' '.join(parts[2:]) if len(parts) > 2 else ''
                    await self.send_safe(f":tmi.twitch.tv CAP * ACK :{capabilities}")
                elif subcmd == "END":
                    logger.debug(f"PS5({self.peername}) CAP协商完成")
            else:
                # 简单的CAP命令，ACK支持的能力
                await self.send_safe(f":tmi.twitch.tv CAP * ACK :twitch.tv/tags twitch.tv/commands")
        elif cmd == "WHO":
            # 处理WHO命令（用户信息查询）
            if len(parts) >= 2:
                channel = parts[1]
                await self.send_safe(f":tmi.twitch.tv 352 {self.nick} {channel} {self.nick} 0.0.0.0 tmi.twitch.tv {self.nick} H :0 {self.nick}")
                await self.send_safe(f":tmi.twitch.tv 315 {self.nick} {channel} :End of WHO list")
        elif cmd == "WHOIS":
            # 处理WHOIS命令
            if len(parts) >= 2:
                target_nick = parts[1]
                await self.send_safe(f":tmi.twitch.tv 311 {self.nick} {target_nick} {target_nick} 0.0.0.0 * :{target_nick}")
                await self.send_safe(f":tmi.twitch.tv 318 {self.nick} {target_nick} :End of WHOIS list")
        elif cmd == "MODE":
            # 处理MODE命令
            if len(parts) >= 2:
                target = parts[1]
                mode = parts[2] if len(parts) > 2 else ''
                await self.send_safe(f":tmi.twitch.tv 324 {self.nick} {target} {mode}")
        elif cmd == "PART":
            # 处理离开频道命令
            if len(parts) >= 2:
                chan = parts[1]
                await self.send_safe(f":{self.nick}!{self.nick}@tmi.twitch.tv PART {chan}")
                logger.info(f"PS5({self.peername}) 离开频道 {chan}")
        elif cmd == "QUIT":
            # 处理退出命令
            logger.info(f"PS5({self.peername}) 请求断开连接")
            self._mark_dead()

    async def run(self):
        try:
            while self.check_alive():
                try:
                    data = await asyncio.wait_for(self.reader.readline(), timeout=10.0)
                except asyncio.TimeoutError:
                    await self.send_safe("PING :tmi.twitch.tv")
                    continue
                if not data:
                    break
                line = data.decode('utf-8', errors='ignore').strip()
                if line:
                    await self.handle_line(line)
        except Exception as e:
            logger.error(f"PS5({self.peername}) 连接异常: {e}")
        finally:
            self._mark_dead()
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except:
                pass
            logger.info(f"PS5({self.peername}) 连接断开")


class IRCServer:
    def __init__(self):
        self.clients: Dict[str, IRCClient] = {}

    async def start(self):
        global IRC_RUNNING
        while True:
            try:
                server = await asyncio.start_server(
                    self._handle_client,
                    CONFIG["IRC_HOST"],
                    CONFIG["IRC_PORT"],
                    reuse_address=True
                )
                IRC_RUNNING = True
                _add_web_log("info", f"IRC 服务已启动: {CONFIG['IRC_HOST']}:{CONFIG['IRC_PORT']}")
                logger.info(f"IRC 服务已启动: {CONFIG['IRC_HOST']}:{CONFIG['IRC_PORT']}")
                async with server:
                    await server.serve_forever()
            except Exception as e:
                IRC_RUNNING = False
                logger.error(f"IRC 服务异常: {e}，{CONFIG['RECONNECT_DELAY']}秒后重试")
                _add_web_log("error", f"IRC 服务异常: {e}，{CONFIG['RECONNECT_DELAY']}秒后重试")
                await asyncio.sleep(CONFIG["RECONNECT_DELAY"])

    async def _handle_client(self, reader, writer):
        client = IRCClient(reader, writer, self)
        await client.run()

    def _get_active_client(self) -> IRCClient | None:
        target = f"#{CONFIG['TWITCH_CHANNEL']}"
        c = self.clients.get(target)
        if c and c.check_alive():
            return c
        for c in list(self.clients.values()):
            if c.check_alive():
                return c
        return None

    async def broadcast_danmaku(self, user: str, text: str):
        global DANMAKU_COUNT
        # 先添加到Web显示记录（不依赖IRC连接）
        now = datetime.now()
        recent_danmaku_log.appendleft({
            "type": "danmaku",
            "user": user,
            "text": text,
            "time": now.strftime("%H:%M:%S"),
            "ts": int(now.timestamp() * 1000)
        })
        DANMAKU_COUNT += 1

        # 如果有IRC客户端，发送到IRC
        client = self._get_active_client()
        if client:
            target = f"#{CONFIG['TWITCH_CHANNEL']}"
            safe_user = ''.join(c for c in user if c.isalnum() or c in '_-') or "user"
            msg = f":{safe_user}!{safe_user}@tmi.twitch.tv PRIVMSG {target} :{text}"
            await client.send_safe(msg)
        else:
            logger.debug("无IRC客户端，跳过弹幕转发")

    async def broadcast_gift(self, user: str, gift_name: str, num: int, coin_type: str, price: int = 0):
        global GIFT_COUNT
        if not CONFIG["ENABLE_GIFT"]:
            return

        # 先添加到Web显示记录（不依赖IRC连接）
        display_coin = "电池" if coin_type == "gold" else "银瓜子"
        logger.info(f"礼物 [{user}]: {gift_name}x{num} ({display_coin} {price})")
        now = datetime.now()
        recent_gift_log.appendleft({
            "type": "gift",
            "user": user, "name": gift_name, "num": num,
            "coin": display_coin, "price": price,
            "time": now.strftime("%H:%M:%S"),
            "ts": int(now.timestamp() * 1000)
        })
        GIFT_COUNT += 1

        # 如果有IRC客户端，发送到IRC
        client = self._get_active_client()
        if client:
            target = f"#{CONFIG['TWITCH_CHANNEL']}"
            safe_user = ''.join(c for c in user if c.isalnum() or c in '_-') or "user"
            gift_text = f"GIFT {user}: {gift_name}x{num}"
            msg = f":{safe_user}!{safe_user}@tmi.twitch.tv PRIVMSG {target} :{gift_text}"
            await client.send_safe(msg)

    async def broadcast_guard(self, user: str, guard_level: int, num: int):
        global GUARD_COUNT
        if not CONFIG["ENABLE_GIFT"]:
            return

        # 先添加到Web显示记录（不依赖IRC连接）
        guard_names = {1: "总督", 2: "提督", 3: "舰长"}
        guard_name = guard_names.get(guard_level, "舰长")
        logger.info(f"大航海 [{user}]: {guard_name}x{num}")
        now = datetime.now()
        recent_gift_log.appendleft({
            "type": "guard",
            "user": user, "name": guard_name, "num": num,
            "guard_level": guard_level,
            "coin": "电池", "price": 0,
            "time": now.strftime("%H:%M:%S"),
            "ts": int(now.timestamp() * 1000)
        })
        GUARD_COUNT += 1

        # 如果有IRC客户端，发送到IRC
        client = self._get_active_client()
        if client:
            target = f"#{CONFIG['TWITCH_CHANNEL']}"
            safe_user = ''.join(c for c in user if c.isalnum() or c in '_-') or "user"
            gift_text = f"GUARD {user} 开通了 {guard_name}x{num}"
            msg = f":{safe_user}!{safe_user}@tmi.twitch.tv PRIVMSG {target} :{gift_text}"
            await client.send_safe(msg)

    async def broadcast_super_chat(self, user: str, message: str, price: int):
        global SC_COUNT

        # 先添加到Web显示记录（不依赖IRC连接）
        logger.info(f"SC [{user}] ¥{price}: {message}")
        now = datetime.now()
        recent_gift_log.appendleft({
            "type": "sc",
            "user": user, "name": "醒目留言", "num": 1,
            "text": message,
            "coin": "电池", "price": price,
            "time": now.strftime("%H:%M:%S"),
            "ts": int(now.timestamp() * 1000)
        })
        SC_COUNT += 1

        # 如果有IRC客户端，发送到IRC
        client = self._get_active_client()
        if client:
            target = f"#{CONFIG['TWITCH_CHANNEL']}"
            safe_user = ''.join(c for c in user if c.isalnum() or c in '_-') or "user"
            gift_text = f"SC Y{price} {user}: {message}"
            msg = f":{safe_user}!{safe_user}@tmi.twitch.tv PRIVMSG {target} :{gift_text}"
            await client.send_safe(msg)


# ==================== B站 WebSocket 弹幕/礼物接收 ====================
class BiliLiveClient:
    HEARTBEAT_INTERVAL = 30

    def __init__(self, room_id: int, irc_server: IRCServer):
        self.room_id = room_id
        self.real_room_id = room_id
        self.irc = irc_server
        self.token = ""
        self.ws_url = "wss://broadcastlv.chat.bilibili.com/sub"
        self._ws = None
        self._running = False
        self._seen_danmaku: Set[str] = set()
        self._seen_gift: Set[str] = set()
        self._switch_event = asyncio.Event()  # 用于通知切换房间

    def _danmaku_uid(self, info: list) -> str:
        try:
            ts = info[0][4] if isinstance(info[0], list) and len(info[0]) > 4 else int(time.time()*1000)
            uid = info[2][0] if len(info) > 2 and isinstance(info[2], list) else 0
            text = info[1] if len(info) > 1 else ''
            return f"{ts}_{uid}_{text}"
        except:
            return str(time.time())

    def _gift_uid(self, data: dict) -> str:
        return f"{data.get('uid')}_{data.get('giftId')}_{data.get('timestamp')}"

    async def _fetch_danmaku_info(self):
        """
        获取弹幕服务器信息，带降级策略
        参考blivedm的实现
        """
        try:
            # 先获取真实房间号
            self.real_room_id = await get_real_room_id(self.room_id)
            
            # 尝试获取弹幕服务器信息和token（带WBI签名）
            sessdata = CONFIG.get("BILIBILI_SESSDATA", "")
            info = await get_danmaku_server_info(self.real_room_id, sessdata)
            
            if info:
                # 成功获取服务器信息
                self.token = info.get("token", "")
                hosts = info.get("host_list", [])
                if hosts:
                    host = hosts[0]
                    self.ws_url = f"wss://{host['host']}:{host['wss_port']}/sub"
                    logger.info(f"WebSocket 服务器: {self.ws_url}")
                    logger.info(f"房间ID: {self.real_room_id}")
                    if self.token:
                        logger.info("已获取鉴权Token ✓")
                    return
            
            # 降级：使用默认弹幕服务器
            logger.warning("获取弹幕服务器信息失败，使用默认服务器（降级模式）")
            self.ws_url = "wss://broadcastlv.chat.bilibili.com/sub"
            self.token = ""
            logger.info(f"WebSocket 服务器: {self.ws_url} (降级)")
            logger.info(f"房间ID: {self.real_room_id}")
            
        except Exception as e:
            logger.error(f"获取弹幕服务器信息失败: {e}")
            # 降级处理
            self.ws_url = "wss://broadcastlv.chat.bilibili.com/sub"
            self.token = ""
            logger.info("使用默认服务器继续连接")

    def _build_auth_packet(self) -> bytes:
        """
        构建认证包
        参考blivedm的实现，添加buvid参数，使用protover=3
        """
        uid = CONFIG.get("BILIBILI_UID", 0)
        auth_params = {
            "uid": uid,
            "roomid": self.real_room_id,
            "protover": 3,  # 使用协议版本3
            "platform": "web",
            "type": 2,
        }
        
        # 如果有token，添加到参数中
        if self.token:
            auth_params["key"] = self.token
        
        # 添加buvid（如果有的话）
        # buvid通常通过访问B站主页获取，这里先留空
        # 如果有SESSDATA，可以尝试从Cookie中获取buvid3
        sessdata = CONFIG.get("BILIBILI_SESSDATA", "")
        if sessdata:
            # 尝试解析buvid（简化版本）
            # 实际项目中应该先访问B站主页获取buvid3 cookie
            auth_params["buvid"] = ""
        
        auth_body = json.dumps(auth_params, ensure_ascii=False)
        return pack_ws_message(WS_OP_USER_AUTH, auth_body.encode('utf-8'))

    def _build_heartbeat_packet(self) -> bytes:
        return pack_ws_message(WS_OP_HEARTBEAT, b'[object Object]', WS_VER_HEARTBEAT)

    async def _send_heartbeat(self, ws):
        while self._running:
            try:
                await ws.send_bytes(self._build_heartbeat_packet())
                await asyncio.sleep(self.HEARTBEAT_INTERVAL)
            except Exception as e:
                logger.debug(f"心跳发送失败: {e}")
                break

    async def _handle_message(self, cmd: str, data: dict):
        global WS_RUNNING
        WS_RUNNING = True

        if cmd == "DANMU_MSG":
            info = data.get("info", [])
            if len(info) < 2:
                logger.debug(f"DANMU_MSG数据不完整: {len(info)}")
                return
            uid_key = self._danmaku_uid(info)
            if uid_key in self._seen_danmaku:
                logger.debug(f"弹幕已去重: {uid_key}")
                return
            self._seen_danmaku.add(uid_key)
            if len(self._seen_danmaku) > CONFIG["MAX_SEEN_DANMAKU"]:
                self._seen_danmaku = set(list(self._seen_danmaku)[-int(CONFIG["MAX_SEEN_DANMAKU"] * 0.8):])
            try:
                text = info[1]
                user = info[2][1] if isinstance(info[2], list) and len(info[2]) > 1 else "未知"
                logger.info(f"收到弹幕: [{user}] {text}")
                await self.irc.broadcast_danmaku(user, text)
            except Exception as e:
                logger.error(f"解析弹幕失败: {e}，数据: {data}")

        elif cmd == "SEND_GIFT":
            d = data.get("data", {})
            uid_key = self._gift_uid(d)
            if uid_key in self._seen_gift:
                logger.debug(f"礼物已去重: {uid_key}")
                return
            self._seen_gift.add(uid_key)
            if len(self._seen_gift) > CONFIG["MAX_SEEN_GIFT"]:
                self._seen_gift = set(list(self._seen_gift)[-int(CONFIG["MAX_SEEN_GIFT"] * 0.8):])
            # 尝试获取用户昵称：优先用uname，如果没有尝试uid
            user = d.get("uname")
            if not user or str(user).startswith("bili_"):
                # 如果uname为空或看起来像UID，尝试从其他字段获取
                user = d.get("uid", "未知")
            gift_name = d.get("giftName", d.get("gift_name", "礼物"))
            num = d.get("num", 1)
            coin_type = d.get("coin_type", "silver")
            price = d.get("total_coin", 0)
            logger.info(f"收到礼物: [{user}] {gift_name}x{num}")
            await self.irc.broadcast_gift(user, gift_name, num, coin_type, price)

        elif cmd == "GUARD_BUY":
            d = data.get("data", {})
            user = d.get("username", "未知")
            guard_level = d.get("guard_level", 3)
            num = d.get("num", 1)
            await self.irc.broadcast_guard(user, guard_level, num)

        elif cmd == "SUPER_CHAT_MESSAGE":
            d = data.get("data", {})
            user = d.get("user_info", {}).get("uname", "未知")
            message = d.get("message", "")
            price = d.get("price", 0)
            await self.irc.broadcast_super_chat(user, message, price)

        elif cmd == "COMBO_SEND":
            d = data.get("data", {})
            user = d.get("uname", "未知")
            gift_name = d.get("gift_name", "礼物")
            combo_num = d.get("combo_num", 1)
            coin_type = d.get("coin_type", "silver")
            uid_key = f"combo_{d.get('uid')}_{d.get('gift_id')}_{d.get('batch_combo_id', '')}"
            if uid_key in self._seen_gift:
                return
            self._seen_gift.add(uid_key)
            await self.irc.broadcast_gift(user, gift_name, combo_num, coin_type, 0)

    async def connect(self):
        global WS_RUNNING, NEED_RECONNECT, NEW_ROOM_ID
        while True:
            # 检查是否需要切换房间
            if NEED_RECONNECT and NEW_ROOM_ID is not None:
                await self.switch_room(NEW_ROOM_ID)
                NEED_RECONNECT = False
                NEW_ROOM_ID = None

            WS_RUNNING = False
            try:
                await self._fetch_danmaku_info()
                _add_web_log("info", f"正在连接 B站直播间 {self.real_room_id}...")
                logger.info(f"连接 B站直播间 {self.real_room_id}...")

                conn_timeout = aiohttp.ClientTimeout(total=20)
                headers = {
                    "User-Agent": CONFIG["USER_AGENT"],
                    "Origin": "https://live.bilibili.com",
                    "Referer": f"https://live.bilibili.com/{self.room_id}",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Sec-WebSocket-Extensions": "permessage-deflate; client_max_window_bits",
                }
                
                # 构建完整的Cookie
                cookies = {}
                sessdata = CONFIG.get("BILIBILI_SESSDATA", "")
                if sessdata:
                    cookies["SESSDATA"] = sessdata
                    bili_jct = CONFIG.get("BILIBILI_BILI_JCT", "")
                    if bili_jct:
                        cookies["bili_jct"] = bili_jct
                    cookies["DedeUserID"] = str(CONFIG.get("BILIBILI_UID", 0))
                    cookies["DedeUserID__ckMd5"] = "00000000000000000000000000000000"
                    cookies["innersign"] = "0"

                async with aiohttp.ClientSession(headers=headers, cookies=cookies) as session:
                    async with session.ws_connect(
                        self.ws_url,
                        ssl=False,
                        heartbeat=None,
                        timeout=conn_timeout
                    ) as ws:
                        self._ws = ws
                        self._running = True
                        WS_RUNNING = True

                        await ws.send_bytes(self._build_auth_packet())
                        logger.info("已发送认证包，等待服务器响应...")
                        _add_web_log("info", "已发送认证包，等待服务器响应...")

                        hb_task = asyncio.create_task(self._send_heartbeat(ws))
                        try:
                            while True:
                                # 同时等待消息和切换事件
                                msg_task = asyncio.create_task(ws.receive())
                                switch_task = asyncio.create_task(self._switch_event.wait())

                                done, pending = await asyncio.wait(
                                    [msg_task, switch_task],
                                    return_when=asyncio.FIRST_COMPLETED
                                )

                                # 取消未完成的任务
                                for task in pending:
                                    task.cancel()

                                # 检查切换事件是否触发
                                if self._switch_event.is_set():
                                    logger.info("检测到房间切换请求，断开当前连接")
                                    _add_web_log("info", "正在切换直播间，断开当前连接...")
                                    self._switch_event.clear()
                                    break

                                # 处理WebSocket消息
                                msg = msg_task.result()
                                if msg.type == aiohttp.WSMsgType.BINARY:
                                    await self._process_ws_data(msg.data)
                                elif msg.type == aiohttp.WSMsgType.ERROR:
                                    logger.error(f"WebSocket 错误: {ws.exception()}")
                                    break
                                elif msg.type == aiohttp.WSMsgType.CLOSED:
                                    break
                        finally:
                            hb_task.cancel()
                            self._running = False

            except asyncio.CancelledError:
                break
            except Exception as e:
                import traceback
                logger.error(f"WebSocket 连接失败: {e}")
                logger.error(f"错误详情: {traceback.format_exc()}")
                _add_web_log("error", f"WebSocket 连接失败: {e}")

            WS_RUNNING = False
            logger.info(f"{CONFIG['RECONNECT_DELAY']} 秒后重连...")
            _add_web_log("warning", f"{CONFIG['RECONNECT_DELAY']} 秒后重新连接...")
            await asyncio.sleep(CONFIG["RECONNECT_DELAY"])

    async def switch_room(self, new_room_id: int):
        """切换到新的直播间"""
        logger.info(f"准备切换直播间: {self.room_id} -> {new_room_id}")
        _add_web_log("info", f"正在切换直播间: {self.room_id} -> {new_room_id}")

        # 先获取真实的房间ID
        real_room_id = await get_real_room_id(new_room_id)

        self.room_id = new_room_id
        self.real_room_id = real_room_id

        # 更新全局配置中的房间ID
        global CONFIG
        CONFIG["BILIBILI_ROOM_ID"] = new_room_id

        logger.info(f"真实房间ID: {real_room_id}")
        _add_web_log("info", f"真实房间ID: {real_room_id}")

        # 清空已见弹幕/礼物记录，避免去重问题
        self._seen_danmaku.clear()
        self._seen_gift.clear()
        # 触发切换事件，通知WebSocket循环中断
        self._switch_event.set()
        logger.info(f"直播间已切换为: {new_room_id} (真实ID: {real_room_id})，等待WebSocket断开...")

    async def _process_ws_data(self, data: bytes):
        try:
            packets = unpack_ws_messages(data)
            for op, ver, body in packets:
                if op == WS_OP_CONNECT_SUCCESS:
                    logger.info("✓ B站 WebSocket 连接成功，开始接收消息")
                    _add_web_log("success", "✓ B站连接成功，开始接收弹幕和礼物")
                elif op == WS_OP_HEARTBEAT_REPLY:
                    if len(body) >= 4:
                        popularity = struct.unpack('>I', body[:4])[0]
                        logger.debug(f"直播间人气: {popularity}")
                elif op == WS_OP_MESSAGE:
                    jsons = decode_ws_body(ver, body)
                    for j in jsons:
                        cmd = j.get("cmd", "")
                        if cmd:
                            # 调试日志：显示收到的命令
                            if cmd not in ["HEARTBEAT_REPLY", "ONLINE_RANK_COUNT", "WATCHED_CHANGE"]:
                                logger.debug(f"收到命令: {cmd}")
                            await self._handle_message(cmd, j)
        except Exception as e:
            logger.error(f"处理 WebSocket 数据失败: {e}")
            _add_web_log("error", f"处理数据失败: {e}")


# ==================== Web 控制台 HTML ====================
WEB_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>阿冰没问题（Icenoproblem）PS5 哔哩哔哩 直播系统 V3.0</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css">
<style>
*{margin:0;padding:0;box-sizing:border-box;font-family:'Segoe UI','Microsoft YaHei',sans-serif}
body{background:linear-gradient(135deg,#0d1117 0%,#161b22 50%,#0d1117 100%);min-height:100vh;color:#e6edf3;padding:16px}
.container{max-width:1680px;margin:0 auto}
.header{text-align:center;padding:18px 0 12px;margin-bottom:16px}
.header h1{font-size:1.85rem;background:linear-gradient(90deg,#00d2ff,#7b2ff7,#ff6eb4);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.header .version{color:#8b949e;font-size:.8rem;margin-top:4px}
.card{background:rgba(22,27,34,.88);border:1px solid #30363d;border-radius:14px;padding:20px;backdrop-filter:blur(12px);overflow:hidden;display:flex;flex-direction:column}
.card-title{font-size:.95rem;font-weight:600;margin-bottom:16px;color:#8b949e;display:flex;align-items:center;gap:10px;border-bottom:1px solid rgba(48,54,61,.7);padding-bottom:12px}
.card-title i{color:#7b2ff7}
.layout{display:grid;grid-template-columns:400px 1fr;gap:16px;margin-bottom:16px}
.left-col{display:flex;flex-direction:column;gap:16px}
.right-col{display:flex;flex-direction:column;gap:16px;min-width:0}
.form-group{margin-bottom:10px}
.form-group label{display:block;font-size:.78rem;color:#8b949e;margin-bottom:3px}
.form-group input[type=text],.form-group input[type=number]{width:100%;padding:7px 10px;background:rgba(13,17,23,.8);border:1px solid #30363d;border-radius:6px;color:#e6edf3;font-size:.86rem}
.form-group input:focus{outline:none;border-color:#7b2ff7}
.form-group select{width:100%;padding:7px 10px;background:rgba(13,17,23,.8);border:1px solid #30363d;border-radius:6px;color:#e6edf3;font-size:.86rem;cursor:pointer}
.form-group select:focus{outline:none;border-color:#7b2ff7}
.toggle-row{display:flex;align-items:center;gap:9px;margin-bottom:10px}
.toggle-row label{font-size:.8rem;color:#8b949e}
.toggle{position:relative;width:42px;height:22px;flex-shrink:0}
.toggle input{opacity:0;width:0;height:0}
.toggle-slider{position:absolute;inset:0;background:#30363d;border-radius:22px;cursor:pointer;transition:.3s}
.toggle-slider::before{content:'';position:absolute;width:16px;height:16px;left:3px;top:3px;background:#8b949e;border-radius:50%;transition:.3s}
.toggle input:checked+.toggle-slider{background:#7b2ff7}
.toggle input:checked+.toggle-slider::before{transform:translateX(20px);background:#fff}
.status-grid{display:grid;grid-template-columns:repeat(6,1fr);gap:8px;margin-bottom:14px}
.stat{background:rgba(13,17,23,.65);border-radius:10px;padding:12px 8px;text-align:center;border:1px solid #21262d;transition:all .2s}
.stat:hover{background:rgba(13,17,23,.8);transform:translateY(-2px)}
.stat-val{font-size:1.3rem;font-weight:700;color:#7b2ff7}
.stat-val.on{color:#3fb950}
.stat-val.off{color:#f85149}
.stat-label{font-size:.72rem;color:#6e7681;margin-top:3px}
.irc-info{background:rgba(88,166,255,.06);border:1px solid rgba(88,166,255,.2);border-radius:7px;padding:9px 12px;font-size:.8rem;color:#8b949e;line-height:1.8;margin-top:10px}
.irc-info b{color:#58a6ff}
.btn-row{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap}
.btn{flex:1;min-width:80px;padding:9px 12px;border:none;border-radius:6px;font-size:.84rem;font-weight:600;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:6px;transition:all .2s}
.btn-primary{background:linear-gradient(135deg,#7b2ff7,#4f8cff);color:#fff}
.btn-primary:hover{opacity:.86;transform:translateY(-1px)}
.btn-secondary{background:#21262d;color:#c9d1d9;border:1px solid #30363d}
.btn-secondary:hover{background:#2d333b}
.btn-danger{background:#da3633;color:#fff}
.btn-danger:hover{opacity:.86}
.btn-sm{flex:none;padding:6px 12px;font-size:.78rem}
.login-bar{display:flex;align-items:center;gap:10px;padding:10px 14px;background:rgba(13,17,23,.6);border:1px solid #30363d;border-radius:8px;margin-bottom:10px;flex-wrap:wrap}
.login-avatar{width:34px;height:34px;border-radius:50%;border:2px solid #30363d}
.login-info{flex:1;min-width:0}
.login-info .uname{font-weight:700;font-size:.9rem}
.login-info .uid{font-size:.72rem;color:#8b949e}
.login-badge{display:inline-flex;align-items:center;gap:4px;padding:2px 9px;border-radius:20px;font-size:.75rem;font-weight:600}
.login-badge.logged{background:rgba(63,185,80,.14);color:#3fb950;border:1px solid #238636}
.login-badge.guest{background:rgba(248,81,73,.1);color:#f85149;border:1px solid rgba(248,81,73,.3)}
.feed-card{flex:1;display:flex;flex-direction:column;min-height:0;overflow:hidden}
.tabs{display:flex;gap:6px;margin-bottom:14px;flex-wrap:wrap}
.tab{padding:8px 16px;border-radius:8px;font-size:.82rem;font-weight:600;cursor:pointer;border:1px solid #30363d;background:#161b22;color:#8b949e;transition:all .2s;display:flex;align-items:center;gap:6px}
.tab:hover{background:#21262d;color:#c9d1d9;transform:translateY(-1px)}
.tab.active{background:linear-gradient(135deg,rgba(123,47,247,.3),rgba(79,140,255,.25));border-color:#7b2ff7;color:#c9d1d9;box-shadow:0 2px 8px rgba(123,47,247,.2)}
.tab .badge{background:#30363d;border-radius:10px;padding:1px 7px;font-size:.72rem;color:#8b949e;margin-left:3px}
.tab.active .badge{background:#7b2ff7;color:#fff}
.feed-actions{display:flex;gap:10px;margin-bottom:12px;flex-wrap:wrap}
.feed-list{list-style:none;overflow-y:auto;max-height:520px;display:flex;flex-direction:column;gap:4px}
.no-item{color:#484f58;text-align:center;padding:40px 20px;font-size:.86rem;line-height:1.6}
.dm-item{display:flex;align-items:flex-start;gap:10px;padding:10px 14px;border-radius:8px;background:rgba(79,140,255,.06);border-left:3px solid #4f8cff;animation:slideIn .25s;transition:all .2s}
.dm-item:hover{background:rgba(79,140,255,.1);transform:translateX(2px)}
.dm-avatar{width:32px;height:32px;border-radius:50%;background:linear-gradient(135deg,#7b2ff7,#4f8cff);flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:.85rem;font-weight:700;color:#fff;box-shadow:0 2px 8px rgba(123,47,247,.3)}
.dm-body{flex:1;min-width:0}
.dm-user{font-size:.86rem;font-weight:700;color:#79c0ff;margin-bottom:3px;display:flex;align-items:center;gap:6px}
.dm-user::before{content:'';width:6px;height:6px;border-radius:50%;background:#4f8cff}
.dm-text{font-size:.92rem;color:#e6edf3;word-break:break-all;line-height:1.45;padding-left:12px}
.dm-time{font-size:.68rem;color:#484f58;margin-top:3px;padding-left:12px}
.gift-item{display:flex;align-items:flex-start;gap:12px;padding:12px 14px;border-radius:10px;background:rgba(255,255,255,.04);border:1px solid rgba(48,54,61,.5);animation:slideIn .25s;position:relative;overflow:hidden;transition:all .2s}
.gift-item:hover{background:rgba(255,255,255,.06);transform:translateX(2px)}
.gift-item::before{content:'';position:absolute;left:0;top:0;bottom:0;width:5px;border-radius:2px 0 0 2px}
.gift-item.t-gift::before{background:linear-gradient(180deg,#f0883e,#ff5f00)}
.gift-item.t-guard::before{background:linear-gradient(180deg,#58a6ff,#0d6efd)}
.gift-item.t-sc::before{background:linear-gradient(180deg,#f6c90e,#e67e22)}
.gift-icon{width:42px;height:42px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:1.2rem;flex-shrink:0;box-shadow:0 2px 8px rgba(0,0,0,.2)}
.gift-icon.t-gift{background:rgba(240,136,62,.18);color:#f0883e}
.gift-icon.t-guard{background:rgba(88,166,255,.18);color:#58a6ff}
.gift-icon.t-sc{background:rgba(246,201,14,.18);color:#f6c90e}
.gift-body{flex:1;min-width:0}
.gift-user{font-size:.88rem;font-weight:700;color:#e6edf3;display:flex;align-items:center;gap:6px;margin-bottom:4px}
.gift-user::before{content:'';width:6px;height:6px;border-radius:50%;background:#f0883e}
.gift-item.t-guard .gift-user::before{background:#58a6ff}
.gift-item.t-sc .gift-user::before{background:#f6c90e}
.gift-desc{font-size:.9rem;color:#c9d1d9;margin-bottom:4px;word-break:break-all;line-height:1.4}
.gift-meta{display:flex;align-items:center;gap:10px;margin-top:4px}
.gift-price{font-size:.78rem;font-weight:700;padding:2px 10px;border-radius:12px}
.gift-price.gold{background:rgba(246,201,14,.18);color:#f6c90e}
.gift-price.silver{background:rgba(139,148,158,.15);color:#8b949e}
.gift-time{font-size:.7rem;color:#484f58}
.sc-message{font-size:.88rem;color:#f6c90e;margin-top:6px;padding:8px 12px;background:rgba(246,201,14,.1);border-radius:6px;font-style:italic;word-break:break-all;line-height:1.4}
.guard-1{color:#ff9800}
.guard-2{color:#58a6ff}
.guard-3{color:#56d364}
@keyframes slideIn{from{opacity:0;transform:translateX(10px)}to{opacity:1;transform:translateX(0)}}
@keyframes fadeIn{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:translateY(0)}}
.toast{position:fixed;top:18px;right:18px;padding:10px 16px;border-radius:7px;font-size:.86rem;z-index:9999;animation:fadeIn .3s;pointer-events:none;max-width:280px}
.toast.ok{background:#1a2f1a;color:#3fb950;border:1px solid #238636}
.toast.err{background:#2d0c0c;color:#f85149;border:1px solid #6e1313}
.toast.info{background:#1a2040;color:#58a6ff;border:1px solid #1f6feb}
.qr-modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.78);z-index:5000;align-items:center;justify-content:center;backdrop-filter:blur(5px)}
.qr-modal.active{display:flex}
.qr-box{background:#161b22;border:1px solid #30363d;border-radius:16px;padding:28px;max-width:340px;width:90%;text-align:center;position:relative;animation:fadeIn .25s}
.qr-box h3{font-size:1.05rem;margin-bottom:4px}
.qr-box p{font-size:.8rem;color:#8b949e;margin-bottom:18px}
.qr-img-wrap{display:inline-block;padding:10px;background:#fff;border-radius:10px;margin-bottom:14px}
.qr-img-wrap img{width:190px;height:190px;display:block}
.qr-status{font-size:.88rem;font-weight:600;min-height:20px;margin-bottom:12px}
.qr-status.waiting{color:#8b949e}
.qr-status.scanned{color:#f0883e}
.qr-status.success{color:#3fb950}
.qr-status.expired{color:#f85149}
.qr-timer{font-size:.76rem;color:#8b949e;margin-bottom:12px}
.qr-close{position:absolute;top:10px;right:12px;background:none;border:none;color:#8b949e;font-size:1.1rem;cursor:pointer;padding:4px}
.qr-close:hover{color:#e6edf3}
.qr-refresh-btn{background:#21262d;border:1px solid #30363d;color:#c9d1d9;border-radius:6px;padding:7px 18px;font-size:.82rem;cursor:pointer}
.log-card{flex:1;display:flex;flex-direction:column;min-height:0;overflow:hidden}
.log-list{list-style:none;overflow-y:auto;padding:10px;background:rgba(13,17,23,.7);border-radius:8px;font-family:'Consolas','Monaco',monospace;font-size:.76rem;line-height:1.6}
.log-item{padding:3px 0;border-bottom:1px solid rgba(48,54,61,.3);word-break:break-all}
.log-item:last-child{border-bottom:none}
.log-time{color:#6e7681;margin-right:6px}
.log-level{font-weight:700;margin-right:6px}
.log-level.info{color:#58a6ff}
.log-level.success{color:#3fb950}
.log-level.warning{color:#f0883e}
.log-level.error{color:#f85149}
.log-msg{color:#c9d1d9}
.rtmp-card{margin-bottom:16px}
.rtmp-info-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:12px}
.rtmp-info-item{background:rgba(13,17,23,.65);border-radius:8px;padding:12px 10px;text-align:center;border:1px solid #21262d}
.rtmp-info-label{font-size:.7rem;color:#6e7681;margin-bottom:4px}
.rtmp-info-value{font-size:1.1rem;font-weight:700;color:#e6edf3}
.rtmp-info-value.active{color:#3fb950}
.rtmp-info-value.inactive{color:#f85149}
.rtmp-key-box{background:rgba(88,166,255,.1);border:1px solid rgba(88,166,255,.3);border-radius:8px;padding:10px 12px;margin-top:10px;font-family:'Consolas','Monaco',monospace;font-size:.78rem;word-break:break-all;color:#58a6ff;user-select:all}
.rtmp-key-box code{background:rgba(0,0,0,.2);padding:2px 6px;border-radius:4px}
.footer{text-align:center;color:#484f58;font-size:.75rem;padding:12px 0}
@media(max-width:1100px){
  .layout{grid-template-columns:1fr}
  .status-grid{grid-template-columns:repeat(3,1fr)}
}
@media(max-width:800px){
  /* 小屏幕下弹幕和礼物上下排列 */
  .right-col > div[style*="grid-template-columns"]{
    grid-template-columns:1fr !important;
  }
}
@media(max-width:600px){
  .status-grid{grid-template-columns:repeat(2,1fr)}
  .tabs{gap:3px}
  .tab{padding:5px 9px;font-size:.75rem}
}
</style>
</head>
<body>
<div class="container">

<div class="header">
  <h1><i class="fas fa-gamepad"></i> 阿冰没问题（Icenoproblem）PS5 哔哩哔哩 直播系统</h1>
  <div class="version">GitHub项目地址：https://github.com/IceNoproblem/PS5BiliDanmaku</div>
</div>

<div class="layout">
  <div class="left-col">

    <!-- 账号卡 -->
    <div class="card">
      <div class="card-title"><i class="fas fa-user-circle"></i> B站账号</div>
      <div class="login-bar">
        {% if BILIBILI_UNAME %}
        <img class="login-avatar" src="https://i0.hdslb.com/bfs/face/default_avatar.jpg" alt="">
        <div class="login-info">
          <div class="uname">{{ BILIBILI_UNAME }}</div>
          <div class="uid">UID: {{ BILIBILI_UID }}</div>
        </div>
        <span class="login-badge logged"><i class="fas fa-check-circle"></i> 已登录</span>
        {% else %}
        <span class="login-badge guest" style="margin-right:auto"><i class="fas fa-user-slash"></i> 游客模式</span>
        {% endif %}
        {% if BILIBILI_UNAME %}
        <button class="btn btn-danger btn-sm" onclick="doLogout()"><i class="fas fa-sign-out-alt"></i> 退出</button>
        {% else %}
        <button class="btn btn-primary btn-sm" onclick="openQR()"><i class="fas fa-qrcode"></i> 扫码登录</button>
        {% endif %}
      </div>
      <div style="font-size:.76rem;color:#6e7681;line-height:1.55">
        <i class="fas fa-info-circle" style="color:#58a6ff"></i>
        登录后使用账号身份连接，解决风控问题，弹幕礼物接收更稳定。Cookie仅保存在你的电脑上。
      </div>
    </div>

    <!-- 配置卡 -->
    <div class="card">
      <div class="card-title"><i class="fas fa-cog"></i> 设置</div>
      <div class="form-group">
        <label><i class="fas fa-tv"></i> B站直播间ID（要弹幕转发的直播间）</label>
        <input type="number" id="BILIBILI_ROOM_ID" value="{{ BILIBILI_ROOM_ID }}" placeholder="请输入直播间ID">
        <div id="room-history" style="margin-top:5px;font-size:.8rem;color:#8b949e;max-height:100px;overflow-y:auto"></div>
        <button class="btn btn-secondary btn-sm" onclick="clearRoomHistory()" style="margin-top:5px;width:100%"><i class="fas fa-trash-alt"></i> 清空历史记录</button>
      </div>
      <div class="form-group">
        <label><i class="fas fa-gamepad"></i> PS5 Twitch频道名（用于识别PS5设备）</label>
        <input type="text" id="TWITCH_CHANNEL" value="{{ TWITCH_CHANNEL }}" placeholder="例如: icenoproblem">
      </div>
      <div class="form-group">
        <label><i class="fas fa-history"></i> PS5连接超时时间（小时，超过则自动断开）</label>
        <input type="number" id="HEARTBEAT_TIMEOUT" value="{{ HEARTBEAT_TIMEOUT }}" placeholder="默认: 5">
      </div>
      <div class="form-group">
        <label><i class="fas fa-list"></i> 最多保留弹幕数量（超过后会自动清理）</label>
        <input type="number" id="MAX_SEEN_DANMAKU" value="{{ MAX_SEEN_DANMAKU }}" placeholder="默认: 1000">
      </div>
      <div class="form-group">
        <label><i class="fas fa-gift"></i> 最多保留礼物数量（超过后会自动清理）</label>
        <input type="number" id="MAX_SEEN_GIFT" value="{{ MAX_SEEN_GIFT }}" placeholder="默认: 500">
      </div>
      <div class="form-group">
        <label><i class="fas fa-terminal"></i> 最多保留日志数量（超过后会自动清理）</label>
        <input type="number" id="MAX_LOG_ITEMS" value="{{ MAX_LOG_ITEMS }}" placeholder="默认: 50">
      </div>
      <div class="form-group">
        <label><i class="fas fa-redo"></i> 重连延迟时间（秒，B站连接断开后的等待时间）</label>
        <input type="number" id="RECONNECT_DELAY" value="{{ RECONNECT_DELAY }}" placeholder="默认: 5">
      </div>
      <div class="toggle-row">
        <div class="toggle">
          <input type="checkbox" id="ENABLE_GIFT" {{ 'checked' if ENABLE_GIFT else '' }}>
          <div class="toggle-slider"></div>
        </div>
        <label for="ENABLE_GIFT"><i class="fas fa-gift"></i> 接收礼物、舰长、醒目留言（开启后会显示并转发到PS5）</label>
      </div>
      <div class="irc-info">
        <b>🎮 PS5 弹幕连接：</b><br>
        需劫持ps5 dns给本机服务器地址 ：<br>
        服务器地址：<b>{{ local_ip }}</b>（你的电脑IP）<br>
        端口：<b>6667</b><br>
        劫持目标：<b>contribute.live-video.net </b><br>
                        <b>global-contribute.live-video.net</b><br>
                        <b>apn20.contribute.live-video.net</b><br>
                        <b>tmi.twitch.tv</b><br>
                        <b>irc.twitch.tv</b><br>
        <span style="color:#f0883e;font-size:.75rem;margin-top:8px;display:block"><i class="fas fa-exclamation-triangle"></i> 重要提示：DNS劫持时只能劫持 上述，不要劫持其他域名，开播需要给ps5开加速器，否则会导致PS5无法启动直播！</span>
      </div>
      <div class="btn-row">
        <button class="btn btn-primary" onclick="saveConfig()"><i class="fas fa-save"></i> 保存配置</button>
        <button class="btn btn-secondary" onclick="refreshStatus()"><i class="fas fa-sync-alt"></i> 刷新状态</button>
      </div>
      <div style="font-size:.7rem;color:#6e7681;margin-top:8px;text-align:center">
        <i class="fas fa-info-circle"></i> 保存配置后，请关闭程序并重新启动，新的设置才会生效
      </div>
    </div>

  </div>

  <div class="right-col">

    <!-- 状态卡 -->
    <div class="card">
      <div class="card-title"><i class="fas fa-tachometer-alt"></i> 运行状态</div>
      <div class="status-grid">
        <div class="stat">
          <div id="s-irc" class="stat-val {{ 'on' if irc_running else 'off' }}">{{ '运行' if irc_running else '停止' }}</div>
          <div class="stat-label">PS5连接服务</div>
        </div>
        <div class="stat">
          <div id="s-ws" class="stat-val {{ 'on' if ws_running else 'off' }}">{{ '已连接' if ws_running else '未连接' }}</div>
          <div class="stat-label">B站直播连接</div>
        </div>
        <div class="stat">
          <div id="s-clients" class="stat-val {{ 'on' if active_clients > 0 else 'off' }}">{{ active_clients }}</div>
          <div class="stat-label">PS5在线设备</div>
        </div>
        <div class="stat">
          <div id="s-dm-cnt" class="stat-val">{{ danmaku_count }}</div>
          <div class="stat-label">弹幕总数</div>
        </div>
        <div class="stat">
          <div id="s-gift-cnt" class="stat-val">{{ gift_count }}</div>
          <div class="stat-label">礼物总数</div>
        </div>
        <div class="stat">
          <div id="s-sc-cnt" class="stat-val" style="color:#f6c90e">{{ sc_count }}</div>
          <div class="stat-label">醒目留言</div>
        </div>
      </div>
      <div style="font-size:.78rem;color:#8b949e;margin-top:10px;padding-top:10px;border-top:1px solid rgba(48,54,61,.5)">
        <i class="fas fa-info-circle" style="color:#58a6ff;margin-right:6px"></i>
        当前监听：<span id="current-room-id">{{ BILIBILI_ROOM_ID }}</span>
        <span id="real-room-id-info" style="margin-left:10px;color:#8b949e"></span>
      </div>
    </div>

    <!-- RTMP推流状态卡 -->
    <div class="card rtmp-card">
      <div class="card-title">
        <i class="fas fa-video" style="color:#f0883e"></i> RTMP 推流状态
        <span class="badge" id="rtmp-badge" style="margin-left:auto;background:#30363d;color:#8b949e;font-size:.72rem;padding:2px 8px;border-radius:10px">未推流</span>
      </div>

      <div style="font-size:.78rem;color:#8b949e;margin-bottom:12px">
        <i class="fas fa-info-circle" style="color:#f0883e;margin-right:6px"></i>
        显示当前PS5推流的状态信息。需要在路由器配置DNS劫持才能显示推流状态。
      </div>

      <div class="rtmp-info-grid">
        <div class="rtmp-info-item">
          <div class="rtmp-info-label"><i class="fas fa-stream"></i> 推流码</div>
          <div style="display:flex;align-items:center;gap:8px;flex:1">
            <div class="rtmp-info-value" id="rtmp-key" style="flex:1;font-family:'Consolas','Monaco',monospace;word-break:break-all">-</div>
            <button class="btn btn-secondary btn-sm" onclick="copyRTMPKey()" title="复制推流地址"><i class="fas fa-copy"></i></button>
          </div>
        </div>
        <div class="rtmp-info-item">
          <div class="rtmp-info-label"><i class="fas fa-code"></i> 编码格式</div>
          <div class="rtmp-info-value" id="rtmp-encoding">-</div>
        </div>
        <div class="rtmp-info-item">
          <div class="rtmp-info-label"><i class="fas fa-tachometer-alt"></i> 码率</div>
          <div class="rtmp-info-value" id="rtmp-bitrate">-</div>
        </div>
        <div class="rtmp-info-item">
          <div class="rtmp-info-label"><i class="fas fa-expand"></i> 分辨率</div>
          <div class="rtmp-info-value" id="rtmp-resolution">-</div>
        </div>
        <div class="rtmp-info-item">
          <div class="rtmp-info-label"><i class="fas fa-sync"></i> 帧率</div>
          <div class="rtmp-info-value" id="rtmp-fps">-</div>
        </div>
        <div class="rtmp-info-item">
          <div class="rtmp-info-label"><i class="fas fa-clock"></i> 最后更新</div>
          <div class="rtmp-info-value" id="rtmp-last-update">-</div>
        </div>
      </div>

      <div style="margin-top:12px;font-size:.76rem;color:#6e7681">
        <i class="fas fa-exclamation-circle" style="color:#f0883e;margin-right:4px"></i>
        RTMP推流信息需要在PS5开启直播并通过DNS劫持后才会显示。
      </div>
    </div>

    <!-- 日志卡 -->
    <div class="card log-card">
      <div class="card-title"><i class="fas fa-terminal"></i> 运行日志</div>
      <ul class="feed-list" id="log-list" style="background:rgba(13,17,23,.7);border-radius:8px;padding:10px;font-family:'Consolas','Monaco',monospace;font-size:.76rem;line-height:1.6;max-height:280px">
        <li class="no-item" style="padding:20px;font-size:.78rem"><i class="fas fa-clock" style="margin-right:6px"></i>等待运行日志...</li>
      </ul>
    </div>

    <!-- 弹幕和礼物记录区 - 分两列显示 -->
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">

      <!-- 弹幕记录卡 -->
      <div class="card feed-card">
        <div class="card-title">
          <i class="fas fa-comment-dots" style="color:#4f8cff"></i> 弹幕记录
          <span class="badge" id="cnt-danmaku" style="margin-left:auto;background:#4f8cff;color:#fff;font-size:.72rem;padding:2px 8px;border-radius:10px">0</span>
        </div>

        <!-- 操作按钮 -->
        <div class="feed-actions" style="margin-bottom:8px">
          <button class="btn btn-secondary btn-sm" onclick="exportCSV('danmaku')"><i class="fas fa-download"></i> 导出</button>
          <button class="btn btn-danger btn-sm" style="margin-left:auto" onclick="clearRecords('danmaku')"><i class="fas fa-trash-alt"></i> 清空</button>
        </div>

        <!-- 弹幕列表 -->
        <ul class="feed-list" id="danmaku-list" style="max-height:400px">
          <li class="no-item"><i class="fas fa-comment-dots" style="font-size:1.5rem;display:block;margin-bottom:8px;color:#30363d"></i>暂无弹幕记录</li>
        </ul>
      </div>

      <!-- 礼物记录卡 -->
      <div class="card feed-card">
        <div class="card-title">
          <i class="fas fa-gift" style="color:#f0883e"></i> 礼物 &amp; 舰长 &amp; SC
          <span class="badge" id="cnt-gift-total" style="margin-left:auto;background:#f0883e;color:#fff;font-size:.72rem;padding:2px 8px;border-radius:10px">0</span>
        </div>

        <!-- Tab -->
        <div class="tabs" id="gift-tabs" style="margin-bottom:8px">
          <div class="tab active" data-tab="all" onclick="switchGiftTab('all')">
            全部 <span class="badge" id="cnt-gift">0</span>
          </div>
          <div class="tab" data-tab="gift" onclick="switchGiftTab('gift')">
            礼物 <span class="badge" id="cnt-gift-only">0</span>
          </div>
          <div class="tab" data-tab="guard" onclick="switchGiftTab('guard')">
            舰长 <span class="badge" id="cnt-guard">0</span>
          </div>
          <div class="tab" data-tab="sc" onclick="switchGiftTab('sc')">
            SC <span class="badge" id="cnt-sc">0</span>
          </div>
        </div>

        <!-- 操作按钮 -->
        <div class="feed-actions" style="margin-bottom:8px">
          <button class="btn btn-secondary btn-sm" onclick="exportCSV('gift')"><i class="fas fa-download"></i> 导出</button>
          <button class="btn btn-danger btn-sm" style="margin-left:auto" onclick="clearRecords('gift')"><i class="fas fa-trash-alt"></i> 清空</button>
        </div>

        <!-- 礼物列表 -->
        <ul class="feed-list" id="gift-list" style="max-height:400px">
          <li class="no-item"><i class="fas fa-gift" style="font-size:1.5rem;display:block;margin-bottom:8px;color:#30363d"></i>暂无礼物记录</li>
        </ul>
      </div>

    </div>

  </div>
</div>

<div class="footer"><a href="https://space.bilibili.com/2250922" target="_blank">阿冰没问题的B站首页</a></div>
</div>

<!-- QR 弹窗 -->
<div class="qr-modal" id="qr-modal">
  <div class="qr-box">
    <button class="qr-close" onclick="closeQR()"><i class="fas fa-times"></i></button>
    <h3><i class="fas fa-qrcode" style="color:#7b2ff7;margin-right:6px"></i>扫码登录 B站</h3>
    <p>使用 bilibili APP 扫描下方二维码</p>
    <div class="qr-img-wrap">
      <img id="qr-img" src="" alt="加载中..." width="190" height="190">
    </div>
    <div class="qr-status waiting" id="qr-status-text">等待扫码...</div>
    <div class="qr-timer" id="qr-timer"></div>
    <button class="qr-refresh-btn" id="qr-refresh-btn" onclick="refreshQR()" style="display:none">
      <i class="fas fa-redo"></i> 重新获取
    </button>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);

let allDanmaku = [];
let allGift = [];
let currentGiftTab = 'all';
let autoScroll = true;
let lastDanmakuTs = 0;
let lastGiftTs = 0;

function switchGiftTab(tab) {
  currentGiftTab = tab;
  document.querySelectorAll('#gift-tabs .tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  renderGiftList();
}

function esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function avatarLetter(user) {
  const s = String(user || '?');
  for (let i=0; i<s.length; i++) {
    const c = s.charCodeAt(i);
    if (c > 127) return s[i];
  }
  return s[0].toUpperCase();
}

function showToast(type, msg) {
  const t = document.createElement('div');
  t.className = 'toast ' + type;
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3500);
}

function renderDmItem(it) {
  const letter = avatarLetter(it.user);
  return `<li class="dm-item">
    <div class="dm-avatar">${esc(letter)}</div>
    <div class="dm-body">
      <div class="dm-user">${esc(it.user)}</div>
      <div class="dm-text">${esc(it.text)}</div>
      <div class="dm-time"><i class="far fa-clock" style="margin-right:4px"></i>${it.time||''}</div>
    </div>
  </li>`;
}

function renderDanmakuList() {
  const ul = $('danmaku-list');
  console.log('renderDanmakuList: 找到元素', !!ul);
  if(!ul) return;

  console.log('renderDanmakuList: allDanmaku.length =', allDanmaku.length);
  if(!allDanmaku.length){
    ul.innerHTML = '<li class="no-item"><i class="fas fa-comment-dots" style="font-size:1.5rem;display:block;margin-bottom:8px;color:#30363d"></i>暂无弹幕记录</li>';
    return;
  }

  const html = allDanmaku.slice(0, 500).map(it => renderDmItem(it)).join('');
  ul.innerHTML = html;
  console.log('renderDanmakuList: 已渲染', html.length, '字节');

  if(autoScroll) {
    ul.scrollTop = 0;
  }
}

function renderGiftItem(it) {
  const t = it.type || 'gift';
  let icon, label, priceHtml = '', extraHtml = '', userPrefix = '';
  const guardColors = ['','guard-1','guard-2','guard-3'];

  if (t === 'gift') {
    icon = '<i class="fas fa-gift"></i>';
    const coinClass = it.coin === '电池' ? 'gold' : 'silver';
    priceHtml = it.price > 0 ? `<span class="gift-price ${coinClass}">${it.coin} ${it.price}</span>` : `<span class="gift-price silver">${it.coin}</span>`;
    label = `<i class="fas fa-gift" style="margin-right:6px;color:#f0883e"></i>${esc(it.name)} <b>x${it.num}</b>`;
    userPrefix = '送出';
  } else if (t === 'guard') {
    icon = '<i class="fas fa-anchor"></i>';
    const level = it.guard_level || 3;
    const cls = guardColors[level] || 'guard-3';
    label = `<i class="fas fa-anchor" style="margin-right:6px;color:#58a6ff"></i>开通了 <span class="${cls}">${esc(it.name)}</span> x${it.num}`;
    priceHtml = `<span class="gift-price gold">大航海</span>`;
    userPrefix = '开通';
  } else if (t === 'sc') {
    icon = '<i class="fas fa-comment-dollar"></i>';
    label = `<i class="fas fa-comment-dollar" style="margin-right:6px;color:#f6c90e"></i>醒目留言 <b style="color:#f6c90e">¥${it.price}</b>`;
    extraHtml = `<div class="sc-message">"${esc(it.text||'')}"</div>`;
    priceHtml = `<span class="gift-price gold">¥${it.price}</span>`;
    userPrefix = '发送';
  }

  return `<li class="gift-item t-${t}">
    <div class="gift-icon t-${t}">${icon}</div>
    <div class="gift-body">
      <div class="gift-user">${userPrefix ? `<span style="font-weight:400;color:#6e7681;margin-right:6px">${userPrefix}</span>` : ''}${esc(it.user)}</div>
      <div class="gift-desc">${label}</div>
      ${extraHtml}
      <div class="gift-meta">${priceHtml}<span class="gift-time"><i class="far fa-clock" style="margin-right:4px"></i>${it.time||''}</span></div>
    </div>
  </li>`;
}

function renderGiftList() {
  const ul = $('gift-list');
  console.log('renderGiftList: 找到元素', !!ul);
  if(!ul) return;

  console.log('renderGiftList: allGift.length =', allGift.length, 'currentGiftTab =', currentGiftTab);
  let items = [];
  if(currentGiftTab === 'all') {
    items = allGift.slice(0, 500);
  } else {
    items = allGift.filter(it => it.type === currentGiftTab).slice(0, 500);
  }

  console.log('renderGiftList: items.length =', items.length);
  if(!items.length){
    const emptyMsg = {
      'gift': '<i class="fas fa-gift" style="font-size:1.5rem;display:block;margin-bottom:8px;color:#30363d"></i>暂无礼物记录',
      'guard': '<i class="fas fa-anchor" style="font-size:1.5rem;display:block;margin-bottom:8px;color:#30363d"></i>暂无舰长记录',
      'sc': '<i class="fas fa-comment-dollar" style="font-size:1.5rem;display:block;margin-bottom:8px;color:#30363d"></i>暂无醒目留言',
      'all': '<i class="fas fa-gift" style="font-size:1.5rem;display:block;margin-bottom:8px;color:#30363d"></i>暂无礼物记录'
    };
    ul.innerHTML = '<li class="no-item">' + (emptyMsg[currentGiftTab] || emptyMsg['all']) + '</li>';
    console.log('renderGiftList: 显示空消息');
    return;
  }

  const html = items.map(it => renderGiftItem(it)).join('');
  ul.innerHTML = html;
  console.log('renderGiftList: 已渲染', html.length, '字节');

  if(autoScroll) {
    ul.scrollTop = 0;
  }
}

function updateBadges() {
  // 弹幕计数
  if($('cnt-danmaku')) $('cnt-danmaku').textContent = allDanmaku.length;

  // 礼物统计
  const giftOnly = allGift.filter(i=>i.type==='gift').length;
  const guardCount = allGift.filter(i=>i.type==='guard').length;
  const scCount = allGift.filter(i=>i.type==='sc').length;

  if($('cnt-gift-total')) $('cnt-gift-total').textContent = allGift.length;
  if($('cnt-gift')) $('cnt-gift').textContent = allGift.length;
  if($('cnt-gift-only')) $('cnt-gift-only').textContent = giftOnly;
  if($('cnt-guard')) $('cnt-guard').textContent = guardCount;
  if($('cnt-sc')) $('cnt-sc').textContent = scCount;
}

function refreshStatus() {
  fetch('/status').then(r=>r.json()).then(d=>{
    if (!d) {
      console.error('status返回数据为空');
      return;
    }
    console.log('refreshStatus: 接收到数据', d);
    if($('s-irc')) {
      $('s-irc').textContent = d.irc_running ? '运行' : '停止';
      $('s-irc').className = 'stat-val ' + (d.irc_running ? 'on' : 'off');
    }
    if($('s-ws')) {
      $('s-ws').textContent = d.ws_running ? '已连接' : '未连接';
      $('s-ws').className = 'stat-val ' + (d.ws_running ? 'on' : 'off');
    }
    if($('s-clients')) {
      $('s-clients').textContent = d.active_clients || 0;
      $('s-clients').className = 'stat-val ' + ((d.active_clients || 0) > 0 ? 'on' : 'off');
    }
    if($('s-dm-cnt')) $('s-dm-cnt').textContent = d.danmaku_count || 0;
    if($('s-gift-cnt')) $('s-gift-cnt').textContent = d.gift_count || 0;
    if($('s-sc-cnt')) $('s-sc-cnt').textContent = d.sc_count || 0;

    // 显示房间ID信息
    if($('current-room-id')) {
      $('current-room-id').textContent = d.room_id || 0;
    }
    if($('real-room-id-info')) {
      const roomId = d.room_id || 0;
      const realRoomId = d.real_room_id || 0;
      if (roomId !== realRoomId) {
        $('real-room-id-info').textContent = `(真实ID: ${realRoomId})`;
      } else {
        $('real-room-id-info').textContent = '';
      }
    }

    if (d.recent_danmaku !== undefined) {
      allDanmaku = d.recent_danmaku || [];
      console.log('refreshStatus: 更新弹幕列表', allDanmaku.length, '条');
      renderDanmakuList();
    }
    if (d.recent_gift !== undefined) {
      allGift = d.recent_gift || [];
      console.log('refreshStatus: 更新礼物列表', allGift.length, '条');
      renderGiftList();
    }

    updateBadges();
  }).catch(err => {
    console.error('获取状态失败:', err);
  });
}

function updateRtmpStatus() {
  fetch('/api/rtmp/status').then(r=>r.json()).then(d=>{
    console.log('updateRtmpStatus: 接收到数据', d);
    if(!d) return;

    // 更新徽章
    const badge = $('rtmp-badge');
    if(badge) {
      if(d.active) {
        badge.textContent = '推流中';
        badge.style.background = 'rgba(63,185,80,.14)';
        badge.style.color = '#3fb950';
        badge.style.border = '1px solid #238636';
      } else {
        badge.textContent = '未推流';
        badge.style.background = '#30363d';
        badge.style.color = '#8b949e';
        badge.style.border = '1px solid #30363d';
      }
    }

    // 更新各个字段
    const fields = ['key', 'encoding', 'bitrate', 'resolution', 'fps', 'last-update'];
    const displayNames = {
      'key': d.stream_key ? `rtmp://{{ local_ip }}/app/${d.stream_key}` : '-',
      'encoding': d.encoding || '-',
      'bitrate': d.bitrate > 0 ? d.bitrate + ' kbps' : '-',
      'resolution': d.resolution || '-',
      'fps': d.fps > 0 ? d.fps + ' fps' : '-',
      'last-update': d.last_update > 0 ? formatTime(d.last_update) : '-'
    };

    fields.forEach(field => {
      const el = $(`rtmp-${field}`);
      if(el) {
        el.textContent = displayNames[field];
        el.className = 'rtmp-info-value ' + (d.active && displayNames[field] !== '-' ? 'active' : 'inactive');
      }
    });

    // 保存当前推流地址供复制使用
    if(d.stream_key) {
      window.currentRTMPUrl = `rtmp://{{ local_ip }}/app/${d.stream_key}`;
    } else {
      window.currentRTMPUrl = '';
    }
  }).catch(err => {
    console.error('获取RTMP状态失败:', err);
  });
}

function formatTime(timestamp) {
  const now = Math.floor(Date.now() / 1000);
  const diff = now - timestamp;
  if(diff < 60) return `${diff}秒前`;
  if(diff < 3600) return `${Math.floor(diff/60)}分钟前`;
  if(diff < 86400) return `${Math.floor(diff/3600)}小时前`;
  return `${Math.floor(diff/86400)}天前`;
}

function exportCSV(type) {
  window.location.href = '/api/export/' + type;
}

function clearRecords(type) {
  const target = type || 'all';
  const msg = type === 'danmaku' ? '确定清空所有弹幕记录吗？' :
              type === 'gift' ? '确定清空所有礼物记录吗？' :
              '确定清空所有弹幕和礼物记录吗？';

  if (!confirm(msg)) return;
  fetch('/api/clear', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({target:target})})
  .then(r=>r.json()).then(d=>{
    if (d.code === 0) {
      if(target === 'danmaku') {
        allDanmaku = [];
        renderDanmakuList();
      } else if(target === 'gift') {
        allGift = [];
        renderGiftList();
      } else {
        allDanmaku = [];
        allGift = [];
        renderDanmakuList();
        renderGiftList();
      }
      updateBadges();
      showToast('ok', '已清空记录');
    }
  }).catch(e=>showToast('err','清空失败: '+e));
}

function copyRTMPKey() {
  if(window.currentRTMPUrl) {
    navigator.clipboard.writeText(window.currentRTMPUrl).then(()=>{
      showToast('ok', '推流地址已复制到剪贴板');
    }).catch(()=>{
      showToast('err', '复制失败，请手动复制');
    });
  } else {
    showToast('err', '当前没有推流，无法复制');
  }
}

function saveConfig() {
  const cfg = {};
  document.querySelectorAll('input[type=text],input[type=number]').forEach(el=>{
    if (el.id && el.id !== 'qr-img') cfg[el.id] = el.value;
  });
  cfg.ENABLE_GIFT = $('ENABLE_GIFT').checked;

  console.log('saveConfig: 准备保存配置', cfg);
  console.log('saveConfig: BILIBILI_ROOM_ID =', cfg.BILIBILI_ROOM_ID);

  fetch('/save_config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(cfg)})
  .then(r=>r.json()).then(d=>{
    console.log('saveConfig: 服务器响应', d);
    showToast(d.code===0?'ok':'err', d.msg);
    if (d.code===0) setTimeout(()=>location.reload(), 1800);
  }).catch(e=>{
    console.error('saveConfig: 保存失败', e);
    showToast('err','保存失败: '+e);
  });
}

let qrPollTimer=null, qrCountdown=null, qrExpireAt=0;

function openQR() { $('qr-modal').classList.add('active'); refreshQR(); }
function closeQR() { $('qr-modal').classList.remove('active'); clearInterval(qrPollTimer); clearInterval(qrCountdown); }

function refreshQR() {
  $('qr-img').src=''; setQRStatus('waiting','正在获取二维码...');
  $('qr-refresh-btn').style.display='none';
  clearInterval(qrPollTimer); clearInterval(qrCountdown);
  fetch('/api/qr/generate').then(r=>r.json()).then(d=>{
    if (d.code===0) {
      $('qr-img').src = d.img; qrExpireAt = Date.now()+180000;
      setQRStatus('waiting','等待扫码...'); startQRCountdown(); startQRPoll();
    } else { setQRStatus('expired','获取失败，请重试'); $('qr-refresh-btn').style.display='inline-block'; }
  }).catch(()=>{ setQRStatus('expired','网络错误'); $('qr-refresh-btn').style.display='inline-block'; });
}

function startQRCountdown() {
  clearInterval(qrCountdown);
  qrCountdown = setInterval(()=>{
    const left = Math.max(0, Math.round((qrExpireAt-Date.now())/1000));
    $('qr-timer').textContent = left>0 ? `${left} 秒后过期` : '已过期';
    if (left===0) clearInterval(qrCountdown);
  }, 1000);
}

function startQRPoll() {
  clearInterval(qrPollTimer);
  qrPollTimer = setInterval(()=>{
    fetch('/api/qr/poll').then(r=>r.json()).then(d=>{
      if (d.status==='success') {
        clearInterval(qrPollTimer); clearInterval(qrCountdown);
        setQRStatus('success','登录成功！欢迎 '+d.uname);
        setTimeout(()=>{ closeQR(); location.reload(); }, 1600);
      } else if (d.status==='scanned') {
        setQRStatus('scanned','已扫码，请确认...');
      } else if (d.status==='expired') {
        clearInterval(qrPollTimer);
        setQRStatus('expired','二维码已过期');
        $('qr-refresh-btn').style.display='inline-block';
      }
    }).catch(()=>{});
  }, 2000);
}

function setQRStatus(cls, text) {
  const el = $('qr-status-text');
  el.className = 'qr-status '+cls; el.textContent = text;
}

function doLogout() {
  if (!confirm('确定退出B站账号吗？')) return;
  fetch('/api/logout',{method:'POST'}).then(r=>r.json()).then(d=>{
    if (d.code===0) { showToast('ok','已退出'); setTimeout(()=>location.reload(),1200); }
    else showToast('err', d.msg);
  }).catch(e=>showToast('err','退出失败'));
}

function refreshLogs() {
  fetch('/api/logs').then(r=>r.json()).then(d=>{
    if(d && d.code===0 && d.logs && d.logs.length){
      renderLogs(d.logs);
    }
  }).catch(err=>{
    console.error('获取日志失败:', err);
  });
}

function renderLogs(logs) {
  const ul = $('log-list');
  if(!ul) return;

  if(!logs.length){
    ul.innerHTML = '<li class="no-item" style="padding:20px;font-size:.78rem"><i class="fas fa-clock" style="margin-right:6px"></i>暂无日志</li>';
    return;
  }
  const html = logs.map(log=>{
    const level = log.level||'info';
    const levelClass = level==='error'?'error':level==='warning'?'warning':level==='success'?'success':'info';
    return `<li class="log-item">
      <span class="log-time">${log.time||''}</span>
      <span class="log-level ${levelClass}">[${level.toUpperCase()}]</span>
      <span class="log-msg">${esc(log.msg)}</span>
    </li>`;
  }).join('');
  ul.innerHTML = html;
}

function loadRoomHistory() {
  fetch('/api/rooms/history').then(r=>r.json()).then(d=>{
    const historyDiv = $('room-history');
    if(!historyDiv) return;

    if(d && d.code===0 && d.history && d.history.length){
      let html = '<div style="margin-bottom:5px;color:#6e7681">历史记录：</div>';
      d.history.forEach(h=>{
        const roomId = h.room_id;
        const title = h.room_title || h.title || h.up_name || `直播间${roomId}`;
        html += `<div style="padding:3px 5px;cursor:pointer;border-radius:3px" onclick="$('BILIBILI_ROOM_ID').value=${roomId};console.log('选择历史记录:',${roomId})">${roomId} - ${title}</div>`;
      });
      historyDiv.innerHTML = html;
    } else {
      historyDiv.innerHTML = '';
    }
  }).catch(err=>{
    console.error('加载历史记录失败:', err);
  });
}

function clearRoomHistory() {
  if(!confirm('确定要清空所有直播间历史记录吗？')) return;

  fetch('/api/rooms/clear', {method:'POST', headers:{'Content-Type':'application/json'}})
  .then(r=>r.json()).then(d=>{
    if(d.code===0){
      showToast('ok', '已清空历史记录');
      // 重新加载历史记录（清空后应该为空）
      loadRoomHistory();
    }else{
      showToast('err', d.msg || '清空失败');
    }
  }).catch(e=>showToast('err','清空失败: '+e));
}

window.onload = function() {
  refreshStatus();
  loadRoomHistory();
  setInterval(refreshStatus, 2000);
  setInterval(refreshLogs, 1500);
  setInterval(updateRtmpStatus, 2000);
  refreshLogs();
  updateRtmpStatus();
};
</script>
</body>
</html>"""


# ==================== Web 路由 ====================
def get_local_ip() -> str:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "127.0.0.1"


def start_web(irc_server):
    app = Flask("ps5-danmaku-web")
    app.config['JSON_AS_ASCII'] = False

    @app.route('/')
    def index():
        return render_template_string(WEB_HTML,
            BILIBILI_ROOM_ID=CONFIG["BILIBILI_ROOM_ID"],
            TWITCH_CHANNEL=CONFIG["TWITCH_CHANNEL"],
            HEARTBEAT_TIMEOUT=CONFIG["HEARTBEAT_TIMEOUT"],
            RECONNECT_DELAY=CONFIG["RECONNECT_DELAY"],
            MAX_SEEN_DANMAKU=CONFIG["MAX_SEEN_DANMAKU"],
            MAX_SEEN_GIFT=CONFIG["MAX_SEEN_GIFT"],
            MAX_LOG_ITEMS=CONFIG.get("MAX_LOG_ITEMS", 50),
            BILIBILI_UNAME=CONFIG.get("BILIBILI_UNAME", ""),
            BILIBILI_UID=CONFIG.get("BILIBILI_UID", 0),
            ENABLE_GIFT=CONFIG["ENABLE_GIFT"],
            irc_running=IRC_RUNNING,
            ws_running=WS_RUNNING,
            active_clients=len(ACTIVE_CONNECTIONS),
            danmaku_count=DANMAKU_COUNT,
            gift_count=GIFT_COUNT,
            sc_count=SC_COUNT,
            local_ip=get_local_ip()
        )

    @app.route('/test')
    def test_page():
        """测试页面路由"""
        try:
            test_file = os.path.join(BASE_DIR, "test_simple.html")
            if os.path.exists(test_file):
                with open(test_file, "r", encoding="utf-8") as f:
                    return f.read()
            else:
                return "<!DOCTYPE html><html><head><meta charset='UTF-8'><title>测试页面</title></head><body><h1>测试页面文件不存在</h1><p>请确保 test_simple.html 文件存在</p></body></html>"
        except Exception as e:
            logger.error(f"加载测试页面失败: {e}")
            return f"<!DOCTYPE html><html><head><meta charset='UTF-8'><title>错误</title></head><body><h1>加载失败</h1><p>错误: {e}</p></body></html>"

    @app.route('/status')
    def status():
        global _GLOBAL_BILI_CLIENT
        # 获取真实的房间ID
        real_room_id = CONFIG["BILIBILI_ROOM_ID"]
        if _GLOBAL_BILI_CLIENT:
            real_room_id = _GLOBAL_BILI_CLIENT.real_room_id

        return jsonify({
            "irc_running": IRC_RUNNING,
            "ws_running": WS_RUNNING,
            "active_clients": len(ACTIVE_CONNECTIONS),
            "danmaku_count": DANMAKU_COUNT,
            "gift_count": GIFT_COUNT,
            "guard_count": GUARD_COUNT,
            "sc_count": SC_COUNT,
            "room_id": CONFIG["BILIBILI_ROOM_ID"],  # 用户输入的房间ID
            "real_room_id": real_room_id,  # 真实的房间ID
            "recent_danmaku": list(recent_danmaku_log),
            "recent_gift": list(recent_gift_log),
            "logged_in": bool(CONFIG.get("BILIBILI_UNAME")),
            "uname": CONFIG.get("BILIBILI_UNAME", "")
        })

    @app.route('/save_config', methods=['POST'])
    def save_config_route():
        global NEED_RECONNECT, NEW_ROOM_ID
        try:
            data = request.get_json()
            if not data:
                return jsonify({"code": 1, "msg": "配置为空"})

            # 检查房间ID是否改变
            old_room_id = CONFIG.get("BILIBILI_ROOM_ID", 0)
            new_room_id = data.get("BILIBILI_ROOM_ID")

            # 如果房间ID是字符串，尝试转为整数
            if new_room_id:
                try:
                    new_room_id = int(new_room_id)
                    data["BILIBILI_ROOM_ID"] = new_room_id
                except:
                    pass

            save_config(data)

            # 如果房间ID改变了，添加到历史记录
            if new_room_id and new_room_id != old_room_id:
                try:
                    # 尝试异步获取直播间信息
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    room_info = loop.run_until_complete(get_room_info(new_room_id))
                    loop.close()

                    room_title = room_info.get("room_title", f"直播间{new_room_id}")
                    add_room_to_history(new_room_id, room_title)
                    _add_web_log("success", f"已添加直播间到历史: {room_title}")
                except Exception as e:
                    logger.error(f"获取房间信息失败: {e}")
                    # 即使获取失败也添加到历史
                    add_room_to_history(new_room_id)

            # 自动重启程序
            _add_web_log("info", "配置已保存，正在重启程序...")
            logger.info("配置已保存，正在重启程序...")

            # 使用线程延迟重启，给前端足够时间显示消息
            def delayed_restart():
                import time
                import subprocess
                import sys
                time.sleep(1.5)  # 等待1.5秒让前端显示消息
                logger.info("正在重启程序...")
                # 使用subprocess重新启动当前脚本
                try:
                    if sys.platform == 'win32':
                        subprocess.Popen([sys.executable, os.path.abspath(__file__)], creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
                    else:
                        subprocess.Popen([sys.executable, os.path.abspath(__file__)])
                    sys.exit(0)
                except Exception as e:
                    logger.error(f"重启失败: {e}")

            restart_thread = threading.Thread(target=delayed_restart, daemon=True)
            restart_thread.start()

            return jsonify({"code": 0, "msg": "配置已保存，程序正在重启，请稍候..."})

        except Exception as e:
            return jsonify({"code": 1, "msg": f"保存失败: {e}"})

    @app.route('/api/export/danmaku')
    def export_danmaku():
        """导出弹幕CSV"""
        import csv, io as _io
        from flask import Response
        buf = _io.StringIO()
        w = csv.writer(buf)
        w.writerow(["时间", "用户", "内容"])
        for item in reversed(list(recent_danmaku_log)):
            w.writerow([item.get("time", ""), item.get("user", ""), item.get("text", "")])
        csv_data = "\ufeff" + buf.getvalue()  # BOM for Excel
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename=danmaku_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"}
        )

    @app.route('/api/export/gift')
    def export_gift():
        """导出礼物CSV"""
        import csv, io as _io
        from flask import Response
        buf = _io.StringIO()
        w = csv.writer(buf)
        w.writerow(["时间", "类型", "用户", "礼物/内容", "数量", "价值"])
        type_map = {"gift": "礼物", "guard": "大航海", "sc": "SC醒目留言"}
        for item in reversed(list(recent_gift_log)):
            t = type_map.get(item.get("type", "gift"), "礼物")
            content = item.get("text", item.get("name", ""))
            w.writerow([item.get("time", ""), t, item.get("user", ""), content,
                        item.get("num", 1), item.get("price", 0)])
        csv_data = "\ufeff" + buf.getvalue()
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename=gift_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"}
        )

    @app.route('/api/clear', methods=['POST'])
    def api_clear():
        """清空弹幕/礼物记录"""
        global DANMAKU_COUNT, GIFT_COUNT, GUARD_COUNT, SC_COUNT
        what = request.get_json(silent=True) or {}
        target = what.get("target", "all")
        if target in ("danmaku", "all"):
            recent_danmaku_log.clear()
            DANMAKU_COUNT = 0
        if target in ("gift", "all"):
            recent_gift_log.clear()
            GIFT_COUNT = 0
            GUARD_COUNT = 0
            SC_COUNT = 0
        return jsonify({"code": 0, "msg": "已清空"})

    # ---- 扫码登录 API ----
    @app.route('/api/qr/generate')
    def api_qr_generate():
        global LOGIN_STATE
        result = qr_generate()
        if not result:
            return jsonify({"code": 1, "msg": "获取二维码失败，请检查网络"})
        key = result["key"]
        url = result["url"]
        img_b64 = _gen_qr_b64(url)
        if not img_b64:
            return jsonify({"code": 1, "msg": "生成二维码图片失败（qrcode库未安装？）"})

        # 停止旧的轮询
        LOGIN_STATE["poll_active"] = False
        time.sleep(0.2)

        LOGIN_STATE.update({
            "qr_key": key,
            "qr_url": url,
            "qr_img_b64": img_b64,
            "status": "waiting",
            "uname": "",
            "uid": 0,
            "expire_at": time.time() + 180,
        })

        # 启动后台轮询线程
        t = threading.Thread(target=qr_login_thread, daemon=True, name="QRPoll")
        t.start()

        return jsonify({"code": 0, "img": img_b64, "key": key})

    @app.route('/api/qr/poll')
    def api_qr_poll():
        s = LOGIN_STATE["status"]
        return jsonify({
            "status": s,
            "uname": LOGIN_STATE.get("uname", ""),
            "uid": LOGIN_STATE.get("uid", 0),
            "logged_in": (s == "success" and bool(CONFIG.get("BILIBILI_UNAME")))
        })

    @app.route('/api/logout', methods=['POST'])
    def api_logout():
        ok = logout_bili()
        if ok:
            return jsonify({"code": 0, "msg": "已退出登录"})
        return jsonify({"code": 1, "msg": "退出失败"})

    @app.route('/api/logs')
    def api_logs():
        """获取Web日志"""
        return jsonify({
            "code": 0,
            "logs": list(web_log_queue)
        })

    @app.route('/api/rooms/history')
    def api_rooms_history():
        """获取直播间历史记录"""
        history = CONFIG.get("ROOM_HISTORY", [])
        if not isinstance(history, list):
            history = []
        return jsonify({
            "code": 0,
            "history": history
        })

    @app.route('/api/rooms/clear', methods=['POST'])
    def api_rooms_clear():
        """清空直播间历史记录"""
        global CONFIG
        try:
            CONFIG["ROOM_HISTORY"] = []
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(CONFIG, f, ensure_ascii=False, indent=4)
            _add_web_log("success", "已清空所有直播间历史记录")
            return jsonify({"code": 0, "msg": "已清空"})
        except Exception as e:
            return jsonify({"code": 1, "msg": f"清空失败: {e}"})

    @app.route('/api/rtmp/status')
    def api_rtmp_status():
        """获取RTMP推流状态"""
        status = get_rtmp_status()
        logger.debug(f'API /api/rtmp/status: 返回状态 {status}')
        return jsonify(status)

    @app.route('/api/rtmp/status/update', methods=['POST'])
    def api_rtmp_status_update():
        """更新RTMP推流状态（用于测试和外部更新）"""
        try:
            data = request.get_json() or {}
            update_rtmp_status(**data)
            return jsonify({"code": 0, "msg": "RTMP状态已更新"})
        except Exception as e:
            return jsonify({"code": 1, "msg": f"更新失败: {e}"})

    port = CONFIG.get("WEB_PORT", 5000)
    logger.info(f"Web 控制台: http://127.0.0.1:{port}  |  局域网: http://{get_local_ip()}:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)


# ==================== 主入口 ====================
_GLOBAL_IRC_SERVER = None
_GLOBAL_BILI_CLIENT = None


async def main():
    global _GLOBAL_IRC_SERVER, _GLOBAL_BILI_CLIENT

    irc_server = IRCServer()
    _GLOBAL_IRC_SERVER = irc_server

    bili_client = BiliLiveClient(CONFIG["BILIBILI_ROOM_ID"], irc_server)
    _GLOBAL_BILI_CLIENT = bili_client

    logger.info("=" * 60)
    logger.info("  阿冰没问题（Icenoproblem）PS5 哔哩哔哩 直播系统 V3.0  (Windows 原生版)")
    logger.info("=" * 60)
    logger.info(f"  监听房间: {CONFIG['BILIBILI_ROOM_ID']}")
    logger.info(f"  IRC 服务: {CONFIG['IRC_HOST']}:{CONFIG['IRC_PORT']}")
    logger.info(f"  Web 控制台: http://127.0.0.1:{CONFIG['WEB_PORT']}")
    logger.info(f"  PS5 频道: #{CONFIG['TWITCH_CHANNEL']}")
    logger.info("  RTMP 推流: 状态监控已启用（需配置DNS劫持）")
    if CONFIG.get("BILIBILI_UNAME"):
        logger.info(f"  已登录账号: {CONFIG['BILIBILI_UNAME']} (uid={CONFIG['BILIBILI_UID']})")
    else:
        logger.info("  账号状态: 游客（建议在Web控制台扫码登录）")
    logger.info("=" * 60)

    await asyncio.gather(
        irc_server.start(),
        bili_client.connect()
    )


if __name__ == "__main__":
    load_config()

    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    def web_starter():
        import time as _t
        _t.sleep(1.2)
        start_web(None)

    web_thread = threading.Thread(target=web_starter, daemon=True, name="WebThread")
    web_thread.start()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("程序已停止")
    except Exception as e:
        logger.error(f"主程序异常退出: {e}")
        import traceback
        traceback.print_exc()
