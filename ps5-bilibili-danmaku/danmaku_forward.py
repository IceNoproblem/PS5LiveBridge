# -*- coding: utf-8 -*-
import os
import asyncio
import logging
import requests
import json
import time
import threading
import queue
import random
from typing import Dict, Set, List, Tuple, Deque
from collections import deque
from datetime import datetime
from flask import Flask, jsonify, request, render_template_string

# ===================== 彻底屏蔽所有警告 =====================
import warnings
warnings.filterwarnings('ignore')
from urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

# ===================== 全局配置 =====================
CONFIG_FILE = "config.json"
DEFAULT_CONFIG = {
    "WEB_PORT": 5000,
    "BILIBILI_ROOM_ID": 669827,
    "TWITCH_CHANNEL": "icenoproblem",
    "IRC_HOST": "0.0.0.0",
    "IRC_PORT": 6667,
    "DANMAKU_POLL_INTERVAL": 3,
    "GIFT_POLL_INTERVAL": 5,
    "MAX_SEEN_DANMAKU": 1000,
    "MAX_SEEN_GIFT": 500,
    "HEARTBEAT_TIMEOUT": 20000,
    "USER_AGENT": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "ENABLE_GIFT": True,
    "MAX_LOG_ITEMS": 20  # NEW: 控制Web界面显示的最新记录条数
}
CONFIG = DEFAULT_CONFIG.copy()

# ===================== 全局变量 =====================
SEEN_DANMAKU_RND = set()
SEEN_GIFT_RND = set()
ACTIVE_CONNECTIONS = set()
IRC_RUNNING = False
DANMAKU_RUNNING = True
GIFT_RUNNING = True

# NEW: 用于存储最近弹幕和礼物记录的队列（线程安全）
recent_danmaku_log: Deque[Tuple[str, str]] = deque(maxlen=CONFIG["MAX_LOG_ITEMS"])  # (user, text)
recent_gift_log: Deque[Tuple[str, str, int, str]] = deque(maxlen=CONFIG["MAX_LOG_ITEMS"])  # (user, gift_name, num, coin_type)

# ===================== 日志配置 =====================
logging.getLogger('urllib3').setLevel(logging.CRITICAL)
logging.getLogger('requests').setLevel(logging.CRITICAL)
logging.getLogger('flask').setLevel(logging.CRITICAL)
logging.getLogger('werkzeug').setLevel(logging.CRITICAL)

logger = logging.getLogger("ps5-bilibili-danmaku")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
logger.addHandler(handler)

os.makedirs("logs", exist_ok=True)

# ===================== 配置加载/保存 =====================
def load_config():
    global CONFIG
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            loaded_config = json.load(f)
            for key in DEFAULT_CONFIG.keys():
                if key in loaded_config:
                    CONFIG[key] = loaded_config[key]
    except Exception as e:
        save_config()
        logger.info(f"配置文件不存在，生成默认配置: {e}")
    logger.info(f"配置加载完成，直播间ID：{CONFIG['BILIBILI_ROOM_ID']}, 礼物抓取：{'启用' if CONFIG['ENABLE_GIFT'] else '禁用'}")

def save_config(new_config=None):
    global CONFIG
    if new_config:
        for key, value in new_config.items():
            if key in DEFAULT_CONFIG:
                if key in ["BILIBILI_ROOM_ID", "IRC_PORT", "DANMAKU_POLL_INTERVAL", 
                           "GIFT_POLL_INTERVAL", "MAX_SEEN_DANMAKU", "MAX_SEEN_GIFT",
                           "HEARTBEAT_TIMEOUT", "WEB_PORT", "MAX_LOG_ITEMS"]:
                    CONFIG[key] = int(value) if str(value).isdigit() else DEFAULT_CONFIG[key]
                elif key == "ENABLE_GIFT":
                    CONFIG[key] = str(value).lower() in ("true", "1", "yes", "on")
                else:
                    CONFIG[key] = value.strip() if isinstance(value, str) else value
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(CONFIG, f, ensure_ascii=False, indent=4)
        logger.info(f"配置已保存：直播间ID={CONFIG['BILIBILI_ROOM_ID']}, 礼物抓取={CONFIG['ENABLE_GIFT']}")
    except Exception as e:
        logger.error(f"保存配置失败: {e}")

# ===================== IRC客户端 =====================
class IRCClient:
    def __init__(self, reader, writer, server):
        self.reader = reader
        self.writer = writer
        self.server = server
        self.nick = ""
        self.peername = writer.get_extra_info("peername")
        self.last_active = datetime.now()
        self.auto_joined = False
        self.is_alive = True
        ACTIVE_CONNECTIONS.add(self.peername)

    def check_alive(self):
        if not self.writer or self.writer.is_closing():
            self.is_alive = False
            if self.peername in ACTIVE_CONNECTIONS: 
                ACTIVE_CONNECTIONS.discard(self.peername)
            return False
        if (datetime.now() - self.last_active).total_seconds() > CONFIG["HEARTBEAT_TIMEOUT"]:
            self.is_alive = False
            if self.peername in ACTIVE_CONNECTIONS: 
                ACTIVE_CONNECTIONS.discard(self.peername)
            logger.warning(f"PS5({self.peername}) 连接超时，已清理")
            return False
        return True

    async def send_safe(self, data):
        if not self.check_alive(): 
            return
        if not data.endswith("\r\n"): 
            data += "\r\n"
        try:
            self.writer.write(data.encode('utf-8'))
            await self.writer.drain()
            self.last_active = datetime.now()
        except Exception as e:
            self.is_alive = False
            if self.peername in ACTIVE_CONNECTIONS: 
                ACTIVE_CONNECTIONS.discard(self.peername)
            logger.error(f"发送数据到PS5({self.peername})失败: {e}")

    async def auto_join_channel(self):
        if self.auto_joined or not self.check_alive(): 
            return
        target = f"#{CONFIG['TWITCH_CHANNEL']}"
        self.server.clients[target] = self
        await self.send_safe(f":{self.nick}!ps5@tmi.twitch.tv JOIN {target}")
        await self.send_safe(f":tmi.twitch.tv 353 {self.nick} = {target} :{self.nick}")
        await self.send_safe(f":tmi.twitch.tv 366 {self.nick} {target} :End of /NAMES list")
        self.auto_joined = True
        logger.info(f"PS5({self.peername}) 已加入频道 {target}")

    async def handle_message(self, line):
        if not line or not self.check_alive(): 
            return
        parts = line.split()
        if not parts: 
            return
        cmd = parts[0].upper()
        self.last_active = datetime.now()

        if cmd == "NICK" and len(parts) >= 2:
            self.nick = parts[1]
            logger.info(f"PS5({self.peername}) 设置昵称: {self.nick}")
            await self.auto_join_channel()

        elif cmd == "USER":
            await self.send_safe(f":tmi.twitch.tv 001 {self.nick} :Welcome to Twitch!")

        elif cmd == "PING":
            ping_arg = parts[1] if len(parts) >= 2 else "tmi.twitch.tv"
            await self.send_safe(f"PONG :{ping_arg}")

    async def run(self):
        try:
            while self.check_alive():
                data = await self.reader.readline()
                if not data: 
                    break
                line = data.decode('utf-8', errors='ignore').strip()
                if line:
                    await self.handle_message(line)
        except Exception as e:
            logger.error(f"PS5({self.peername}) 连接异常: {e}")
        finally:
            self.is_alive = False
            if self.peername in ACTIVE_CONNECTIONS: 
                ACTIVE_CONNECTIONS.discard(self.peername)
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except:
                pass
            logger.info(f"PS5({self.peername}) 连接已关闭")

class IRCServer:
    def __init__(self):
        self.clients: Dict[str, IRCClient] = {}

    async def start(self):
        global IRC_RUNNING
        try:
            server = await asyncio.start_server(
                self.handle_client, 
                CONFIG["IRC_HOST"], 
                CONFIG["IRC_PORT"],
                reuse_address=True,
                reuse_port=True
            )
            IRC_RUNNING = True
            logger.info(f"IRC服务已启动: {CONFIG['IRC_HOST']}:{CONFIG['IRC_PORT']}")
            async with server:
                await server.serve_forever()
        except Exception as e:
            IRC_RUNNING = False
            logger.error(f"IRC服务器启动失败: {e}")
            await asyncio.sleep(5)
            await self.start()

    async def handle_client(self, reader, writer):
        client = IRCClient(reader, writer, self)
        await client.run()

    async def send_danmaku(self, user, text):
        target = f"#{CONFIG['TWITCH_CHANNEL']}"
        client = self.clients.get(target)
        
        if not client:
            for c in self.clients.values():
                if c.check_alive():
                    client = c
                    break
        
        if not client:
            logger.warning(f"无活跃PS5客户端，弹幕[{user}:{text}]转发失败")
            return
        
        msg = f":{user}!{user}@tmi.twitch.tv PRIVMSG {target} :{text}"
        await client.send_safe(msg)
        logger.info(f"转发弹幕 [{user}]: {text}")
        # NEW: 记录到最近弹幕日志
        recent_danmaku_log.appendleft((user, text))

    async def send_gift(self, user, gift_name, gift_num, coin_type):
        """MODIFIED: 简化礼物推送信息，单位改为电池/金仓鼠"""
        target = f"#{CONFIG['TWITCH_CHANNEL']}"
        client = self.clients.get(target)
        
        if not client:
            for c in self.clients.values():
                if c.check_alive():
                    client = c
                    break
        
        if not client:
            logger.warning(f"无活跃PS5客户端，礼物[{user}:{gift_name}x{gift_num}]转发失败")
            return
        
        # MODIFIED: 简化信息，仅保留用户、礼物名和数量
        gift_text = f"🎁 {user}: {gift_name}x{gift_num}"
        
        msg = f":{user}!{user}@tmi.twitch.tv PRIVMSG {target} :{gift_text}"
        await client.send_safe(msg)
        logger.info(f"转发礼物 [{user}]: {gift_name}x{gift_num}")
        # NEW: 记录到最近礼物日志
        # MODIFIED: 将coin_type的显示值改为电池/金仓鼠
        display_coin = "电池" if coin_type == "gold" else "金仓鼠"
        recent_gift_log.appendleft((user, gift_name, gift_num, display_coin))

# ===================== B站弹幕抓取 =====================
SESSION = requests.Session()
SESSION.verify = False

def update_session_headers():
    SESSION.headers.update({
        "User-Agent": CONFIG["USER_AGENT"],
        "Referer": f"https://live.bilibili.com/{CONFIG['BILIBILI_ROOM_ID']}"
    })

def get_danmaku():
    global DANMAKU_RUNNING, SEEN_DANMAKU_RND
    try:
        DANMAKU_RUNNING = True
        update_session_headers()
        
        url = f"https://api.live.bilibili.com/xlive/web-room/v1/dM/gethistory?roomid={CONFIG['BILIBILI_ROOM_ID']}"
        response = SESSION.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        new_danmakus = []
        if data.get("code") == 0:
            danmaku_list = data["data"].get("room", []) or data["data"].get("list", [])
            
            for dm in danmaku_list:
                dm_id = f"{dm.get('timeline', dm.get('ctime', ''))}_{dm.get('text', '')}"
                
                if not isinstance(SEEN_DANMAKU_RND, set):
                    SEEN_DANMAKU_RND = set()
                
                if dm_id not in SEEN_DANMAKU_RND:
                    SEEN_DANMAKU_RND.add(dm_id)
                    uname = dm.get("nickname", dm.get("uname", "未知用户"))
                    content = dm.get("text", "").strip()
                    if content:
                        new_danmakus.append((uname, content))
        
        max_cache = CONFIG["MAX_SEEN_DANMAKU"]
        if len(SEEN_DANMAKU_RND) > max_cache:
            SEEN_DANMAKU_RND = set(list(SEEN_DANMAKU_RND)[-int(max_cache*0.8):])
            logger.debug(f"清理弹幕缓存，当前缓存数: {len(SEEN_DANMAKU_RND)}")
        
        if new_danmakus:
            logger.info(f"抓取到{len(new_danmakus)}条新弹幕")
        return new_danmakus
    
    except Exception as e:
        DANMAKU_RUNNING = False
        logger.error(f"抓取弹幕失败: {str(e)}")
        return []

def get_gift():
    """从弹幕历史接口中筛选礼物消息"""
    global GIFT_RUNNING, SEEN_GIFT_RND
    if not CONFIG["ENABLE_GIFT"]:
        return []

    try:
        GIFT_RUNNING = True
        update_session_headers()
        
        # 使用与弹幕相同的稳定接口
        url = f"https://api.live.bilibili.com/xlive/web-room/v1/dM/gethistory?roomid={CONFIG['BILIBILI_ROOM_ID']}"
        response = SESSION.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        new_gifts = []
        if data.get("code") == 0:
            # 获取消息列表
            danmaku_list = data["data"].get("room", []) or data["data"].get("list", [])
            
            for msg in danmaku_list:
                # 检查消息是否为礼物类型
                # 通常礼物消息会有特殊标识
                text = msg.get("text", "").lower()
                
                # 识别礼物消息的关键词
                is_gift_message = False
                gift_keywords = ["赠送", "送出", "投喂", "上舰", "提督", "总督", "舰长"]
                for keyword in gift_keywords:
                    if keyword in text:
                        is_gift_message = True
                        break
                
                if is_gift_message:
                    # 提取用户信息
                    uname = msg.get("nickname", msg.get("uname", "未知用户"))
                    
                    # 解析礼物信息
                    gift_name = "礼物"
                    gift_num = 1
                    
                    # 尝试从消息文本中提取礼物名称和数量
                    import re
                    
                    # 匹配 "赠送了XXXxN" 模式
                    gift_pattern = r'赠送了?\s*([^\sxX]+)\s*[xX]?\s*(\d+)'
                    match = re.search(gift_pattern, text)
                    if match:
                        gift_name = match.group(1)
                        gift_num = int(match.group(2))
                    else:
                        # 匹配 "XXXxN" 模式
                        simple_pattern = r'([^\sxX]+)\s*[xX]\s*(\d+)'
                        match = re.search(simple_pattern, text)
                        if match:
                            gift_name = match.group(1)
                            gift_num = int(match.group(2))
                        else:
                            # 提取可能的礼物名称
                            for keyword in ["辣条", "小电视", "飞机", "火箭", "舰长", "提督"]:
                                if keyword in text:
                                    gift_name = keyword
                                    break
                    
                    # 判断礼物价值（简单判断）
                    coin_type = "silver"  # 默认银瓜子/金仓鼠
                    if any(word in gift_name for word in ["电视", "飞机", "火箭", "提督", "总督"]):
                        coin_type = "gold"  # 金瓜子/电池
                    
                    # 生成唯一ID
                    msg_id = msg.get("id") or f"{uname}_{gift_name}_{msg.get('timeline', int(time.time()))}"
                    gift_record_id = f"gift_{msg_id}"
                    
                    if not isinstance(SEEN_GIFT_RND, set):
                        SEEN_GIFT_RND = set()
                    
                    if gift_record_id not in SEEN_GIFT_RND:
                        SEEN_GIFT_RND.add(gift_record_id)
                        new_gifts.append((uname, gift_name, gift_num, coin_type))
                        logger.info(f"从弹幕中解析到礼物: {uname} 赠送了 {gift_name}x{gift_num}")
        
        # 清理旧的礼物ID
        max_cache = CONFIG["MAX_SEEN_GIFT"]
        if len(SEEN_GIFT_RND) > max_cache:
            SEEN_GIFT_RND = set(list(SEEN_GIFT_RND)[-int(max_cache*0.8):])
            logger.debug(f"清理礼物缓存，当前缓存数: {len(SEEN_GIFT_RND)}")
        
        if new_gifts:
            logger.info(f"从弹幕接口抓取到{len(new_gifts)}个礼物消息")
        return new_gifts
    
    except Exception as e:
        GIFT_RUNNING = False
        logger.error(f"从弹幕接口抓取礼物失败: {str(e)}")
        return []

async def danmaku_worker(irc_srv):
    logger.info(f"开始监听B站直播间 {CONFIG['BILIBILI_ROOM_ID']} 的弹幕")
    await asyncio.sleep(2)
    
    while True:
        try:
            new_danmakus = get_danmaku()
            for user, text in new_danmakus:
                await irc_srv.send_danmaku(user, text)
        except Exception as e:
            logger.error(f"弹幕处理异常: {e}")
        await asyncio.sleep(CONFIG["DANMAKU_POLL_INTERVAL"])

async def gift_worker(irc_srv):
    if not CONFIG["ENABLE_GIFT"]:
        logger.info("礼物抓取功能已禁用")
        return
    
    logger.info(f"开始监听B站直播间 {CONFIG['BILIBILI_ROOM_ID']} 的礼物")
    await asyncio.sleep(3)
    
    while True:
        try:
            new_gifts = get_gift()
            for user, gift_name, gift_num, coin_type in new_gifts:
                await irc_srv.send_gift(user, gift_name, gift_num, coin_type)
        except Exception as e:
            logger.error(f"礼物处理异常: {e}")
        await asyncio.sleep(CONFIG["GIFT_POLL_INTERVAL"])

# ===================== Web服务（增加实时记录）=====================
def start_web():
    web_port = CONFIG.get("WEB_PORT", 5000)
    app = Flask("ps5-danmaku-web")

    # 横屏优化的Web配置界面（增加实时记录区域）
    WEB_HTML = """
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
        <title>PS5-B站弹幕/礼物转发控制台</title>
        <style>
            * { 
                margin: 0; 
                padding: 0; 
                box-sizing: border-box; 
                font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif; 
            }
            body { 
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                display: flex;
                flex-direction: column;
                align-items: center;
                padding: 20px;
                color: #333;
            }
            .container { 
                width: 100%;
                max-width: 1600px; /* MODIFIED: 增加总宽度以容纳记录区域 */
                background: rgba(255, 255, 255, 0.95);
                border-radius: 20px;
                padding: 30px;
                box-shadow: 0 20px 60px rgba(0,0,0,0.3);
                backdrop-filter: blur(10px);
                margin-bottom: 20px;
            }
            .header { 
                text-align: center;
                margin-bottom: 30px;
                padding-bottom: 20px;
                border-bottom: 2px solid #f0f0f0;
            }
            .header h1 { 
                color: #2d3748;
                font-size: 2.2rem;
                margin-bottom: 8px;
                display: flex;
                align-items: center;
                justify-content: center;
                gap: 12px;
            }
            .header h1 i { 
                color: #667eea; 
                font-size: 1.8rem; 
            }
            .header p { 
                color: #718096; 
                font-size: 1rem; 
            }
            .dashboard { 
                display: grid;
                grid-template-columns: 1fr 1fr; /* 保持左右两栏 */
                gap: 30px;
                margin-bottom: 30px;
            }
            .card { 
                background: white;
                border-radius: 15px;
                padding: 25px;
                box-shadow: 0 5px 20px rgba(0,0,0,0.08);
                border: 1px solid #e2e8f0;
                transition: transform 0.3s, box-shadow 0.3s;
                display: flex;
                flex-direction: column;
            }
            .card:hover { 
                transform: translateY(-5px);
                box-shadow: 0 10px 30px rgba(0,0,0,0.12);
            }
            .card h2 { 
                color: #4a5568;
                font-size: 1.3rem;
                margin-bottom: 20px;
                padding-bottom: 10px;
                border-bottom: 2px solid #667eea;
                display: flex;
                align-items: center;
                gap: 10px;
            }
            .card h2 i { 
                color: #667eea; 
            }
            .config-grid { 
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
                gap: 20px;
                flex: 1;
            }
            .form-group { 
                margin-bottom: 20px; 
            }
            .form-group label { 
                display: block;
                color: #4a5568;
                font-weight: 600;
                margin-bottom: 8px;
                font-size: 0.95rem;
            }
            .form-group input, .form-group select { 
                width: 100%;
                padding: 12px 15px;
                border: 2px solid #e2e8f0;
                border-radius: 10px;
                font-size: 1rem;
                transition: all 0.3s;
                background: #f8fafc;
            }
            .form-group input:focus, .form-group select:focus { 
                outline: none;
                border-color: #667eea;
                background: white;
                box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
            }
            .switch { 
                position: relative;
                display: inline-block;
                width: 60px;
                height: 30px;
                margin-left: 15px;
            }
            .switch input { 
                opacity: 0;
                width: 0;
                height: 0;
            }
            .slider { 
                position: absolute;
                cursor: pointer;
                top: 0;
                left: 0;
                right: 0;
                bottom: 0;
                background-color: #ccc;
                transition: .4s;
                border-radius: 34px;
            }
            .slider:before { 
                position: absolute;
                content: "";
                height: 22px;
                width: 22px;
                left: 4px;
                bottom: 4px;
                background-color: white;
                transition: .4s;
                border-radius: 50%;
            }
            input:checked + .slider { 
                background-color: #10b981;
            }
            input:checked + .slider:before { 
                transform: translateX(30px);
            }
            .status-grid { 
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                gap: 20px;
                margin-top: 10px;
                margin-bottom: 25px; /* 为记录区域腾出空间 */
            }
            .status-item { 
                background: #f8fafc;
                padding: 20px;
                border-radius: 10px;
                text-align: center;
                border-left: 4px solid #667eea;
            }
            .status-label { 
                color: #718096;
                font-size: 0.9rem;
                margin-bottom: 8px;
            }
            .status-value { 
                color: #2d3748;
                font-size: 1.5rem;
                font-weight: 700;
            }
            .status-value.online { 
                color: #10b981; 
            }
            .status-value.offline { 
                color: #ef4444; 
            }
            /* NEW: 实时记录区域样式 */
            .live-logs { 
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 20px;
                margin-top: auto; /* 推到底部 */
            }
            .log-panel { 
                background: #f8fafc;
                border-radius: 10px;
                padding: 15px;
                border: 1px solid #e2e8f0;
                max-height: 300px;
                overflow-y: auto;
            }
            .log-panel h3 { 
                color: #4a5568;
                font-size: 1.1rem;
                margin-bottom: 12px;
                display: flex;
                align-items: center;
                gap: 8px;
            }
            .log-panel h3 i { 
                color: #667eea;
            }
            .log-list { 
                list-style: none;
            }
            .log-item { 
                padding: 10px 12px;
                margin-bottom: 8px;
                background: white;
                border-radius: 8px;
                border-left: 4px solid #4299e1; /* 弹幕蓝色 */
                font-size: 0.9rem;
                word-break: break-word;
                animation: fadeInUp 0.5s ease;
            }
            .log-item.gift { 
                border-left-color: #ed8936; /* 礼物橙色 */
            }
            .log-item .user { 
                font-weight: 600;
                color: #2d3748;
            }
            .log-item .content { 
                color: #4a5568;
                margin-top: 4px;
            }
            .log-item .meta { 
                font-size: 0.8rem;
                color: #a0aec0;
                margin-top: 4px;
            }
            .no-logs { 
                text-align: center;
                color: #a0aec0;
                font-style: italic;
                padding: 20px;
            }
            @keyframes fadeInUp { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
            
            .button-group { 
                display: flex;
                gap: 15px;
                margin-top: 25px;
            }
            .btn { 
                flex: 1;
                padding: 15px 25px;
                border: none;
                border-radius: 10px;
                font-size: 1rem;
                font-weight: 600;
                cursor: pointer;
                transition: all 0.3s;
                display: flex;
                align-items: center;
                justify-content: center;
                gap: 10px;
            }
            .btn-primary { 
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
            }
            .btn-primary:hover { 
                opacity: 0.9;
                transform: translateY(-2px);
                box-shadow: 0 7px 20px rgba(102, 126, 234, 0.4);
            }
            .btn-secondary { 
                background: #e2e8f0;
                color: #4a5568;
            }
            .btn-secondary:hover { 
                background: #cbd5e0;
            }
            .message { 
                padding: 15px;
                border-radius: 10px;
                margin-top: 20px;
                text-align: center;
                display: none;
                animation: fadeIn 0.5s;
            }
            .message.success { 
                background: #d1fae5;
                color: #065f46;
                display: block;
            }
            .message.error { 
                background: #fee2e2;
                color: #991b1b;
                display: block;
            }
            @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
            .footer { 
                text-align: center;
                color: #a0aec0;
                font-size: 0.9rem;
                margin-top: 20px;
                padding-top: 20px;
                border-top: 1px solid #e2e8f0;
            }
            @media (max-width: 768px) { 
                .dashboard { grid-template-columns: 1fr; }
                .live-logs { grid-template-columns: 1fr; }
                .container { padding: 20px; }
            }
        </style>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    </head>
    <body>
        <div class="container">
            <!-- 页眉 -->
            <div class="header">
                <h1><i class="fas fa-gamepad"></i> PS5-B站弹幕/礼物转发控制台</h1>
                <p>实时监控B站直播间弹幕与礼物，并转发至PS5直播画面</p>
            </div>
            
            <!-- 仪表板：配置 + 状态&记录 -->
            <div class="dashboard">
                <!-- 配置面板（左侧不变） -->
                <div class="card">
                    <h2><i class="fas fa-cog"></i> 系统配置</h2>
                    <div class="config-grid">
                        <!-- 基础配置 -->
                        <div class="form-group">
                            <label><i class="fas fa-desktop"></i> B站直播间ID</label>
                            <input type="number" id="BILIBILI_ROOM_ID" value="{{ BILIBILI_ROOM_ID }}" placeholder="例如：669827">
                        </div>
                        <div class="form-group">
                            <label><i class="fas fa-hashtag"></i> PS5 IRC频道名</label>
                            <input type="text" id="TWITCH_CHANNEL" value="{{ TWITCH_CHANNEL }}" placeholder="例如：yu332506767">
                        </div>
                        <div class="form-group">
                            <label><i class="fas fa-server"></i> IRC服务地址</label>
                            <input type="text" id="IRC_HOST" value="{{ IRC_HOST }}" placeholder="0.0.0.0">
                        </div>
                        <div class="form-group">
                            <label><i class="fas fa-plug"></i> IRC服务端口</label>
                            <input type="number" id="IRC_PORT" value="{{ IRC_PORT }}" placeholder="6667">
                        </div>
                        <div class="form-group">
                            <label><i class="fas fa-globe"></i> Web服务端口</label>
                            <input type="number" id="WEB_PORT" value="{{ WEB_PORT }}" placeholder="5000">
                        </div>
                        
                        <!-- 高级配置 -->
                        <div class="form-group">
                            <label><i class="fas fa-comment-dots"></i> 弹幕抓取间隔(秒)</label>
                            <input type="number" id="DANMAKU_POLL_INTERVAL" value="{{ DANMAKU_POLL_INTERVAL }}" min="1" max="10">
                        </div>
                        <div class="form-group">
                            <label><i class="fas fa-gift"></i> 礼物抓取间隔(秒)</label>
                            <input type="number" id="GIFT_POLL_INTERVAL" value="{{ GIFT_POLL_INTERVAL }}" min="3" max="15">
                        </div>
                        <div class="form-group">
                            <label><i class="fas fa-database"></i> 弹幕缓存数</label>
                            <input type="number" id="MAX_SEEN_DANMAKU" value="{{ MAX_SEEN_DANMAKU }}" placeholder="1000">
                        </div>
                        <div class="form-group">
                            <label><i class="fas fa-database"></i> 礼物缓存数</label>
                            <input type="number" id="MAX_SEEN_GIFT" value="{{ MAX_SEEN_GIFT }}" placeholder="500">
                        </div>
                        <div class="form-group">
                            <label><i class="fas fa-heartbeat"></i> PS5连接超时(秒)</label>
                            <input type="number" id="HEARTBEAT_TIMEOUT" value="{{ HEARTBEAT_TIMEOUT }}" placeholder="300">
                        </div>
                        <div class="form-group">
                            <label><i class="fas fa-list"></i> 最大记录条数</label>
                            <input type="number" id="MAX_LOG_ITEMS" value="{{ MAX_LOG_ITEMS }}" placeholder="20">
                        </div>
                        <div class="form-group">
                            <label><i class="fas fa-toggle-on"></i> 启用礼物抓取</label>
                            <div style="display: flex; align-items: center;">
                                <input type="checkbox" id="ENABLE_GIFT" {{ 'checked' if ENABLE_GIFT else '' }}>
                                <label for="ENABLE_GIFT" class="switch">
                                    <input type="checkbox" id="ENABLE_GIFT_CHECKBOX" {{ 'checked' if ENABLE_GIFT else '' }}>
                                    <span class="slider"></span>
                                </label>
                                <span style="margin-left: 10px; color: #4a5568;">{{ '已启用' if ENABLE_GIFT else '已禁用' }}</span>
                            </div>
                        </div>
                        <div class="form-group">
                            <label><i class="fas fa-user-agent"></i> 请求User-Agent</label>
                            <input type="text" id="USER_AGENT" value="{{ USER_AGENT }}" placeholder="浏览器标识">
                        </div>
                    </div>
                </div>
                
                <!-- 状态面板（右侧，增加实时记录） -->
                <div class="card">
                    <h2><i class="fas fa-chart-bar"></i> 实时状态与记录</h2>
                    <div class="status-grid">
                        <div class="status-item">
                            <div class="status-label">活跃PS5客户端</div>
                            <div id="active_clients" class="status-value {{ 'online' if active_clients > 0 else 'offline' }}">{{ active_clients }}</div>
                        </div>
                        <div class="status-item">
                            <div class="status-label">IRC服务状态</div>
                            <div id="irc_running" class="status-value {{ 'online' if irc_running else 'offline' }}">
                                {{ "运行中" if irc_running else "已停止" }}
                            </div>
                        </div>
                        <div class="status-item">
                            <div class="status-label">弹幕抓取状态</div>
                            <div id="danmaku_running" class="status-value {{ 'online' if danmaku_running else 'offline' }}">
                                {{ "运行中" if danmaku_running else "已停止" }}
                            </div>
                        </div>
                        <div class="status-item">
                            <div class="status-label">礼物抓取状态</div>
                            <div id="gift_running" class="status-value {{ 'online' if gift_running else 'offline' }}">
                                {{ "运行中" if gift_running else "已停止" }}
                            </div>
                        </div>
                        <div class="status-item">
                            <div class="status-label">最后更新</div>
                            <div id="update_time" class="status-value">{{ update_time }}</div>
                        </div>
                        <div class="status-item">
                            <div class="status-label">B站房间号</div>
                            <div class="status-value">{{ BILIBILI_ROOM_ID }}</div>
                        </div>
                        <div class="status-item">
                            <div class="status-label">目标IRC频道</div>
                            <div class="status-value">#{{ TWITCH_CHANNEL }}</div>
                        </div>
                    </div>
                    
                    <!-- NEW: 实时记录区域 -->
                    <div class="live-logs">
                        <div class="log-panel">
                            <h3><i class="fas fa-comment-alt"></i> 最新弹幕</h3>
                            <ul class="log-list" id="danmaku_logs">
                                {% if recent_danmaku %}
                                    {% for user, text in recent_danmaku %}
                                    <li class="log-item">
                                        <div class="user">{{ user[:15] }}{% if user|length > 15 %}...{% endif %}</div>
                                        <div class="content">{{ text[:30] }}{% if text|length > 30 %}...{% endif %}</div>
                                    </li>
                                    {% endfor %}
                                {% else %}
                                    <li class="no-logs">暂无弹幕记录</li>
                                {% endif %}
                            </ul>
                        </div>
                        <div class="log-panel">
                            <h3><i class="fas fa-gift"></i> 最新礼物</h3>
                            <ul class="log-list" id="gift_logs">
                                {% if recent_gift %}
                                    {% for user, name, num, coin in recent_gift %}
                                    <li class="log-item gift">
                                        <div class="user">{{ user[:15] }}{% if user|length > 15 %}...{% endif %}</div>
                                        <div class="content">{{ name }} x{{ num }}</div>
                                        <div class="meta">{{ coin }}</div>
                                    </li>
                                    {% endfor %}
                                {% else %}
                                    <li class="no-logs">暂无礼物记录</li>
                                {% endif %}
                            </ul>
                        </div>
                    </div>
                    
                    <!-- 操作按钮 -->
                    <div class="button-group">
                        <button class="btn btn-primary" onclick="saveConfig()">
                            <i class="fas fa-save"></i> 保存所有配置
                        </button>
                        <button class="btn btn-secondary" onclick="refreshStatus()">
                            <i class="fas fa-sync-alt"></i> 刷新状态
                        </button>
                    </div>
                    
                    <!-- 消息提示 -->
                    <div id="message" class="message"></div>
                </div>
            </div>
            
            <!-- 页脚 -->
            <div class="footer">
                <p><i class="fas fa-code"></i> PS5BiliDanmaku v2.0 | 支持弹幕与礼物转发 | 横屏优化版</p>
            </div>
        </div>

        <script>
            // 同步复选框状态
            document.getElementById('ENABLE_GIFT_CHECKBOX').addEventListener('change', function() {
                document.getElementById('ENABLE_GIFT').checked = this.checked;
            });
            document.getElementById('ENABLE_GIFT').addEventListener('change', function() {
                document.getElementById('ENABLE_GIFT_CHECKBOX').checked = this.checked;
            });

            // 保存配置
            function saveConfig() {
                const config = {};
                const inputs = document.querySelectorAll('input[type="text"], input[type="number"]');
                inputs.forEach(input => {
                    if (input.id && input.id !== 'ENABLE_GIFT_CHECKBOX') {
                        config[input.id] = input.value;
                    }
                });
                
                config['ENABLE_GIFT'] = document.getElementById('ENABLE_GIFT').checked;

                fetch('/save_config', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(config)
                })
                .then(res => res.json())
                .then(data => {
                    const msgEl = document.getElementById('message');
                    msgEl.textContent = data.msg;
                    msgEl.className = data.code === 0 ? 'message success' : 'message error';
                    
                    if (data.code === 0) {
                        setTimeout(() => {
                            window.location.reload();
                        }, 2000);
                    }
                    
                    setTimeout(() => {
                        msgEl.style.display = 'none';
                    }, 3000);
                })
                .catch(err => {
                    const msgEl = document.getElementById('message');
                    msgEl.textContent = '保存失败：' + err.message;
                    msgEl.className = 'message error';
                });
            }

            // 刷新状态和记录
            function refreshStatus() {
                fetch('/status')
                .then(res => res.json())
                .then(data => {
                    // 更新状态
                    document.getElementById('active_clients').textContent = data.active_clients;
                    document.getElementById('irc_running').textContent = data.irc_running ? '运行中' : '已停止';
                    document.getElementById('irc_running').className = data.irc_running ? 'status-value online' : 'status-value offline';
                    document.getElementById('danmaku_running').textContent = data.danmaku_running ? '运行中' : '已停止';
                    document.getElementById('danmaku_running').className = data.danmaku_running ? 'status-value online' : 'status-value offline';
                    document.getElementById('gift_running').textContent = data.gift_running ? '运行中' : '已停止';
                    document.getElementById('gift_running').className = data.gift_running ? 'status-value online' : 'status-value offline';
                    document.getElementById('update_time').textContent = new Date().toLocaleString('zh-CN');
                    
                    // 更新弹幕记录
                    const danmakuList = document.getElementById('danmaku_logs');
                    danmakuList.innerHTML = '';
                    if (data.recent_danmaku && data.recent_danmaku.length > 0) {
                        data.recent_danmaku.forEach(item => {
                            const li = document.createElement('li');
                            li.className = 'log-item';
                            li.innerHTML = `
                                <div class="user">${item.user.length > 15 ? item.user.substring(0,15)+'...' : item.user}</div>
                                <div class="content">${item.text.length > 30 ? item.text.substring(0,30)+'...' : item.text}</div>
                            `;
                            danmakuList.appendChild(li);
                        });
                    } else {
                        const li = document.createElement('li');
                        li.className = 'no-logs';
                        li.textContent = '暂无弹幕记录';
                        danmakuList.appendChild(li);
                    }
                    
                    // 更新礼物记录
                    const giftList = document.getElementById('gift_logs');
                    giftList.innerHTML = '';
                    if (data.recent_gift && data.recent_gift.length > 0) {
                        data.recent_gift.forEach(item => {
                            const li = document.createElement('li');
                            li.className = 'log-item gift';
                            li.innerHTML = `
                                <div class="user">${item.user.length > 15 ? item.user.substring(0,15)+'...' : item.user}</div>
                                <div class="content">${item.name} x${item.num}</div>
                                <div class="meta">${item.coin}</div>
                            `;
                            giftList.appendChild(li);
                        });
                    } else {
                        const li = document.createElement('li');
                        li.className = 'no-logs';
                        li.textContent = '暂无礼物记录';
                        giftList.appendChild(li);
                    }
                });
            }

            // MODIFIED: 自动刷新状态（改为2秒一次）
            window.onload = function() {
                refreshStatus();
                setInterval(refreshStatus, 2000); // 2秒刷新一次
            };
        </script>
    </body>
    </html>
    """

    # Web首页
    @app.route('/')
    def index():
        # NEW: 准备最近记录数据
        danmaku_list = list(recent_danmaku_log)  # 转换为列表供模板使用
        gift_list = list(recent_gift_log)
        
        render_data = {
            "BILIBILI_ROOM_ID": CONFIG["BILIBILI_ROOM_ID"],
            "TWITCH_CHANNEL": CONFIG["TWITCH_CHANNEL"],
            "IRC_HOST": CONFIG["IRC_HOST"],
            "IRC_PORT": CONFIG["IRC_PORT"],
            "WEB_PORT": CONFIG["WEB_PORT"],
            "DANMAKU_POLL_INTERVAL": CONFIG["DANMAKU_POLL_INTERVAL"],
            "GIFT_POLL_INTERVAL": CONFIG["GIFT_POLL_INTERVAL"],
            "MAX_SEEN_DANMAKU": CONFIG["MAX_SEEN_DANMAKU"],
            "MAX_SEEN_GIFT": CONFIG["MAX_SEEN_GIFT"],
            "HEARTBEAT_TIMEOUT": CONFIG["HEARTBEAT_TIMEOUT"],
            "USER_AGENT": CONFIG["USER_AGENT"],
            "MAX_LOG_ITEMS": CONFIG["MAX_LOG_ITEMS"],
            "ENABLE_GIFT": CONFIG["ENABLE_GIFT"],
            "active_clients": len(ACTIVE_CONNECTIONS),
            "irc_running": IRC_RUNNING,
            "danmaku_running": DANMAKU_RUNNING,
            "gift_running": GIFT_RUNNING,
            "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "recent_danmaku": danmaku_list,  # NEW: 传递给模板
            "recent_gift": gift_list         # NEW: 传递给模板
        }
        return render_template_string(WEB_HTML, **render_data)

    # 保存配置接口
    @app.route('/save_config', methods=['POST'])
    def save_config_api():
        try:
            new_config = request.get_json()
            if not new_config:
                return jsonify({"code": 1, "msg": "配置数据为空"})
            
            save_config(new_config)
            return jsonify({"code": 0, "msg": "所有配置保存成功！2秒后自动刷新页面"})
        except Exception as e:
            return jsonify({"code": 1, "msg": f"保存配置失败：{str(e)}"})

    # 状态查询接口
    @app.route('/status')
    def get_status():
        # NEW: 返回最近的记录
        danmaku_for_json = [{"user": u, "text": t} for u, t in recent_danmaku_log]
        gift_for_json = [{"user": u, "name": n, "num": num, "coin": c} for u, n, num, c in recent_gift_log]
        
        return jsonify({
            "active_clients": len(ACTIVE_CONNECTIONS),
            "irc_running": IRC_RUNNING,
            "danmaku_running": DANMAKU_RUNNING,
            "gift_running": GIFT_RUNNING,
            "web_port": web_port,
            "room_id": CONFIG["BILIBILI_ROOM_ID"],
            "recent_danmaku": danmaku_for_json,  # NEW: 包含在JSON响应中
            "recent_gift": gift_for_json         # NEW: 包含在JSON响应中
        })

    logger.info(f"Web配置界面已启动: http://0.0.0.0:{web_port}")
    app.run(
        host="0.0.0.0", 
        port=web_port, 
        debug=False, 
        use_reloader=False,
        threaded=True
    )

# ===================== 主程序入口 =====================
async def main():
    load_config()
    irc_server = IRCServer()
    await asyncio.gather(
        irc_server.start(),
        danmaku_worker(irc_server),
        gift_worker(irc_server)
    )

if __name__ == "__main__":
    load_config()
    
    web_thread = threading.Thread(target=start_web, daemon=True)
    web_thread.start()
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("程序被手动终止")
    except Exception as e:
        logger.error(f"主程序异常: {e}")