# -*- coding: utf-8 -*-
"""
main.py —— 程序入口

启动顺序（顺序本身有讲究）：
    0. 崩溃兜底（只用标准库）——双击后毫无反应是最难排查的故障，必须留痕迹
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
import traceback

APP_VERSION = "4.5"


# ──────────────────────────────────────────────────────────────
# 崩溃兜底（必须最先装，且只依赖标准库 —— core 本身 import 失败也要能报出来）
#
# 为什么需要：打包成 --noconsole 后，任何未捕获异常都表现为「双击后闪退/毫无反应」。
# 出问题的机器往往不在手边，没有痕迹就只能猜。这里保证：
#     1. 每走一步写一行 启动轨迹.txt（最后停在哪一步，就是死在哪一步）
#     2. 真炸了写 崩溃日志.txt（含 traceback + 原生崩溃的线程栈）
#     3. 弹窗告诉用户发生了什么，而不是无声无息消失
# ──────────────────────────────────────────────────────────────
def _base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


_BASE = _base_dir()
_TRACE_FILE = os.path.join(_BASE, "启动轨迹.txt")
_CRASH_FILE = os.path.join(_BASE, "崩溃日志.txt")


def _trace(msg: str) -> None:
    try:
        with open(_TRACE_FILE, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def _alert(text: str, title: str = "PS5 直播桥") -> None:
    if (os.environ.get("PS5LB_NO_POPUP") or "").strip():
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, str(text)[:2000], title, 0x10)
    except Exception:
        pass


def _is_admin_simple() -> str:
    try:
        import ctypes
        return "是" if ctypes.windll.shell32.IsUserAnAdmin() else "否"
    except Exception:
        return "未知"


def _win_ver() -> str:
    try:
        if sys.platform == "win32":
            v = sys.getwindowsversion()
            return "Windows %d.%d (build %d)" % (v.major, v.minor, v.build)
    except Exception:
        pass
    return sys.platform


def _write_crash(header: str, body: str) -> None:
    text = (
        "\n==== %s ====\n"
        "版本：%s\n系统：%s\n程序目录：%s\n管理员：%s\n\n%s"
        % (time.strftime("%Y-%m-%d %H:%M:%S"), APP_VERSION,
           _win_ver(), _BASE, _is_admin_simple(), body)
    )
    for path in (_CRASH_FILE, os.path.join(os.environ.get("TEMP") or _BASE, "ps5livebridge-crash.log")):
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass
    _trace("!! %s" % header)


def _excepthook(tp, val, tb) -> None:
    body = "".join(traceback.format_exception(tp, val, tb))
    _write_crash("%s: %s" % (tp.__name__, val), body)
    _alert("程序出错了：%s\n\n错误详情已写进：\n%s\n\n把这两个文件发给作者就能定位问题：\n%s"
           % (val, _CRASH_FILE, _TRACE_FILE))


def _thread_excepthook(args) -> None:
    # 线程崩溃不会带走进程，但同样要留痕迹（比如劫持线程被杀软掐掉）
    body = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
    _write_crash("后台线程异常: %s" % args.exc_value, body)


def _install_crash_guard() -> None:
    try:
        import faulthandler
        _fh = open(_CRASH_FILE, "a", encoding="utf-8")
        faulthandler.enable(file=_fh, all_threads=True)   # 原生崩溃也留线程栈
    except Exception:
        pass
    sys.excepthook = _excepthook
    try:
        threading.excepthook = _thread_excepthook
    except Exception:
        pass


_install_crash_guard()

from core import logbus, netutil, paths  # noqa: E402  兜底装好后才拉 core


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
    p.add_argument("--headless", action="store_true",
                   help="纯服务模式：不建窗口、不开浏览器，收到 SIGTERM 就收工（容器用）")
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
        if isinstance(sys.stdout, _Null) and not (os.environ.get("PS5LB_NO_POPUP") or "").strip():
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
# 完整性自检
# ──────────────────────────────────────────────────────────────
def _bundle_problems() -> list:
    """
    打包版最常见的「闪退」不是程序坏了，是文件不齐：
    在压缩包里直接双击（资源管理器只解出 exe），或者只把 exe 拷给了别人。
    这里查出来就明说，别让用户对着一个消失的窗口猜。
    """
    if not getattr(sys, "frozen", False):
        return []
    probs = []
    internal = os.path.join(_BASE, "_internal")
    if not os.path.isdir(internal):
        probs.append(
            "缺少 _internal 文件夹。\n\n"
            "多半是没解压就双击了，或者只拷了 exe 一个文件。\n"
            "请把压缩包完整解压到一个文件夹（比如桌面），再双击里面的 PS5LiveBridge.exe。"
        )
        return probs
    for name in ("python312.dll", "base_library.zip"):
        if not os.path.exists(os.path.join(internal, name)):
            probs.append("文件不完整：缺少 _internal\\%s。\n请重新解压整个压缩包。" % name)
    meipass = getattr(sys, "_MEIPASS", "")
    rtmp_ok = any(os.path.isdir(os.path.join(p, "nginx-rtmp"))
                  for p in (_BASE, meipass) if p)
    if not rtmp_ok:
        probs.append("缺少 nginx-rtmp 文件夹（收流服务）。\n请重新解压整个压缩包。")
    return probs


# ──────────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────────
def main() -> int:
    # 每次启动覆写轨迹：用户发过来的就是最近一次的现场
    try:
        with open(_TRACE_FILE, "w", encoding="utf-8") as f:
            f.write("PS5 直播桥 v%s 启动轨迹（只保留最近一次）\n" % APP_VERSION)
    except Exception:
        pass
    _trace("进程启动 | %s | 管理员=%s" % (_win_ver(), _is_admin_simple()))
    try:
        return _main()
    except Exception:
        _excepthook(*sys.exc_info())
        return 1


def _main() -> int:
    _fix_streams()
    _trace("输出流就绪")
    args = parse_args()
    _trace("参数解析完成: %s" % " ".join(sys.argv[1:]) or "(无)")

    if args.version:
        print("PS5 直播桥 v%s" % APP_VERSION)
        _trace("--version 查询，退出")
        return 0

    for prob in _bundle_problems():
        _trace("完整性检查失败: %s" % prob.replace("\n", " "))
        _alert(prob, "文件不完整")
        return 3

    _trace("完整性检查通过")
    paths.ensure_dirs()
    _trace("数据目录就绪: %s" % paths.BASE_DIR)

    if args.check:
        _trace("--check 体检")
        return do_check()

    # 容器里没有桌面可交互：不提权、不开窗口、不探浏览器
    headless = args.headless or paths.PLATFORM == "docker"

    # 提权：非管理员跑起来，内置 DNS 与防火墙都会失败，用户会一头雾水
    if netutil.IS_WINDOWS and not netutil.is_admin() and not args.no_elevate:
        _trace("不是管理员，发起提权重启")
        if netutil.elevate(sys.argv[1:]):
            _trace("提权请求已发出，本进程退出（后续由管理员进程接手）")
            return 0
        logbus.warn("没有取得管理员权限，继续运行（内置 DNS 与防火墙可能不可用）")
        _trace("提权被拒绝或失败，降级继续运行")

    if not acquire_single_instance():
        _trace("已有实例在运行，退出")
        logbus.warn("已经有一个 PS5 直播桥在运行了，本次启动取消")
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None, "已经有一个 PS5 直播桥窗口在运行。\n请切到那个窗口，或先关掉它。",
                "PS5 直播桥", 0x40)
        except Exception:
            print("已经有一个实例在运行。")
        return 2

    _trace("单实例通过")
    logbus.step("PS5 直播桥 v%s 启动（%s）" % (APP_VERSION, paths.PLATFORM))

    # 让 nginx / ffmpeg 跟着本进程一起收掉，避免强杀后端口被僵尸进程占住
    if netutil.add_kill_on_close_job():
        logbus.info("已启用子进程随主程序退出（Job 对象）")
    _trace("Job 对象已设置")

    # 防火墙（失败不致命，但要明确告诉用户）
    ok, msgs = netutil.ensure_firewall()
    for m in msgs:
        logbus.info(m, tag="防火墙")
    if not ok:
        logbus.warn("部分防火墙规则没加上，PS5 可能连不上本机", tag="防火墙")
    _trace("防火墙处理完成（全部成功=%s）" % ok)

    _trace("加载核心模块")
    from core.orchestrator import Orchestrator

    orch = Orchestrator()
    cfg = paths.load_config()
    port = args.port or int(cfg.get("WEB_PORT", 5000))
    # 桌面版只听回环（不需要把控制台暴露到局域网）；
    # 容器版必须对外，否则宿主机浏览器进不来。
    host = str(cfg.get("WEB_HOST") or ("0.0.0.0" if headless else "127.0.0.1"))
    url = "http://127.0.0.1:%d/" % port

    # 先把收流服务拉起来，再起编排器。
    # 顺序反过来的话，监控线程第一拍会看到「nginx 还没起」，白打一条警告。
    r = orch.ensure_nginx()
    if not r["ok"]:
        logbus.error("收流服务启动失败：%s" % r["detail"], tag="收流")
    _trace("收流服务: %s" % ("OK" if r["ok"] else r.get("detail", "")[:80]))
    orch.start()
    _trace("编排器已启动（监控/IRC/弹幕）")

    # 「退出」按钮和窗口关闭都通过这个事件让主线程收工
    stop_event = threading.Event()

    def request_exit():
        stop_event.set()

    # 容器停容器时发的是 SIGTERM，要抓住它做优雅退出（否则 B 站那边还在播）
    if headless:
        import signal as _signal

        def _on_signal(signum, _frame):
            logbus.info("收到退出信号（%d），正在收尾" % signum)
            stop_event.set()

        for sig in (_signal.SIGTERM, _signal.SIGINT):
            try:
                _signal.signal(sig, _on_signal)
            except Exception:
                pass

    # Web 线程
    from web import server as webserver
    web_thread = threading.Thread(
        target=webserver.serve,
        args=(orch, host, port),
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
    _trace("Web 服务%s，界面地址 %s" % ("就绪" if ready else "未确认就绪", url))

    if headless:
        logbus.ok("界面地址：http://<这台机器的 IP>:%d/" % port)
        try:
            while not stop_event.wait(1):
                pass
        except KeyboardInterrupt:
            pass
        orch.shutdown()
        logbus.info("退出完成")
        logbus.close()
        return 0

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
        _trace("浏览器模式已打开")
        logbus.info("已在浏览器打开 %s（关闭请点界面右上角「退出」或按 Ctrl+C）" % url)
        try:
            while not stop_event.wait(1):
                pass
        except KeyboardInterrupt:
            pass
        orch.shutdown()
    else:
        # 阻塞直到窗口关闭（窗口内部会调用 on_close 置位 stop_event）
        _trace("打开软件窗口（此后若再闪退，多半是杀软拦截，见崩溃日志）")
        window.open_app_window(url, "PS5 直播桥",
                               on_close=request_exit, stop_event=stop_event)
        orch.shutdown()

    _trace("正常退出")
    logbus.info("退出完成")
    logbus.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
