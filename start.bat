@echo off
chcp 65001 > nul
title 阿冰没问题（Icenoproblem）PS5 哔哩哔哩 直播系统 V3.0

REM ================================================
REM   阿冰没问题（Icenoproblem）PS5 哔哩哔哩 直播系统 V3.0 - 启动脚本
REM   Windows 10/11 原生运行，无需Docker
REM ================================================

REM 查找Python（优先已知安装路径）
set PYTHON_EXE=
if exist "C:\Program Files\Python312\python.exe" set PYTHON_EXE=C:\Program Files\Python312\python.exe
if exist "C:\Program Files\Python311\python.exe" set PYTHON_EXE=C:\Program Files\Python311\python.exe
if exist "C:\Program Files\Python310\python.exe" set PYTHON_EXE=C:\Program Files\Python310\python.exe
if exist "C:\Python312\python.exe" set PYTHON_EXE=C:\Python312\python.exe
if exist "C:\Python311\python.exe" set PYTHON_EXE=C:\Python311\python.exe
if exist "C:\Python310\python.exe" set PYTHON_EXE=C:\Python310\python.exe

if "%PYTHON_EXE%"=="" (
    where python >nul 2>&1
    if %errorlevel%==0 set PYTHON_EXE=python
)

if "%PYTHON_EXE%"=="" (
    echo [错误] 未找到Python！请先运行 install.bat
    echo 或访问 https://www.python.org/downloads/ 安装Python 3.10+
    pause
    exit /b 1
)

REM 检查主程序文件
set SCRIPT_DIR=%~dp0
if not exist "%SCRIPT_DIR%danmaku_forward.py" (
    echo [错误] 找不到 danmaku_forward.py
    pause
    exit /b 1
)

echo ================================================
echo   阿冰没问题（Icenoproblem）PS5 哔哩哔哩 直播系统 V3.0
echo   Windows 原生版 - WebSocket 实时接收
echo ================================================
echo.
echo [信息] Python: %PYTHON_EXE%
echo [信息] 程序目录: %SCRIPT_DIR%
echo.
echo [信息] 正在启动，请稍候...
echo [信息] 启动后请用浏览器访问 http://127.0.0.1:5000 进行配置
echo.
echo [提示] 按 Ctrl+C 停止程序
echo ================================================
echo.

cd /d "%SCRIPT_DIR%"
"%PYTHON_EXE%" danmaku_forward.py

if %errorlevel% neq 0 (
    echo.
    echo [错误] 程序异常退出，错误码: %errorlevel%
    echo [提示] 如果提示缺少模块，请先运行 install.bat
    echo.
    pause
)
