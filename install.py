#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自动安装脚本
自动下载所有依赖并完成配置

功能：
- 自动下载nginx-rtmp-win32
- 自动安装Python依赖包
- 自动配置防火墙
- 自动生成配置文件
- 自动启动所有服务
"""

import os
import sys
import subprocess
import requests
import zipfile
import shutil
import time
import json
from pathlib import Path

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
NGINX_URL = "https://github.com/illuspas/nginx-rtmp-win32/archive/refs/tags/v1.2.1.zip"
NGINX_MIRROR_URL = "https://gitee.com/bmw360/nginx-rtmp-win32/archive/refs/tags/v1.2.1.zip"
NGINX_DIR = os.path.join(BASE_DIR, "nginx-rtmp")
NGINX_ZIP = os.path.join(BASE_DIR, "nginx-rtmp.zip")

# Python依赖包
PYTHON_PACKAGES = [
    "requests",
    "aiohttp",
    "flask"
]

# ==================== 日志和输出 ====================
def print_header(title):
    """打印标题"""
    print("\n" + "="*70)
    print(f"  {title}")
    print("="*70)

def print_success(msg):
    """打印成功消息"""
    print(f"[OK] {msg}")

def print_error(msg):
    """打印错误消息"""
    print(f"[ERROR] {msg}")

def print_info(msg):
    """打印信息"""
    print(f"  {msg}")

def print_warning(msg):
    """打印警告"""
    print(f"[WARNING] {msg}")

# ==================== 检查运行环境 ====================
def check_python_version():
    """检查Python版本"""
    print_header("检查Python版本")
    version = sys.version_info
    print_info(f"当前Python版本: {version.major}.{version.minor}.{version.micro}")

    if version.major < 3 or (version.major == 3 and version.minor < 8):
        print_error("Python版本过低，需要Python 3.8或更高版本")
        return False

    print_success("Python版本符合要求")
    return True

def check_admin():
    """检查是否以管理员身份运行"""
    print_header("检查管理员权限")

    try:
        import ctypes
        is_admin = ctypes.windll.shell32.IsUserAnAdmin() != 0

        if is_admin:
            print_success("已以管理员身份运行")
            return True
        else:
            print_warning("未以管理员身份运行")
            print_info("部分功能（如配置防火墙）可能无法使用")
            return False
    except:
        print_warning("无法检测管理员权限")
        return False

# ==================== 下载nginx-rtmp-win32 ====================
def download_file(url, dest, desc):
    """下载文件"""
    print_info(f"正在下载 {desc}...")
    print_info(f"  来源: {url}")

    try:
        response = requests.get(url, stream=True, timeout=60)
        response.raise_for_status()

        total_size = int(response.headers.get('content-length', 0))
        block_size = 8192
        downloaded = 0

        with open(dest, 'wb') as f:
            for chunk in response.iter_content(chunk_size=block_size):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        percent = (downloaded / total_size) * 100
                        sys.stdout.write(f"\r    进度: {percent:.1f}% ({downloaded}/{total_size} bytes)")
                        sys.stdout.flush()

        print()  # 换行
        print_success(f"{desc} 下载完成")
        return True

    except requests.exceptions.RequestException as e:
        print_error(f"下载失败: {e}")
        return False

def download_nginx_rtmp():
    """下载nginx-rtmp-win32"""
    print_header("下载nginx-rtmp-win32")

    # 检查是否已存在
    if os.path.exists(NGINX_DIR):
        print_warning("nginx-rtmp-win32已存在")
        choice = input("是否重新下载？(y/N): ").strip().lower()
        if choice != 'y':
            print_info("跳过下载")
            return True

    # 尝试从GitHub下载
    print_info("尝试从GitHub下载...")
    if download_file(NGINX_URL, NGINX_ZIP, "nginx-rtmp-win32 (GitHub)"):
        success = True
    else:
        print_error("GitHub下载失败")
        print_info("尝试从Gitee镜像下载...")
        if download_file(NGINX_MIRROR_URL, NGINX_ZIP, "nginx-rtmp-win32 (Gitee)"):
            success = True
        else:
            print_error("所有下载源均失败")
            print_info("请手动下载:")
            print_info(f"  GitHub: {NGINX_URL}")
            print_info(f"  Gitee: {NGINX_MIRROR_URL}")
            print_info(f"下载后解压到: {NGINX_DIR}")
            return False

    # 解压文件
    print_info("正在解压文件...")
    try:
        with zipfile.ZipFile(NGINX_ZIP, 'r') as zip_ref:
            zip_ref.extractall(BASE_DIR)

        # 重命名目录 - archive格式解压后是 nginx-rtmp-win32-1.2.1
        extracted_dir = None
        for item in os.listdir(BASE_DIR):
            if item.startswith("nginx-rtmp") and os.path.isdir(os.path.join(BASE_DIR, item)):
                extracted_dir = os.path.join(BASE_DIR, item)
                break

        if extracted_dir and extracted_dir != NGINX_DIR:
            if os.path.exists(NGINX_DIR):
                print_info("删除旧的nginx-rtmp目录...")
                shutil.rmtree(NGINX_DIR)
            os.rename(extracted_dir, NGINX_DIR)
            print_success(f"重命名: {os.path.basename(extracted_dir)} -> nginx-rtmp")
        elif not extracted_dir:
            print_error("未找到解压后的目录")
            return False

        print_success("解压成功")

        # 清理zip文件
        if os.path.exists(NGINX_ZIP):
            os.remove(NGINX_ZIP)
            print_success("清理下载的zip文件")

        print_success("nginx-rtmp-win32安装完成")
        return True

    except Exception as e:
        print_error(f"解压失败: {e}")
        return False

# ==================== 安装Python依赖 ====================
def install_python_packages():
    """安装Python依赖包"""
    print_header("安装Python依赖包")

    for package in PYTHON_PACKAGES:
        print_info(f"正在安装 {package}...")

        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", package],
                check=True,
                capture_output=True,
                text=True
            )
            print_success(f"{package} 安装成功")

        except subprocess.CalledProcessError as e:
            print_error(f"{package} 安装失败")
            print_info(f"错误: {e.stderr}")

    print_success("所有Python依赖安装完成")

# ==================== 配置防火墙 ====================
def configure_firewall(is_admin):
    """配置Windows防火墙"""
    print_header("配置Windows防火墙")

    if not is_admin:
        print_warning("未以管理员身份运行，跳过防火墙配置")
        print_info("请手动配置:")
        print_info('  1. 打开"控制面板" -> "Windows Defender 防火墙"')
        print_info('  2. 点击"高级设置" -> "入站规则" -> "新建规则"')
        print_info('  3. 选择"端口" -> "TCP" -> 输入特定本地端口: 1935, 8080, 5000')
        print_info('  4. 选择"允许连接" -> 全部网络类型勾选')
        print_info('  5. 规则名称: PS5-RTMP-Server')
        return False

    ports = [
        (1935, "PS5-RTMP-1935", "RTMP推流端口"),
        (8080, "PS5-RTMP-8080", "RTMP统计端口"),
        (5000, "PS5-Web-5000", "Web控制台端口")
    ]

    for port, name, desc in ports:
        print_info(f"配置 {desc} ({port})...")

        try:
            # 检查规则是否已存在
            result = subprocess.run(
                ["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"],
                capture_output=True,
                text=True
            )

            if "找不到" not in result.stdout:
                print_warning(f"防火墙规则 {name} 已存在")
                continue

            # 添加防火墙规则
            result = subprocess.run(
                ["netsh", "advfirewall", "firewall", "add", "rule",
                 f"name={name}",
                 "dir=in",
                 "action=allow",
                 "protocol=TCP",
                 f"localport={str(port)}"],
                capture_output=True,
                text=True
            )

            if result.returncode == 0:
                print_success(f"防火墙规则 {name} 添加成功")
            else:
                print_error(f"防火墙规则 {name} 添加失败: {result.stderr}")

        except Exception as e:
            print_error(f"配置防火墙失败: {e}")

    print_success("防火墙配置完成")
    return True

# ==================== 生成配置文件 ====================
def generate_config_files():
    """生成配置文件"""
    print_header("生成配置文件")

    # 检查config.json是否存在
    config_file = os.path.join(BASE_DIR, "config.json")

    if os.path.exists(config_file):
        print_warning("config.json 已存在")
        choice = input("是否重新生成？(y/N): ").strip().lower()
        if choice != 'y':
            print_info("跳过配置文件生成")
            return True

    # 生成默认配置
    default_config = {
        "WEB_PORT": 5000,
        "BILIBILI_ROOM_ID": 943565,
        "TWITCH_CHANNEL": "icenoproblem",
        "IRC_HOST": "0.0.0.0",
        "IRC_PORT": 6667,
        "MAX_SEEN_DANMAKU": 1000,
        "MAX_SEEN_GIFT": 500,
        "HEARTBEAT_TIMEOUT": 30,
        "USER_AGENT": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "ENABLE_GIFT": True,
        "MAX_LOG_ITEMS": 50,
        "BILIBILI_SESSDATA": "",
        "BILIBILI_BILI_JCT": "",
        "BILIBILI_UID": 0,
        "BILIBILI_UNAME": "",
        "RECONNECT_DELAY": 5,
        "ROOM_HISTORY": []
    }

    try:
        with open(config_file, 'w', encoding='utf-8') as f:
            json.dump(default_config, f, indent=2, ensure_ascii=False)

        print_success("config.json 生成完成")
        return True

    except Exception as e:
        print_error(f"生成配置文件失败: {e}")
        return False

# ==================== 获取本机IP ====================
def get_local_ip():
    """获取本机局域网IP"""
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "192.168.1.100"

# ==================== 生成DNS劫持配置说明 ====================
def generate_dns_config_guide():
    """生成DNS劫持配置说明文件"""
    print_header("生成DNS配置说明")

    local_ip = get_local_ip()

    guide_content = f"""# DNS劫持配置说明

## 重要：DNS劫持必须在路由器上配置

### 本机IP地址
{local_ip}

### 需要配置的DNS记录

请在路由器中添加以下DNS记录，指向上面的本机IP地址：

| 域名 | IP地址 | 说明 |
|------|--------|------|
| `ingest.global-contribute.live-video.net` | {local_ip} | Twitch全球推流服务器 |
| `live-eu-fra.twitch.tv` | {local_ip} | Twitch欧洲推流服务器 |
| `live.twitch.tv` | {local_ip} | Twitch直播服务器 |

### 路由器配置步骤

1. **登录路由器管理界面**
   - 在浏览器访问：http://192.168.1.1 或 http://192.168.0.1
   - 输入用户名和密码登录

2. **找到DNS设置**
   不同路由器位置不同，通常在：
   - 网络设置 -> DNS
   - 高级设置 -> DNS
   - DHCP服务 -> DNS设置

3. **添加DNS记录**
   - 选择"添加DNS记录"或"自定义DNS"
   - 域名：输入上述表格中的域名
   - IP地址：输入本机IP ({local_ip})
   - 保存配置

4. **重启路由器**
   - 保存配置后，重启路由器使DNS设置生效

### 配置PS5网络

1. PS5设置 -> 网络 -> 设置互联网连接
2. 选择你使用的网络（Wi-Fi或LAN）
3. 选择"自定义"
4. IP地址设置：自动
5. DHCP主机名：不指定
6. DNS设置：手动
7. 主DNS：{local_ip}
8. 辅助DNS：8.8.8.8
9. MTU设置：自动
10. 代理服务器：不使用
11. 保存并测试连接

### 验证DNS劫持是否生效

在PS5上打开浏览器，访问：http://{local_ip}:8080

如果能打开nginx欢迎页面，说明DNS劫持配置成功。

### 如果不配置DNS劫持

如果不配置DNS劫持，PS5无法将推流重定向到本机，将无法使用推流功能。
"""

    guide_file = os.path.join(BASE_DIR, "DNS_CONFIG.md")

    try:
        with open(guide_file, 'w', encoding='utf-8') as f:
            f.write(guide_content)

        print_success(f"DNS配置说明已生成: DNS_CONFIG.md")
        print_info(f"  本机IP: {local_ip}")
        return True

    except Exception as e:
        print_error(f"生成DNS配置说明失败: {e}")
        return False

# ==================== 测试nginx ====================
def test_nginx():
    """测试nginx是否正常工作"""
    print_header("测试nginx")

    nginx_exe = os.path.join(NGINX_DIR, "nginx.exe")

    if not os.path.exists(nginx_exe):
        print_error("nginx.exe 不存在")
        return False

    print_info("测试nginx启动...")

    try:
        # 生成临时配置
        import rtmp_server
        from rtmp_server import NginxRTMPManager

        manager = NginxRTMPManager()

        # 尝试启动nginx
        if manager.start():
            print_success("nginx启动成功")

            # 等待2秒
            time.sleep(2)

            # 检查是否在运行
            if manager.is_running():
                print_success("nginx运行正常")

                # 测试HTTP访问
                try:
                    response = requests.get("http://127.0.0.1:8080", timeout=5)
                    if response.status_code == 200:
                        print_success("HTTP服务正常")

                        # 停止nginx
                        manager.stop()
                        print_success("nginx测试完成")
                        return True
                    else:
                        print_error(f"HTTP服务异常: {response.status_code}")
                        manager.stop()
                        return False

                except Exception as e:
                    print_error(f"HTTP访问失败: {e}")
                    manager.stop()
                    return False
            else:
                print_error("nginx启动失败")
                return False
        else:
            print_error("nginx启动失败")
            return False

    except Exception as e:
        print_error(f"测试nginx失败: {e}")
        return False

# ==================== 显示安装完成信息 ====================
def show_completion_info():
    """显示安装完成信息"""
    print_header("安装完成")

    local_ip = get_local_ip()

    print("""
    ========================================================
                    安装完成！现在可以开始使用
    ========================================================
    """)

    print_info("后续步骤:")
    print()

    print("1. 配置DNS劫持 (重要)")
    print(f"   - 本机IP: {local_ip}")
    print("   - 参考: DNS_CONFIG.md")
    print()

    print("2. 配置PS5网络")
    print(f"   - 主DNS: {local_ip}")
    print("   - 辅助DNS: 8.8.8.8")
    print()

    print("3. 启动服务")
    print("   python start_all.py")
    print()

    print("4. 访问Web界面")
    print(f"   - 弹幕管理: http://127.0.0.1:5000")
    print(f"   - RTMP统计: http://127.0.0.1:8080/stat")
    print()

    print("5. 开始推流")
    print("   - 在PS5上启动Twitch广播")
    print("   - 查看RTMP统计页面获取推流地址")
    print("   - 配置OBS推流到B站")
    print()

    print_warning("重要提示:")
    print("  - 必须配置路由器DNS劫持才能使用推流功能")
    print("  - PS5和运行脚本的电脑必须在同一局域网")
    print("  - 请阅读 README_WINDOWS.md 和 INSTALL_WINDOWS.md 了解更多")

# ==================== 主程序 ====================
def main():
    """主函数"""
    print("""
=================================================================
        PS5-Bilibili-Danmaku 自动安装程序

  此程序将自动：
  - 下载nginx-rtmp-win32
  - 安装Python依赖包
  - 配置Windows防火墙
  - 生成配置文件

=================================================================
    """)

    input("按回车键继续...")

    # 检查环境
    is_admin = check_admin()
    if not check_python_version():
        input("\n按回车键退出...")
        sys.exit(1)

    # 安装Python依赖
    install_python_packages()

    # 下载nginx-rtmp
    if not download_nginx_rtmp():
        print_warning("nginx-rtmp-win32下载失败，但可以继续安装其他组件")

    # 配置防火墙
    configure_firewall(is_admin)

    # 生成配置文件
    generate_config_files()

    # 生成DNS配置说明
    generate_dns_config_guide()

    # 测试nginx
    if os.path.exists(NGINX_DIR):
        test_nginx()

    # 显示完成信息
    show_completion_info()

    # 询问是否立即启动
    print()
    choice = input("是否立即启动所有服务？(Y/n): ").strip().lower()
    if choice != 'n':
        print()
        print_header("启动服务")
        try:
            subprocess.run([sys.executable, "start_all.py"])
        except KeyboardInterrupt:
            print("\n\n程序已停止")

    print()
    print("感谢使用！")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n安装已取消")
        sys.exit(0)
    except Exception as e:
        print_error(f"安装过程中发生错误: {e}")
        import traceback
        traceback.print_exc()
        input("\n按回车键退出...")
        sys.exit(1)
