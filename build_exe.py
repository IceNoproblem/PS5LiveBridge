#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
打包脚本：将 launcher.py 打包成 EXE
"""

import os
import sys
import subprocess
import shutil

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
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)

def check_pyinstaller():
    """检查 PyInstaller 是否已安装"""
    try:
        import PyInstaller
        print(f"✓ PyInstaller 已安装 (版本: {PyInstaller.__version__})")
        return True
    except ImportError:
        print("✗ PyInstaller 未安装")
        return False

def install_pyinstaller():
    """安装 PyInstaller"""
    print("\n正在安装 PyInstaller...")
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "pyinstaller"],
            check=True
        )
        print("✓ PyInstaller 安装成功")
        return True
    except subprocess.CalledProcessError as e:
        print(f"✗ PyInstaller 安装失败: {e}")
        return False

def build_exe():
    """打包 EXE"""
    print("\n开始打包...")
    print("=" * 60)

    # 检查图标文件
    icon_path = os.path.join(BASE_DIR, "avatar.ico")
    if not os.path.exists(icon_path):
        print(f"⚠ 警告: 未找到图标文件 {icon_path}")
        print("  将使用默认图标")
    else:
        print(f"✓ 使用图标: {icon_path}")

    # 删除旧的 build 和 dist 目录
    if os.path.exists("build"):
        print("清理旧的 build 目录...")
        shutil.rmtree("build")
    if os.path.exists("dist"):
        print("清理旧的 dist 目录...")
        shutil.rmtree("dist")

    # 使用 PyInstaller 打包
    try:
        result = subprocess.run(
            [sys.executable, "-m", "PyInstaller", "launcher.spec"],
            check=True,
            capture_output=True,
            text=True
        )

        print(result.stdout)
        if result.stderr:
            print(result.stderr)

        print("\n" + "=" * 60)
        print("✓ 打包成功！")

        # 检查生成的文件
        dist_dir = os.path.join(BASE_DIR, "dist")
        if os.path.exists(dist_dir):
            exe_files = [f for f in os.listdir(dist_dir) if f.endswith(".exe")]
            if exe_files:
                print(f"\n生成的 EXE 文件:")
                for exe in exe_files:
                    exe_path = os.path.join(dist_dir, exe)
                    size = os.path.getsize(exe_path) / (1024 * 1024)
                    print(f"  • {exe} ({size:.2f} MB)")
                print(f"\n文件位置: {dist_dir}")

        return True

    except subprocess.CalledProcessError as e:
        print(f"\n✗ 打包失败: {e}")
        print(e.stdout)
        print(e.stderr)
        return False

def main():
    print("=" * 60)
    print("  阿冰没问题（Icenoproblem）PS5 哔哩哔哩 直播系统 V3.0 - EXE 打包工具")
    print("  版本: V3.0")
    print("=" * 60)

    # 检查 PyInstaller
    if not check_pyinstaller():
        print("\n需要安装 PyInstaller 才能打包 EXE")
        choice = input("是否现在安装？(y/n): ").strip().lower()
        if choice == 'y':
            if not install_pyinstaller():
                print("\n无法继续打包")
                return
        else:
            print("\n打包已取消")
            return

    # 打包
    if build_exe():
        print("\n" + "=" * 60)
        print("打包完成！")
        print("=" * 60)
        print("\n使用说明：")
        print("1. 将生成的 EXE 文件复制到项目目录 (C:\\PS5-Danmaku-System)")
        print("2. 双击运行 EXE 文件")
        print("3. 在 GUI 界面中点击'一键安装'安装依赖")
        print("4. 点击'一键启动'启动所有服务")
        print("5. 访问 http://127.0.0.1:5000 配置和管理系统")
        print("\n备选方案：")
        print("  如果EXE无法正常启动Web界面，")
        print("  请双击运行'启动系统.bat'使用命令行启动")
        print("\n详细说明请查看：使用说明.md")
    else:
        print("\n打包失败，请检查错误信息")

if __name__ == "__main__":
    main()
