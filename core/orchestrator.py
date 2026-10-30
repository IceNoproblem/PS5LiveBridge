from __future__ import annotations

import os
import sys
import threading
import time
from collections import deque

from . import bili as bilimod
from . import overlay, rtmf
from . import (config_store, danmaku_history, danmaku_throttle, divert,
               dnscheck, dnsserver, eventbus, ffmpeg_mgr, logbus, netutil,
               netwatch, nginxmgr, paths, relay, routercfg, services,
               statemachine)


class Orchestrator(object):
    def __init__(self):
        self.cfg = paths.load_config()
        self.progress = paths.load_progress()
        try:
            logbus.set_debug(bool(self.cfg.get("DEBUG_LOG")))
        except Exception:
            pass
        self.bili = bilimod.Bilibili()
        self.services = services.Services(
            self.bili,
            on_danmaku=self._on_danmaku,
            on_ps5_join=self._on_ps5_join,
            on_ps5_drop=self._on_ps5_drop,
        )
        _impl = (os.environ.get("PS5LB_RTMP_IMPL")
                 or self.cfg.get("RTMP_IMPL") or "nginx").strip().lower()
        _rtmp_port = int(self.cfg.get("RTMP_PORT", 1935))
        if _impl in ("rtmpf", "rtmp-forwarder", "rtmp_forwarder"):
            self.nginx = rtmf.RtmpForwarderManager(rtmp_port=_rtmp_port)
            self.native_relay = True
        else:
            self.nginx = nginxmgr.NginxManager(
                rtmp_port=_rtmp_port,
                stat_port=int(os.environ.get("PS5LB_STAT_PORT", "8099")),
            )
            self.native_relay = False
        self.relay = relay.Relay()
        self.overlay = overlay.Overlay()
        # 叠加模式下用来接住 ffmpeg 的第二路输出，供本地预览
        self.preview_srv = rtmf.RtmpForwarderManager(
            rtmp_port=19350, web_port=8097)
        self.dns = None
        self.divert = None
        self.hijack_error = ""

        self.lock = threading.RLock()
        self.lan_ip = self.cfg.get("LOCAL_IP_FORCE") or self.cfg.get("LAN_IP") or netutil.get_lan_ip()

        self.live = {
            "state": "idle",
            "error": "",
            "since": 0.0,
            "ps5": {},
            "bili": {},
            "local": {},
            "confirm_tries": 0,
        }
        self.danmaku_recent = deque(maxlen=60)
        self.danmaku_total = 0
        self.gift_total = 0
        self._stop = False
        self._shutdown_done = False
        self._monitor = None
        self._login_task = None
        self._qr = {"key": "", "url": "", "png": "", "status": "idle", "expire": 0}

        self._sm = statemachine.LiveStateMachine()
        self._bus = eventbus.EventBus()
        self._bus.on_event(eventbus.EventType.STATE_CHANGED, self._log_state_change)
        self._store = config_store.ConfigStore(
            os.path.join(paths.DATA_DIR, "state"))
        self._dm_throttle = danmaku_throttle.DanmakuThrottle(interval=1.5)
        self._dm_throttle.set_sender(self._inject_raw)
        self._dm_history = danmaku_history.DanmakuHistory(
            os.path.join(paths.DATA_DIR, "danmaku"))
        self._netwatch = netwatch.NetworkWatcher(interval=30)
        self._netwatch.on_change(self._on_lan_ip_change)

    def start(self) -> None:
        logbus.step("PS5 直播桥启动（%s），本机 IP：%s"
                    % (paths.PLATFORM, self.lan_ip or "未探测到"))
        logbus.info("版本 %s | 平台 %s | 数据目录 %s"
                    % (paths.APP_VERSION, paths.PLATFORM, paths.DATA_DIR),
                    tag="环境")
        logbus.info("Python %s | 收流端口 %s | 弹幕端口 %s | 界面端口 %s"
                    % (sys.version.split()[0],
                       self.cfg.get("RTMP_PORT", 1935),
                       self.cfg.get("IRC_PORT", 6667),
                       os.environ.get("PS5LB_WEB_PORT", "5000")),
                    tag="环境")
        if not self.lan_ip:
            logbus.error("没有探测到局域网 IP，PS5 无法连到本机。请检查网络。")

        _ff = ffmpeg_mgr.find_ffmpeg()
        if _ff:
            logbus.info("ffmpeg 就绪：%s" % _ff, tag="转发")
        else:
            logbus.error("找不到 ffmpeg，PS5 画面将无法转到 B 站", tag="转发")

        self.services.set_channel(self.cfg.get("TWITCH_CHANNEL") or "ps5")
        self.services.start(int(self.cfg.get("IRC_PORT", 6667)))

        if self.bili.logged_in:
            threading.Thread(target=self._verify_login_bg, daemon=True).start()

        if paths.caps()["divert"] and self.cfg.get("HIJACK_MODE", "divert") == "divert":
            threading.Thread(target=self._auto_enable_divert, daemon=True).start()

        self._monitor = threading.Thread(target=self._monitor_loop, name="orchestrator", daemon=True)
        self._monitor.start()

        self._dm_throttle.start()
        try:
            p = self._dm_history.start_session(self.bili.room_id)
            logbus.info("弹幕记录：%s" % os.path.basename(p), tag="弹幕")
        except Exception as e:
            logbus.warn("弹幕记录文件创建失败：%s" % e, tag="弹幕")
        self._netwatch.start()

    def _auto_enable_divert(self):
        r = self.enable_divert()
        if not r.get("ok"):
            self.hijack_error = r.get("detail", "")

    def shutdown(self) -> None:
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._stop = True
        try:
            if self.live["state"] in ("relaying", "live"):
                self.stop_live(reason="程序退出")
        except Exception:
            pass
        self.relay.stop()
        self.nginx.stop()
        if self.divert:
            self.divert.stop()
            self.divert = None
        if self.dns:
            self.dns.stop()
            self.dns = None
        self._stop_https_relay()
        try:
            self._dm_throttle.stop()
        except Exception:
            pass
        try:
            self._dm_history.close_session()
        except Exception:
            pass
        try:
            self._netwatch.stop()
        except Exception:
            pass
        self.services.stop()
        logbus.info("已退出")

    def _verify_login_bg(self):
        ok = self.bili.verify_login()
        if ok:
            logbus.ok("登录状态有效：%s（直播间 %s）" % (self.bili.uname or self.bili.uid,
                                                        self.bili.room_id), tag="B站")
            if self.bili.room_id:
                self.services.start_danmaku(self.bili.room_id)
            self._clear_login_error()
        else:
            logbus.warn("上次的登录已失效，需要重新扫码", tag="B站")

    def _clear_login_error(self) -> None:
        """
        登录成功后，把「还没登录 B 站」那条已经不成立的错误摘掉。

        不摘的话它会一直挂在第 5 步上：用户先在 PS5 试推流（报没登录），
        再去第 4 步扫码，登成功了界面还是红的，看起来就像登录没生效。
        """
        cleared = False
        with self.lock:
            if self.live["state"] == "error":
                err = self.live.get("error") or ""
                if "登录" in err or "扫码" in err:
                    self._set_state("idle")
                    self.live["error"] = ""
                    self.live["since"] = 0.0
                    cleared = True
        if not cleared:
            return

        try:
            pub = self.nginx.publisher()
        except Exception:
            pub = {}
        if pub.get("publishing") and self.output_mode() == "direct":
            logbus.info("PS5 还在推流，登录后自动接着开播", tag="直播")
            threading.Thread(target=self._start_bili_and_relay,
                             args=(pub.get("app", "app"), pub.get("name", "")),
                             name="start-live", daemon=True).start()

    def env(self) -> dict:
        adapters = netutil.list_adapters()
        accel_procs = netutil.detect_accelerator()
        accel_dns = netutil.accelerator_dns()
        return {
            "lan_ip": self.lan_ip,
            "gateway": netutil.default_gateway(),
            "adapters": adapters,
            "system_dns": netutil.system_dns(),
            "accel_procs": accel_procs,
            "accel_running": bool(accel_procs),
            "accel_dns": accel_dns,
            "accel_gateway_ip": netutil.accelerator_gateway_ip(),
            "is_admin": netutil.is_admin(),
            "path_warnings": netutil.path_warnings(paths.BASE_DIR),
            "ports": {
                "dns": netutil.port_free(int(self.cfg.get("DNS_PORT", 53)), "udp"),
                "rtmp": netutil.port_free(int(self.cfg.get("RTMP_PORT", 1935))),
                "irc": netutil.port_free(int(self.cfg.get("IRC_PORT", 6667))),
                "web": netutil.port_free(int(self.cfg.get("WEB_PORT", 5000))),
            },
        }

    def guides(self) -> dict:
        return {
            "ip": self.lan_ip,
            "domains": self.cfg.get("HIJACK_DOMAINS", []),
            "guides": routercfg.guides(self.lan_ip, self.cfg.get("HIJACK_DOMAINS", [])),
        }

    def export_router_files(self) -> list:
        return routercfg.write_files(paths.DATA_DIR, self.lan_ip,
                                     self.cfg.get("HIJACK_DOMAINS", []))

    def router_detect(self, ip: str = "") -> dict:
        """探测路由器类型（OpenWrt / 未知）。"""
        from . import router_api
        host = ip or netutil.default_gateway() or ""
        if not host:
            return {"ok": False, "msg": "探测不到默认网关，请手动填路由器 IP"}
        kind = router_api.detect_router(host)
        logbus.info("路由器探测：%s → %s" % (host, kind), tag="路由器")
        return {
            "ok": kind == "openwrt",
            "host": host,
            "type": kind,
            "msg": ("识别到 OpenWrt" if kind == "openwrt"
                    else "未识别为 OpenWrt，可能不支持自动配置，请手动改 hosts"),
        }

    def router_apply(self, ip: str = "", username: str = "root",
                     password: str = "") -> dict:
        """一键把推流域名写进 OpenWrt 的 hosts。"""
        from . import router_api
        host = ip or netutil.default_gateway() or ""
        if not host:
            return {"ok": False, "msg": "探测不到默认网关，请手动填路由器 IP"}
        if not self.lan_ip:
            return {"ok": False, "msg": "本机 IP 还没探测到，稍后再试"}
        logbus.step("正在配置路由器 hosts（%s → %s）…" % (host, self.lan_ip))
        luci = router_api.OpenWrtLUCI(host, username, password)
        if not luci.login():
            return {"ok": False, "msg": "路由器登录失败，检查用户名/密码"}
        ok = luci.set_hosts(self.lan_ip)
        if ok:
            logbus.ok("路由器 hosts 已指向 %s" % self.lan_ip, tag="路由器")
        return {"ok": ok, "host": host,
                "msg": "已写入路由器 hosts" if ok else "写入失败，请检查路由器权限"}

    def router_restore(self, ip: str = "", username: str = "root",
                       password: str = "") -> dict:
        """清掉路由器上我们写的 hosts。"""
        from . import router_api
        host = ip or netutil.default_gateway() or ""
        if not host:
            return {"ok": False, "msg": "探测不到默认网关"}
        luci = router_api.OpenWrtLUCI(host, username, password)
        if not luci.login():
            return {"ok": False, "msg": "路由器登录失败"}
        ok = luci.clear_hosts()
        return {"ok": ok, "host": host,
                "msg": "路由器 hosts 已清空" if ok else "清空失败"}

    def verify_dns(self, target: str = "", mode: str = "router") -> dict:
        where = "路由器" if mode == "router" else "本机内置 DNS"
        logbus.step("检测推流域名是否已指向本机（%s，验%s）…" % (self.lan_ip or "?", where))
        r = dnscheck.verify(self.lan_ip, target=target or None, mode=mode)
        r["builtin_running"] = bool(self.dns and self.dns.running)
        if r["ok"]:
            self.progress = paths.save_progress({"dns_verified": True, "step": 3,
                                                 "lan_ip": self.lan_ip})
            src = r.get("source") or {}
            self.cfg = paths.save_config({"DNS_MODE": "builtin" if src.get("kind") == "self"
                                          else "router"})
            logbus.ok(r["hint"])
        else:
            self.progress = paths.save_progress({"dns_verified": False})
            logbus.warn(r["hint"])
        return r

    def _pick_upstream(self) -> list:
        """上游优先级：用户填的加速器 DNS > 自动探测加速器 DNS > 本机系统 DNS > 兜底。"""
        ups = []
        ps5_dns = (self.cfg.get("PS5_NET") or {}).get("dns", "")
        cfg_up = self.cfg.get("DNS_UPSTREAM", "")
        for raw in (cfg_up, ps5_dns):
            for s in str(raw).replace("，", ",").split(","):
                s = s.strip()
                if s and s not in ups and netutil.classify_dns(s) != "none":
                    ups.append(s)
        for s in netutil.accelerator_dns():
            if s not in ups:
                ups.append(s)
        local_ip = {self.lan_ip, netutil.socket_lan_ip()}
        for s in netutil.system_dns():
            if s not in ups and s not in local_ip:
                ups.append(s)
        if not ups:
            ups = list(dnsserver.DEFAULT_UPSTREAM)
        return ups

    def enable_builtin_dns(self) -> dict:
        if self.dns and self.dns.running:
            ok, detail = self.dns.selftest()
            return {"ok": ok, "detail": detail, "upstreams": self.dns.upstreams}

        if self.divert:
            self.divert.stop()
            self.divert = None
            self.cfg = paths.save_config({"HIJACK_MODE": "dns"})
            logbus.info("已停掉端口劫持，改用内置 DNS", tag="DNS")

        if not self.lan_ip:
            return {"ok": False, "detail": "没有探测到本机局域网 IP，内置 DNS 无法答复"}

        port = int(self.cfg.get("DNS_PORT", 53))
        if not netutil.port_free(port, "udp"):
            pid, name = netutil.port_owner(port, "udp")
            who = ("%s（PID %d）" % (name, pid)) if pid else "其他程序"
            hint = "53 端口被 %s 占用。" % who
            if any(h in (name or "").lower() for h in ("uu", "xunyou", "leigod", "biubiu")):
                hint += "这是加速器自己接管了 DNS。请改用「路由器 hosts」方案（回到上一步）。"
            else:
                hint += "请关掉占用它的程序后重试，或改用「路由器 hosts」方案。"
            logbus.error(hint, tag="DNS")
            return {"ok": False, "detail": hint}

        ups = self._pick_upstream()
        answer_ip = self._dns_answer_ip()
        logbus.info("内置 DNS 将把推流域名答成 %s" % answer_ip, tag="DNS")
        dns_domains = sorted(set(dnsserver.REDIRECT_DOMAINS)
                             | set(self.cfg.get("HIJACK_DOMAINS", [])))
        self.dns = dnsserver.DnsServer(
            answer_ip=answer_ip,
            domains=dns_domains,
            upstreams=ups,
            port=port,
            verbose=False,
        )
        if not self.dns.start():
            detail = self.dns.last_error or "未知原因"
            logbus.error("内置 DNS 启动失败：%s" % detail, tag="DNS")
            self.dns = None
            return {"ok": False, "detail": detail}

        ok, detail = self.dns.selftest()
        self.cfg = paths.save_config({"DNS_MODE": "builtin"})
        self.progress = paths.save_progress({"dns_mode": "builtin", "dns_verified": True, "step": 3})
        threading.Thread(target=self._auto_start_https_relay,
                         args=(ups,), daemon=True).start()
        if ok:
            logbus.ok("内置 DNS 已启动：%s，上游 %s" % (detail, ",".join(ups)), tag="DNS")
            # 这里不再提示「上游不是加速器 DNS」：
            # 这个方案（内置 DNS）本来就是让 PS5 把 DNS 指向本机，
            # 上游只负责转发普通域名解析，跟加速器无关。
            # 那条提示只对「路由器 hosts」方案有意义，之前挂错地方了。
        else:
            logbus.error("内置 DNS 自检失败：%s" % detail, tag="DNS")
        return {"ok": ok, "detail": detail, "upstreams": ups}

    def _auto_start_https_relay(self, upstreams=None) -> None:
        """内置 DNS 模式配套：443 上起中继，把劫持域名的 HTTPS 转回真服务器。"""
        try:
            from . import https_relay
            _old = getattr(self, "_https_relay", None)
            if _old is not None and _old.is_running:
                return
            if _old is not None:
                try: _old.stop()
                except Exception: pass
                self._https_relay = None
            up_dns = ""
            for u in (upstreams or []):
                if u and netutil.classify_dns(u) != "none":
                    up_dns = u
                    break
            proxy = (self.cfg.get("TWITCH_PROXY") or "").strip()
            if not proxy:
                try:
                    from . import twitch_title as _tt
                    auto = _tt.detect_local_proxy()
                    if auto:
                        proxy = auto
                        logbus.info("代理栏留空，自动使用本机代理 %s" % auto, tag="中继")
                except Exception as e:
                    logbus.warn("探测本机代理失败：%s" % e, tag="中继")
            r = https_relay.HttpsRelay(port=443, upstream_dns=up_dns or "223.5.5.5",
                                       proxy=proxy)
            if r.start():
                self._https_relay = r
                if proxy:
                    logbus.ok("443 中继已启动，走代理 %s" % proxy, tag="中继")
                else:
                    logbus.warn("443 中继已启动，但没配代理 —— "
                                "被劫持域名的 HTTPS 在国内大概率转发不出去，"
                                "Twitch 可能打不开。去第 4 步填 Twitch 代理。", tag="中继")
            else:
                logbus.warn("443 中继没起来（端口多半被占），"
                            "被劫持域名的 HTTPS 会打不开", tag="中继")
        except Exception as e:
            logbus.warn("443 中继启动异常：%s" % e, tag="中继")

    def _stop_https_relay(self) -> None:
        r = getattr(self, "_https_relay", None)
        if r:
            try:
                r.stop()
            except Exception:
                pass
            self._https_relay = None

    def disable_builtin_dns(self) -> dict:
        if self.dns:
            self.dns.stop()
            self.dns = None
            self._stop_https_relay()
            logbus.info("内置 DNS 已停止", tag="DNS")
        self.cfg = paths.save_config({"DNS_MODE": "router"})
        return {"ok": True}

    def _divert_target(self) -> str:
        """改写目标：本机局域网 IP（绝不能用 127.0.0.1，会被当 martian 丢掉）。"""
        return divert.pick_target_ip(self.lan_ip, "")

    def enable_divert(self) -> dict:
        if not paths.caps()["divert"]:
            if paths.PLATFORM == "docker":
                detail = ("容器里没有端口劫持。这个能力要改本机的网络栈，只能在 Windows/macOS 上跑。"
                          "容器版靠路由器把推流域名或端口转到这台机器，"
                          "加速器要开在路由器上，PS5 保持自动。")
            else:
                detail = "当前平台不支持端口劫持。"
            self.hijack_error = detail
            logbus.error(detail, tag="劫持")
            return {"ok": False, "detail": detail, "unavailable": True}

        if self.divert and self.divert.running:
            return {"ok": True, "detail": "端口劫持已在运行", **self.divert.snapshot()}

        if not netutil.is_admin():
            if paths.IS_MACOS:
                detail = "没有管理员权限，流量接管开不了。请在终端用 sudo 启动：sudo /path/to/PS5LiveBridge"
            else:
                detail = "没有管理员权限，流量接管开不了。右键程序 → 以管理员身份运行。"
            self.hijack_error = detail
            logbus.error(detail, tag="劫持")
            return {"ok": False, "detail": detail}

        target = self._divert_target()
        if not target:
            detail = "没拿到本机 IP。先检查网络连接。"
            self.hijack_error = detail
            logbus.error(detail, tag="劫持")
            return {"ok": False, "detail": detail}

        ng = self.ensure_nginx()
        if not ng.get("ok"):
            detail = "收流服务没起来（%s），先排查这个。" % ng.get("detail", "")
            self.hijack_error = detail
            logbus.error(detail, tag="劫持")
            return {"ok": False, "detail": detail}

        ports = tuple(int(p) for p in (self.cfg.get("DIVERT_PORTS") or [1935, 6667]))
        ic = divert.PortInterceptor(ports=ports, target_ip=target, verbose=False)
        ok, err = ic.start()
        if not ok:
            self.hijack_error = err
            logbus.error("直播桥没开起来：%s" % err, tag="劫持")
            return {"ok": False, "detail": err}

        self.divert = ic
        self.hijack_error = ""
        self.cfg = paths.save_config({"HIJACK_MODE": "divert"})
        self.progress = paths.save_progress({"dns_verified": True, "lan_ip": self.lan_ip})
        logbus.ok("流量接管已开，%s 的连接都转到 %s"
                  % ("/".join(str(p) for p in ports), target), tag="劫持")
        if self.dns:
            self.dns.stop()
            self.dns = None
            logbus.info("内置 DNS 已停，直播桥模式下用不到", tag="DNS")
        return {"ok": True, "detail": "端口劫持已生效", **ic.snapshot()}

    def disable_divert(self) -> dict:
        if self.divert:
            self.divert.stop()
            self.divert = None
            logbus.info("端口劫持已停止（正在进行的推流不受影响）", tag="劫持")
        return {"ok": True}

    def divert_status(self) -> dict:
        caps = paths.caps()
        if not self.divert:
            return {"running": False, "available": bool(caps["divert"]) and divert.pyk_available(),
                    "target": self._divert_target(), "total": 0,
                    "error": self.hijack_error}
        return self.divert.snapshot()

    def hijack_state(self) -> dict:
        """
        给界面用的统一「劫持」视图：不管走 DNS 还是端口劫持，都从这里读。

        mode 决定第 2 步走哪条分支，界面不要自己猜。
        """
        d = self.divert_status()
        running = bool(d.get("running")) or bool(self.dns and self.dns.running)
        mode = self.cfg.get("HIJACK_MODE", "divert") if paths.caps()["divert"] else "router"
        ps5_seen = False
        try:
            if self.dns and self.dns.running:
                local = {self.lan_ip, netutil.socket_lan_ip(), "127.0.0.1", "::1"}
                ps5_seen = self.dns.stats.client_hit_from_other(local)
        except Exception:
            ps5_seen = False

        return {
            "mode": mode,
            "ps5_seen": ps5_seen,
            "running": running,
            "divert": d,
            "dns_running": bool(self.dns and self.dns.running),
            "error": self.hijack_error or d.get("error", ""),
        }

    def _dns_answer_ip(self) -> str:
        """
        A 记录该答哪个地址 —— 必须是「PS5 能直达的本机地址」。

        ⚠️ 这里绝不能答「加速器网卡 IP」—— 2026-10-26 真机踩过，代价很直观：
        UU 的网卡 IP 是 172.19.84.237，DNS 答这个地址后 PS5 确实连上了，
        但 UU 认为这是它自己的加速流量，直接从本机代理去了真 Twitch：
            netstat: 192.168.110.117:57206 -> 35.55.5.15:1935  by uu.exe
        本机 nginx 一个字节都没收到，PS5 那边还显示「正在直播」。
        所以 A 记录必须答物理网卡的局域网 IP —— 加速器对内网地址一般是直通的。

        注意区分两个概念：
          · 「PS5 的 DNS 填谁」= 能到达本机 53 的地址（加速器网卡 IP 实测能通）
          · 「A 记录答什么」  = 加速器不会抢走的地址（局域网 IP）
        两者可以不同，不要混。
        """
        return self.lan_ip

    def _follow_dns_answer(self) -> None:
        """
        让内置 DNS 的答复地址跟住「PS5 视角下这台电脑的地址」。

        典型场景就是标准流程：第 2B 步先把 DNS 起起来（此时加速器还没开，
        只能答局域网 IP），第 3 步才开加速器 —— 网卡一出现，这里就把 DNS
        重建一次，后续查询立刻答新地址。重建只需几十毫秒。
        """
        if not (self.dns and self.dns.running):
            return
        want = self._dns_answer_ip()
        if not want or want == self.dns.answer_ip:
            return
        old = self.dns.answer_ip
        self.dns.answer_ip = want
        logbus.ok("加速器网段已就绪，内置 DNS 答复地址 %s → %s（PS5 的 DNS 填 %s）"
                  % (old, want, want), tag="DNS")
        self.services.notify_ps5("DNS 已适配加速器网段")

    def dns_answer_hint(self) -> dict:
        """
        告诉用户「PS5 的 DNS 该填什么」，并给出备选。

        为什么要给备选：加速器给的那套参数里，网关有可能是本机、有可能是
        加速器自己的虚拟网关（不在本机）。哪种能通取决于加速器实现，
        软件无法从 PS5 侧验证，所以主推一个、备一个，让用户照现象换。
        """
        accel_ip = netutil.accelerator_gateway_ip()
        if accel_ip and accel_ip != self.lan_ip:
            return {"ip": accel_ip, "alt": self.lan_ip,
                    "note": ("填加速器网段里这台电脑的地址 %s（实测能到本机）；"
                             "如果 PS5 上不去网，改填 %s 再试。" % (accel_ip, self.lan_ip))}
        return {"ip": self.lan_ip, "alt": "",
                "note": "填这台电脑的 IP。若 PS5 提示连不上网，把加速器显示的「网关地址」填进 DNS 再试一次。"}

    def dns_status(self) -> dict:
        if not self.dns:
            return {"running": False}
        snap = self.dns.stats.snapshot()
        local_ips = {self.lan_ip, netutil.socket_lan_ip(), "127.0.0.1"}
        return {
            "running": self.dns.running,
            "bind": self.dns.bind_host,
            "port": self.dns.port,
            "upstreams": self.dns.upstreams,
            "ps5_seen": self.dns.stats.client_hit_from_other(local_ips),
            **{k: snap[k] for k in ("total", "hijacked", "forwarded", "failed",
                                    "clients", "recent", "uptime")},
        }

    def save_ps5_net(self, ip="", mask="", gateway="", dns="", accelerator="") -> dict:
        patch = {"PS5_NET": {"ip": ip, "mask": mask, "gateway": gateway, "dns": dns}}
        if accelerator:
            patch["ACCELERATOR"] = accelerator
        if dns:
            patch["DNS_UPSTREAM"] = dns
        self.cfg = paths.save_config(patch)
        answer_changed = False
        if self.dns and self.dns.running:
            want = self._dns_answer_ip()
            if want and want != self.dns.answer_ip:
                logbus.warn("加速器网段已就绪，内置 DNS 的答复地址从 %s 换成 %s"
                            % (self.dns.answer_ip, want), tag="DNS")
                self.dns.answer_ip = want
                answer_changed = True
        if self.dns and dns:
            changed = self.dns.set_upstreams(
                [s.strip() for s in str(dns).replace("，", ",").split(",") if s.strip()])
            health = dnscheck.upstream_health(dns)
            if changed:
                logbus.info("DNS 上游已切换为加速器 DNS：%s" % dns, tag="DNS")
            return {"ok": True, "upstream_updated": changed, "health": health,
                    "answer_changed": answer_changed,
                    "answer_hint": self.dns_answer_hint()}
        return {"ok": True, "upstream_updated": False, "answer_changed": answer_changed,
                "answer_hint": self.dns_answer_hint()}

    def autofill_ps5_net(self) -> dict:
        """
        给用户一个「用本机探测值填」的选项（加速器网关/本机 IP）。
        这里先清缓存：用户往往是「刚打开加速器」就点这个按钮，
        不清缓存会拿到 10 秒前的旧结果，误报「没检测到加速器」。
        """
        netutil.clear_cache("accel")
        netutil.clear_cache("adapters")
        accel_gw_ip = netutil.accelerator_gateway_ip()
        accel_dns = netutil.accelerator_dns()
        return {
            "gateway": accel_gw_ip or self.lan_ip,
            "dns": accel_dns[0] if accel_dns else "",
            "ip": "",
            "mask": "255.255.255.0",
            "note": ("已探测到加速器虚拟网卡 IP %s" % accel_gw_ip) if accel_gw_ip
                    else "没探测到加速器虚拟网卡，请照抄加速器界面上显示的值",
        }

    def qr_new(self) -> dict:
        r = self.bili.qr_generate()
        if not r.get("ok"):
            return {"ok": False, "msg": r.get("msg", "获取二维码失败")}
        self._qr = {
            "key": r["key"], "url": r["url"],
            "png": bilimod.qr_png_data_url(r["url"], box=7),
            "status": "waiting", "expire": time.time() + 170,
        }
        logbus.info("已生成 B 站登录二维码，用手机 B 站 App 扫码", tag="B站")
        return {"ok": True, "png": self._qr["png"], "expire": 170}

    def qr_poll(self) -> dict:
        if not self._qr.get("key"):
            return {"status": "idle"}
        if time.time() > self._qr.get("expire", 0) and self._qr["status"] == "waiting":
            self._qr["status"] = "expired"
            return {"status": "expired"}

        r = self.bili.qr_poll(self._qr["key"])
        status = r.get("status", "waiting")
        self._qr["status"] = status
        if status == "success":
            self.progress = paths.save_progress({"step": 5})
            if self.bili.room_id:
                self.services.start_danmaku(self.bili.room_id)
            self._apply_saved_area()
            self._clear_login_error()
            return {
                "status": "success",
                "uname": r.get("uname", ""),
                "room_id": r.get("room_id", 0),
                "uid": r.get("uid", 0),
            }
        return {"status": status}

    def _apply_saved_area(self) -> None:
        """登录后把上次用的分区/标题同步到 B 站房间（失败不阻塞流程）。"""
        try:
            area = self.cfg.get("BILIBILI_AREA_ID", "236")
            title = self.cfg.get("BILIBILI_TITLE", "")
            if area:
                self.bili.update_room(area_id=str(area))
            if title:
                self.bili.update_room(title=title)
        except Exception as e:
            logbus.warn("同步分区/标题失败：%s" % e, tag="B站")

    def logout(self) -> dict:
        self.services.stop_danmaku()
        self.bili.logout()
        self.progress = paths.save_progress({"step": 4})
        return {"ok": True}

    def partition_choices(self) -> dict:
        parts = self.bili.partitions()
        current = str(self.cfg.get("BILIBILI_AREA_ID", "236"))
        return {"partitions": parts, "current": current}

    def save_area(self, area_id: str, title: str = None,
                  twitch_channel: str = None,
                  twitch_proxy: str = None) -> dict:
        patch = {"BILIBILI_AREA_ID": str(area_id)}
        if title is not None:
            patch["BILIBILI_TITLE"] = title
        if twitch_channel is not None:
            patch["TWITCH_CHANNEL"] = (twitch_channel or "").strip()
        if twitch_proxy is not None:
            patch["TWITCH_PROXY"] = (twitch_proxy or "").strip()
        self.cfg = paths.save_config(patch)
        if self.bili.logged_in and title is not None:
            self.bili.update_room(area_id=str(area_id), title=title or None)
        return {"ok": True,
                "twitch_channel": self.cfg.get("TWITCH_CHANNEL", ""),
                "twitch_proxy": self.cfg.get("TWITCH_PROXY", "")}

    def _monitor_loop(self) -> None:
        """1 秒一拍：看 PS5 推没推、要不要自动开播。"""
        was_publishing = False
        last_pub = 0.0
        nginx_retry_at = 0.0
        while not self._stop:
            try:
                now = time.time()

                if not self.nginx.running() and now >= nginx_retry_at:
                    nginx_retry_at = now + 30
                    logbus.warn("收流服务不在了，重新拉起", tag="收流")
                    self.ensure_nginx()

                pub = self.nginx.publisher() if self.nginx.running() else {"publishing": False}
                publishing = bool(pub.get("publishing"))
                with self.lock:
                    self.live["ps5"] = pub

                if publishing:
                    last_pub = now
                    if not was_publishing:
                        self._on_ps5_publish(pub)
                elif last_pub and (now - last_pub) >= 3.0:
                    if self.live["state"] in ("local", "waiting", "starting",
                                              "relaying", "live", "error"):
                        self._on_ps5_unpublish()
                    last_pub = 0.0

                was_publishing = publishing

                self._follow_dns_answer()

                if self.live["state"] == "relaying":
                    self._confirm_bili_live()

            except Exception as e:
                logbus.warn("监控循环异常：%s" % e)
            time.sleep(1.0)

    def _on_ps5_publish(self, pub: dict) -> None:
        app = pub.get("app", "app")
        name = pub.get("name", "")
        res = "%dx%d" % (pub.get("width", 0), pub.get("height", 0)) \
            if pub.get("width") else "未知分辨率"
        logbus.ok("PS5 开始推流：%s/%s | %s @ %sfps | %s kbps"
                  % (app,
                     (name[:10] + "***") if len(name) > 10 else name,
                     res, pub.get("fps", 0) or "?", pub.get("bw_in", 0)),
                  tag="推流")

        if not self.push_bili():
            with self.lock:
                self.live["ps5"] = pub
                self._set_state("local")
                self.live["since"] = time.time()
                self.live["error"] = ""
                self.live["local"] = {"app": app, "name": name}
            logbus.ok("本地流已就绪：%s" % self.local_play_url(app, name), tag="推流")
            self.services.notify_ps5("画面已就绪，可在电脑上拉取")
            return

        with self.lock:
            self.live["ps5"] = pub
            self._set_state("starting" if self.cfg.get("AUTO_START_LIVE", True) else "waiting")
            self.live["since"] = time.time()
            self.live["error"] = ""

        if not self.bili.logged_in:
            self._fail_live("还没有登录 B 站，无法开播。请先完成第 4 步扫码。")
            return
        if not self.cfg.get("AUTO_START_LIVE", True):
            self.services.notify_ps5("PS5 已推流，等待你手动开播")
            return

        threading.Thread(target=self._start_bili_and_relay, args=(app, name),
                         name="start-live", daemon=True).start()

    def _on_ps5_unpublish(self) -> None:
        state = self.live["state"]
        logbus.warn("PS5 停止推流了", tag="推流")
        if state in ("relaying", "live", "starting", "face_verify"):
            self.stop_live(reason="PS5 停止推流")
        with self.lock:
            self._set_state("idle")
            self.live["ps5"] = {}
            self.live["local"] = {}
            self.live["since"] = 0.0
            self.live["face_url"] = ""

    def _fetch_twitch_title(self, stream_key: str = "") -> str:
        """
        从 Twitch 查 PS5 的直播标题。

        照搬 PSLinkB 的思路：Twitch 推流密钥长这样
            live_1348515979_DGbH2bro...
        `live_` 后面那段数字就是 broadcaster_id，拿它去问 GQL
        user(id){ lastBroadcast { title } } 就能拿到 PS5 上填的标题
        —— 不需要用户填频道名。

        PS5 的标题本来就是写给 Twitch 账号的（走 api.twitch.tv，我们不劫持
        那个域名），所以只能反过来问 Twitch。
        """
        try:
            from . import twitch_title
        except Exception:
            return ""

        px = (self.cfg.get("TWITCH_PROXY") or "").strip() or None

        bid = twitch_title.parse_broadcaster_id(stream_key)
        if bid:
            logbus.info("从 Twitch 查标题（broadcaster_id=%s）…" % bid, tag="标题")
            t = twitch_title.fetch_stream_title(stream_key, proxy=px)
            if t:
                logbus.ok("拿到 Twitch 标题：%s" % t, tag="标题")
                return t

        ch = (self.cfg.get("TWITCH_CHANNEL") or "").strip()
        if ch:
            t = twitch_title.fetch_twitch_title(ch, proxy=px)
            if t:
                logbus.ok("拿到 Twitch 标题（按频道名 %s）：%s" % (ch, t), tag="标题")
                return t

        if bid or ch:
            logbus.warn("没查到 Twitch 标题（PS5 还没设标题？或本地代理不通）", tag="标题")
        return ""

    def _sync_twitch_title(self) -> None:
        """
        开播成功后异步把 PS5 的标题补到 B 站。
        放在后台跑：抓取要走代理，可能慢，不能拖住开播主流程。
        """
        if (self.cfg.get("BILIBILI_TITLE") or "").strip():
            return
        name = (self.live.get("ps5") or {}).get("name", "")
        title = self._fetch_twitch_title(name)
        if not title:
            return
        try:
            r = self.bili.update_room(title=title)
            if r.get("ok"):
                logbus.ok("B 站直播间标题已同步：%s" % title, tag="标题")
                try:
                    self._store.update("runtime", last_title=title,
                                       title_at=time.time())
                except Exception:
                    pass
                try:
                    self._bus.emit_event(eventbus.Event(
                        eventbus.EventType.TITLE_SYNCED, {"title": title}))
                except Exception:
                    pass
            else:
                logbus.warn("标题同步失败：%s" % r.get("msg"), tag="标题")
        except Exception as e:
            logbus.warn("标题同步异常：%s" % e, tag="标题")

    def _start_bili_and_relay(self, app: str, name: str) -> None:
        """
        开播入口。这层是异常兜底：它在独立线程里跑，
        一旦抛未捕获异常线程就静默死掉，状态会永远卡在「正在开播」，
        界面上看起来就是点了开播后一直转圈、什么都不发生。
        """
        if not self.push_bili():
            logbus.warn("「推流到 B 站」是关着的，不会开播。要推请在第五步把它打开", tag="直播")
            return
        try:
            self._do_start_live(app, name)
        except Exception as e:
            logbus.error("开播流程异常：%s" % e, tag="直播")
            self._fail_live("开播流程异常：%s" % e)

    def _do_start_live(self, app: str, name: str) -> None:
        area = str(self.cfg.get("BILIBILI_AREA_ID", "236"))
        title = (self.cfg.get("BILIBILI_TITLE") or "").strip()
        logbus.step("正在让 B 站开播（分区 %s%s）…"
                    % (area, ("，标题 " + title) if title else "，标题稍后同步"))

        r = self.bili.start_live(area_id=area, title=title)
        if not r.get("ok"):
            code = r.get("code")
            if code in (60024, 60043):
                url = r.get("face_url") or ""
                self._enter_face_verify(url)
                return
            elif code == -101:
                self._fail_live("B 站登录已失效，请重新扫码登录（第 4 步）。")
            else:
                self._fail_live("B 站开播失败：%s（%s）" % (r.get("msg"), code))
            return

        addr = r["rtmp_addr"]
        code = r["rtmp_code"]
        with self.lock:
            self.live["bili"] = {
                "addr": addr, "code_tail": code[-6:] if code else "",
                "room_id": r.get("room_id", self.bili.room_id),
                "channel": r.get("channel", ""),
            }
            self._set_state("relaying")

        if not self.cfg.get("AUTO_RELAY", True):
            logbus.info("自动转发已关闭。B 站推流地址：%s / 密钥末六位 %s"
                        % (addr, code[-6:]), tag="转发")
            return

        out_url = addr.rstrip("/") + "/" + code.lstrip("/")

        self._protect_relay_target(out_url, addr)

        self._start_push(addr, code)

    def has_overlay(self) -> bool:
        """
        要不要烧字幕。只看悬浮文字。

        弹幕叠加已经下线：drawtext 的 reload 在 Windows 上会让 ffmpeg
        反复崩（0xC0000005 / 0xC0000374），画面断断续续没法用。
        B 站观众在播放器里本来就能看弹幕，不烧进画面不影响观看。
        """
        return bool((self.cfg.get("OVERLAY_TEXT") or "").strip())

    def _start_push(self, addr: str, code: str) -> bool:
        """
        统一的推流入口。

        两条路：
          · 没有叠加 → 用收流端自己的转发器，零转码，最轻
          · 有叠加   → ffmpeg 拉本机的流，把 ASS 字幕烧进画面再推 B 站
                       （必须重编码，走 QSV 硬编）
        """
        out_url = addr.rstrip("/") + "/" + code.lstrip("/")
        self._protect_relay_target(out_url, addr)

        # 缓存推流地址：改叠加参数要重启转码时直接复用，
        # 不要重走 B 站开播接口（会触发 60031 风控）
        self._last_out_url = out_url
        self._last_addr = addr
        self._last_code = code

        _to_bili = bool(self.cfg.get("OVERLAY_TO_BILI", True))
        _to_local = bool(self.cfg.get("OVERLAY_TO_LOCAL", True))

        # 两个都不勾 = 等同没开叠加，走零转码那条路
        if self.has_overlay() and (_to_bili or _to_local):
            pub = self.live.get("ps5") or {}
            w = int(pub.get("width") or 1920)
            h = int(pub.get("height") or 1080)
            try:
                self.overlay.write(self.cfg)
            except Exception as e:
                logbus.warn("生成叠加文本失败：%s" % e, tag="叠加")
            # 用 drawtext 烧字（不用 reload：reload 在 Windows 上会让 ffmpeg 崩）
            vf = self.overlay.filter_chain(self.cfg)
            if not vf:
                logbus.warn("叠加滤镜为空（可能缺中文字体），本次不叠加", tag="叠加")

            # rtmp-forwarder 只收推流、不提供 RTMP 播放（拉它会报 Already publishing），
            # 只能从它的 HTTP-FLV 接口拿流喂给 ffmpeg。
            _web = int(getattr(self.nginx, "web_port", 0) or 0)
            if _web:
                in_url = "http://127.0.0.1:%d/live/stream.flv" % _web
            else:
                in_url = "rtmp://127.0.0.1:%d/%s/%s" % (
                    int(self.cfg.get("RTMP_PORT", 1935)),
                    pub.get("app", "app"), pub.get("name", ""))
            br = int(pub.get("bw_in") or 0) or 6000
            bitrate = "%dk" % br

            # 「叠加到本地」才额外输出一路给 OBS 拉；
            # 本地画面预览不从这里取 —— 预览看的是 PS5 原始画面，
            # 叠加效果由前端覆盖层实时呈现，这样省掉一路编码。
            preview_url = ""
            if _to_local:
                try:
                    if not self.preview_srv.running():
                        self.preview_srv.start()
                    if self.preview_srv.running():
                        preview_url = "rtmp://127.0.0.1:19350/preview/ov"
                except Exception as e:
                    logbus.warn("本地叠加输出起不来：%s" % e, tag="叠加")

            target = out_url if _to_bili else (preview_url or out_url)

            # 推 B 站的时候不传 preview_url，强制单路输出。
            #
            # 原因：i3-8100T 的核显扛不住两个 h264_qsv session（会 0xC0000005 崩），
            # CPU 编码又跟不上实时（speed<1 直接卡死）。
            # 而本地预览本来就不需要烧字——它拉的是 PS5 原始流，叠加效果由
            # 前端覆盖层实时画。所以推 B 站时那一路没有存在的必要。
            _pv = "" if _to_bili else preview_url
            if not self.relay.start(in_url, target, vf=vf, bitrate=bitrate,
                                    preview_url=_pv):
                self._fail_live(self.relay.last_error or "叠加转发启动失败")
                return False
            self.relay.start_watchdog(
                on_drop=lambda reason: self._fail_live("转发中断：%s" % reason))
            logbus.ok("画面叠加已启用（%s + %s 重编码）"
                      % ("推 B 站" if _to_bili else "仅本地",
                         bitrate), tag="叠加")
            self.services.notify_ps5("B 站已开播，画面正在同步")
            return True

        if hasattr(self.nginx, "set_destination"):
            try:
                ok = self.nginx.set_destination(addr, code)
                if ok is False:
                    self._fail_live(self.nginx.last_error or "转发启动失败")
                    return False
                self.services.notify_ps5("B 站已开播，画面正在同步")
                return True
            except Exception as e:
                self._fail_live("转发启动失败：%s" % e)
                return False

        pub = self.live.get("ps5") or {}
        in_url = "rtmp://127.0.0.1:%d/%s/%s" % (
            int(self.cfg.get("RTMP_PORT", 1935)),
            pub.get("app", "app"), pub.get("name", ""))
        if not self.relay.start(in_url, out_url):
            self._fail_live(self.relay.last_error or "转发启动失败")
            return False
        self.relay.start_watchdog(
            on_drop=lambda reason: self._fail_live("转发中断：%s" % reason))
        self.services.notify_ps5("B 站已开播，画面正在同步")
        return True

    def _fix_relay_url(self, url: str) -> str:
        """
        推流地址的域名如果被本机解析指到了自己，换成真实公网 IP。

        为什么要这一步：路由器 hosts 里一旦带上 B 站推流域名（很多人会把
        Twitch 和 B 站域名一起抄进去），本机 ffmpeg 解析它就会得到自己的 IP，
        于是"推流给自己"，报 I/O error，直播永远起不来。用户不该为了这个
        去路由器上做精细的增删。
        """
        try:
            import socket
            rest = url.split("://", 1)[-1] if "://" in url else url
            host = rest.split("/", 1)[0].split(":", 1)[0].strip()
            if not host:
                return url

            my_ips = {self.lan_ip, netutil.socket_lan_ip(), "127.0.0.1"}
            try:
                local_ip = socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
            except Exception:
                return url

            if local_ip not in my_ips:
                return url

            from . import https_relay
            real = https_relay.resolve_real_ip(host, "223.5.5.5")
            if real and real not in my_ips:
                logbus.warn("推流地址 %s 被本机解析指到了自己（%s），"
                            "已自动换成真实 IP %s" % (host, local_ip, real), tag="转发")
                return url.replace(host, real, 1)
            logbus.error("推流地址 %s 被解析成本机 IP，且查不到真实 IP —— "
                         "请把路由器 hosts 里的 B 站域名删掉" % host, tag="转发")
            return url
        except Exception:
            return url

    def _protect_relay_target(self, out_url: str, addr: str = "") -> list:
        """
        把 B 站推流服务器的 IP 加进端口劫持白名单。

        为什么必须有：B 站（live-push.bilivideo.com）推流端口就是 1935，
        和 Twitch 的推流端口一模一样。端口劫持只认「目的端口 1935」，
        分不清是 PS5 往上推还是 ffmpeg 往 B 站推 —— 于是 ffmpeg 的推流
        被劫回本机 nginx，自己推给自己，实测报：
            Error opening output rtmp://live-push.bilivideo.com/... : I/O error

        做法：把推流地址里的主机名解析成全部 A 记录，全部放行。
        一个不留 —— 只放行其中一个的话，ffmpeg 换到另一个 IP 就又死了。
        """
        ips = []
        host = ""
        try:
            rest = out_url.split("://", 1)[-1] if "://" in out_url else out_url
            host = rest.split("/", 1)[0].split(":", 1)[0].strip()
        except Exception:
            host = ""

        if host:
            try:
                import socket
                for info in socket.getaddrinfo(host, None, socket.AF_INET):
                    ip = info[4][0]
                    if ip and ip not in ips:
                        ips.append(ip)
            except Exception as e:
                logbus.warn("解析 B 站推流地址失败（%s）：%s" % (host, e), tag="转发")

        if not ips:
            logbus.warn("没解析到 B 站推流 IP，转发可能推不出去", tag="转发")
            return []

        if self.divert:
            cur = self.divert.set_exclude(ips, replace=True)
            logbus.info("B 站推流 IP 已放行：%s" % ", ".join(ips), tag="劫持")
            return cur
        return ips

    def _confirm_bili_live(self) -> None:
        """B 站 live_status 变成 1 才算真的开播成功。"""
        self.live["confirm_tries"] += 1
        tries = self.live["confirm_tries"]
        if tries % 3 != 1:
            return
        status = self.bili.live_status()
        if status == 1:
            with self.lock:
                self._set_state("live")
                self.live["since"] = time.time()
            logbus.ok("B 站直播间已开播 —— 现在可以看了", tag="直播")
            self.progress = paths.save_progress({"step": 6})
            threading.Thread(target=self._sync_twitch_title, daemon=True).start()
            def _notify_after_join():
                for _ in range(40):
                    if self.services.ps5_connected:
                        break
                    time.sleep(0.5)
                if not self.services.ps5_connected:
                    logbus.warn("PS5 弹幕通道没连上，开播提示发不出去", tag="IRC")
                    return
                time.sleep(1.0)
                self.services.notify_ps5("B 站直播间已开播")
                time.sleep(1.5)
                ok = self.services.call(
                    self.services.irc.notify("正在直播自动发送"), timeout=3)
                if ok:
                    logbus.ok("已向 PS5 画面发送测试弹幕", tag="IRC")
                else:
                    logbus.warn("测试弹幕发送失败（PS5 聊天层未就绪）", tag="IRC")
            threading.Thread(target=_notify_after_join, daemon=True).start()
        elif status == 2:
            logbus.info("B 站显示轮播中，等待切换为直播…", tag="直播")
        elif tries >= 30:
            logbus.warn("等了 30 秒 B 站仍显示未开播。可能是推流地址/密钥不对，"
                        "或 B 站审核延迟。", tag="直播")

    def _fail_live(self, msg: str) -> None:
        logbus.error(msg, tag="直播")
        with self.lock:
            self._set_state("error")
            self.live["error"] = msg
            self.live["since"] = 0.0
        self.services.notify_ps5("开播失败，详见电脑上的提示")

    def _enter_face_verify(self, face_url: str = "") -> None:
        """
        B 站要求人脸验证（60024/60043）。

        不直接 _fail_live，而是进「等待验证」状态 + 后台自动重试：
        用户在手机上完成人脸后，下次重试就能拿到推流地址。
        face_url 是一次性 QR，过期了重试会拿到新的。
        """
        logbus.warn("B 站要求人脸验证，等待完成后自动重试", tag="B站")
        with self.lock:
            self._set_state("face_verify")
            self.live["error"] = ""
            self.live["face_url"] = face_url
            self.live["since"] = 0.0
        self.services.notify_ps5("B 站要求人脸验证，请在手机上完成")
        if not getattr(self, "_face_retry_thread", None) or not self._face_retry_thread.is_alive():
            self._face_retry_thread = threading.Thread(
                target=self._face_verify_retry_loop, daemon=True, name="face-verify-retry")
            self._face_retry_thread.start()

    def _face_verify_retry_loop(self) -> None:
        """
        人脸验证自动重试（参照 PSLinkB 的 face_watcher 设计）。
        - 前段：每 1 秒查 check_face_status，Verified 就立即开播
        - fallback：验证过后/查询异常时，每 10 秒盲试开播
        - 超时 3 分钟
        """
        start = time.time()
        fallback = False

        while True:
            if time.time() - start > 180:
                self._fail_live("人脸验证等待超时（3 分钟）。请在手机上完成验证后点「手动开播」。")
                return
            if self.live.get("state") != "face_verify":
                return
            if not self.bili.logged_in:
                self._fail_live("登录已失效，请重新扫码（第 4 步）。")
                return

            try:
                if not fallback:
                    time.sleep(1)
                    status = self.bili.check_face_status()
                    if status == "verified":
                        logbus.ok("人脸验证已完成，正在开播…", tag="B站")
                        fallback = True
                    elif str(status).startswith("error"):
                        logbus.warn("查询人脸状态异常（%s），切 fallback 模式" % status, tag="B站")
                        fallback = True
                else:
                    time.sleep(10)

                area = str(self.cfg.get("BILIBILI_AREA_ID", "236"))
                title = self.cfg.get("BILIBILI_TITLE", "")
                r = self.bili.start_live(area_id=area, title=title)
            except Exception as e:
                logbus.warn("人脸重试遇到异常（%s），2 秒后继续" % e, tag="B站")
                time.sleep(2)
                continue

            if r.get("ok"):
                logbus.ok("人脸验证通过，B 站已开播", tag="B站")
                threading.Thread(target=self._relay_after_start, args=(r,),
                                 daemon=True).start()
                return
            code = r.get("code")
            if code in (60024, 60043):
                url = r.get("face_url") or ""
                with self.lock:
                    self.live["face_url"] = url
                continue
            self._fail_live("B 站开播失败：%s（%s）" % (r.get("msg"), code))
            return

    def _relay_after_start(self, r: dict) -> None:
        """转发启动入口（在线程里跑，异常必须兜住）。"""
        try:
            self._do_relay_after_start(r)
        except Exception as e:
            logbus.error("转发启动异常：%s" % e, tag="转发")
            self._fail_live("转发启动异常：%s" % e)

    def _do_relay_after_start(self, r: dict) -> None:
        """start_live 成功后的转发启动（从 _start_bili_and_relay 后半段拆出来复用）。"""
        addr = r["rtmp_addr"]
        code = r["rtmp_code"]
        channel = r.get("channel", "")
        with self.lock:
            self.live["bili"] = {
                "addr": addr, "code_tail": code[-6:] if code else "",
                "room_id": r.get("room_id", self.bili.room_id),
                "channel": channel,
            }
            self._set_state("relaying")
            self.live["error"] = ""

        if not self.cfg.get("AUTO_RELAY", True):
            logbus.info("自动转发已关闭。B 站推流地址已获取。", tag="转发")
            return

        out_url = addr.rstrip("/") + "/" + code.lstrip("/")
        out_url = self._fix_relay_url(out_url)
        self._protect_relay_target(out_url, addr)

        if not self._start_push(addr, code):
            return
        threading.Thread(target=self._confirm_stream, daemon=True).start()

    def _confirm_stream(self, timeout: float = 5.0, retry: bool = True) -> None:
        """
        流确认（v5.0.0）：开播后确认流到达 B 站。
        超时未确认 → 重试一次 → 再不行 cleanup。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(1)
            try:
                status = self.bili.live_status()
                if status == 1:
                    logbus.ok("流已确认到达 B 站", tag="转发")
                    return
            except Exception:
                pass
        if retry:
            logbus.warn("流确认超时，重试一次…", tag="转发")
            self._confirm_stream(timeout=timeout, retry=False)
        else:
            logbus.error("流确认失败（重试后仍超时），自动清理", tag="转发")
            self.cleanup(was_live=True, reason="流确认失败")

    def start_live_manual(self) -> dict:
        """手动开播（自动模式失败或用户关掉自动时用）。"""
        pub = self.nginx.publisher()
        if not pub.get("publishing"):
            return {"ok": False, "msg": "PS5 还没推流，先在 PS5 上开直播"}
        if not self.bili.logged_in:
            return {"ok": False, "msg": "请先扫码登录 B 站"}
        with self.lock:
            self._set_state("starting")
            self.live["error"] = ""
        threading.Thread(target=self._start_bili_and_relay,
                         args=(pub.get("app", "app"), pub.get("name", "")),
                         name="start-live-manual", daemon=True).start()
        return {"ok": True}

    def output_mode(self) -> str:
        return "local" if self.cfg.get("OUTPUT_MODE") == "local" else "direct"

    def push_bili(self) -> bool:
        """
        是否把流转推到 B 站。

        旧配置只有 OUTPUT_MODE（direct/local 二选一），这里做一次兼容：
        没写过 PUSH_BILI 的老用户，按 OUTPUT_MODE 推断。
        """
        if "PUSH_BILI" in self.cfg:
            return bool(self.cfg.get("PUSH_BILI"))
        return self.cfg.get("OUTPUT_MODE") != "local"

    def local_stream(self) -> bool:
        return bool(self.cfg.get("LOCAL_STREAM", True))

    def set_outputs(self, push_bili=None, local_stream=None) -> dict:
        upd = {}
        if push_bili is not None:
            upd["PUSH_BILI"] = bool(push_bili)
        if local_stream is not None:
            upd["LOCAL_STREAM"] = bool(local_stream)
        if not upd:
            return {"ok": False, "msg": "没有要改的"}
        self.cfg = paths.save_config(upd)
        logbus.info("输出方式：推 B 站 %s / 本地流 %s"
                    % ("开" if self.push_bili() else "关",
                       "开" if self.local_stream() else "关"), tag="推流")
        return {"ok": True, "push_bili": self.push_bili(),
                "local_stream": self.local_stream()}

    def local_play_url(self, app: str = "", name: str = "") -> str:
        """
        给 OBS 之类软件拉的地址。

        优先用局域网 IP：OBS 和本程序不在同一台机器上时也能拉。
        传空则取当前正在推的流。
        """
        cur = self.live.get("local") or {}
        app = app or cur.get("app") or "app"
        name = name or cur.get("name") or ""
        if not name:
            ps5 = self.live.get("ps5") or {}
            app = ps5.get("app") or app
            name = ps5.get("name") or ""
        host = self.lan_ip or "127.0.0.1"
        # 「叠加到本地」开着且预览端在跑，就给烧过字的那一路
        try:
            if (self.cfg.get("OVERLAY_TO_LOCAL", True)
                    and self.has_overlay()
                    and self.preview_srv.running()):
                return "rtmp://%s:19350/preview/ov" % host
        except Exception:
            pass
        port = int(self.cfg.get("RTMP_PORT", 1935))
        return "rtmp://%s:%d/%s/%s" % (host, port, app, name)

    def set_output_mode(self, mode: str) -> dict:
        """
        切换输出方式。正在直播时先收尾，避免 B 站挂着没有画面的直播。
        """
        mode = "local" if str(mode) == "local" else "direct"
        if mode == self.output_mode():
            return {"ok": True, "mode": mode, "unchanged": True}
        if self.live["state"] in ("relaying", "live", "starting"):
            self.stop_live(reason="切换输出方式")
        self.cfg = paths.save_config({"OUTPUT_MODE": mode})
        if mode == "local":
            logbus.info("已切换为本地取流：PS5 推流后不再自动开播 B 站", tag="推流")
        else:
            logbus.info("已切换为直接推流：PS5 推流后自动开播并转发到 B 站", tag="推流")

        try:
            pub = self.nginx.publisher() if self.nginx.running() else {"publishing": False}
            if pub.get("publishing"):
                self._on_ps5_publish(pub)
        except Exception as e:
            logbus.warn("切换输出方式后接管当前推流失败：%s" % e, tag="推流")
        return {"ok": True, "mode": mode}

    def cleanup(self, was_live: bool = True, reason: str = "") -> None:
        """
        统一清理（v5.0.0）。参照 PSLinkB 的 cleanup 模式。
        先清状态（防止竞态），再调外部 API。
        """
        if reason:
            logbus.step("清理：%s" % reason)
        with self.lock:
            old_state = self.live.get("state", "")
            old_bili = dict(self.live.get("bili") or {})
            self._set_state("idle")
            self.live["bili"] = {}
            self.live["confirm_tries"] = 0
            self.live["face_url"] = ""
        _clear = getattr(self.nginx, "clear_destination", None)
        if _clear:
            try:
                _clear()
            except Exception:
                pass
        try:
            self.relay.stop()
        except Exception:
            pass
        if was_live and old_state in ("relaying", "live", "starting", "face_verify"):
            channel = old_bili.get("channel") or getattr(self.bili, "last_channel", "")
            for attempt in range(3):
                try:
                    r = self.bili.stop_live(channel=channel)
                    if r.get("ok"):
                        break
                    logbus.warn("关播失败（第 %d 次）：%s" % (attempt + 1, r.get("msg")), tag="B站")
                    time.sleep(1)
                except Exception as e:
                    logbus.warn("关播异常（第 %d 次）：%s" % (attempt + 1, e), tag="B站")
                    time.sleep(1)
        try:
            self.services.notify_ps5("B 站已关播")
        except Exception:
            pass

    def stop_live(self, reason: str = "") -> dict:
        self.cleanup(was_live=True, reason=reason or "停止直播")
        return {"ok": True}

    def _inject_raw(self, user: str, text: str) -> None:
        """限速队列真正发消息时回调这里。"""
        if self.services.ps5_connected:
            self.services.inject(user, text)

    def _log_state_change(self, ev) -> None:
        """事件总线订阅：状态变化记一行日志（旁路，不参与决策）。"""
        d = ev.data or {}
        logbus.info("状态 %s → %s" % (d.get("from", "?"), d.get("to", "?")), tag="状态")

    def _set_state(self, new: str, force: bool = False) -> None:
        """
        统一状态入口。

        用状态机校验转换是否在白名单里：不在就记一条警告再放行 ——
        历史流程里有几处跨状态直跳，硬拒会把链路卡死；校验的价值是
        把异常路径暴露到日志，而不是阻断业务。
        """
        with self.lock:
            old = self.live.get("state", "idle")
            if old == new:
                return
            target = statemachine.to_state(new)
            ok = self._sm.transition(target, force=force)
            if not ok:
                self._sm.transition(target, force=True)
            self.live["state"] = new
        if not ok:
            logbus.warn("状态跳变不在白名单：%s → %s（已放行并同步）" % (old, new), tag="状态")
        try:
            self._bus.emit_event(eventbus.Event(
                eventbus.EventType.STATE_CHANGED, {"from": old, "to": new}))
        except Exception:
            pass

    def _on_lan_ip_change(self, old_ip: str, new_ip: str) -> None:
        """本机 IP 变了：界面提示、DNS 答复地址都要跟着换。"""
        logbus.warn("本机 IP 变化：%s → %s" % (old_ip or "?", new_ip), tag="网络")
        self.lan_ip = new_ip
        try:
            if self.dns and getattr(self.dns, "answer_ip", None) is not None:
                self.dns.answer_ip = new_ip
        except Exception:
            pass

    def _on_danmaku(self, item: dict) -> None:
        # 弹幕只做转发，不烧进画面。
        # 烧弹幕靠 drawtext 读文件，而 drawtext 在 Windows 上做动态文字必崩
        #（reload 会让 ffmpeg 0xC0000005），所以这个功能已经下线。
        self.danmaku_total += 1
        if item["kind"] in ("gift", "guard", "sc"):
            self.gift_total += 1
        self.danmaku_recent.appendleft(item)
        try:
            self._dm_history.append(
                item.get("kind", "danmaku"), item.get("user", ""),
                item.get("text", ""),
                {"price": item.get("price", 0)} if item.get("price") else None)
        except Exception:
            pass

        # ── 弹幕日志 ──
        # 记「收到」和「转发」两个动作，出问题时能一眼看出断在哪一段：
        # 只有「收到」没有「转发」→ 是 PS5 没连上
        # 两个都有但画面没弹幕 → PS5 那边的问题
        _u = item.get("user") or ""
        _x = item.get("text") or ""
        _k = item.get("kind") or "danmaku"
        _label = {"danmaku": "弹幕", "gift": "礼物",
                  "guard": "上舰", "sc": "醒目留言"}.get(_k, _k)
        logbus.info("收到%s %s：%s" % (_label, _u, _x), tag="弹幕")

        if not self.services.ps5_connected:
            logbus.warn("PS5 没连上弹幕通道，%s 的这条没转发" % (_u or "观众"),
                        tag="弹幕")
            return
        kind = item["kind"]
        if kind == "danmaku":
            self._dm_throttle.push(item["user"], item["text"], priority=0)
        elif kind == "gift":
            self._dm_throttle.push(item["user"], "送出 %s" % item["text"], priority=1)
        elif kind == "guard":
            self._dm_throttle.push(item["user"], item["text"], priority=1)
        elif kind == "sc":
            self._dm_throttle.push(item["user"],
                                   "醒目留言 ¥%s：%s" % (item["price"] // 1000,
                                                        item["text"]),
                                   priority=1)

        logbus.info("转发→PS5 %s：%s" % (_u, _x), tag="弹幕")

    def _on_ps5_join(self, channel: str) -> None:
        self.progress = paths.save_progress({"ps5_joined": time.time()})

    def _on_ps5_drop(self) -> None:
        logbus.info("PS5 的弹幕连接断开了", tag="IRC")

    def test_inject(self, text: str = "") -> dict:
        text = (text or "").strip() or "这是一条来自 PS5 直播桥的测试弹幕"
        if not self.services.ps5_connected:
            return {"ok": False, "msg": "PS5 还没连上弹幕通道（先让 PS5 开始直播）"}
        self.services.inject("测试", text)
        return {"ok": True}

    def _publisher_view(self, live: dict) -> dict:
        """
        PS5 推流信息。uptime 自己算，不用 nginx stat 给的那个累计值。
        """
        pub = dict(live.get("ps5") or {})
        if pub.get("publishing"):
            since = live.get("since") or 0
            if since:
                pub["uptime"] = max(0, int(time.time() - since))
            else:
                pub["uptime"] = 0
        return pub

    def netcheck(self, force: bool = False) -> dict:
        """
        运行状态面板要的网络检查。60 秒缓存，别让每次 tick 都去打网络。

        三项分开测：代理本身、Twitch、GitHub。Twitch 必须过代理，
        GitHub 直连或代理都算通 —— 国内直连 api.github.com 一般能通。
        """
        import time as _t
        now = _t.time()
        if not force and getattr(self, "_nc_cache", None) \
                and now - getattr(self, "_nc_at", 0) < 60:
            return self._nc_cache

        out = {
            "proxy": {"configured": "", "ok": False, "ms": 0},
            "twitch": {"ok": False, "ms": 0, "detail": ""},
            "github": {"ok": False, "ms": 0, "detail": ""},
            "version": {"current": paths.APP_VERSION, "latest": "",
                        "is_latest": None},
        }

        proxy = (self.cfg.get("TWITCH_PROXY") or "").strip()
        out["proxy"]["configured"] = proxy

        def _probe(url, use_proxy):
            import urllib.request
            t0 = _t.time()
            try:
                handlers = []
                if use_proxy and proxy:
                    handlers.append(urllib.request.ProxyHandler(
                        {"http": proxy, "https": proxy}))
                else:
                    handlers.append(urllib.request.ProxyHandler({}))
                op = urllib.request.build_opener(*handlers)
                req = urllib.request.Request(
                    url, headers={"User-Agent": "Mozilla/5.0 PS5LiveBridge"})
                with op.open(req, timeout=14) as r:
                    r.read(256)
                return True, int((_t.time() - t0) * 1000), ""
            except Exception as e:
                return False, int((_t.time() - t0) * 1000), str(e)[:80]

        # 代理本身：直接连它的端口
        if proxy:
            try:
                from urllib.parse import urlparse
                u = urlparse(proxy if "://" in proxy else "http://" + proxy)
                host, port = u.hostname, u.port or 80
                import socket as _s
                t0 = _t.time()
                with _s.create_connection((host, port), timeout=4):
                    pass
                out["proxy"]["ok"] = True
                out["proxy"]["ms"] = int((_t.time() - t0) * 1000)
            except Exception as e:
                out["proxy"]["ok"] = False
                out["proxy"]["ms"] = 0

        ok, ms, err = _probe("https://www.twitch.tv/", True)
        if not ok and ("401" in err or "403" in err):
            ok = True
            err = ""
        out["twitch"] = {"ok": ok, "ms": ms, "detail": err}

        # GitHub 先试直连，不行再过代理（国内直连通常更快）
        ok, ms, err = _probe("https://api.github.com/", False)
        if not ok and proxy:
            ok2, ms2, err2 = _probe("https://api.github.com/", True)
            if ok2:
                ok, ms, err = True, ms2, err2 + " (走代理)"
        out["github"] = {"ok": ok, "ms": ms, "detail": err}

        try:
            import json as _j, urllib.request as _u
            url = ("https://api.github.com/repos/IceNoproblem/"
                   "PS5LiveBridge/releases/latest")
            op = _u.build_opener(_u.ProxyHandler({}))
            req = _u.Request(url, headers={
                "User-Agent": "PS5LiveBridge",
                "Accept": "application/vnd.github+json"})
            with op.open(req, timeout=8) as r:
                data = _j.loads(r.read().decode("utf-8", "ignore"))
            tag = (data.get("tag_name") or "").lstrip("vV")
            if tag:
                out["version"]["latest"] = tag
                cur = paths.APP_VERSION
                if tag == cur:
                    out["version"]["is_latest"] = True
                else:
                    # 本地可能比线上新（开发版还没发），这种情况也算"已最新"
                    try:
                        from .updater import parse_ver
                        out["version"]["is_latest"] = parse_ver(cur) >= parse_ver(tag)
                    except Exception:
                        out["version"]["is_latest"] = False
        except Exception as e:
            out["version"]["detail"] = str(e)[:60]

        self._nc_cache = out
        self._nc_at = now
        return out

    def snapshot(self) -> dict:
        # 443 中继只在「点按钮启动内置 DNS」时被拉起，重启后会丢。
        # 这里做一次性恢复：DNS_MODE 是 builtin 但中继没在跑，就补起来。
        if not getattr(self, "_hr_checked", False):
            self._hr_checked = True
            _dns_ok = bool(self.dns and self.dns.running)
            _cur = getattr(self, "_https_relay", None)
            _hr_ok = bool(_cur is not None and _cur.is_running)
            if self.cfg.get("DNS_MODE") == "builtin" and not _dns_ok:
                # 重启后内置 DNS 不会自己回来（它只在点按钮时启动）。
                # 这里补一次，起来后它会顺手把 443 中继拉起。
                threading.Thread(target=self.enable_builtin_dns,
                                 daemon=True).start()
            elif not _hr_ok:
                threading.Thread(target=self._auto_start_https_relay,
                                 kwargs={"upstreams": None}, daemon=True).start()

        _hr = getattr(self, "_https_relay", None)
        _hr_on = bool(_hr is not None and _hr.is_running)
        with self.lock:
            live = dict(self.live)
            live["bili"] = dict(live.get("bili") or {})
            if not live["bili"].get("channel"):
                live["bili"]["channel"] = getattr(self.bili, "last_channel", "")
        dns_state = self.dns_status()
        if hasattr(self.nginx, "forwarding"):
            relay_running = self.nginx.forwarding
            relay_uptime = self.nginx.uptime
            relay_error = self.nginx.last_error
        else:
            relay_running = self.relay.running
            relay_uptime = self.relay.uptime
            relay_error = self.relay.last_error or self.nginx.last_error
        return {
            "caps": paths.caps(),
            "platform": paths.PLATFORM,
            "version": paths.APP_VERSION,
            "step": self.progress.get("step", 1),
            "progress_dns_ok": bool(self.progress.get("dns_verified"))
                               or bool(self.dns and self.dns.running)
                               or bool(self.divert and self.divert.running),
            "lan_ip": self.lan_ip,
            "gateway": netutil.default_gateway(),
            "dns_mode": self.cfg.get("DNS_MODE", "router"),
            "dns_answer": self.dns_answer_hint(),
            "hijack": self.hijack_state(),
            "outputs": {"push_bili": self.push_bili(),
                        "local_stream": self.local_stream(),
                        "overlay": self.has_overlay()},
            "rtmp_impl": "rtmpf" if getattr(self, "native_relay", False) else "nginx",
            "services": {
                "dns": dns_state,
                "irc": {"running": self.services.irc.running,
                        "ps5": self.services.ps5_connected},
                "nginx": {"running": self.nginx.running(),
                          "publisher": self._publisher_view(live)},
                # 这里原来只有一个 running，但它是 nginx.forwarding（rtmpf 的转发状态），
                # 跟 ffmpeg 编码进程没关系。排查时拿它判断 ffmpeg 死活会完全看错，
                # 所以把两个状态分开暴露：
                #   encoder    → ffmpeg 编码进程在不在（推 B 站那一路）
                #   forwarding → rtmpf 收流端有没有在转发
                "relay": {"running": relay_running,
                          "encoder": self.relay.running,
                          "forwarding": getattr(self.nginx, "forwarding", None),
                          "uptime": relay_uptime,
                          "restarts": getattr(self.relay, "restarts", 0),
                          "error": relay_error},
            "https_relay": {"running": _hr_on, "port": 443},
                "danmaku": {"connected": self.services.danmaku_connected,
                            "count": self.danmaku_total, "gift": self.gift_total},
            },
            "bili": {
                "logged_in": self.bili.logged_in,
                "uname": self.bili.uname,
                "uid": self.bili.uid,
                "room_id": self.bili.room_id,
                "area_id": self.cfg.get("BILIBILI_AREA_ID", "236"),
                "title": ((self.cfg.get("BILIBILI_TITLE") or "").strip()
                          or self._store.get("runtime", "last_title") or ""),
            },
            "live": {
                "state": live.get("state", "idle"),
                "error": live.get("error", ""),
                "since": live.get("since", 0),
                "bili": live.get("bili", {}),
                "ps5": live.get("ps5", {}),
                "face_url": live.get("face_url", ""),
                "output_mode": self.output_mode(),
                "local_url": self.local_play_url(),
                "uptime": (int(time.time() - live["since"])
                           if live.get("since") and live.get("state")
                           in ("local", "relaying", "live") else 0),
            },
            "danmaku_recent": list(self.danmaku_recent)[:20],
            "config": {
                "auto_start_live": bool(self.cfg.get("AUTO_START_LIVE", True)),
                "auto_relay": bool(self.cfg.get("AUTO_RELAY", True)),
                "ps5_net": self.cfg.get("PS5_NET", {}),
                "twitch_channel": self.cfg.get("TWITCH_CHANNEL", ""),
                "twitch_proxy": self.cfg.get("TWITCH_PROXY", ""),
            },
        }

    def set_flags(self, auto_start=None, auto_relay=None, twitch_channel=None) -> dict:
        patch = {}
        if auto_start is not None:
            patch["AUTO_START_LIVE"] = bool(auto_start)
        if auto_relay is not None:
            patch["AUTO_RELAY"] = bool(auto_relay)
        if twitch_channel is not None:
            patch["TWITCH_CHANNEL"] = str(twitch_channel)
            self.services.set_channel(str(twitch_channel) or "ps5")
        if patch:
            self.cfg = paths.save_config(patch)
        return {"ok": True}

    def goto_step(self, step: int) -> dict:
        step = max(1, min(6, int(step)))
        self.progress = paths.save_progress({"step": step})
        return {"ok": True, "step": step}

    def ensure_nginx(self) -> dict:
        if self.nginx.running():
            return {"ok": True, "detail": "收流服务已在运行"}
        ok = self.nginx.start()
        return {"ok": ok, "detail": self.nginx.last_error or "收流服务已启动"}
