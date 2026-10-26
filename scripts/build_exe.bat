@echo off
REM ============================================================
REM  PS5 LiveBridge - build Windows distribution
REM  Output: dist\PS5LiveBridge\PS5LiveBridge.exe
REM  Keep this file ASCII-only: cmd.exe garbles UTF-8 in .bat
REM ============================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0.."

echo [1/5] Checking Python...
python --version || goto :fail

echo [2/5] Installing dependencies...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt || goto :fail
python -m pip install "pydivert>=3.0" || goto :fail
python -m pip install pyinstaller || goto :fail

echo [3/5] Detecting optional pywebview...
set PV=
python -c "import webview" 2>nul && set PV=--collect-all webview
if defined PV (echo     pywebview found - native window enabled) else (echo     pywebview not installed - will use Edge app mode)

echo [4/5] Cleaning old output...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo [5/5] Building...
python -m PyInstaller --noconfirm --clean --noconsole ^
  --name PS5LiveBridge ^
  --add-data "web;web" ^
  --add-data "nginx-rtmp;nginx-rtmp" ^
  --add-data "README.md;." ^
  --add-data "LICENSE;." ^
  --hidden-import brotli ^
  --hidden-import qrcode ^
  --hidden-import PIL._imaging ^
  --hidden-import aiohttp ^
  --hidden-import flask ^
  --collect-all pydivert ^
  %PV% ^
  main.py || goto :fail

echo.
echo Copying extra files...
if exist tools\ffmpeg.exe (
  if not exist "dist\PS5LiveBridge\tools" mkdir "dist\PS5LiveBridge\tools"
  copy /y tools\ffmpeg.exe "dist\PS5LiveBridge\tools\" >nul
  echo     bundled ffmpeg.exe
) else (
  echo     WARNING: tools\ffmpeg.exe not found.
  echo     End users must install ffmpeg and add it to PATH,
  echo     otherwise the relay (PS5 -> Bilibili) will not start.
)

REM Output names must stay ASCII - no Chinese filenames anywhere in this file,
REM not even in comments. cmd.exe garbles multi-byte text and then parses the
REM rest of the line as commands; that silently kills the tail of this script.
REM (Symptoms seen: "'dge' is not recognized", "|| was unexpected at this time".)
REM If a Chinese README/launcher name is wanted, rename it afterwards with
REM PowerShell - this file itself only deals in ASCII names.
copy /y README.md "dist\PS5LiveBridge\README.md" >nul 2>nul
copy /y LICENSE "dist\PS5LiveBridge\LICENSE.txt" >nul 2>nul
if exist config.example.json copy /y config.example.json "dist\PS5LiveBridge\" >nul

echo.
echo ============================================================
echo  DONE.  Run: dist\PS5LiveBridge\PS5LiveBridge.exe
echo  Remember: UAC prompt must be accepted (port 53 + firewall).
echo ============================================================
goto :eof

:fail
echo.
echo BUILD FAILED - see the error above.
exit /b 1
