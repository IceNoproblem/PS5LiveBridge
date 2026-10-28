@echo off
REM ============================================================
REM  PS5 LiveBridge - build Windows distribution
REM  Output: dist\PS5LiveBridge\PS5LiveBridge.exe
REM  Keep this file ASCII-only: cmd.exe garbles UTF-8 in .bat
REM ============================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0.."

echo [1/6] Checking Python...
python --version || goto :fail

echo [2/6] Installing dependencies...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt || goto :fail
python -m pip install "pydivert>=3.0" || goto :fail
python -m pip install pyinstaller || goto :fail

echo [3/6] Detecting optional pywebview...
set PV=
python -c "import webview" 2>nul && set PV=--collect-all webview
if defined PV (echo     pywebview found - native window enabled) else (echo     pywebview not installed - will use Edge app mode)

echo [4/6] Cleaning old output...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo [5/6] Building...
python -m PyInstaller --noconfirm --clean --noconsole ^
  --name PS5LiveBridge ^
  --icon "assets\icon.ico" ^
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
  --collect-data certifi ^
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
  echo     otherwise the relay from PS5 to Bilibili will not start.
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
echo [6/6] Verifying bundle...
if not exist "dist\PS5LiveBridge\_internal\python312.dll" goto :fail
if not exist "dist\PS5LiveBridge\_internal\base_library.zip" goto :fail
if not exist "dist\PS5LiveBridge\PS5LiveBridge.exe" goto :fail
echo     bundle layout OK

REM Smoke test: run the exe headless once. --noconsole programs are GUI
REM subsystem, so plain invocation returns immediately - use start /wait.
REM PS5LB_NO_POPUP keeps the error dialog away during automated builds.
set PS5LB_NO_POPUP=1
start /wait "" "dist\PS5LiveBridge\PS5LiveBridge.exe" --version
if errorlevel 1 goto :fail
echo     exe starts OK

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
