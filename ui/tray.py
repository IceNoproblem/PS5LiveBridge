"""
系统托盘图标。

为什么要有它：桌面版打包成无控制台窗口（console=False），双击之后界面上
只有浏览器里一个标签页。用户关掉标签，程序还在后台跑，占着 1935、抱着
WinDivert 句柄、维持着 B 站的登录心跳。以前只有在任务管理器里才能看出
它活着，这是最容易让人以为「已经关了」的地方。

托盘把三件事变回可见：
  1. 程序在不在跑（图标在就是跑着）
  2. 端口劫持有没有起来（图标右下角的黄点、红点）
  3. 怎么退出（右键菜单里有一个真能退干净的入口）

只在 Windows 上启用。Mac 版保持原样，那边 pystray 还要拖 pyobjc 依赖，
不想为了一个图标把已经能用的包搞复杂。
"""

from __future__ import annotations

import os
import sys
import threading

try:
    import pystray
    from PIL import Image, ImageDraw
except Exception:                                  # 没装、或平台不支持
    pystray = None
    Image = None
    ImageDraw = None

from core import logbus, paths

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "PS5LiveBridge"

# 图标右下角的小圆点。正常时不画，只在出问题时出现，免得平时太扎眼。
COLOR_WARN = (245, 158, 11, 255)
COLOR_ERROR = (239, 68, 68, 255)
RING = (12, 18, 30, 255)


def available() -> bool:
    """托盘能不能用。缺组件或者不是 Windows 都返回 False，调用方据此跳过。"""
    if pystray is None or Image is None:
        return False
    return paths.PLATFORM == "windows"


# ---------------------------------------------------------------- 开机自启

def autostart_enabled() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            v, _ = winreg.QueryValueEx(k, RUN_VALUE)
            return bool(str(v).strip())
    except Exception:
        return False


def autostart_target() -> str:
    """注册表里当前记着的那个 exe 路径，没有就是空串。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            v, _ = winreg.QueryValueEx(k, RUN_VALUE)
            return str(v).strip().strip('"')
    except Exception:
        return ""


def set_autostart(on: bool):
    """
    打开或关闭开机自启。返回 (ok, 失败原因)。

    写的是 HKCU 的 Run 键，不需要管理员，也不会像计划任务那样在系统里
    留一个带触发器、带运行身份的实体。路径每次写入都取当前 exe，
    所以换版本目录之后点一下勾选框就自动跟上，不会指着旧目录。
    """
    if not getattr(sys, "frozen", False):
        return False, "源码运行模式不支持开机自启，请用打包后的程序"
    try:
        import winreg
    except Exception as e:
        return False, "读不到注册表：%s" % e

    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            if on:
                winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ,
                                  '"%s"' % sys.executable)
            else:
                try:
                    winreg.DeleteValue(k, RUN_VALUE)
                except FileNotFoundError:
                    pass
        return True, ""
    except Exception as e:
        return False, str(e)


def _norm(p) -> str:
    try:
        return os.path.normcase(os.path.abspath(str(p)))
    except Exception:
        return str(p)


def _autostart_stale() -> bool:
    """注册表里记的路径和当前 exe 不一致（换过版本目录）。"""
    t = autostart_target()
    if not t:
        return False
    return _norm(t) != _norm(sys.executable)


# ---------------------------------------------------------------- 图标绘制

def _base_image():
    p = paths.resource_path("assets", "icon.png")
    try:
        im = Image.open(p).convert("RGBA")
    except Exception:
        im = Image.new("RGBA", (128, 128), (37, 99, 235, 255))
    return im.resize((128, 128), Image.LANCZOS)


def _with_dot(base, color):
    im = base.copy()
    d = ImageDraw.Draw(im)
    r = 26
    x0, y0 = 128 - r * 2 - 2, 128 - r * 2 - 2
    d.ellipse((x0, y0, x0 + r * 2, y0 + r * 2), fill=color, outline=RING, width=6)
    return im


# ---------------------------------------------------------------- 托盘本体

class Tray:
    def __init__(self, orch, url: str, on_quit, on_restart=None):
        self.orch = orch
        self.url = url
        self.on_quit = on_quit
        self.on_restart = on_restart
        self.icon = None
        self._base = None
        self._key = None
        self._stop = threading.Event()

    # -- 状态

    def _snapshot(self):
        err = str(getattr(self.orch, "hijack_error", "") or "").strip()
        div = getattr(self.orch, "divert", None)
        running = bool(div is not None and getattr(div, "running", False))
        live = str((getattr(self.orch, "live", {}) or {}).get("state") or "idle")
        return err, running, live

    def _keyof(self, err, running):
        if err:
            return "error"
        if not running:
            return "warn"
        return "ok"

    def _image(self, key):
        if self._base is None:
            self._base = _base_image()
        if key == "error":
            return _with_dot(self._base, COLOR_ERROR)
        if key == "warn":
            return _with_dot(self._base, COLOR_WARN)
        return self._base

    def _tooltip(self, err, running, live):
        # Windows 托盘提示限制 127 字符，这几段最多 60 来个，安全
        if err:
            tail = "端口劫持未启动"
        elif running:
            tail = "分流正常"
        else:
            tail = "分流未开启"
        mid = "直播中" if live in ("relaying", "live") else "空闲"
        return "PS5 直播桥 v%s · %s · %s" % (paths.APP_VERSION, tail, mid)

    # -- 菜单动作

    def _open(self, icon=None, item=None):
        try:
            import webbrowser
            webbrowser.open(self.url)
        except Exception as e:
            logbus.warn("打开界面失败：%s" % e, tag="托盘")

    def _toggle_auto(self, icon=None, item=None):
        want = not autostart_enabled()
        ok, msg = set_autostart(want)
        if not ok:
            logbus.warn("开机自启设置失败：%s" % msg, tag="托盘")
            self._notify("开机自启没设上：%s" % msg)
        else:
            logbus.info("开机自启已%s" % ("打开" if want else "关闭"), tag="托盘")
        try:
            (icon or self.icon).update_menu()
        except Exception:
            pass

    def _restart(self, icon=None, item=None):
        if not self.on_restart:
            return
        logbus.info("从托盘重启", tag="托盘")
        try:
            self.on_restart()
        except Exception as e:
            logbus.warn("重启失败：%s" % e, tag="托盘")
            return
        self._close_soon(2.5)

    def _quit(self, icon=None, item=None):
        logbus.info("从托盘退出", tag="托盘")
        try:
            self.on_quit()
        except Exception as e:
            logbus.warn("退出回调出错：%s" % e, tag="托盘")
        # 给主流程几秒收尾（关播、停收流、停转发），图标先留着，
        # 不然用户点了退出图标立刻消失、进程其实还在跑，反而更疑惑
        self._close_soon(2.5)

    def _close_soon(self, delay):
        def _later():
            self._stop.set()
            try:
                if self.icon:
                    self.icon.stop()
            except Exception:
                pass
        threading.Timer(delay, _later).start()

    def _notify(self, msg, title="PS5 直播桥"):
        try:
            if self.icon:
                self.icon.notify(msg, title)
        except Exception:
            pass

    def _menu(self):
        return pystray.Menu(
            pystray.MenuItem("打开界面", self._open, default=True),
            pystray.MenuItem("开机自启", self._toggle_auto,
                             checked=lambda item: autostart_enabled()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("重启程序", self._restart),
            pystray.MenuItem("退出", self._quit),
        )

    # -- 生命周期

    def _on_ready(self, icon):
        """pystray 完成初始化后回调，此时才能弹气泡。"""
        self._first_tip()

    def _first_tip(self):
        if paths.load_config().get("TRAY_TIP_SHOWN"):
            return
        try:
            self.icon.notify("程序在这里跑，关掉网页不会退出。要退出右键点图标。",
                             "PS5 直播桥")
            paths.save_config({"TRAY_TIP_SHOWN": True})
        except Exception:
            pass

    def _refresh(self):
        while not self._stop.wait(2.0):
            try:
                err, running, live = self._snapshot()
                key = self._keyof(err, running)
                if key != self._key:
                    self._key = key
                    self.icon.icon = self._image(key)
                self.icon.title = self._tooltip(err, running, live)
            except Exception:
                pass

    def start(self) -> bool:
        if not available():
            return False
        try:
            err, running, live = self._snapshot()
            self._key = self._keyof(err, running)
            self.icon = pystray.Icon(
                "PS5LiveBridge",
                self._image(self._key),
                self._tooltip(err, running, live),
                menu=self._menu(),
            )
            threading.Thread(target=lambda: self.icon.run(setup=self._on_ready),
                             name="tray", daemon=True).start()
            threading.Thread(target=self._refresh, name="tray-refresh",
                             daemon=True).start()
            logbus.info("已放进系统托盘", tag="界面")
            if _autostart_stale():
                logbus.info("开机自启还记着旧路径 %s，点托盘菜单里那项会改成当前版本"
                            % autostart_target(), tag="界面")
            return True
        except Exception as e:
            logbus.warn("托盘没起来（不影响使用）：%s" % e, tag="界面")
            return False


_TRAY = None


def start(orch, url: str, on_quit, on_restart=None) -> bool:
    """给 main 用的入口，失败一律返回 False，绝不抛异常上去。"""
    global _TRAY
    try:
        t = Tray(orch, url, on_quit, on_restart)
        if t.start():
            _TRAY = t
            return True
    except Exception as e:
        logbus.warn("托盘初始化失败（不影响使用）：%s" % e, tag="界面")
    return False
