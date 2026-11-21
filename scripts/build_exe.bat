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

rem PyInstaller's analysis imports the packages, which drops __pycache__ into the
rem source tree. --add-data "web;web" then copies those .pyc into the bundle, and
rem their embedded co_filename gives away the build machine path. Clear them first.
for /d /r . %%d in (__pycache__) do @if exist "%%d" rd /s /q "%%d"

rem nginx leaves a nested copy plus runtime logs when it runs from this folder.
rem those carry real stream keys and client IPs - never ship them.
if exist "nginx-rtmp\nginx-rtmp" rmdir /s /q "nginx-rtmp\nginx-rtmp"
del /q "nginx-rtmp\logs\*.log" 2>nul
del /q "nginx-rtmp\logs\*.pid" 2>nul

rem three binaries are gitignored: a fresh export has none of them.
rem shipping without nginx-rtmp\nginx.exe breaks the nginx receive option.
if not exist "nginx-rtmp\nginx.exe" goto :nobin
if not exist "tools\ffmpeg.exe" goto :nobin
if not exist "tools\rtmf.exe" goto :nobin

echo [5/6] Building...
python -m PyInstaller --noconfirm --clean --noconsole ^
  --name PS5LiveBridge ^
  --icon "assets\icon.ico" ^
  --add-data "web;web" ^
  --add-data "assets\icon.png;assets" ^
  --add-data "nginx-rtmp;nginx-rtmp" ^
  --add-data "README.md;." ^
  --add-data "LICENSE;." ^
  --hidden-import brotli ^
  --hidden-import qrcode ^
  --hidden-import PIL._imaging ^
  --hidden-import aiohttp ^
  --hidden-import flask ^
  --hidden-import pystray ^
  --hidden-import pystray._win32 ^
  --hidden-import PIL.ImageDraw ^
  --hidden-import PIL.ImageWin ^
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

python scripts\copy_usage.py "dist\PS5LiveBridge" || goto :fail
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

rem --version runs before "from ui import window", so it cannot catch a missing
rem ui package. Check the frozen archive directly instead.
python -m PyInstaller.utils.cliutils.archive_viewer -l -r "dist\PS5LiveBridge\PS5LiveBridge.exe" 2>nul | findstr /i /c:"'ui'" >nul
if errorlevel 1 goto :fail
echo     ui module bundled OK

echo.
echo [last] generating manifest for incremental update...
rem The --version self-check above runs inside dist and leaves runtime debris behind,
rem and --add-data copies web/__pycache__ into the bundle. Neither may ship.
python scripts\clean_dist.py dist\PS5LiveBridge
if errorlevel 1 echo     WARN: clean_dist failed - check the bundle by hand
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

:nobin
echo.
echo BUILD STOPPED - missing binary in the source tree.
echo Need: nginx-rtmp\nginx.exe, tools\ffmpeg.exe, tools\rtmf.exe
echo They are gitignored on purpose, copy them in before building.
exit /b 1
