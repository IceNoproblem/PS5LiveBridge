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

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

REPO = "IceNoproblem/PS5LiveBridge"
API_LATEST = "https://api.github.com/repos/%s/releases/latest" % REPO
RELEASES_PAGE = "https://github.com/%s/releases" % REPO

CHECK_DELAY = 30.0
CHECK_INTERVAL = 12 * 3600.0

UPDATE_DIR = os.path.join(paths.DATA_DIR, "update")

KEEP = {"config.json", "bili_credential.json", "logs", "data",
        "启动轨迹.txt", "崩溃日志.txt", "update", ".DS_Store"}


MANIFEST_NAME = "manifest.json"
WEB_ASSET_TMPL = "PS5LiveBridge-web-%s.zip"


def _proxy_candidates() -> list:
    """
    依次要试的通道：直连 → 配置里的代理 → 本机代理 → 系统代理。

    直连优先是有原因的：api.github.com 在国内大多数网络是通的，
    先试它就不用等代理超时；不通再往后走，不让代理成为硬性依赖。
    """
    out = [""]
    cfg_px = paths.configured_proxy()
    if cfg_px:
        out.append(cfg_px)
    try:
        from . import twitch_title
        for p in twitch_title._local_proxies():
            if p not in out:
                out.append(p)
    except Exception:
        pass
    try:
        syspx = (urllib.request.getproxies().get("https")
                 or urllib.request.getproxies().get("http") or "")
        if syspx and syspx not in out:
            out.append(syspx)
    except Exception:
        pass
    return out


def _opener(proxy: str):
    if proxy:
        h = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    else:
        h = urllib.request.ProxyHandler({})
    # context 只能交给 HTTPSHandler。OpenerDirector.open() 不认这个参数，
    # 传了会抛 TypeError，更新检查会静默失败。
    https = urllib.request.HTTPSHandler(context=_SSL_CTX)
    return urllib.request.build_opener(h, https)


def _fetch_bytes(url: str, timeout: float = 15.0, retries: int = 2) -> bytes:
    """带代理兜底和重试地取一个 URL。"""
    last = None
    for px in _proxy_candidates():
        for _ in range(retries):
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "PS5LiveBridge/" + str(_VERSION_HINT[0])})
                with _opener(px).open(req, timeout=timeout) as r:
                    return r.read()
            except Exception as e:
                last = e
                time.sleep(0.6)
    raise last or RuntimeError("取不到 %s" % url)


_VERSION_HINT = [""]


def _sha256_of_file(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def _local_file_ok(rel: str, entry: dict) -> bool:
    """本地这个文件跟 manifest 记的是否一致。"""
    full = os.path.join(paths.BASE_DIR, rel.replace("/", os.sep))
    if not os.path.exists(full):
        return False
    try:
        want_size = int(entry.get("size") or 0)
        if want_size and os.path.getsize(full) != want_size:
            return False
    except Exception:
        return False
    return _sha256_of_file(full) == entry.get("sha256", "")


def _web_diff(manifest: dict) -> list:
    """web 那几个文件里，本地跟远端不一致的路径。"""
    out = []
    for rel, entry in (manifest.get("web_files") or {}).items():
        if not _local_file_ok(rel, entry):
            out.append(rel)
    return out


def parse_ver(tag: str) -> tuple:
    """
    'v4.6.1' -> (4, 6, 1)。比大小用；尾部 0 归一化，保证 4.5 == 4.5.0。

    字母后缀也参与比较：'5.1.7a' -> (5, 1, 7, 1)，排在 '5.1.7' -> (5, 1, 7) 之后。
    以前直接把字母丢掉，5.1.7a 会被认成 5.1.7，装了 5.1.7 的人就收不到这一版的
    更新提示，等于白发。
    """
    out = []
    for part in str(tag or "").lstrip("vV").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        letters = "".join(ch for ch in part if ch.isalpha())
        out.append(int(digits) if digits else 0)
        if letters:
            out.append(ord(letters[0].lower()) - 96)      # a=1, b=2 ...
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
            "state": "idle",
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

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

    def _set(self, **kw) -> None:
        with self._lock:
            self._state.update(kw)

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

    def check(self, force: bool = False) -> dict:
        """查 GitHub 最新版。force=True 忽略缓存。返回 snapshot()。"""
        with self._lock:
            if self._state["state"] == "available" and not force:
                return dict(self._state)
            if self._state["state"] in ("downloading", "installing"):
                return dict(self._state)

        _VERSION_HINT[0] = self.app_version
        self._set(state="checking", error="")
        try:
            info = self._fetch_latest()
            if info.get("manifest"):
                self._state["manifest"] = info["manifest"]
        except Exception as e:
            self._set(state="idle", error=str(e)[:200], checked_at=time.time())
            return self.snapshot()

        latest = info["latest"]
        if parse_ver(latest) <= parse_ver(self.app_version):
            self._set(state="idle", latest=latest, notes=info["notes"],
                      checked_at=time.time(), error="")
            logbus.info("已是最新版本 v%s" % self.app_version, tag="更新")
            return self.snapshot()

        man = info.get("manifest") or {}
        scope, web_files = "full", []
        if man:
            try:
                web_files = _web_diff(man)
            except Exception:
                web_files = []
            code_same = (man.get("code_sha") or "") == self._local_code_sha()
            if code_same and info.get("web_url"):
                scope = "web"

        self._set(state="available", latest=latest, notes=info["notes"][:800],
                  asset_url=info["asset_url"], asset_size=info["asset_size"],
                  asset_digest=info.get("asset_digest", ""),
                  web_url=info.get("web_url", ""), web_size=info.get("web_size", 0),
                  scope=scope, web_total=len(man.get("web_files") or {}),
                  web_changed=len(web_files),
                  checked_at=time.time(), error="")
        if scope == "web":
            logbus.warn("发现新版本 v%s（当前 v%s），只有界面文件变了，"
                        "更新包 %.0f KB" % (latest, self.app_version,
                                         (info.get("web_size") or 0) / 1024), tag="更新")
        else:
            logbus.warn("发现新版本 v%s（当前 v%s），界面右上角可更新"
                        % (latest, self.app_version), tag="更新")
        return self.snapshot()

    def _local_code_sha(self) -> str:
        """本地非 web 文件的聚合值，跟 manifest 里的 code_sha 比。"""
        man = (self._state.get("manifest") or {})
        files = man.get("files") or {}
        if not files:
            return ""
        import hashlib
        h = hashlib.sha256()
        for rel in sorted(files):
            entry = files[rel]
            full = os.path.join(paths.BASE_DIR, rel.replace("/", os.sep))
            if not os.path.exists(full):
                return ""
            try:
                if int(entry.get("size") or 0) and os.path.getsize(full) != int(entry["size"]):
                    return ""
            except Exception:
                return ""
            if _sha256_of_file(full) != entry.get("sha256"):
                return ""
            h.update(rel.encode("utf-8"))
            h.update(b"\0")
            h.update(entry["sha256"].encode("ascii"))
            h.update(b"\n")
        return h.hexdigest()

    def _fetch_latest(self) -> dict:
        raw = _fetch_bytes(API_LATEST, timeout=12)
        data = json.loads(raw.decode("utf-8"))
        tag = data.get("tag_name") or ""
        ver = tag.lstrip("vV")
        assets = data.get("assets") or []

        asset_url, asset_size = "", 0
        want = "PS5LiveBridge-%s-win64.zip" % tag
        picked = next((a for a in assets if a.get("name") == want), None)
        if picked is None:
            picked = next((a for a in assets
                           if str(a.get("name", "")).endswith(".zip")
                           and "-web-" not in str(a.get("name", ""))), None)
        asset_digest = ""
        if picked:
            asset_url = picked.get("browser_download_url", "")
            asset_size = int(picked.get("size") or 0)
            dig = str(picked.get("digest") or "")
            if dig.startswith("sha256:"):
                asset_digest = dig[7:]

        manifest_url = ""
        m = next((a for a in assets if a.get("name") == MANIFEST_NAME), None)
        if m:
            manifest_url = m.get("browser_download_url", "")
        manifest = {}
        if manifest_url:
            try:
                manifest = json.loads(_fetch_bytes(manifest_url, timeout=20).decode("utf-8"))
            except Exception as e:
                logbus.warn("取 manifest 失败：%s" % e, tag="更新")

        web_url, web_size = "", 0
        wname = WEB_ASSET_TMPL % ver
        w = next((a for a in assets if a.get("name") == wname), None)
        if w:
            web_url = w.get("browser_download_url", "")
            web_size = int(w.get("size") or 0)

        return {"latest": ver, "notes": data.get("body") or "",
                "asset_url": asset_url, "asset_size": asset_size,
                "asset_digest": asset_digest,
                "manifest": manifest, "web_url": web_url, "web_size": web_size}

    def install(self, on_before_exit=None) -> dict:
        """
        下载 → 校验 → 解压 → 交棒更新脚本 → 退出。
        on_before_exit：主程序收尾回调（关 nginx / 给 B 站发关播）。
        """
        snap = self.snapshot()
        if snap["state"] != "available" or not snap["asset_url"]:
            return {"ok": False, "detail": "没有可安装的更新"}
        if paths.PLATFORM == "docker":
            return {"ok": False, "detail": "容器版不能自我替换，请在宿主机执行：\n"
                                          "docker compose pull && docker compose up -d"}
        if paths.PLATFORM == "macos":
            return {"ok": False, "detail": "macOS 版暂不支持自动更新。\n"
                                          "请到 GitHub Releases 手动下载新版 .app 替换。"}
        if paths.PLATFORM != "windows":
            return {"ok": False, "detail": "当前平台不支持自动更新。"}

        t = threading.Thread(target=self._install_bg,
                             args=(on_before_exit,), daemon=True)
        t.start()
        return {"ok": True, "detail": "开始下载并安装"}

    def _install_bg(self, on_before_exit) -> None:
        try:
            snap = self.snapshot()
            if (snap.get("scope") or "full") == "web" and snap.get("web_url"):
                self._install_web(snap)
                return
            self._install_full(snap, on_before_exit)
        except Exception as e:
            logbus.error("更新失败：%s" % e, tag="更新")
            self._set(state="error", error=str(e)[:300])

    def _install_web(self, snap: dict) -> None:
        """
        只换界面文件。

        这一类更新不动 exe，所以不强制退出——用户可能正在直播。
        下载几万个字节，装完提示重启即可。
        """
        url = snap["web_url"]
        size = int(snap.get("web_size") or 0)
        tag = snap["latest"]
        os.makedirs(UPDATE_DIR, exist_ok=True)
        zp = os.path.join(UPDATE_DIR, "web-%s.zip" % tag)
        if os.path.exists(zp):
            try:
                os.remove(zp)
            except Exception:
                pass

        self._set(state="downloading", percent=0, error="", message="正在下载界面更新")
        self._download(url, zp, size)
        self._set(state="downloading", percent=100)

        count = 0
        base_abs = os.path.abspath(paths.BASE_DIR)
        with zipfile.ZipFile(zp) as z:
            for name in z.namelist():
                if name.endswith("/") or ".." in name or name.startswith("/"):
                    continue
                rel = name.replace("/", os.sep)
                dest = os.path.abspath(os.path.join(base_abs, rel))
                if not dest.startswith(base_abs + os.sep):
                    logbus.warn("跳过越界路径：%s" % name, tag="更新")
                    continue
                folder = os.path.dirname(dest)
                if folder:
                    os.makedirs(folder, exist_ok=True)
                with z.open(name) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                count += 1
        try:
            os.remove(zp)
        except Exception:
            pass

        logbus.warn("界面文件已更新（%d 个），重启程序后生效" % count, tag="更新")
        self._set(state="ready", percent=100, message="界面文件已更新，重启程序后生效")

    def _install_full(self, snap: dict, on_before_exit) -> None:
        url, size, tag = snap["asset_url"], snap["asset_size"], snap["latest"]
        os.makedirs(UPDATE_DIR, exist_ok=True)
        zip_path = os.path.join(UPDATE_DIR, "PS5LiveBridge-v%s.zip" % tag)

        self._set(state="downloading", percent=0, error="", message="正在下载完整包")
        self._download(url, zip_path, size)

        self._set(state="downloading", percent=100)
        if size and os.path.getsize(zip_path) != size:
            raise RuntimeError("下载不完整（大小对不上），请重试")

        want = (snap.get("asset_digest") or "").strip()
        if want:
            got = _sha256_of_file(zip_path)
            if got and got != want:
                try:
                    os.remove(zip_path)
                except Exception:
                    pass
                raise RuntimeError("下载的文件校验不通过（sha256 不符），请重试")

        # 解压到**同级**目录：同盘 move 是元数据操作，毫秒级完成。
        # 不再逐个覆盖正在运行的文件 —— 那才是被杀软拦、被文件锁卡住的根源。
        base = os.path.abspath(paths.BASE_DIR).rstrip("\\/")
        staging = base + ".next"
        if os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(staging)
        root = staging
        sub = os.path.join(staging, "PS5LiveBridge")
        if os.path.isdir(sub):
            root = sub
        if not os.path.exists(os.path.join(root, "PS5LiveBridge.exe")):
            raise RuntimeError("更新包里没有 PS5LiveBridge.exe，包不对")

        self._set(state="ready", percent=100)
        script = self._write_update_script(root, tag, base)
        logbus.warn("更新已就绪，正在重启以完成安装…", tag="更新")

        if on_before_exit:
            try:
                on_before_exit()
            except Exception:
                pass

        self._set(state="installing")
        kwargs = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
        # 用 cmd 跑 bat。不再用 powershell -ExecutionPolicy Bypass，
        # 那个组合是杀软最敏感的特征。
        subprocess.Popen(["cmd", "/c", script], **kwargs)
        time.sleep(1.0)
        os._exit(0)

    def _download(self, url: str, dest: str, total: int) -> None:
        """
        下载，支持续传和代理兜底。

        52MB 的包在国内断一次就白下，所以先看本地已有多少，用 Range 接着下；
        一个通道失败换下一个（直连 → 配置代理 → 本机代理 → 系统代理）。
        """
        done0 = os.path.getsize(dest) if os.path.exists(dest) else 0
        if total and done0 >= total:
            self._set(percent=100)
            return

        last = None
        for px in _proxy_candidates():
            for _ in range(3):
                try:
                    self._download_once(url, dest, total, px)
                    got = os.path.getsize(dest) if os.path.exists(dest) else 0
                    if not total or got >= total:
                        return
                    last = RuntimeError("下载不完整：%d/%d" % (got, total))
                except Exception as e:
                    last = e
                    logbus.warn("下载失败（%s）：%s" % (px or "直连", str(e)[:80]), tag="更新")
                    time.sleep(1.2)
        raise last or RuntimeError("下载失败")

    def _download_once(self, url: str, dest: str, total: int, proxy: str) -> None:
        done = os.path.getsize(dest) if os.path.exists(dest) else 0
        headers = {"User-Agent": "PS5LiveBridge/" + self.app_version}
        if done:
            headers["Range"] = "bytes=%d-" % done
        req = urllib.request.Request(url, headers=headers)
        mode = "ab" if done else "wb"
        with _opener(proxy).open(req, timeout=60) as resp:
            code = getattr(resp, "status", 200)
            if done and code != 206:
                done = 0
                mode = "wb"
            with open(dest, mode) as f:
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        self._set(percent=min(99, int(done * 100 / total)))

    def _write_update_script(self, new_dir: str, tag: str, base: str = "") -> str:
        """
        生成 update.bat：等旧进程退出 -> 整个目录改名备份 -> 新版整目录就位
        -> 用户数据搬回来 -> 启动 -> 校验 -> 失败回滚 -> 自删。

        为什么不再用 PowerShell 逐文件替换：
          1. powershell -ExecutionPolicy Bypass 是杀软最敏感的特征；
          2. 逐个覆盖正在运行的 exe/dll 本来就不可靠。
        现在只做两次目录 move，同盘元数据操作，毫秒级，杀软也不管。
        """
        import tempfile
        base = (base or os.path.abspath(paths.BASE_DIR)).rstrip("\\/")
        bak = base + ".old"
        log = os.path.join(tempfile.gettempdir(), "ps5lb_update.log")
        script = os.path.join(tempfile.gettempdir(), "ps5lb_update.bat")
        keep = " ".join('"%s"' % k for k in
                        ("config.json", "bili_credential.json", "rtmpf.json",
                         "ov_title.txt"))

        tpl = r'''@echo off
setlocal
set BASE=__BASE__
set NEW=__NEW__
set BAK=__BAK__
set LOG=__LOG__
set PIDW=__PID__

echo [%date% %time%] update start (__TAG__) >> "%LOG%"
call :wait_exit
echo [%date% %time%] old process gone >> "%LOG%"

if not exist "%NEW%\PS5LiveBridge.exe" goto :fail
if exist "%BAK%" rd /s /q "%BAK%" >nul 2>&1

rem 1) old dir renamed as a whole (nothing being written inside it)
move /y "%BASE%" "%BAK%" >nul 2>&1
if errorlevel 1 (
  echo [%date% %time%] rename old failed >> "%LOG%"
  goto :fail
)
echo [%date% %time%] old renamed >> "%LOG%"

rem 2) new build takes its place
move /y "%NEW%" "%BASE%" >nul 2>&1
if errorlevel 1 (
  echo [%date% %time%] move new failed >> "%LOG%"
  goto :rollback
)
echo [%date% %time%] new in place >> "%LOG%"

rem 3) bring user data back
for %%F in (__KEEP__) do (
  if exist "%BAK%\%%~F" copy /y "%BAK%\%%~F" "%BASE%\%%~F" >nul 2>&1
)
if exist "%BAK%\logs"  xcopy /e /i /y /q "%BAK%\logs"  "%BASE%\logs"  >nul 2>&1
if exist "%BAK%\state" xcopy /e /i /y /q "%BAK%\state" "%BASE%\state" >nul 2>&1

rem 4) start it and verify it survives
start "" "%BASE%\PS5LiveBridge.exe"
timeout /t 7 /nobreak >nul
tasklist /fi "IMAGENAME eq PS5LiveBridge.exe" 2>nul | find /i "PS5LiveBridge.exe" >nul
if errorlevel 1 (
  echo [%date% %time%] new build did not start >> "%LOG%"
  goto :rollback
)
echo [%date% %time%] ok, new build running >> "%LOG%"
rd /s /q "%BAK%" >nul 2>&1
rd /s /q "%NEW%" >nul 2>&1
del "%~f0" >nul 2>&1
exit /b 0

:rollback
echo [%date% %time%] rolling back >> "%LOG%"
if exist "%BASE%" rd /s /q "%BASE%" >nul 2>&1
move /y "%BAK%" "%BASE%" >nul 2>&1
start "" "%BASE%\PS5LiveBridge.exe"
del "%~f0" >nul 2>&1
exit /b 1

:fail
echo [%date% %time%] aborted, old version untouched >> "%LOG%"
if exist "%BASE%\PS5LiveBridge.exe" start "" "%BASE%\PS5LiveBridge.exe"
del "%~f0" >nul 2>&1
exit /b 1

:wait_exit
for /l %%i in (1,1,60) do (
  tasklist /fi "PID eq %PIDW%" 2>nul | find "%PIDW%" >nul
  if errorlevel 1 goto :eof
  timeout /t 1 /nobreak >nul
)
goto :eof
'''
        text = (tpl.replace("__BASE__", base)
                   .replace("__NEW__", new_dir.rstrip("\\/"))
                   .replace("__BAK__", bak)
                   .replace("__LOG__", log)
                   .replace("__PID__", str(os.getpid()))
                   .replace("__KEEP__", keep)
                   .replace("__TAG__", tag))
        with open(script, "w", encoding="mbcs", errors="replace") as f:
            f.write(text.replace("\n", "\r\n"))
        return script

_UPDATER: Updater = None
_UP_LOCK = threading.Lock()


def get_updater(app_version: str) -> Updater:
    global _UPDATER
    with _UP_LOCK:
        if _UPDATER is None:
            _UPDATER = Updater(app_version)
        return _UPDATER
