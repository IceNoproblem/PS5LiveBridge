# -*- coding: utf-8 -*-
"""
core/updater.py —— 自动更新（GitHub Releases）

两端一套检查逻辑，安装方式不同：
    Windows 桌面版：下载 zip → 起更新脚本 → 自己退出 → 脚本换文件 → 自动重启。
        正在运行的 exe 不能覆盖自己，所以必须借外部脚本的手。
    容器版：容器不能替换自己。检查到新版只提示，真正的更新交给
        watchtower（compose 的 auto-update profile）或手动 docker compose pull。

安全边界：
    只从 GitHub 官方仓库的 Releases 下载，不跟随任何网页里的链接；
    下载前后校验文件大小；用户数据（config / 凭据 / 日志）永不覆盖。
"""
from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile

from . import logbus, paths

# PyInstaller 冻结后 urllib 找不到系统 CA 证书，必须用 certifi 提供的 CA 包。
# 不加这段，更新检查会报 SSL: CERTIFICATE_VERIFY_FAILED。
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

REPO = "IceNoproblem/PS5LiveBridge"
API_LATEST = "https://api.github.com/repos/%s/releases/latest" % REPO
RELEASES_PAGE = "https://github.com/%s/releases" % REPO

# 检查频率：启动 30 秒后第一次，之后每 12 小时。
# GitHub 匿名 API 限速 60 次/小时，这个频率远在限内。
CHECK_DELAY = 30.0
CHECK_INTERVAL = 12 * 3600.0

# 更新包落盘位置（随用户数据走，程序目录里的 data/update）
UPDATE_DIR = os.path.join(paths.DATA_DIR, "update")

# 换版本时绝不覆盖的文件 / 目录
KEEP = {"config.json", "bili_credential.json", "logs", "data",
        "启动轨迹.txt", "崩溃日志.txt", "update", ".DS_Store"}


def parse_ver(tag: str) -> tuple:
    """'v4.6.1' -> (4, 6, 1)。比大小用；尾部 0 归一化，保证 4.5 == 4.5.0。"""
    out = []
    for part in str(tag or "").lstrip("vV").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    while len(out) > 1 and out[-1] == 0:
        out.pop()
    return tuple(out or [0])


class Updater(object):
    """
    线程安全的更新器。后台线程定期 check()，界面随时读 status()。
    """

    def __init__(self, app_version: str):
        self.app_version = str(app_version)
        self._lock = threading.RLock()
        self._state = {
            "state": "idle",          # idle / checking / available / downloading / ready / installing / error
            "current": self.app_version,
            "latest": "",
            "notes": "",
            "asset_url": "",
            "asset_size": 0,
            "percent": 0,
            "error": "",
            "checked_at": 0.0,
        }
        self._stop = False
        self._thread = None

    # ── 状态 ──────────────────────────────────────────
    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

    def _set(self, **kw) -> None:
        with self._lock:
            self._state.update(kw)

    # ── 后台巡检 ──────────────────────────────────────
    def start(self, enabled: bool = True) -> None:
        if not enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="updater", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop = True

    def _loop(self) -> None:
        time.sleep(CHECK_DELAY)
        while not self._stop:
            try:
                self.check(force=True)
            except Exception as e:
                logbus.warn("检查更新失败：%s" % e, tag="更新")
            for _ in range(int(CHECK_INTERVAL)):
                if self._stop:
                    return
                time.sleep(1)

    # ── 检查 ──────────────────────────────────────────
    def check(self, force: bool = False) -> dict:
        """查 GitHub 最新版。force=True 忽略缓存。返回 snapshot()。"""
        with self._lock:
            if self._state["state"] == "available" and not force:
                return dict(self._state)
            if self._state["state"] in ("downloading", "installing"):
                return dict(self._state)

        self._set(state="checking", error="")
        try:
            info = self._fetch_latest()
        except Exception as e:
            # 网络不通不是错误，安静降级；点过「检查」再把原因带出去
            self._set(state="idle", error=str(e)[:200], checked_at=time.time())
            return self.snapshot()

        latest = info["latest"]
        if parse_ver(latest) <= parse_ver(self.app_version):
            self._set(state="idle", latest=latest, notes=info["notes"],
                      checked_at=time.time(), error="")
            logbus.info("已是最新版本 v%s" % self.app_version, tag="更新")
            return self.snapshot()

        self._set(state="available", latest=latest, notes=info["notes"][:800],
                  asset_url=info["asset_url"], asset_size=info["asset_size"],
                  checked_at=time.time(), error="")
        logbus.warn("发现新版本 v%s（当前 v%s），界面右上角可更新" % (latest, self.app_version),
                    tag="更新")
        return self.snapshot()

    def _fetch_latest(self) -> dict:
        req = urllib.request.Request(API_LATEST, headers={
            "User-Agent": "PS5LiveBridge/" + self.app_version,
            "Accept": "application/vnd.github+json",
        })
        with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tag = data.get("tag_name") or ""
        asset_url, asset_size = "", 0
        want = "PS5LiveBridge-%s-win64.zip" % tag
        assets = data.get("assets") or []
        picked = next((a for a in assets if a.get("name") == want), None)
        if picked is None:
            picked = next((a for a in assets if str(a.get("name", "")).endswith(".zip")), None)
        if picked:
            asset_url = picked.get("browser_download_url", "")
            asset_size = int(picked.get("size") or 0)
        return {"latest": tag.lstrip("vV"), "notes": data.get("body") or "",
                "asset_url": asset_url, "asset_size": asset_size}

    # ── 安装（Windows）────────────────────────────────
    def install(self, on_before_exit=None) -> dict:
        """
        下载 → 校验 → 解压 → 交棒更新脚本 → 退出。
        on_before_exit：主程序收尾回调（关 nginx / 给 B 站发关播）。
        """
        snap = self.snapshot()
        if snap["state"] != "available" or not snap["asset_url"]:
            return {"ok": False, "detail": "没有可安装的更新"}
        if paths.PLATFORM != "windows":
            return {"ok": False, "detail": "容器版不能自我替换，请在宿主机执行：\n"
                                          "docker compose pull && docker compose up -d"}

        t = threading.Thread(target=self._install_bg,
                             args=(on_before_exit,), daemon=True)
        t.start()
        return {"ok": True, "detail": "开始下载并安装"}

    def _install_bg(self, on_before_exit) -> None:
        try:
            snap = self.snapshot()
            url, size, tag = snap["asset_url"], snap["asset_size"], snap["latest"]
            os.makedirs(UPDATE_DIR, exist_ok=True)
            zip_path = os.path.join(UPDATE_DIR, "PS5LiveBridge-v%s.zip" % tag)

            self._set(state="downloading", percent=0, error="")
            self._download(url, zip_path, size)

            self._set(state="downloading", percent=100)
            if size and os.path.getsize(zip_path) != size:
                raise RuntimeError("下载不完整（大小对不上），请重试")

            # 解压出 <zip>/PS5LiveBridge/ 这一层
            new_dir = os.path.join(UPDATE_DIR, "new")
            if os.path.isdir(new_dir):
                shutil.rmtree(new_dir, ignore_errors=True)
            with zipfile.ZipFile(zip_path) as z:
                z.extractall(new_dir)
            root = new_dir
            sub = os.path.join(new_dir, "PS5LiveBridge")
            if os.path.isdir(sub):
                root = sub
            if not os.path.exists(os.path.join(root, "PS5LiveBridge.exe")):
                raise RuntimeError("更新包里没有 PS5LiveBridge.exe，包不对")

            self._set(state="ready", percent=100)
            script = self._write_update_script(root, tag)
            logbus.warn("更新已就绪，正在重启以完成安装…", tag="更新")

            if on_before_exit:
                try:
                    on_before_exit()
                except Exception:
                    pass

            self._set(state="installing")
            # 起更新脚本（独立进程），主程序随即退出，脚本换完文件再把新程序拉起来
            kwargs = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
            subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", script],
                **kwargs)
            time.sleep(1.0)
            os._exit(0)     # 交给更新脚本收尾；走正常退出路径会把 Job 对象里的子进程带走，nginx 已经在回调里停了
        except Exception as e:
            logbus.error("更新失败：%s" % e, tag="更新")
            self._set(state="error", error=str(e)[:300])

    def _download(self, url: str, dest: str, total: int) -> None:
        req = urllib.request.Request(url, headers={"User-Agent": "PS5LiveBridge/" + self.app_version})
        with urllib.request.urlopen(req, timeout=60, context=_SSL_CTX) as resp, open(dest, "wb") as f:
            done = 0
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    self._set(percent=min(99, int(done * 100 / total)))

    def _write_update_script(self, new_dir: str, tag: str) -> str:
        """
        生成 update.ps1：等本进程退出 → 备份 → 换文件（不动用户数据）→ 重启 → 自删。
        用 PowerShell 而不是 bat：中文路径和异常处理都稳。
        """
        app_dir = paths.BASE_DIR
        backup_dir = os.path.join(paths.DATA_DIR, "backup-v%s-%s" % (
            self.app_version, time.strftime("%Y%m%d-%H%M%S")))
        os.makedirs(UPDATE_DIR, exist_ok=True)
        script = os.path.join(UPDATE_DIR, "update.ps1")
        keep = ", ".join("'%s'" % k for k in sorted(KEEP))
        text = """$ErrorActionPreference = 'Stop'
$AppDir   = '{app}'
$NewDir   = '{new}'
$Backup   = '{backup}'
$PidWait  = {pid}
$Keep     = @({keep})

# 1) 等旧程序真正退出（最多 30 秒）
for ($i = 0; $i -lt 60; $i++) {{
  $p = Get-Process -Id $PidWait -ErrorAction SilentlyContinue
  if (-not $p) {{ break }}
  Start-Sleep -Milliseconds 500
}}

# 2) 备份旧版（只备程序文件，用户数据留在原地本来就不动）
New-Item -ItemType Directory -Force -Path $Backup | Out-Null
Get-ChildItem -Path $AppDir -Force | Where-Object {{
  $Keep -notcontains $_.Name
}} | ForEach-Object {{
  Copy-Item -Path $_.FullName -Destination (Join-Path $Backup $_.Name) -Recurse -Force -ErrorAction SilentlyContinue
}}

# 3) 删旧程序文件，换新（保留用户数据）
Get-ChildItem -Path $AppDir -Force | Where-Object {{
  $Keep -notcontains $_.Name
}} | ForEach-Object {{
  Remove-Item -Path $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
}}
Get-ChildItem -Path $NewDir -Force | ForEach-Object {{
  if ($Keep -notcontains $_.Name) {{
    Copy-Item -Path $_.FullName -Destination (Join-Path $AppDir $_.Name) -Recurse -Force
  }}
}}

# 4) 拉起新版
Start-Process -FilePath (Join-Path $AppDir 'PS5LiveBridge.exe')

# 5) 自删脚本所在的更新目录（保留备份）
Remove-Item -Path $NewDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -Path $PSCommandPath -Force -ErrorAction SilentlyContinue
""".format(app=app_dir.replace("'", "''"),
           new=new_dir.replace("'", "''"),
           backup=backup_dir.replace("'", "''"),
           pid=os.getpid(), keep=keep)
        with open(script, "w", encoding="utf-8-sig") as f:
            f.write(text)
        return script


# 进程级单例（orchestrator / web 共用一个实例）
_UPDATER: Updater = None
_UP_LOCK = threading.Lock()


def get_updater(app_version: str) -> Updater:
    global _UPDATER
    with _UP_LOCK:
        if _UPDATER is None:
            _UPDATER = Updater(app_version)
        return _UPDATER
