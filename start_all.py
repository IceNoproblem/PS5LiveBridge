#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键启动脚本
同时启动RTMP服务器和弹幕转发系统

使用方法：
python start_all.py
"""

import os
import sys

# 修复 Windows 控制台编码问题
if sys.platform == 'win32':
    import io
    import codecs
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
    # 同时也尝试通过 locale 设置
    import locale
    try:
        locale.setlocale(locale.LC_ALL, 'zh_CN.UTF-8')
    except:
        pass

import subprocess
import time
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def start_rtmp_server():
    """启动RTMP服务器"""
    print("\n" + "="*60)
    print("启动RTMP服务器...")
    print("="*60)

    rtmp_script = os.path.join(BASE_DIR, "rtmp_server.py")

    if not os.path.exists(rtmp_script):
        print(f"错误: 未找到 rtmp_server.py")
        print(f"请确保文件位于: {rtmp_script}")
        return

    # 启动RTMP服务器
    import subprocess
    process = subprocess.Popen(
        [sys.executable, rtmp_script],
        cwd=BASE_DIR,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
        encoding='utf-8',
        errors='replace'
    )

    # 等待启动并自动选择启动选项
    time.sleep(3)

    # 向脚本发送"1"命令启动服务器
    try:
        process.stdin.write("1\n")
        process.stdin.flush()
    except:
        pass

    time.sleep(2)

    # 监控输出
    def monitor_output():
        for line in process.stdout:
            if line.strip():
                print(f"[RTMP] {line.strip()}")

    output_thread = threading.Thread(target=monitor_output)
    output_thread.daemon = False
    output_thread.start()

    print("✓ RTMP服务器已启动")
    print(f"  RTMP端口: 1935")
    print(f"  HTTP端口: 8080")
    print(f"  统计页面: http://127.0.0.1:8080/stat")

    return process


def start_danmaku_system():
    """启动弹幕转发系统"""
    print("\n" + "="*60)
    print("启动弹幕转发系统...")
    print("="*60)

    danmaku_script = os.path.join(BASE_DIR, "danmaku_forward.py")

    if not os.path.exists(danmaku_script):
        print(f"错误: 未找到 danmaku_forward.py")
        print(f"请确保文件位于: {danmaku_script}")
        return

    # 启动弹幕转发系统
    process = subprocess.Popen(
        [sys.executable, danmaku_script],
        cwd=BASE_DIR,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
        encoding='utf-8',
        errors='replace'
    )

    # 监控输出
    def monitor_output():
        for line in process.stdout:
            if line.strip():
                print(f"[DANMAKU] {line.strip()}")

    output_thread = threading.Thread(target=monitor_output)
    output_thread.daemon = False
    output_thread.start()

    print("✓ 弹幕转发系统已启动")
    print(f"  Web控制台: http://127.0.0.1:5000")

    return process


def start_rtmp_monitor():
    """启动RTMP监控脚本"""
    print("\n" + "="*60)
    print("启动RTMP监控...")
    print("="*60)

    monitor_script = os.path.join(BASE_DIR, "monitor_rtmp.py")

    if not os.path.exists(monitor_script):
        print(f"警告: 未找到 monitor_rtmp.py")
        print(f"跳过RTMP监控")
        return None

    # 启动监控脚本
    process = subprocess.Popen(
        [sys.executable, monitor_script],
        cwd=BASE_DIR,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
        encoding='utf-8',
        errors='replace'
    )

    # 监控输出
    def monitor_output():
        for line in process.stdout:
            if line.strip():
                print(f"[MONITOR] {line.strip()}")

    output_thread = threading.Thread(target=monitor_output)
    output_thread.daemon = False
    output_thread.start()

    print("✓ RTMP监控已启动")

    return process


def show_banner():
    """显示欢迎横幅"""
    print("""
╔════════════════════════════════════════════════════════════╗
║                                                              ║
║          PS5-Bilibili-Danmaku 一键启动                       ║
║                                                              ║
║  组件：                                                      ║
║  • RTMP流媒体服务器 (基于nginx-rtmp-win32)                   ║
║  • 弹幕转发系统 (danmaku_forward.py)                        ║
║  • RTMP推流监控 (monitor_rtmp.py)                            ║
║                                                              ║
╚════════════════════════════════════════════════════════════╝
""")


def main():
    """主函数"""
    show_banner()

    processes = []

    try:
        # 询问用户要启动哪些服务
        print("\n请选择要启动的服务:")
        print("  1. 仅启动RTMP服务器")
        print("  2. 仅启动弹幕转发系统")
        print("  3. 仅启动RTMP监控")
        print("  4. 启动全部服务（推荐）")
        print("  0. 退出")

        choice = input("\n请输入选项 (默认4): ").strip() or "4"

        if choice == '1':
            p = start_rtmp_server()
            if p:
                processes.append(p)

        elif choice == '2':
            p = start_danmaku_system()
            if p:
                processes.append(p)

        elif choice == '3':
            p = start_rtmp_monitor()
            if p:
                processes.append(p)

        elif choice == '4':
            # 启动全部服务
            p1 = start_rtmp_server()
            time.sleep(3)

            p2 = start_danmaku_system()
            time.sleep(2)

            p3 = start_rtmp_monitor()
            time.sleep(1)

            if p1:
                processes.append(p1)
            if p2:
                processes.append(p2)
            if p3:
                processes.append(p3)

        elif choice == '0':
            print("\n再见！")
            return

        else:
            print("\n无效选项")
            return

        # 显示运行状态
        if processes:
            print("\n" + "="*60)
            print("所有服务已启动！")
            print("="*60)
            print("\n访问地址:")
            print("  • Web控制台: http://127.0.0.1:5000")
            print("  • RTMP统计: http://127.0.0.1:8080/stat")
            print("\n按 Ctrl+C 停止所有服务")

            # 等待用户中断
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                print("\n\n正在停止所有服务...")

                # 停止所有进程
                for p in processes:
                    try:
                        p.terminate()
                    except:
                        pass

                # 等待进程结束
                time.sleep(2)

                # 强制结束
                for p in processes:
                    try:
                        p.kill()
                    except:
                        pass

                print("✓ 所有服务已停止")

    except Exception as e:
        print(f"\n错误: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
