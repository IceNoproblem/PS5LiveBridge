# -*- coding: utf-8 -*-
"""
core/nginxmgr.py —— nginx-rtmp 收流端管理

为什么还用 nginx-rtmp：Windows 上这是唯一稳定可靠的 RTMP 收流方案，
自己写 RTMP 服务端（PSLinkB 试过 rtmp-rs/odv-rml 等多个库）最终也回到这套。
它同时提供 /stat，我们据此知道「PS5 有没有在推、推的流名是什么」。

两个老坑：
    1. nginx 打不开含中文的路径 —— 会以 1113 失败退出，日志里是一句
       "No mapping for the Unicode character"。所以必需时把 prefix 搬到纯英文目录。
    2. 1935 被上次残留的 nginx 占着 —— 表现为 bind() failed，需要先清干净。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET

from . import logbus, netutil, paths


CONF_TEMPLATE = r"""# 由 PS5 直播桥生成，请勿手改（每次启动会覆盖）
worker_processes 1;
error_log logs/error.log warn;
pid logs/nginx.pid;

events {{
    worker_connections 1024;
}}

rtmp {{
    server {{
        listen {rtmp_port};
        chunk_size 4096;
        # PS5 推的 app 名固定是 app（Twitch ingest 的标准路径 /app/<streamkey>）；
        # live / stream 是给其他设备/工具兜底的别名。
        application app {{
            live on;
            record off;
            interleave on;
            # 半开推流会话 5 秒释放流名，否则 PS5 重连会被拒（BadName）
            drop_idle_publisher 5s;
            allow play all;
        }}
        application live {{
            live on;
            record off;
            drop_idle_publisher 5s;
            allow play all;
        }}
        application stream {{
            live on;
            record off;
            drop_idle_publisher 5s;
            allow play all;
        }}
    }}
}}

http {{
    access_log off;
    server {{
        listen {stat_port};
        location /stat {{
            rtmp_stat all;
        }}
        location /ping {{
            return 200 "ok";
        }}
    }}
}}
"""


class NginxManager(object):
    def __init__(self, rtmp_port=1935, stat_port=8080):
        self.rtmp_port = int(rtmp_port)
        self.stat_port = int(stat_port)
        self.proc = None
        self.prefix = ""
        self.exe = ""
        self.last_error = ""
        # 串行化 start()：见 start() 里的注释
        self._start_lock = threading.Lock()

    # ──────────────────────────────────────────────
    # 路径准备
    # ──────────────────────────────────────────────
    def _is_ascii(self, p: str) -> bool:
        try:
            p.encode("ascii")
            return True
        except UnicodeEncodeError:
            return False

    def prepare(self) -> bool:
        """
        选定 nginx 运行目录。
        程序路径本身就是纯英文时原地跑（用户能看到日志）；
        否则整棵树复制到 %LOCALAPPDATA%\\PS5LiveBridge\\nginx-rtmp。
        """
        src = paths.resource_path("nginx-rtmp")
        if not os.path.isdir(src):
            self.last_error = "找不到 nginx-rtmp 目录：%s" % src
            return False

        if self._is_ascii(src) and " " not in src:
            self.prefix = src
        else:
            dst = os.path.join(paths.ASCII_SAFE_DIR, "nginx-rtmp")
            try:
                self._sync_tree(src, dst)
            except Exception as e:
                self.last_error = "复制 nginx 到英文目录失败：%s" % e
                return False
            self.prefix = dst
            logbus.info("程序路径含中文/空格，nginx 已搬到 %s" % dst, tag="收流")

        for sub in ("logs", "temp", "conf", "html"):
            try:
                os.makedirs(os.path.join(self.prefix, sub), exist_ok=True)
            except Exception:
                pass

        self.exe = os.path.join(self.prefix, "nginx.exe" if netutil.IS_WINDOWS else "nginx")
        if not os.path.exists(self.exe):
            # 开发机（macOS/Linux）上通常没有 nginx.exe，允许用系统 nginx 调试
            alt = shutil.which("nginx")
            if alt:
                self.exe = alt
            else:
                self.last_error = "找不到 nginx 可执行文件：%s" % self.exe
                return False

        self._write_conf()
        return True

    def _sync_tree(self, src: str, dst: str) -> None:
        os.makedirs(dst, exist_ok=True)
        for name in os.listdir(src):
            if name in ("logs", "temp"):
                os.makedirs(os.path.join(dst, name), exist_ok=True)
                continue
            s, d = os.path.join(src, name), os.path.join(dst, name)
            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)

    def _write_conf(self) -> None:
        conf_dir = os.path.join(self.prefix, "conf")
        os.makedirs(conf_dir, exist_ok=True)
        text = CONF_TEMPLATE.format(rtmp_port=self.rtmp_port, stat_port=self.stat_port)
        # nginx 配置文件用 \n 即可，但 Windows 下按 UTF-8 写不会出问题
        with open(os.path.join(conf_dir, "nginx.conf"), "w", encoding="utf-8") as f:
            f.write(text)

    # ──────────────────────────────────────────────
    # 启停
    # ──────────────────────────────────────────────
    def running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def start(self) -> bool:
        # 串行化：main.py 和「自动开劫持」线程可能同时调到这里，
        # 两个调用都看到「没在跑、端口没占」就会各起一组 nginx，
        # 结果 1935 只被一组占住，另一组空转（实测一次启动留下 4 个进程）。
        with self._start_lock:
            return self._start_locked()

    def _start_locked(self) -> bool:
        if self.running():
            return True
        if netutil.port_listening(self.rtmp_port):
            # 端口已经在听，说明已经有一组在跑了（可能不是本对象起的残留），
            # 直接认它，别再起第二组
            pid, name = netutil.port_owner(self.rtmp_port)
            if pid and "nginx" in (name or "").lower():
                logbus.info("端口 %d 已有 nginx 在监听（PID %d），复用它" % (self.rtmp_port, pid),
                            tag="收流")
                return True
        if not self.exe and not self.prepare():
            return False
        if netutil.port_listening(self.rtmp_port):
            logbus.warn("端口 %d 已被占用，先清理残留进程" % self.rtmp_port, tag="收流")
            pid, name = netutil.port_owner(self.rtmp_port)
            if pid and "nginx" in (name or "").lower():
                netutil.kill_pid(pid)
                time.sleep(0.8)
            elif pid:
                self.last_error = "端口 %d 被 %s（PID %d）占用" % (self.rtmp_port, name or "?", pid)
                return False

        startup = {}
        if netutil.IS_WINDOWS:
            startup["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = subprocess.SW_HIDE
            startup["startupinfo"] = si

        try:
            self.proc = subprocess.Popen(
                [self.exe], cwd=self.prefix,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                **startup)
        except Exception as e:
            self.last_error = "启动 nginx 失败：%s" % e
            return False

        for _ in range(30):
            time.sleep(0.2)
            if self.proc.poll() is not None:
                break
            if netutil.port_listening(self.rtmp_port):
                logbus.ok("收流服务已就绪（RTMP %d）" % self.rtmp_port, tag="收流")
                return True

        if self.proc.poll() is not None:
            err = ""
            try:
                err = (self.proc.stderr.read() or b"").decode("utf-8", "ignore")[:300]
            except Exception:
                pass
            if "1113" in err or "Unicode" in err:
                self.last_error = ("nginx 无法在中文路径运行。请把程序放到 D:\\PS5 这类"
                                   "纯英文路径后再试。（%s）" % err.strip()[:120])
            else:
                self.last_error = "nginx 启动失败：%s" % (err.strip()[:200] or "无输出")
            logbus.error(self.last_error, tag="收流")
            return False

        logbus.warn("nginx 已启动但 %d 端口未就绪，可能仍在初始化" % self.rtmp_port, tag="收流")
        return True

    def stop(self) -> None:
        # 优先用 nginx -s quit 优雅退出（让 RTMP 会话正常结束）
        if self.exe and os.path.exists(self.exe):
            try:
                subprocess.run([self.exe, "-s", "quit"], cwd=self.prefix or None,
                               timeout=5, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except Exception:
                pass
        time.sleep(0.5)
        if self.running():
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        if netutil.IS_WINDOWS:
            # nginx 会 fork worker，父进程退了 worker 可能还在
            netutil.run(["taskkill", "/F", "/IM", "nginx.exe"], timeout=5)
        self.proc = None

    # ──────────────────────────────────────────────
    # 状态（/stat）
    # ──────────────────────────────────────────────
    def stat_xml(self, timeout=1.5) -> str:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open("http://127.0.0.1:%d/stat" % self.stat_port, timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception:
            return ""

    def publisher(self) -> dict:
        """
        返回当前推流信息：
            {publishing: bool, app, name, uptime, bw_in(kbps), width, height, fps}
        """
        empty = {"publishing": False, "app": "", "name": "", "uptime": 0,
                 "bw_in": 0, "width": 0, "height": 0, "fps": 0}
        xml = self.stat_xml()
        if not xml:
            return empty
        try:
            root = ET.fromstring(xml)
        except Exception:
            return empty

        for app in root.iter("application"):
            app_name = (app.findtext("name") or "").strip()
            for stream in app.iter("stream"):
                if stream.find("publishing") is None:
                    continue
                name = (stream.findtext("name") or "").strip()

                def num(tag, div=1):
                    try:
                        return int(float(stream.findtext(tag) or 0)) // div
                    except Exception:
                        return 0

                width = height = fps = 0
                meta = stream.find("meta")
                if meta is not None:
                    # 真机实测：nginx-rtmp 把媒体信息放在嵌套子节点里
                    #   <meta><video><width>1280</width><height>720</height>
                    #   <frame_rate>30</frame_rate></video><audio>…</audio></meta>
                    # 早先按属性写法（meta width=）解析，结果界面上永远显示 0×0。
                    # 两种写法都兼容，谁的机器版本不同都不至于又不显示。
                    video = meta.find("video")
                    if video is not None:
                        try:
                            width = int(float(video.findtext("width") or 0))
                            height = int(float(video.findtext("height") or 0))
                            fps = int(float(video.findtext("frame_rate")
                                            or video.findtext("framerate") or 0))
                        except Exception:
                            pass
                    if not width:
                        try:
                            width = int(float(meta.get("width") or 0))
                            height = int(float(meta.get("height") or 0))
                            fps = int(float(meta.get("framerate") or 0))
                        except Exception:
                            pass
                return {
                    "publishing": True,
                    "app": app_name or "app",
                    "name": name,
                    "uptime": num("time"),
                    "bw_in": num("bw_in", 1024),   # kbps
                    "width": width, "height": height, "fps": fps,
                }
        return empty
