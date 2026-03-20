@echo off
chcp 65001 >nul
cd /d C:\ps5-bilibili-danmaku

echo 正在停止旧进程...
taskkill /F /IM python.exe 2>nul
timeout /t 2 /nobreak >nul

echo 正在启动程序...
start /B python danmaku_forward.py >nul 2>&1

echo 等待程序启动...
timeout /t 5 /nobreak >nul

echo.
echo 正在测试接口...
python -c "import requests; r = requests.get('http://127.0.0.1:5000/status', timeout=5); print(f'状态码: {r.status_code}'); print(f'响应: {r.json() if r.status_code == 200 else \"失败\"}')"

echo.
echo 正在测试主页...
python -c "import requests; r = requests.get('http://127.0.0.1:5000/', timeout=5); print(f'状态码: {r.status_code}'); print(f'内容长度: {len(r.text)} 字节'); print(f'包含PS5标题: {\"PS5 B站弹幕转发\" in r.text}')"

echo.
echo ========================================
echo 测试完成
echo 请访问: http://127.0.0.1:5000
echo ========================================
pause
