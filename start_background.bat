@echo off
chcp 65001 > nul
cd /d "%~dp0"

echo ================================================
echo   阿冰没问题（Icenoproblem）PS5 哔哩哔哩 直播系统 V3.0 - 后台启动
echo ================================================
echo.
echo [信息] 正在启动程序...
echo [信息] 请访问 http://127.0.0.1:5000 查看Web控制台
echo.
echo [信息] 按任意键关闭此窗口（程序继续在后台运行）
echo ================================================
echo.

start /B python danmaku_forward.py

pause
