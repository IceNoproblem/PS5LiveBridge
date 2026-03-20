@echo off
chcp 65001 >nul
echo ╔════════════════════════════════════════════════════════════╗
echo ║                                                            ║
echo ║        PS5-Bilibili-Danmaku 一键启动                      ║
echo ║                                                            ║
echo ╚════════════════════════════════════════════════════════════╝
echo.

REM 检查Python是否安装
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo 错误: 未检测到Python
    echo 请先安装Python 3.8或更高版本
    echo 下载地址: https://www.python.org/downloads/
    pause
    exit /b 1
)

REM 检查是否首次运行
if not exist "nginx-rtmp\nginx.exe" (
    echo 检测到首次运行，开始自动安装...
    echo.
    python install.py
    if %errorlevel% neq 0 (
        echo 安装失败，请手动安装
        pause
        exit /b 1
    )
    echo.
)

REM 启动服务
echo 正在启动服务...
echo.
python start_all.py

pause
