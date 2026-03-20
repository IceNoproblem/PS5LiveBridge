@echo off
chcp 65001 > nul
cd /d "%~dp0"

echo ============================================================
echo   阿冰没问题（Icenoproblem）PS5 哔哩哔哩 直播系统 V3.0
echo ============================================================
echo.

echo [1/3] 启动RTMP服务器...
start /B "" pythonw rtmp_server.py > nul 2>&1
timeout /t 2 /nobreak > nul

echo [2/3] 启动弹幕转发系统（包含Web界面）...
start /B "" pythonw danmaku_forward.py > nul 2>&1
timeout /t 2 /nobreak > nul

echo [3/3] 启动RTMP监控...
start /B "" pythonw monitor_rtmp.py > nul 2>&1
timeout /t 2 /nobreak > nul

echo.
echo ============================================================
echo   服务启动完成！
echo ============================================================
echo.
echo Web管理界面: http://127.0.0.1:5000
echo RTMP统计:     http://127.0.0.1:8080/stat
echo.
echo 按任意键打开Web界面...
pause > nul

start http://127.0.0.1:5000

echo.
echo Web界面已打开。
echo 日志文件：
echo   - rtmp.log (RTMP服务器日志)
echo   - danmaku.log (弹幕转发日志)
echo   - monitor.log (RTMP监控日志)
echo.
echo 按Ctrl+C可以停止所有服务
echo.

:KEEP_RUNNING
timeout /t 60 /nobreak > nul
goto KEEP_RUNNING
