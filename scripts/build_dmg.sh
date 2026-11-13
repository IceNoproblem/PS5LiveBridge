#!/bin/bash
# 把 dist/PS5LiveBridge.app 打成可直接安装的 DMG
set -e
cd "$(dirname "$0")/.."

APP="dist/PS5LiveBridge.app"
VER=$(python3 -c "import sys; sys.path.insert(0,'.'); from core import paths; print(paths.APP_VERSION)" 2>/dev/null || echo "0.0.0")
# 产物名带上真实架构，别把 Intel 包标成 arm64
case "$(uname -m)" in
  arm64)  DMG_ARCH="arm64" ;;
  x86_64) DMG_ARCH="x86_64" ;;
  *)      DMG_ARCH="$(uname -m)" ;;
esac
OUT="dist/PS5LiveBridge-${VER}-macOS-${DMG_ARCH}.dmg"

if [ ! -d "$APP" ]; then
  echo "先跑 build_mac.sh 生成 .app"; exit 1
fi

STAGE="/tmp/ps5lb-dmg-$$"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
# 拖拽安装的惯例：放一个 Applications 软链
ln -s /Applications "$STAGE/Applications"

cat > "$STAGE/安装说明.txt" <<'TXT'
PS5 直播桥 · macOS 版

安装：
  把左边的 PS5LiveBridge 拖到右边的 Applications 文件夹里。

第一次打开：
  macOS 会提示「无法验证开发者」（这个包没有 Apple 付费签名）。
  到「应用程序」文件夹里，右键点 PS5LiveBridge → 打开 → 再点一次「打开」。
  之后就可以正常双击了。

启动后：
  程序会自动打开浏览器，地址是 http://127.0.0.1:5001
  如果 5000 端口被系统占用（隔空播放接收器常年占着），会自动让开。

配置和数据：
  ~/Library/Application Support/PS5LiveBridge
  卸载时把这个文件夹一起删掉即可。
TXT

echo "[1/2] 生成 DMG"
rm -f "$OUT"
hdiutil create -volname "PS5 直播桥" -srcfolder "$STAGE" -ov -format UDZO "$OUT" > /tmp/dmg.log 2>&1 || {
  echo "hdiutil 失败"; tail -5 /tmp/dmg.log; exit 1;
}

echo "[2/2] 完成"
ls -lh "$OUT" | awk '{print "产物:", $9, "("$5")"}'
echo "SHA256: $(shasum -a 256 "$OUT" | cut -d' ' -f1)"

# 清理 staging（只删自己刚建的那层）
# staging 在 /tmp，系统会自己清；这里不删，避免触发批量删除保护
