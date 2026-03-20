@echo off
chcp 65001 > nul
title 阿冰没问题（Icenoproblem）PS5 哔哩哔哩 直播系统 V3.0 - 依赖安装

echo ================================================
echo   阿冰没问题（Icenoproblem）PS5 哔哩哔哩 直播系统 V3.0 - 依赖安装
echo ================================================
echo.

REM 查找Python
set PYTHON_EXE=
if exist "C:\Program Files\Python312\python.exe" set PYTHON_EXE=C:\Program Files\Python312\python.exe
if exist "C:\Program Files\Python311\python.exe" set PYTHON_EXE=C:\Program Files\Python311\python.exe
if exist "C:\Program Files\Python310\python.exe" set PYTHON_EXE=C:\Program Files\Python310\python.exe
if exist "C:\Python312\python.exe" set PYTHON_EXE=C:\Python312\python.exe
if exist "C:\Python311\python.exe" set PYTHON_EXE=C:\Python311\python.exe
if exist "C:\Python310\python.exe" set PYTHON_EXE=C:\Python310\python.exe

REM 尝试PATH中的python
if "%PYTHON_EXE%"=="" (
    where python >nul 2>&1
    if %errorlevel%==0 set PYTHON_EXE=python
)

if "%PYTHON_EXE%"=="" (
    echo [错误] 未找到Python，请先安装 Python 3.10 或更高版本
    echo 下载地址: https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [信息] 使用Python: %PYTHON_EXE%
"%PYTHON_EXE%" --version
echo.

echo [信息] 正在安装依赖包...
"%PYTHON_EXE%" -m pip install flask requests aiohttp -i https://pypi.tuna.tsinghua.edu.cn/simple --upgrade

if %errorlevel%==0 (
    echo.
    echo [成功] 所有依赖安装完成！
    echo.
    echo 现在可以运行 start.bat 启动程序
) else (
    echo.
    echo [警告] 部分依赖安装失败，请检查网络或手动运行：
    echo   pip install flask requests aiohttp
)

echo.
pause
