#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
阿冰没问题（Icenoproblem）PS5 哔哩哔哩 直播系统 - 图形化启动器
提供一键安装、部署、启动功能
"""

import os
import sys
import subprocess
import threading
import time
from tkinter import *
from tkinter import ttk, scrolledtext, messagebox
import queue

# Windows 特定导入
if sys.platform == 'win32':
    import ctypes

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

# 获取脚本所在目录
if getattr(sys, 'frozen', False):
    # 如果是打包的EXE，使用EXE文件所在目录
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    # 开发环境，使用脚本所在目录
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

os.chdir(BASE_DIR)


class PS5LauncherGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("阿冰没问题（Icenoproblem）PS5 哔哩哔哩 直播系统 V3.0")
        self.root.geometry("900x700")
        self.root.resizable(True, True)
        
        # 窗口关闭时停止所有进程
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        
        # 进程列表
        self.processes = []
        
        # 日志队列
        self.log_queue = queue.Queue()
        
        # 创建界面
        self.create_widgets()
        
        # 启动日志更新
        self.root.after(100, self.update_log)
        
        # 自动检查依赖
        self.root.after(500, self.check_dependencies)
    
    def create_widgets(self):
        """创建界面组件"""
        # 顶部标题
        title_frame = Frame(self.root, bg="#2c3e50", pady=10)
        title_frame.pack(fill=X)
        
        title_label = Label(
            title_frame,
            text="阿冰没问题（Icenoproblem）PS5 哔哩哔哩 直播系统 V3.0",
            font=("Microsoft YaHei UI", 16, "bold"),
            bg="#2c3e50",
            fg="#ffffff"
        )
        title_label.pack()
        
        subtitle_label = Label(
            title_frame,
            text="将 PS5 Twitch 推流的弹幕转发到哔哩哔哩直播间",
            font=("Microsoft YaHei UI", 9),
            bg="#2c3e50",
            fg="#bdc3c7"
        )
        subtitle_label.pack()
        
        # 按钮区域
        button_frame = Frame(self.root, pady=15)
        button_frame.pack(fill=X)
        
        # 按钮配置
        button_style = {
            "font": ("Microsoft YaHei UI", 10),
            "width": 15,
            "height": 2,
            "pady": 5
        }
        
        # 一键安装按钮
        self.install_btn = Button(
            button_frame,
            text="📦 一键安装\n(下载依赖)",
            command=self.install_dependencies,
            bg="#3498db",
            fg="white",
            **button_style
        )
        self.install_btn.pack(side=LEFT, padx=10)
        
        # 一键启动按钮
        self.start_btn = Button(
            button_frame,
            text="▶️ 一键启动\n(全部服务)",
            command=self.start_all,
            bg="#2ecc71",
            fg="white",
            **button_style
        )
        self.start_btn.pack(side=LEFT, padx=10)
        
        # 停止按钮
        self.stop_btn = Button(
            button_frame,
            text="⏹️ 停止所有\n服务",
            command=self.stop_all,
            bg="#e74c3c",
            fg="white",
            **button_style
        )
        self.stop_btn.pack(side=LEFT, padx=10)
        
        # 访问Web按钮
        self.web_btn = Button(
            button_frame,
            text="🌐 打开Web\n管理界面",
            command=self.open_web,
            bg="#9b59b6",
            fg="white",
            **button_style
        )
        self.web_btn.pack(side=LEFT, padx=10)
        
        # 状态栏
        status_frame = Frame(self.root, pady=5)
        status_frame.pack(fill=X)
        
        self.status_label = Label(
            status_frame,
            text="状态: 等待操作",
            font=("Microsoft YaHei UI", 9),
            fg="#7f8c8d"
        )
        self.status_label.pack()
        
        # 服务状态显示
        services_frame = Frame(self.root, pady=5)
        services_frame.pack(fill=X)
        
        self.rtmp_status = Label(services_frame, text="RTMP: 未运行", font=("Microsoft YaHei UI", 9), fg="#e74c3c")
        self.rtmp_status.pack(side=LEFT, padx=20)
        
        self.danmaku_status = Label(services_frame, text="弹幕: 未运行", font=("Microsoft YaHei UI", 9), fg="#e74c3c")
        self.danmaku_status.pack(side=LEFT, padx=20)
        
        self.monitor_status = Label(services_frame, text="监控: 未运行", font=("Microsoft YaHei UI", 9), fg="#e74c3c")
        self.monitor_status.pack(side=LEFT, padx=20)
        
        # 日志显示区域
        log_frame = Frame(self.root, padx=10, pady=5)
        log_frame.pack(fill=BOTH, expand=True)
        
        log_title = Label(
            log_frame,
            text="运行日志",
            font=("Microsoft YaHei UI", 10, "bold"),
            anchor=W
        )
        log_title.pack(fill=X)
        
        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            font=("Consolas", 9),
            wrap=WORD,
            state=DISABLED
        )
        self.log_text.pack(fill=BOTH, expand=True)
        
        # 底部信息
        footer_frame = Frame(self.root, bg="#ecf0f1", pady=10)
        footer_frame.pack(fill=X, side=BOTTOM)
        
        footer_label = Label(
            footer_frame,
            text="Web控制台: http://127.0.0.1:5000  |  RTMP统计: http://127.0.0.1:8080/stat",
            font=("Microsoft YaHei UI", 9),
            bg="#ecf0f1",
            fg="#7f8c8d"
        )
        footer_label.pack()
    
    def log(self, message, level="INFO"):
        """添加日志"""
        timestamp = time.strftime("%H:%M:%S")
        color_map = {
            "INFO": "#2ecc71",
            "WARNING": "#f39c12",
            "ERROR": "#e74c3c",
            "SUCCESS": "#3498db"
        }
        color = color_map.get(level, "#2c3e50")
        
        self.log_queue.put((timestamp, level, message, color))
        self.update_status(message)
    
    def update_log(self):
        """更新日志显示"""
        try:
            while True:
                timestamp, level, message, color = self.log_queue.get_nowait()
                self.log_text.config(state=NORMAL)
                self.log_text.insert(END, f"[{timestamp}] [{level}] {message}\n", color)
                self.log_text.tag_config(color, foreground=color)
                self.log_text.see(END)
                self.log_text.config(state=DISABLED)
        except queue.Empty:
            pass
        
        self.root.after(100, self.update_log)
    
    def update_status(self, message):
        """更新状态栏"""
        self.status_label.config(text=f"状态: {message}")
    
    def check_dependencies(self):
        """检查依赖是否已安装"""
        try:
            import flask
            import requests
            import aiohttp
            self.log("依赖检查完成：所有依赖已安装 ✓", "SUCCESS")
            self.install_btn.config(text="📦 依赖已安装", state=DISABLED)
        except ImportError as e:
            self.log(f"依赖检查：{e}", "WARNING")
            self.log("请点击'一键安装'按钮安装依赖", "WARNING")
    
    def install_dependencies(self):
        """安装依赖"""
        self.log("开始安装依赖...", "INFO")
        self.install_btn.config(state=DISABLED, text="📦 安装中...")
        
        def install_thread():
            try:
                # 检查 requirements.txt 是否存在
                if not os.path.exists(os.path.join(BASE_DIR, "requirements.txt")):
                    self.log("错误: 未找到 requirements.txt", "ERROR")
                    return
                
                # 安装依赖
                process = subprocess.Popen(
                    [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
                    cwd=BASE_DIR,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding='utf-8',
                    errors='replace'
                )
                
                # 实时输出安装日志
                for line in process.stdout:
                    line = line.strip()
                    if line:
                        self.log(line, "INFO")
                
                process.wait()
                
                if process.returncode == 0:
                    self.log("依赖安装完成 ✓", "SUCCESS")
                    self.root.after(0, lambda: self.install_btn.config(text="📦 依赖已安装", state=DISABLED))
                else:
                    self.log(f"依赖安装失败，错误代码: {process.returncode}", "ERROR")
                    self.root.after(0, lambda: self.install_btn.config(state=NORMAL, text="📦 重新安装"))
            
            except Exception as e:
                self.log(f"安装依赖时出错: {e}", "ERROR")
                self.root.after(0, lambda: self.install_btn.config(state=NORMAL, text="📦 重新安装"))
        
        threading.Thread(target=install_thread, daemon=True).start()
    
    def start_all(self):
        """启动所有服务"""
        self.log("启动所有服务...", "INFO")
        self.start_btn.config(state=DISABLED, text="▶️ 启动中...")
        
        # 启动 RTMP 服务器
        self.start_rtmp_server()
        time.sleep(2)
        
        # 启动弹幕转发系统
        self.start_danmaku_system()
        time.sleep(2)
        
        # 启动 RTMP 监控
        self.start_rtmp_monitor()
        
        self.log("所有服务启动完成 ✓", "SUCCESS")
        self.start_btn.config(state=DISABLED, text="▶️ 运行中")
    
    def start_rtmp_server(self):
        """启动 RTMP 服务器"""
        try:
            self.log("启动 RTMP 服务器...", "INFO")

            # 获取Python解释器路径
            python_exe = self.get_python_executable()
            if not python_exe:
                return

            # 使用全局的 BASE_DIR
            script_path = os.path.join(BASE_DIR, "rtmp_server.py")

            # 检查脚本文件是否存在
            if not os.path.exists(script_path):
                self.log(f"错误: 未找到脚本文件 {script_path}", "ERROR")
                self.log(f"工作目录: {BASE_DIR}", "INFO")
                return

            # 创建启动信息，隐藏窗口
            startupinfo = None
            if sys.platform == 'win32':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE

            process = subprocess.Popen(
                [python_exe, script_path],
                cwd=BASE_DIR,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
                encoding='utf-8',
                errors='replace',
                startupinfo=startupinfo
            )
            self.processes.append(("RTMP", process))

            # 发送启动命令
            time.sleep(3)
            try:
                process.stdin.write("1\n")
                process.stdin.flush()
            except:
                pass

            # 启动输出监控
            self.monitor_output(process, "RTMP")

            self.rtmp_status.config(text="RTMP: 运行中", fg="#2ecc71")
            self.log("RTMP 服务器已启动 ✓", "SUCCESS")

        except Exception as e:
            self.log(f"启动 RTMP 服务器失败: {e}", "ERROR")
    
    def get_python_executable(self):
        """获取正确的Python解释器路径"""
        # 检查是否是打包的EXE
        if getattr(sys, 'frozen', False):
            # 如果是打包的EXE，使用系统的Python
            python_exe = "python"
            # 验证python命令是否可用
            try:
                result = subprocess.run([python_exe, "--version"],
                                      capture_output=True,
                                      text=True,
                                      timeout=5)
                if result.returncode == 0:
                    self.log(f"使用系统Python: {result.stdout.strip()}", "INFO")
                    return python_exe
            except:
                pass

            # 尝试其他常见的Python路径
            possible_paths = [
                r"C:\Python312\python.exe",
                r"C:\Python311\python.exe",
                r"C:\Python310\python.exe",
                r"C:\Python39\python.exe",
                r"C:\Program Files\Python312\python.exe",
                r"C:\Program Files\Python311\python.exe",
            ]

            for path in possible_paths:
                if os.path.exists(path):
                    self.log(f"找到Python: {path}", "INFO")
                    return path

            # 都找不到，报错
            self.log("错误: 未找到Python解释器，请先安装Python", "ERROR")
            return None
        else:
            # 开发环境，使用当前的Python
            return sys.executable

    def start_danmaku_system(self):
        """启动弹幕转发系统"""
        try:
            self.log("启动弹幕转发系统...", "INFO")

            # 获取Python解释器路径
            python_exe = self.get_python_executable()
            if not python_exe:
                return

            # 使用全局的 BASE_DIR
            script_path = os.path.join(BASE_DIR, "danmaku_forward.py")

            # 检查脚本文件是否存在
            if not os.path.exists(script_path):
                self.log(f"错误: 未找到脚本文件 {script_path}", "ERROR")
                self.log(f"工作目录: {BASE_DIR}", "INFO")
                return

            # 创建启动信息，隐藏窗口
            startupinfo = None
            if sys.platform == 'win32':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE

            process = subprocess.Popen(
                [python_exe, script_path],
                cwd=BASE_DIR,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
                encoding='utf-8',
                errors='replace',
                startupinfo=startupinfo
            )
            self.processes.append(("弹幕", process))

            # 启动输出监控
            self.monitor_output(process, "弹幕")

            self.danmaku_status.config(text="弹幕: 运行中", fg="#2ecc71")
            self.log("弹幕转发系统已启动 ✓", "SUCCESS")

        except Exception as e:
            self.log(f"启动弹幕转发系统失败: {e}", "ERROR")
    
    def start_rtmp_monitor(self):
        """启动 RTMP 监控"""
        try:
            self.log("启动 RTMP 监控...", "INFO")

            # 获取Python解释器路径
            python_exe = self.get_python_executable()
            if not python_exe:
                return

            # 使用全局的 BASE_DIR
            script_path = os.path.join(BASE_DIR, "monitor_rtmp.py")

            # 检查脚本文件是否存在
            if not os.path.exists(script_path):
                self.log(f"错误: 未找到脚本文件 {script_path}", "ERROR")
                self.log(f"工作目录: {BASE_DIR}", "INFO")
                return

            # 创建启动信息，隐藏窗口
            startupinfo = None
            if sys.platform == 'win32':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE

            process = subprocess.Popen(
                [python_exe, script_path],
                cwd=BASE_DIR,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
                encoding='utf-8',
                errors='replace',
                startupinfo=startupinfo
            )
            self.processes.append(("监控", process))

            # 启动输出监控
            self.monitor_output(process, "监控")

            self.monitor_status.config(text="监控: 运行中", fg="#2ecc71")
            self.log("RTMP 监控已启动 ✓", "SUCCESS")

        except Exception as e:
            self.log(f"启动 RTMP 监控失败: {e}", "ERROR")
    
    def monitor_output(self, process, name):
        """监控进程输出"""
        def output_thread():
            try:
                for line in process.stdout:
                    if line.strip():
                        self.log(f"[{name}] {line.strip()}", "INFO")
            except:
                pass
        
        threading.Thread(target=output_thread, daemon=True).start()
    
    def stop_all(self):
        """停止所有服务"""
        self.log("停止所有服务...", "INFO")
        
        for name, process in self.processes:
            try:
                process.terminate()
                self.log(f"已停止 {name} 服务", "INFO")
            except:
                pass
        
        # 等待进程结束
        time.sleep(2)
        
        # 强制结束
        for name, process in self.processes:
            try:
                process.kill()
            except:
                pass
        
        self.processes.clear()
        
        self.rtmp_status.config(text="RTMP: 未运行", fg="#e74c3c")
        self.danmaku_status.config(text="弹幕: 未运行", fg="#e74c3c")
        self.monitor_status.config(text="监控: 未运行", fg="#e74c3c")
        
        self.start_btn.config(state=NORMAL, text="▶️ 一键启动\n(全部服务)")
        self.log("所有服务已停止", "SUCCESS")
    
    def open_web(self):
        """打开 Web 管理界面"""
        import webbrowser
        webbrowser.open("http://127.0.0.1:5000")
        self.log("已打开 Web 管理界面", "INFO")
    
    def on_closing(self):
        """窗口关闭事件"""
        if self.processes:
            if messagebox.askyesno("确认退出", "确定要停止所有服务并退出吗？"):
                self.stop_all()
                self.root.destroy()
        else:
            self.root.destroy()


def main():
    root = Tk()
    app = PS5LauncherGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
