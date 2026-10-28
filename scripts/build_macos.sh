#!/bin/bash
set -e
cd "$(dirname "$0")/.."

echo "[1/6] Checking Python..."
python3 --version || exit 1

echo "[2/6] Installing dependencies..."
python3 -m pip install --upgrade pip -q
python3 -m pip install -r requirements.txt -q
python3 -m pip install pyinstaller -q

echo "[3/6] Detecting optional pywebview..."
PV=""
python3 -c "import webview" 2>/dev/null && PV="--collect-all webview"
if [ -n "$PV" ]; then echo "    pywebview found - native window enabled"; else echo "    pywebview not installed - will use browser mode"; fi

echo "[4/6] Cleaning old output..."
rm -rf build dist

echo "[5/6] Building..."
python3 -m PyInstaller --noconfirm --clean \
  --name PS5LiveBridge \
  --windowed \
  --icon assets/icon.icns \
  --add-data "web:web" \
  --add-data "nginx-rtmp:nginx-rtmp" \
  --add-data "README.md:." \
  --add-data "LICENSE:." \
  --hidden-import brotli \
  --hidden-import qrcode \
  --hidden-import PIL._imaging \
  --hidden-import aiohttp \
  --hidden-import flask \
  --collect-data certifi \
  $PV \
  main.py

echo ""
echo "Copying extra files..."
if [ -f "tools/ffmpeg" ]; then
  mkdir -p "dist/PS5LiveBridge.app/Contents/MacOS/tools"
  cp tools/ffmpeg "dist/PS5LiveBridge.app/Contents/MacOS/tools/"
  echo "    bundled ffmpeg"
else
  echo "    WARNING: tools/ffmpeg not found."
  echo "    Install via brew install ffmpeg or bundle it manually."
fi

cp README.md "dist/PS5LiveBridge.app/Contents/Resources/README.md" 2>/dev/null || true
cp README.md "dist/PS5LiveBridge.app/Contents/Resources/使用说明.md" 2>/dev/null || true
cp LICENSE "dist/PS5LiveBridge.app/Contents/Resources/LICENSE.txt" 2>/dev/null || true
cp config.example.json "dist/PS5LiveBridge.app/Contents/Resources/" 2>/dev/null || true

echo ""
echo "[6/6] Verifying bundle..."
if [ ! -f "dist/PS5LiveBridge.app/Contents/MacOS/PS5LiveBridge" ]; then
  echo "BUILD FAILED - binary not found"
  exit 1
fi
echo "    bundle layout OK"

PS5LB_NO_POPUP=1 "dist/PS5LiveBridge.app/Contents/MacOS/PS5LiveBridge" --version 2>/dev/null && echo "    binary starts OK" || echo "    WARNING: smoke test failed (may need --no-browser)"

echo ""
echo "============================================================"
echo " DONE.  Open: dist/PS5LiveBridge.app"
echo "============================================================"
