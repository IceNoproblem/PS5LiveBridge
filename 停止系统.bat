@echo off
chcp 65001 > nul

echo ============================================================
echo   停止 阿冰没问题（Icenoproblem）PS5 哔哩哔哩 直播系统
echo ============================================================
echo.

echo 正在停止所有相关进程...

taskkill /F /IM python.exe /T 2>nul
taskkill /F /IM launcher.exe /T 2>nul

timeout /t 1 /nobreak > nul

echo.
echo 所有服务已停止。
echo.
pause
