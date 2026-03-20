#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RTMP流媒体服务器 (Windows原生版本)
集成nginx-rtmp功能，用于接收PS5推流
替代Docker版本的bao3/playstation

功能：
- RTMP推流接收 (通过调用nginx-rtmp-win32)
- 推流状态监控
- 自动转发到B站/斗鱼等平台
- Web管理界面

依赖：
- nginx-rtmp-win32 (已编译的Windows版nginx with rtmp模块)
- Python 3.8+
- requests
"""

import os
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

import subprocess
import time
import json
import logging
import requests
import threading
from typing import Dict, List, Optional
from pathlib import Path
import shutil

# ==================== 全局配置 ====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
NGINX_DIR = os.path.join(BASE_DIR, "nginx-rtmp")
NGINX_CONF = os.path.join(NGINX_DIR, "conf", "nginx.conf")
NGINX_EXE = os.path.join(NGINX_DIR, "nginx.exe")
STAT_FILE = os.path.join(BASE_DIR, "rtmp_stat.json")

# 默认配置
DEFAULT_CONFIG = {
    "rtmp_port": 1935,
    "http_port": 8080,
    "stats_port": 8080,
    "chunk_size": 4096,
    "application_name": "app",
    "auto_forward": False,
    "forward_targets": {
        "bilibili": {
            "enabled": False,
            "rtmp_url": "",
            "stream_key": ""
        },
        "douyu": {
            "enabled": False,
            "rtmp_url": "",
            "stream_key": ""
        }
    },
    "enable_stats": True,
    "enable_hls": False,
    "max_bitrate": 10000  # 最大码率 10000 kbps
}

CONFIG = DEFAULT_CONFIG.copy()

# 全局状态
nginx_process = None
nginx_running = False
active_streams = {}

# ==================== 日志配置 ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('rtmp_server.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)


class NginxRTMPManager:
    """Nginx RTMP管理器"""

    def __init__(self):
        self.nginx_dir = NGINX_DIR
        self.config_file = NGINX_CONF
        self.exe_path = NGINX_EXE
        self.process = None

    def check_installation(self) -> bool:
        """检查nginx-rtmp是否已安装"""
        if not os.path.exists(self.exe_path):
            logger.error(f"nginx.exe 未找到: {self.exe_path}")
            return False
        return True

    def generate_config(self) -> bool:
        """生成nginx配置文件"""
        try:
            config_content = f'''# nginx-rtmp配置文件
# 由PS5-Bilibili-Danmaku自动生成

worker_processes  1;
error_log  logs/error.log;
pid        logs/nginx.pid;

events {{
    worker_connections  1024;
}}

rtmp {{
    server {{
        listen {CONFIG['rtmp_port']};
        chunk_size {CONFIG['chunk_size']};

        application {CONFIG['application_name']} {{
            live on;
            record off;
            interleave on;
        }}
'''

            # 添加HLS支持
            if CONFIG['enable_hls']:
                hls_path = os.path.join(self.nginx_dir, "temp", "hls")
                os.makedirs(hls_path, exist_ok=True)

                config_content += f'''
        application hls {{
            live on;
            hls on;
            hls_path {hls_path};
            hls_fragment 3s;
        }}
'''

            config_content += '    }\n}\n\n'

            # HTTP服务器配置
            config_content += '''http {
    server {
        listen ''' + str(CONFIG['http_port']) + ''';
        location / {
            root html;
            index index.html;
        }

'''

            # 添加统计页面
            if CONFIG['enable_stats']:
                config_content += '''        location /stat {
            rtmp_stat all;
            rtmp_stat_stylesheet stat.xsl;
        }

        location /stat.xsl {
            root html;
        }

'''

            # 添加HLS支持
            if CONFIG['enable_hls']:
                hls_path = os.path.join(self.nginx_dir, "temp", "hls")
                config_content += f'''        location /hls {{
            types {{
                application/vnd.apple.mpegurl m3u8;
                video/mp2t ts;
            }}
            alias {hls_path};
            add_header Cache-Control no-cache;
        }}

'''

            config_content += '    }\n}\n'

            # 写入配置文件
            os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
            with open(self.config_file, 'w', encoding='utf-8') as f:
                f.write(config_content)

            logger.info(f"配置文件已生成: {self.config_file}")
            return True

        except Exception as e:
            logger.error(f"生成配置文件失败: {e}")
            return False

    def start(self) -> bool:
        """启动nginx-rtmp服务"""
        if not self.check_installation():
            logger.error("nginx-rtmp未安装，请先下载nginx-rtmp-win32")
            return False

        if self.is_running():
            logger.warning("nginx-rtmp已在运行")
            return True

        try:
            # 生成配置文件
            if not self.generate_config():
                return False

            # 启动nginx
            logger.info(f"启动nginx-rtmp: {self.exe_path}")
            self.process = subprocess.Popen(
                [self.exe_path, '-c', self.config_file],
                cwd=self.nginx_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )

            # 等待启动
            time.sleep(2)

            if self.is_running():
                logger.info("✓ nginx-rtmp启动成功")
                logger.info(f"  RTMP端口: {CONFIG['rtmp_port']}")
                logger.info(f"  HTTP端口: {CONFIG['http_port']}")
                if CONFIG['enable_stats']:
                    logger.info(f"  统计页面: http://127.0.0.1:{CONFIG['http_port']}/stat")
                return True
            else:
                logger.error("nginx-rtmp启动失败")
                return False

        except Exception as e:
            logger.error(f"启动nginx-rtmp失败: {e}")
            return False

    def stop(self) -> bool:
        """停止nginx-rtmp服务"""
        try:
            if self.is_running():
                logger.info("停止nginx-rtmp...")
                subprocess.run([self.exe_path, '-s', 'stop'], cwd=self.nginx_dir)
                time.sleep(2)

                if self.is_running():
                    # 强制停止
                    subprocess.run(['taskkill', '/F', '/IM', 'nginx.exe'], capture_output=True)
                    time.sleep(1)

                logger.info("✓ nginx-rtmp已停止")
                return True
            return True
        except Exception as e:
            logger.error(f"停止nginx-rtmp失败: {e}")
            return False

    def restart(self) -> bool:
        """重启nginx-rtmp服务"""
        self.stop()
        time.sleep(2)
        return self.start()

    def reload(self) -> bool:
        """重新加载nginx配置"""
        try:
            if not self.is_running():
                logger.warning("nginx-rtmp未运行，无法重载配置")
                return False

            logger.info("重新加载nginx配置...")
            subprocess.run([self.exe_path, '-s', 'reload'], cwd=self.nginx_dir)
            time.sleep(1)
            logger.info("✓ 配置已重载")
            return True
        except Exception as e:
            logger.error(f"重载配置失败: {e}")
            return False

    def is_running(self) -> bool:
        """检查nginx是否正在运行"""
        try:
            result = subprocess.run(
                ['tasklist', '/FI', 'IMAGENAME eq nginx.exe'],
                capture_output=True,
                text=True
            )
            return 'nginx.exe' in result.stdout
        except Exception:
            return False

    def get_stats(self) -> Dict:
        """获取RTMP统计信息"""
        if not CONFIG['enable_stats']:
            return {"error": "统计功能未启用"}

        try:
            url = f"http://127.0.0.1:{CONFIG['http_port']}/stat"
            resp = requests.get(url, timeout=5)

            if resp.status_code == 200:
                # 解析XML格式的统计信息
                import xml.etree.ElementTree as ET
                root = ET.fromstring(resp.content)

                streams = []
                server = root.find('server')
                if server is not None:
                    for application in server.findall('application'):
                        app_name = application.get('name', '')
                        for stream in application.findall('stream'):
                            stream_name = stream.get('name', '')
                            bw_in = stream.get('bw_in', '0')
                            bw_out = stream.get('bw_out', '0')
                            fps = stream.get('fps', '0')
                            resolution = stream.get('resolution', '')

                            streams.append({
                                'name': stream_name,
                                'application': app_name,
                                'bitrate_in': int(int(bw_in) / 1000) if bw_in.isdigit() else 0,
                                'bitrate_out': int(int(bw_out) / 1000) if bw_out.isdigit() else 0,
                                'fps': int(fps) if fps.isdigit() else 0,
                                'resolution': resolution
                            })

                return {
                    'running': True,
                    'streams': streams,
                    'total': len(streams)
                }
            else:
                return {'running': False, 'error': f'HTTP {resp.status_code}'}

        except Exception as e:
            return {'running': False, 'error': str(e)}


class Forwarder:
    """RTMP转发器（转发到其他平台）"""

    def __init__(self):
        self.active = False
        self.ffmpeg_processes = {}

    def start_forward(self, stream_name: str, target: str, target_key: str) -> bool:
        """开始转发流到目标平台"""
        try:
            source_url = f"rtmp://127.0.0.1:{CONFIG['rtmp_port']}/{CONFIG['application_name']}/{stream_name}"
            target_url = f"{target}/{target_key}"

            logger.info(f"开始转发: {source_url} -> {target_url}")

            # 使用FFmpeg转发
            cmd = [
                'ffmpeg',
                '-i', source_url,
                '-c', 'copy',
                '-f', 'flv',
                target_url,
                '-loglevel', 'error'
            ]

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )

            self.ffmpeg_processes[stream_name] = process
            return True

        except Exception as e:
            logger.error(f"启动转发失败: {e}")
            return False

    def stop_forward(self, stream_name: str):
        """停止转发"""
        if stream_name in self.ffmpeg_processes:
            process = self.ffmpeg_processes[stream_name]
            process.terminate()
            del self.ffmpeg_processes[stream_name]
            logger.info(f"已停止转发: {stream_name}")

    def stop_all(self):
        """停止所有转发"""
        for stream_name in list(self.ffmpeg_processes.keys()):
            self.stop_forward(stream_name)


# ==================== 主程序 ====================

def show_banner():
    """显示欢迎横幅"""
    print("""
╔════════════════════════════════════════════════════════════╗
║                                                              ║
║          PS5 RTMP流媒体服务器 v1.0 (Windows原生)            ║
║          PS5-Bilibili-Danmaku 集成模块                        ║
║                                                              ║
║  功能：                                                      ║
║  • RTMP推流接收 (基于nginx-rtmp-win32)                      ║
║  • 推流状态监控                                               ║
║  • 自动转发到B站/斗鱼等平台                                   ║
║  • Web管理界面                                               ║
║                                                              ║
╚════════════════════════════════════════════════════════════╝
""")


def show_menu():
    """显示菜单"""
    print("\n" + "="*60)
    print("请选择操作:")
    print("  1. 启动RTMP服务器")
    print("  2. 停止RTMP服务器")
    print("  3. 重启RTMP服务器")
    print("  4. 查看服务器状态")
    print("  5. 查看推流统计")
    print("  6. 配置服务器")
    print("  7. 查看日志")
    print("  8. 测试推流")
    print("  0. 退出")
    print("="*60)


def main():
    """主函数"""
    show_banner()

    manager = NginxRTMPManager()
    forwarder = Forwarder()

    while True:
        show_menu()
        choice = input("\n请输入选项: ").strip()

        if choice == '1':
            manager.start()

        elif choice == '2':
            manager.stop()

        elif choice == '3':
            manager.restart()

        elif choice == '4':
            if manager.is_running():
                print("\n✓ RTMP服务器运行中")
                print(f"  RTMP地址: rtmp://192.168.x.x:{CONFIG['rtmp_port']}/{CONFIG['application_name']}/<stream_key>")
                if CONFIG['enable_stats']:
                    print(f"  统计页面: http://192.168.x.x:{CONFIG['http_port']}/stat")
            else:
                print("\n✗ RTMP服务器未运行")

        elif choice == '5':
            stats = manager.get_stats()
            if stats.get('running'):
                print(f"\n当前推流数: {stats.get('total', 0)}")
                for stream in stats.get('streams', []):
                    print(f"\n  流名称: {stream['name']}")
                    print(f"  分辨率: {stream['resolution']}")
                    print(f"  帧率: {stream['fps']} fps")
                    print(f"  码率: {stream['bitrate_in']} kbps")
            else:
                print(f"\n错误: {stats.get('error', '服务器未运行')}")

        elif choice == '6':
            print("\n配置功能暂未实现，请手动编辑配置文件")

        elif choice == '7':
            log_file = os.path.join(BASE_DIR, "rtmp_server.log")
            if os.path.exists(log_file):
                with open(log_file, 'r', encoding='utf-8') as f:
                    print("\n" + f.read()[-2000:])  # 显示最后2000字符
            else:
                print("\n日志文件不存在")

        elif choice == '8':
            print("\n测试推流说明:")
            print("1. 启动OBS Studio")
            print("2. 设置 -> 推流")
            print(f"3. 服务器: rtmp://127.0.0.1:{CONFIG['rtmp_port']}/{CONFIG['application_name']}")
            print("4. 串流密钥: test_stream")
            print("5. 点击'开始推流'")

        elif choice == '0':
            manager.stop()
            print("\n再见！")
            break

        else:
            print("\n无效选项，请重新选择")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n程序已中断")
        manager.stop()
        sys.exit(0)
