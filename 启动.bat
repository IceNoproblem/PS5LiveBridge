@echo off
REM ============================================================
REM  PS5 LiveBridge - run from source (Windows)
REM  Requires: python + pip install -r requirements.txt
REM  Keep ASCII-only in this file.
REM ============================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found in PATH.
  echo Install Python 3.9+ and tick "Add python.exe to PATH".
  pause
  exit /b 1
)

echo Starting PS5 LiveBridge...
python main.py %*
if errorlevel 1 (
  echo.
  echo Exited with error. Check logs\run_*.log
  pause
)
