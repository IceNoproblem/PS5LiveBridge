#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nginx进程管理工具
防止nginx重复启动，管理nginx进程
"""

import os
import sys
import subprocess
import time
from pathlib import Path

# nginx目录
NGINX_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nginx-rtmp")
NGINX_EXE = os.path.join(NGINX_DIR, "nginx.exe")


def is_nginx_running():
    """检查nginx是否正在运行"""
    try:
        result = subprocess.run(
            ['tasklist'],
            capture_output=True,
            text=True,
            encoding='gbk'
        )
        return 'nginx.exe' in result.stdout
    except:
        return False


def stop_nginx():
    """停止所有nginx进程"""
    try:
        print("正在停止nginx...")
        subprocess.run(
            ['taskkill', '/F', '/IM', 'nginx.exe'],
            capture_output=True,
            check=True
        )
        print("[OK] nginx已停止")
        return True
    except subprocess.CalledProcessError as e:
        if "未找到" in str(e) or "not found" in str(e).lower():
            print("[INFO] nginx未运行")
            return True
        print(f"[ERROR] 停止nginx失败: {e}")
        return False


def start_nginx():
    """启动nginx"""
    if not os.path.exists(NGINX_EXE):
        print(f"[ERROR] 未找到nginx.exe: {NGINX_EXE}")
        return False

    # 先停止现有进程
    if is_nginx_running():
        print("[INFO] 检测到nginx正在运行，正在停止...")
        if not stop_nginx():
            print("[ERROR] 无法停止现有nginx进程")
            return False
        time.sleep(1)  # 等待进程完全停止

    # 测试配置
    print("[INFO] 测试nginx配置...")
    test_result = subprocess.run(
        [NGINX_EXE, '-t'],
        capture_output=True,
        text=True,
        encoding='gbk'
    )

    if test_result.returncode != 0:
        print(f"[ERROR] nginx配置测试失败:")
        print(test_result.stdout)
        print(test_result.stderr)
        return False

    print("[OK] nginx配置测试通过")

    # 启动nginx
    print("[INFO] 正在启动nginx...")
    subprocess.run(
        [NGINX_EXE],
        capture_output=True,
        cwd=NGINX_DIR
    )

    time.sleep(1)  # 等待启动

    if is_nginx_running():
        print("[OK] nginx启动成功")
        print(f"  RTMP端口: 1935")
        print(f"  HTTP端口: 8080")
        print(f"  统计页面: http://127.0.0.1:8080/stat")
        return True
    else:
        print("[ERROR] nginx启动失败")
        return False


def restart_nginx():
    """重启nginx"""
    print("[INFO] 正在重启nginx...")
    if stop_nginx():
        time.sleep(1)
        return start_nginx()
    return False


def reload_nginx():
    """重载nginx配置（平滑重启）"""
    if not is_nginx_running():
        print("[ERROR] nginx未运行，无法重载")
        return False

    print("[INFO] 正在重载nginx配置...")
    subprocess.run(
        [NGINX_EXE, '-s', 'reload'],
        capture_output=True,
        cwd=NGINX_DIR
    )
    print("[OK] nginx配置已重载")
    return True


def check_ports():
    """检查端口占用情况"""
    print("\n检查端口占用情况:")
    print("="*50)

    ports = [1935, 8080, 5000]
    for port in ports:
        result = subprocess.run(
            ['netstat', '-ano'],
            capture_output=True,
            text=True,
            encoding='gbk'
        )
        occupied = f":{port}" in result.stdout
        status = "[占用]" if occupied else "[空闲]"
        print(f"  端口 {port}: {status}")

    print("="*50 + "\n")


def show_menu():
    """显示菜单"""
    print("\n" + "="*50)
    print("  nginx-rtmp 管理工具")
    print("="*50)
    print("  1. 启动nginx")
    print("  2. 停止nginx")
    print("  3. 重启nginx")
    print("  4. 重载配置")
    print("  5. 检查状态")
    print("  6. 检查端口")
    print("  0. 退出")
    print("="*50)


if __name__ == "__main__":
    while True:
        show_menu()
        choice = input("\n请选择操作 (0-6): ").strip()

        if choice == '1':
            start_nginx()
        elif choice == '2':
            stop_nginx()
        elif choice == '3':
            restart_nginx()
        elif choice == '4':
            reload_nginx()
        elif choice == '5':
            if is_nginx_running():
                print("[OK] nginx正在运行")
            else:
                print("[INFO] nginx未运行")
        elif choice == '6':
            check_ports()
        elif choice == '0':
            print("退出")
            break
        else:
            print("[ERROR] 无效选择")

        input("\n按回车键继续...")
