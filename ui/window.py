# -*- coding: utf-8 -*-
"""
ui/window.py —— 把界面装进一个「看起来像软件」的窗口

三级降级，从好到将就：
    1. pywebview（装了才用）—— 真正的原生窗口，能拿到关闭事件
    2. Edge / Chrome 的 --app 模式 —— 没有地址栏和标签页，视觉上就是独立应用；
       用一个独立 user-data-dir 起进程，窗口关掉进程就退，能当关闭信号用
    3. 默认浏览器 —— 最差也能用，只是多一个标签页

产品不能因为「某个依赖没装」就打不开，所以这里必须层层兜底。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

from core import logbus, paths

# Windows 上 Edge 的常见安装位置
EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
MAC_BROWSERS = [
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def _find_browser() -> str:
    if sys.platform == "win32":
        for p in EDGE_PATHS + CHROME_PATHS:
            if os.path.exists(p):
                return p
        return ""
    for p in MAC_BROWSERS:
        if os.path.exists(p):
            return p
    return ""


def _app_mode(name_hint: str, browser: str, url: str):
    """
    用独立 profile 起一个 app 窗口。
    返回 Popen 或 None：进程活着就代表窗口开着。
    """
    profile = os.path.join(paths.ASCII_SAFE_DIR, "uiprofile")
    try:
        os.makedirs(profile, exist_ok=True)
    except Exception:
        return None
    args = [
        browser,
        "--app=" + url,
        "--user-data-dir=" + profile,
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,MediaRouter",
        "--window-size=1200,860",
        "--disable-background-networking",
    ]
    try:
        return subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except Exception as e:
        logbus.warn("app 模式启动失败：%s" % e)
        return None


def open_app_window(url: str, title: str, on_close=None, stop_event=None) -> str:
    """
    阻塞直到窗口关闭 / stop_event 被置位。
    返回实际使用的模式："window" / "app" / "browser" / "none"
    """
    def wait_loop(predicate, timeout=None):
        """predicate() 为真就返回。"""
        t0 = time.time()
        while True:
            if stop_event is not None and stop_event.is_set():
                return
            if predicate():
                return
            if timeout and time.time() - t0 > timeout:
                return
            time.sleep(0.5)

    # ── 1. 原生窗口 ──
    try:
        import webview  # pywebview
    except Exception:
        webview = None

    if webview is not None:
        try:
            win = webview.create_window(title, url, width=1200, height=860,
                                        min_size=(940, 680),
                                        background_color="#0b0d11")
            closed = {"v": False}

            def _on_closed():
                closed["v"] = True
                if on_close:
                    try:
                        on_close()
                    except Exception:
                        pass

            win.events.closed += _on_closed
            logbus.info("界面窗口已打开（原生窗口）")
            webview.start(private_mode=False)
            return "window"
        except Exception as e:
            logbus.warn("原生窗口不可用（%s），尝试 app 模式" % e)

    # ── 2. 浏览器 app 模式 ──
    browser = _find_browser()
    if browser:
        proc = _app_mode(title, browser, url)
        if proc is not None:
            logbus.info("界面窗口已打开（app 模式）")
            # 窗口进程退出 = 用户关掉了窗口
            wait_loop(lambda: proc.poll() is not None)
            if on_close:
                try:
                    on_close()
                except Exception:
                    pass
            return "app"

    # ── 3. 默认浏览器 ──
    try:
        import webbrowser
        webbrowser.open(url)
        logbus.info("已在默认浏览器打开界面：%s" % url)
        wait_loop(lambda: False)      # 只能靠界面上的「退出」或 Ctrl+C
        return "browser"
    except Exception as e:
        logbus.error("连浏览器都没打开（%s）。请手动访问 %s" % (e, url))
        return "none"
