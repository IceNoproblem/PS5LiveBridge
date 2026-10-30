@echo off
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

if exist tools\rtmf.exe (
  if not exist "dist\PS5LiveBridge\tools" mkdir "dist\PS5LiveBridge\tools"
  copy /y tools\rtmf.exe "dist\PS5LiveBridge\tools\" >nul
  echo     bundled rtmf.exe
) else (
  echo     note: tools\rtmf.exe not found - rtmp-forwarder impl unavailable
)

copy /y README.md "dist\PS5LiveBridge\README.md" >nul 2>nul
copy /y LICENSE "dist\PS5LiveBridge\LICENSE.txt" >nul 2>nul
if exist config.example.json copy /y config.example.json "dist\PS5LiveBridge\" >nul

echo.
echo [6/6] Verifying bundle...
if not exist "dist\PS5LiveBridge\_internal\python312.dll" goto :fail
if not exist "dist\PS5LiveBridge\_internal\base_library.zip" goto :fail
if not exist "dist\PS5LiveBridge\PS5LiveBridge.exe" goto :fail
echo     bundle layout OK

set PS5LB_NO_POPUP=1
start /wait "" "dist\PS5LiveBridge\PS5LiveBridge.exe" --version
if errorlevel 1 goto :fail
echo     exe starts OK

echo.
echo [last] generating manifest for incremental update...
python scripts\gen_manifest.py dist\PS5LiveBridge
if errorlevel 1 echo     WARN: manifest failed - update will fall back to full package
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
