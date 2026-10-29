from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import zipfile

WEB_PREFIXES = ("_internal/web/", "assets/", "docs/")
SKIP_NAMES = {"config.json", "progress.json", "rtmp_progress.txt"}
SKIP_DIRS = {"logs", "data", "diag", "__pycache__", ".git", "update"}


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def walk(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn in SKIP_NAMES:
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace("\\", "/")
            yield rel, full


def aggregate(items: dict) -> str:
    """把 (路径 + 内容哈希) 排序后整体再哈希一次。任何文件变了聚合值就变。"""
    h = hashlib.sha256()
    for rel in sorted(items):
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(items[rel]["sha256"].encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def is_web(rel: str) -> bool:
    return any(rel.startswith(p) for p in WEB_PREFIXES)


def main(root: str, version: str) -> int:
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        print("目录不存在：%s" % root)
        return 1

    files, web_files = {}, {}
    total = 0
    for rel, full in walk(root):
        entry = {"sha256": sha256_file(full), "size": os.path.getsize(full)}
        total += entry["size"]
        (web_files if is_web(rel) else files)[rel] = entry

    manifest = {
        "version": version,
        "tag": "v" + version,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "web_sha": aggregate(web_files),
        "code_sha": aggregate(files),
        "total_size": total,
        "web_size": sum(v["size"] for v in web_files.values()),
        "web_files": web_files,
        "files": files,
    }

    out_json = os.path.join(os.path.dirname(root), "manifest.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)

    zip_path = os.path.join(os.path.dirname(root), "PS5LiveBridge-web-%s.zip" % version)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in sorted(web_files):
            z.write(os.path.join(root, rel), rel)

    print("manifest : %s (%.1f KB)" % (out_json, os.path.getsize(out_json) / 1024))
    print("web zip  : %s (%.1f KB)" % (zip_path, os.path.getsize(zip_path) / 1024))
    print("文件      : 总 %d（web %d / 其余 %d），全量 %.1f MB"
          % (len(files) + len(web_files), len(web_files), len(files), total / 1024 / 1024))
    print("web_sha  : %s" % manifest["web_sha"][:16])
    print("code_sha : %s" % manifest["code_sha"][:16])
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print("用法: gen_manifest.py <dist/PS5LiveBridge> [版本号]")
        sys.exit(2)
    ver = args[1] if len(args) > 1 else ""
    if not ver:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        try:
            from core import paths
            ver = paths.APP_VERSION
        except Exception:
            ver = "0.0.0"
    sys.exit(main(args[0], ver))
