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

from core import logbus, netutil, paths

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
    返回进程对象或 None：进程活着就代表窗口开着。

    浏览器必须以普通用户身份启动（run_unelevated）——本软件是管理员权限跑的，
    Edge / Chrome 被提权启动时有自我保护，起来一两秒就自动退出。
    --disable-background-mode：不让浏览器退到后台驻留，否则下次启动会被
    已存在的实例「接走」，新进程立刻退出，看起来就是开了个寂寞。
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
        "--disable-background-mode",
        "--new-window",
        "--window-size=1200,860",
        "--disable-background-networking",
    ]
    try:
        proc = netutil.run_unelevated(args)
        if proc is None:
            logbus.warn("app 模式启动失败：浏览器没能拉起来")
        return proc
    except Exception as e:
        logbus.warn("app 模式启动失败：%s" % e)
        return None


def open_app_window(url: str, title: str, on_close=None, stop_event=None) -> str:
    """
    阻塞直到窗口关闭 / stop_event 被置位。
    返回实际使用的模式："window" / "app" / "browser" / "none"

    关键判定：窗口进程「3 秒内就退出」不算用户关窗——那是浏览器拒绝启动
    （提权被自我保护杀掉、被杀软拦、或被已有实例接走），必须降级重开，
    否则整个软件跟着闪退。用户真关窗至少会看几眼。
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
        t0 = time.time()
        proc = _app_mode(title, browser, url)
        if proc is not None:
            logbus.info("界面窗口已打开（app 模式）")
            # 窗口进程退出 = 用户关掉了窗口
            wait_loop(lambda: proc.poll() is not None)
            elapsed = time.time() - t0
            if stop_event is not None and stop_event.is_set():
                # 用户点了「退出」
                return "app"
            if proc.poll() is None or elapsed >= 3.0:
                if on_close:
                    try:
                        on_close()
                    except Exception:
                        pass
                return "app"
            # 秒退 = 浏览器没活下来，不是用户关窗
            logbus.warn("浏览器窗口 %.1f 秒就退出了，多半是浏览器拒绝管理员权限启动，改开默认浏览器"
                        % elapsed)

    # ── 3. 默认浏览器 ──
    try:
        # 走 explorer.exe 交给 shell 启动：不继承管理员令牌，浏览器不会自我保护退出。
        # 直接 webbrowser.open() 在提权进程里拉起来的还是提权浏览器，可能重蹈覆辙。
        if netutil.IS_WINDOWS:
            subprocess.Popen(["explorer.exe", url],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            import webbrowser
            webbrowser.open(url)
        logbus.info("已在默认浏览器打开界面：%s" % url)
        _notify_browser_fallback(url)
        wait_loop(lambda: False)      # 只能靠界面上的「退出」或 Ctrl+C
        return "browser"
    except Exception as e:
        logbus.error("连浏览器都没打开（%s）。请手动访问 %s" % (e, url))
        return "none"


def _notify_browser_fallback(url: str) -> None:
    """
    降级开浏览器时明确告诉用户一声。
    不说的话用户看桌面上没窗口，会以为又闪退了 —— 实际软件在后台好好的。
    """
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None,
            "界面已经在你的浏览器里打开了：%s\n\n"
            "软件继续在后台运行，关掉它请到浏览器里点右上角「退出」。" % url,
            "PS5 直播桥", 0x40)
    except Exception:
        pass
