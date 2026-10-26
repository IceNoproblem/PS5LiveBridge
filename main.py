# -*- coding: utf-8 -*-
"""
main.py —— 程序入口

启动顺序（顺序本身有讲究）：
    1. 补标准输出（打包成无控制台程序后 stdout 是 None，任何 print 都会炸）
    2. 提权（53 端口和防火墙都要管理员）
    3. 单实例（两个实例会抢 1935/6667/5000）
    4. 放行防火墙（不做这步，PS5 永远连不上，这是最高频的"失败原因"）
    5. 起编排器（IRC + 监控）、起 nginx（收流）
    6. 起 Web，再开窗口
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import threading
import time

from core import logbus, netutil, paths

APP_VERSION = "4.2"


# ──────────────────────────────────────────────────────────────
# 标准输出兜底（--noconsole 打包后 sys.stdout 为 None）
# ──────────────────────────────────────────────────────────────
class _Null(io.TextIOBase):
    def write(self, _s):
        return len(_s)

    def flush(self):
        pass


def _fix_streams():
    if sys.stdout is None:
        sys.stdout = _Null()
    if sys.stderr is None:
        sys.stderr = _Null()
    if netutil.IS_WINDOWS:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────
# 单实例
# ──────────────────────────────────────────────────────────────
_mutex = None


def acquire_single_instance() -> bool:
    """
    两个实例同时跑会互抢端口，表现为「劫持/弹幕时好时坏」，排查成本极高，
    所以这里硬性只允许一个。
    """
    global _mutex
    if netutil.IS_WINDOWS:
        try:
            import ctypes
            _mutex = ctypes.windll.kernel32.CreateMutexW(None, True, "Global\\PS5LiveBridge_4x")
            already = ctypes.windll.kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
            return not already
        except Exception:
            return True
    try:
        import fcntl
        lock_path = os.path.join(paths.LOGS_DIR, "app.lock")
        paths.ensure_dirs()
        fh = open(lock_path, "w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        _mutex = fh        # 持有引用，进程退出前不释放
        return True
    except Exception:
        return True


# ──────────────────────────────────────────────────────────────
# 参数
# ──────────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(description="PS5 直播桥 —— 把 PS5 直播画面接到 B 站")
    p.add_argument("--port", type=int, default=0, help="界面端口（默认 5000）")
    p.add_argument("--no-elevate", action="store_true", help="不尝试提权（仅调试用）")
    p.add_argument("--no-browser", action="store_true", help="不自动打开界面")
    p.add_argument("--no-window", action="store_true", help="不用原生窗口，直接开浏览器")
    p.add_argument("--check", action="store_true", help="只做环境体检，然后退出")
    p.add_argument("--version", action="store_true", help="显示版本")
    return p.parse_args()


def do_check() -> int:
    """体检。打包成无控制台程序后看不到 print，所以同时写一份报告并尝试打开。"""
    import builtins
    lines = []
    _print = builtins.print

    def print(*a, **kw):          # noqa: A001  故意遮蔽，收集到报告里
        text = " ".join(str(x) for x in a)
        lines.append(text)
        _print(*a, **kw)

    rc = _do_check_body(print)
    try:
        paths.ensure_dirs()
        report = os.path.join(paths.LOGS_DIR, "体检报告.txt")
        with open(report, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        _print("报告已写入：%s" % report)
        if isinstance(sys.stdout, _Null):
            try:
                os.startfile(report)          # Windows
            except Exception:
                pass
    except Exception:
        pass
    return rc


def _do_check_body(print) -> int:
    print("PS5 直播桥 v%s · 环境体检" % APP_VERSION)
    print("-" * 52)
    print("管理员权限：%s" % ("是" if netutil.is_admin() else "否（内置 DNS 与防火墙需要）"))
    print("程序目录：%s" % paths.BASE_DIR)
    for w in netutil.path_warnings(paths.BASE_DIR):
        print("  ! %s" % w)
    print("局域网 IP：%s" % (netutil.get_lan_ip() or "未探测到"))
    for it in netutil.list_adapters():
        print("  - %-22s %-16s 网关 %s%s" % (it["alias"], it["ip"], it["gateway"] or "-",
                                             "  [加速器]" if it["accel"] else ""))
    print("默认网关：%s" % (netutil.default_gateway() or "-"))
    print("系统 DNS：%s" % (", ".join(netutil.system_dns()) or "-"))
    accel = netutil.detect_accelerator()
    print("加速器进程：%s" % (", ".join(accel) if accel else "未检测到"))
    print("加速器 DNS：%s" % (", ".join(netutil.accelerator_dns()) or "未检测到"))
    print("-" * 52)
    for name, port, proto in (("DNS", 53, "udp"), ("收流", 1935, "tcp"),
                              ("弹幕", 6667, "tcp"), ("界面", 5000, "tcp")):
        free = netutil.port_free(port, proto)
        print("端口 %-5s %d/%s：%s" % (name, port, proto, "空闲" if free else "被占用"))
    from core import relay
    print("ffmpeg：%s" % relay.find_ffmpeg())
    print("nginx-rtmp：%s" % ("已随包" if os.path.isdir(paths.resource_path("nginx-rtmp"))
                             else "缺少（收流会失败）"))
    return 0


# ──────────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────────
def main() -> int:
    _fix_streams()
    args = parse_args()

    if args.version:
        print("PS5 直播桥 v%s" % APP_VERSION)
        return 0

    paths.ensure_dirs()

    if args.check:
        return do_check()

    # 提权：非管理员跑起来，内置 DNS 与防火墙都会失败，用户会一头雾水
    if netutil.IS_WINDOWS and not netutil.is_admin() and not args.no_elevate:
        if netutil.elevate(sys.argv[1:]):
            return 0
        logbus.warn("没有取得管理员权限，继续运行（内置 DNS 与防火墙可能不可用）")

    if not acquire_single_instance():
        logbus.warn("已经有一个 PS5 直播桥在运行了，本次启动取消")
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None, "已经有一个 PS5 直播桥窗口在运行。\n请切到那个窗口，或先关掉它。",
                "PS5 直播桥", 0x40)
        except Exception:
            print("已经有一个实例在运行。")
        return 2

    logbus.step("PS5 直播桥 v%s 启动" % APP_VERSION)

    # 让 nginx / ffmpeg 跟着本进程一起收掉，避免强杀后端口被僵尸进程占住
    if netutil.add_kill_on_close_job():
        logbus.info("已启用子进程随主程序退出（Job 对象）")

    # 防火墙（失败不致命，但要明确告诉用户）
    ok, msgs = netutil.ensure_firewall()
    for m in msgs:
        logbus.info(m, tag="防火墙")
    if not ok:
        logbus.warn("部分防火墙规则没加上，PS5 可能连不上本机", tag="防火墙")

    from core.orchestrator import Orchestrator

    orch = Orchestrator()
    cfg = paths.load_config()
    port = args.port or int(cfg.get("WEB_PORT", 5000))
    url = "http://127.0.0.1:%d/" % port

    orch.start()
    r = orch.ensure_nginx()
    if not r["ok"]:
        logbus.error("收流服务启动失败：%s" % r["detail"], tag="收流")

    # 「退出」按钮和窗口关闭都通过这个事件让主线程收工
    stop_event = threading.Event()

    def request_exit():
        stop_event.set()

    # Web 线程
    from web import server as webserver
    web_thread = threading.Thread(
        target=webserver.serve,
        args=(orch, "127.0.0.1", port),
        kwargs={"on_exit": request_exit},
        name="web", daemon=True)
    web_thread.start()

    ready = False
    for _ in range(60):
        time.sleep(0.2)
        ok, _code = netutil.probe_http(url + "api/state", timeout=1.0)
        if ok:
            ready = True
            break
    if not ready:
        logbus.warn("界面服务似乎没起来，仍尝试打开 %s" % url)

    if args.no_browser:
        logbus.info("界面地址：%s（按 Ctrl+C 退出）" % url)
        try:
            while not stop_event.wait(1):
                pass
        except KeyboardInterrupt:
            pass
        orch.shutdown()
        return 0

    from ui import window

    if args.no_window:
        import webbrowser
        webbrowser.open(url)
        logbus.info("已在浏览器打开 %s（关闭请点界面右上角「退出」或按 Ctrl+C）" % url)
        try:
            while not stop_event.wait(1):
                pass
        except KeyboardInterrupt:
            pass
        orch.shutdown()
    else:
        # 阻塞直到窗口关闭（窗口内部会调用 on_close 置位 stop_event）
        window.open_app_window(url, "PS5 直播桥",
                               on_close=request_exit, stop_event=stop_event)
        orch.shutdown()

    logbus.info("退出完成")
    logbus.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
