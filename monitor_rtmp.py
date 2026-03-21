#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RTMP推流监控脚本
监控bao3/playstation (或类似的RTMP服务器) 状态，并更新到danmaku_forward.py

使用方法：
1. 启动bao3/playstation容器
2. 修改本脚本中的配置（RTMP服务器地址）
3. 运行本脚本：python monitor_rtmp.py
4. 在danmaku_forward.py的Web界面查看RTMP状态
"""

import sys

# 修复 Windows 控制台编码问题
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
    import locale
    try:
        locale.setlocale(locale.LC_ALL, 'zh_CN.UTF-8')
    except:
        pass

import requests
import time
import json
import logging
from typing import Dict, Optional

# ==================== 配置区 ====================

# danmaku_forward.py的API地址
DANMAKU_API_URL = "http://127.0.0.1:5000/api/rtmp/status/update"

# RTMP服务器监控API地址（根据实际使用的服务调整）
# 方式1: 如果使用本地nginx-rtmp（当前使用）
RTMP_MONITOR_URL = "http://127.0.0.1:8080/stat"

# 方式2: 如果使用SRS
# RTMP_MONITOR_URL = "http://127.0.0.1:1985/api/v1/streams"

# 方式3: 如果使用远程bao3/playstation
# RTMP_MONITOR_URL = "http://192.168.1.100:8080"

# 方式4: 如果使用自定义API，填写实际地址
# RTMP_MONITOR_URL = "http://192.168.1.100:8080/api/status"

# 监控间隔（秒）
CHECK_INTERVAL = 2

# RTMP服务器类型：'playstation', 'nginx-rtmp', 'srs', 'custom'
RTMP_SERVER_TYPE = 'nginx-rtmp'

# 推流码前缀（PS5推流时通常会生成）
STREAM_KEY_PREFIX = "live_"

# ==================== 日志配置 ====================
logging.basicConfig(
    level=logging.DEBUG,  # 改为 DEBUG 以显示详细日志
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('monitor_rtmp.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)


class RTMPMonitor:
    """RTMP推流监控器"""

    def __init__(self, rtmp_url: str, danmaku_url: str, server_type: str = 'playstation'):
        self.rtmp_url = rtmp_url
        self.danmaku_url = danmaku_url
        self.server_type = server_type
        self.last_active = False

    def check_connection(self) -> bool:
        """检查RTMP服务器连接状态"""
        try:
            resp = requests.get(self.rtmp_url, timeout=5)
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"连接RTMP服务器失败: {e}")
            return False

    def get_rtmp_status(self) -> Dict:
        """
        获取RTMP推流状态
        根据不同的服务器类型解析不同的API响应格式
        """
        try:
            if self.server_type == 'playstation':
                return self._get_playstation_status()
            elif self.server_type == 'nginx-rtmp':
                return self._get_nginx_status()
            elif self.server_type == 'srs':
                return self._get_srs_status()
            elif self.server_type == 'custom':
                return self._get_custom_status()
            else:
                return {"active": False}
        except Exception as e:
            logger.error(f"获取RTMP状态失败: {e}")
            return {"active": False}

    def _get_playstation_status(self) -> Dict:
        """
        获取bao3/playstation的状态
        注意：具体API格式可能需要根据实际镜像调整
        """
        try:
            # 尝试访问根路径获取HTML页面
            resp = requests.get(self.rtmp_url, timeout=5)
            if resp.status_code != 200:
                return {"active": False}

            html = resp.text

            # 检查是否有活跃的流
            # bao3/playstation通常会在页面中显示推流信息
            # 这里需要根据实际HTML结构进行解析
            # 如果没有提供API，可能需要使用模拟状态

            # 示例：查找包含stream_key的元素
            import re
            stream_key_match = re.search(r'live_\w+', html)

            if stream_key_match:
                stream_key = stream_key_match.group(0)
                logger.info(f"检测到推流: {stream_key}")

                # 尝试提取更多信息（如果有API）
                return {
                    "active": True,
                    "stream_key": stream_key,
                    "encoding": "H.264",
                    "bitrate": 4500,  # 默认值，实际应从API获取
                    "resolution": "1920x1080",
                    "fps": 60
                }
            else:
                return {"active": False}

        except Exception as e:
            logger.error(f"解析playstation状态失败: {e}")
            return {"active": False}

    def _get_nginx_status(self) -> Dict:
        """
        获取nginx-rtmp-module的状态
        需要nginx配置中开启stats
        """
        try:
            resp = requests.get(self.rtmp_url, timeout=5)
            if resp.status_code != 200:
                return {"active": False}

            data = resp.text

            # nginx-rtmp stats通常是XML格式
            # 需要解析XML查找活跃的流
            import xml.etree.ElementTree as ET
            import re
            root = ET.fromstring(data)

            # 查找live流
            for stream in root.findall('.//stream'):
                # name是子元素，不是属性
                name_elem = stream.find('name')
                name = name_elem.text if name_elem is not None else ''

                # 检查是否有active元素（自闭合标签表示活跃）
                active_elem = stream.find('active')
                is_active = active_elem is not None  # 如果存在active元素，说明流是活跃的

                if name and is_active:
                    # 使用正则表达式从原始XML中提取所有信息
                    bw_in_match = re.search(r'<bw_in>(\d+)</bw_in>', data)
                    width_match = re.search(r'<width>(\d+)</width>', data)
                    height_match = re.search(r'<height>(\d+)</height>', data)
                    fps_match = re.search(r'<frame_rate>(\d+)</frame_rate>', data)
                    codec_match = re.search(r'<codec>(\w+)</codec>', data)

                    # 提取并转换数据
                    bw_in = bw_in_match.group(1) if bw_in_match else '0'
                    bitrate = int(bw_in) // 1000 if bw_in.isdigit() else 0

                    width = width_match.group(1) if width_match else '1920'
                    height = height_match.group(1) if height_match else '1080'
                    resolution = f"{width}x{height}"

                    fps = fps_match.group(1) if fps_match else '60'

                    encoding = codec_match.group(1) if codec_match else 'H.264'

                    return {
                        "active": True,
                        "stream_key": name,
                        "encoding": encoding,
                        "bitrate": bitrate,
                        "resolution": resolution,
                        "fps": int(fps) if fps.isdigit() else 60
                    }

            return {"active": False}

        except Exception as e:
            logger.error(f"解析nginx-rtmp状态失败: {e}")
            return {"active": False}

    def _get_custom_status(self) -> Dict:
        """
        获取自定义API的状态
        假设API返回JSON格式
        """
        try:
            resp = requests.get(self.rtmp_url, timeout=5)
            if resp.status_code != 200:
                return {"active": False}

            data = resp.json()

            # 根据实际API响应格式解析
            # 示例格式（需要根据实际调整）:
            # {
            #   "live": true,
            #   "stream_key": "live_abc123",
            #   "bitrate": 4500,
            #   "resolution": "1920x1080",
            #   "fps": 60
            # }

            if data.get("live", False):
                return {
                    "active": True,
                    "stream_key": data.get("stream_key", ""),
                    "encoding": data.get("encoding", "H.264"),
                    "bitrate": data.get("bitrate", 0),
                    "resolution": data.get("resolution", ""),
                    "fps": data.get("fps", 0)
                }

            return {"active": False}

        except Exception as e:
            logger.error(f"解析自定义API状态失败: {e}")
            return {"active": False}

    def _get_srs_status(self) -> Dict:
        """
        获取SRS (Simple Realtime Server) 的状态
        SRS提供JSON格式的API: /api/v1/streams
        """
        try:
            resp = requests.get(self.rtmp_url, timeout=5)
            if resp.status_code != 200:
                logger.warning(f"SRS API返回状态码: {resp.status_code}")
                return {"active": False}

            data = resp.json()
            
            # SRS API返回格式:
            # {
            #   "code": 0,
            #   "server": {...},
            #   "streams": {
            #     "live_xxx": {
            #       "name": "live_xxx",
            #       "vhost": "__defaultVhost__",
            #       "app": "live",
            #       "stream": "live_xxx",
            #       "publish": true,
            #       "active": true,
            #       "video": {
            #         "codec": "H264",
            #         "profile": "High",
            #         "level": "4.2",
            #         "width": 1920,
            #         "height": 1080,
            #         "frame_rate": 60.0,
            #         "bitrate": 15000,
            #         "pix_fmt": "yuv420p"
            #       },
            #       "audio": {
            #         "codec": "AAC",
            #         "sample_rate": 48000,
            #         "channel": 2,
            #         "bitrate": 384
            #       }
            #     }
            #   }
            # }

            if data.get("code") != 0:
                logger.warning(f"SRS API返回错误: {data.get('message')}")
                return {"active": False}

            streams = data.get("streams", {})
            
            # 查找活跃的推流
            for stream_name, stream_info in streams.items():
                is_publishing = stream_info.get("publish", False)
                is_active = stream_info.get("active", False)
                
                if is_publishing and is_active:
                    # 提取视频信息
                    video = stream_info.get("video", {})
                    audio = stream_info.get("audio", {})
                    
                    # 提取分辨率
                    width = video.get("width", 1920)
                    height = video.get("height", 1080)
                    resolution = f"{width}x{height}"
                    
                    # 提取帧率
                    fps = int(video.get("frame_rate", 60))
                    
                    # 提取码率 (kbps)
                    video_bitrate = video.get("bitrate", 0)
                    audio_bitrate = audio.get("bitrate", 0)
                    total_bitrate = video_bitrate + audio_bitrate
                    
                    # 编码格式
                    codec = video.get("codec", "H264")
                    profile = video.get("profile", "High")
                    encoding = f"{codec} {profile}"
                    
                    logger.info(
                        f"检测到推流: {stream_name} | "
                        f"{resolution} | {fps}fps | "
                        f"视频:{video_bitrate}kbps 音频:{audio_bitrate}kbps"
                    )

                    return {
                        "active": True,
                        "stream_key": stream_name,
                        "encoding": encoding,
                        "bitrate": total_bitrate,
                        "resolution": resolution,
                        "fps": fps
                    }

            logger.debug("没有检测到活跃推流")
            return {"active": False}

        except requests.exceptions.RequestException as e:
            logger.error(f"连接SRS API失败: {e}")
            return {"active": False}
        except Exception as e:
            logger.error(f"解析SRS状态失败: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            return {"active": False}

    def update_danmaku_status(self, status: Dict) -> bool:
        """更新到danmaku_forward.py的状态"""
        try:
            logger.debug(f'update_danmaku_status: 发送数据 = {status}')
            resp = requests.post(
                self.danmaku_url,
                json=status,
                timeout=5,
                headers={'Content-Type': 'application/json'}
            )

            if resp.status_code == 200:
                result = resp.json()
                logger.debug(f'update_danmaku_status: 响应 = {result}')
                if result.get('code') == 0:
                    logger.debug("RTMP状态已更新到danmaku_forward.py")
                    return True
                else:
                    logger.warning(f"更新失败: {result.get('msg')}")
                    return False
            else:
                logger.error(f"更新失败，状态码: {resp.status_code}")
                return False

        except Exception as e:
            logger.error(f"更新状态时发生错误: {e}")
            return False

    def run(self):
        """运行监控循环"""
        logger.info("=" * 60)
        logger.info("RTMP推流监控服务启动")
        logger.info(f"  RTMP服务器: {self.rtmp_url}")
        logger.info(f"  RTMP类型: {self.server_type}")
        logger.info(f"  监控间隔: {CHECK_INTERVAL}秒")
        logger.info(f"  Danmaku API: {self.danmaku_url}")
        logger.info("=" * 60)

        while True:
            try:
                # 获取RTMP状态
                rtmp_status = self.get_rtmp_status()

                if rtmp_status.get("active", False):
                    logger.info(
                        f"推流中 - {rtmp_status.get('stream_key')} | "
                        f"{rtmp_status.get('resolution')} | "
                        f"{rtmp_status.get('fps')}fps | "
                        f"{rtmp_status.get('bitrate')}kbps"
                    )

                    # 更新到danmaku_forward.py
                    self.update_danmaku_status(rtmp_status)
                else:
                    if self.last_active:
                        logger.info("推流已停止")

                    # 更新为非活跃状态
                    self.update_danmaku_status({"active": False})

                self.last_active = rtmp_status.get("active", False)

            except KeyboardInterrupt:
                logger.info("收到停止信号，退出监控")
                break
            except Exception as e:
                logger.error(f"监控循环错误: {e}", exc_info=True)

            # 等待下一次检查
            time.sleep(CHECK_INTERVAL)


def test_connection():
    """测试配置是否正确"""
    logger.info("测试RTMP服务器连接...")
    monitor = RTMPMonitor(RTMP_MONITOR_URL, DANMAKU_API_URL, RTMP_SERVER_TYPE)

    if monitor.check_connection():
        logger.info("✓ RTMP服务器连接成功")
    else:
        logger.error("✗ RTMP服务器连接失败，请检查配置")
        return False

    logger.info("测试Danmaku API连接...")
    try:
        resp = requests.post(
            DANMAKU_API_URL,
            json={"active": False},
            timeout=5
        )
        if resp.status_code == 200:
            logger.info("✓ Danmaku API连接成功")
        else:
            logger.error(f"✗ Danmaku API返回错误: {resp.status_code}")
            return False
    except Exception as e:
        logger.error(f"✗ Danmaku API连接失败: {e}")
        return False

    return True


if __name__ == "__main__":
    # 测试连接
    if not test_connection():
        logger.error("连接测试失败，请检查配置")
        input("按回车键退出...")
        exit(1)

    # 启动监控
    monitor = RTMPMonitor(RTMP_MONITOR_URL, DANMAKU_API_URL, RTMP_SERVER_TYPE)
    monitor.run()
