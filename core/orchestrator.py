# -*- coding: utf-8 -*-
"""
core/orchestrator.py —— 大脑：把六个模块串成一条可用的流水线

它管两件事，不要混：
    1. 向导进度（用户走到第几步、DNS 走哪条分支）—— 给人看的
    2. 直播状态机（PS5 有没有推、B 站有没有开、流转发没有）—— 给机器跑的

直播状态机（对应 PSLinkB 的 Idle -> Live，但加了 B 站开播确认）：
    idle          什么都没发生
    waiting       在等 PS5 推流（nginx 上没有 publisher）
    starting      拿到 PS5 流了，正在调 B 站开播接口
    relaying      B 站给了推流地址，ffmpeg 正在转发
    live          B 站 live_status == 1，真正在直播（PS5 画面显示「正在直播」）
    error         出错了，需要人看
"""
from __future__ import annotations

import threading
import time
from collections import deque

from . import bili as bilimod
from . import (divert, dnscheck, dnsserver, logbus, netutil, nginxmgr, paths,
               relay, routercfg, services)


class Orchestrator(object):
    def __init__(self):
        self.cfg = paths.load_config()
        self.progress = paths.load_progress()
        self.bili = bilimod.Bilibili()
        self.services = services.Services(
            self.bili,
            on_danmaku=self._on_danmaku,
            on_ps5_join=self._on_ps5_join,
            on_ps5_drop=self._on_ps5_drop,
        )
        self.nginx = nginxmgr.NginxManager(
            rtmp_port=int(self.cfg.get("RTMP_PORT", 1935)),
            stat_port=int(self.cfg.get("STAT_PORT", 8080)),
        )
        self.relay = relay.Relay()
        self.dns = None
        self.divert = None             # 端口劫持（配合加速器时的主方案）
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

    # ══════════════════════════════════════════════
    # 启动 / 停止
    # ══════════════════════════════════════════════
    def start(self) -> None:
        logbus.step("PS5 直播桥启动（%s），本机 IP：%s"
                    % (paths.PLATFORM, self.lan_ip or "未探测到"))
        if not self.lan_ip:
            logbus.error("没有探测到局域网 IP，PS5 无法连到本机。请检查网络。")

        # 弹幕通道（IRC）先起来，PS5 随时可能来连
        self.services.set_channel(self.cfg.get("TWITCH_CHANNEL") or "ps5")
        self.services.start(int(self.cfg.get("IRC_PORT", 6667)))

        if self.bili.logged_in:
            threading.Thread(target=self._verify_login_bg, daemon=True).start()

        # 端口劫持：只有桌面版能用。容器里容器动不了宿主机的网络栈，
        # 那种场景靠路由器把域名/端口转过来，不需要（也没法）劫持。
        if paths.caps()["divert"] and self.cfg.get("HIJACK_MODE", "divert") == "divert":
            # 放到后台起：WinDivertOpen 要等驱动，别把主窗口卡在启动画面
            threading.Thread(target=self._auto_enable_divert, daemon=True).start()

        self._monitor = threading.Thread(target=self._monitor_loop, name="orchestrator", daemon=True)
        self._monitor.start()

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
                    self.live["state"] = "idle"
                    self.live["error"] = ""
                    self.live["since"] = 0.0
                    cleared = True
        if not cleared:
            return

        # PS5 还在推流的话接着开播，省得再回 PS5 点一次
        try:
            pub = self.nginx.publisher()
        except Exception:
            pub = {}
        if pub.get("publishing") and self.output_mode() == "direct":
            logbus.info("PS5 还在推流，登录后自动接着开播", tag="直播")
            threading.Thread(target=self._start_bili_and_relay,
                             args=(pub.get("app", "app"), pub.get("name", "")),
                             name="start-live", daemon=True).start()

    # ══════════════════════════════════════════════
    # 环境 / 向导数据
    # ══════════════════════════════════════════════
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

    # ══════════════════════════════════════════════
    # 步骤 2：检测重定向
    # ══════════════════════════════════════════════
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

    # ══════════════════════════════════════════════
    # 步骤 2B：内置 DNS
    # ══════════════════════════════════════════════
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

        # 两套方案不能同时开：一边改 IP 目的地址、一边改域名解析，
        # PS5 的行为会变得没法预测。DNS 要走就先把端口劫持关掉。
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
        self.dns = dnsserver.DnsServer(
            answer_ip=answer_ip,
            domains=self.cfg.get("HIJACK_DOMAINS", []),
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
        if ok:
            logbus.ok("内置 DNS 已启动：%s，上游 %s" % (detail, ",".join(ups)), tag="DNS")
            # 上游如果是公网 DNS，PS5 开播握手会绕过加速 —— 必须提醒
            health = dnscheck.upstream_health(ups[0]) if ups else {"ok": False}
            if health.get("ok") and not health.get("accelerated"):
                logbus.warn("上游 DNS 不是加速器 DNS，PS5 很可能开不了播。"
                            "请在下一步把加速器给出的 DNS 填进来。", tag="DNS")
        else:
            logbus.error("内置 DNS 自检失败：%s" % detail, tag="DNS")
        return {"ok": ok, "detail": detail, "upstreams": ups}

    def disable_builtin_dns(self) -> dict:
        if self.dns:
            self.dns.stop()
            self.dns = None
            logbus.info("内置 DNS 已停止", tag="DNS")
        self.cfg = paths.save_config({"DNS_MODE": "router"})
        return {"ok": True}

    # ══════════════════════════════════════════════
    # 端口劫持（搭配加速器时的主方案）
    #
    # 为什么不走 DNS：真机实测，PS5 一旦经加速器，DNS 方案必然失败 ——
    # 答加速器网卡 IP 会被加速器当成自己的流量代推到真 Twitch，
    # 答局域网 IP 则在加速器网段里不可达。详见 core/divert.py 顶部。
    # ══════════════════════════════════════════════
    def _divert_target(self) -> str:
        """改写目标：本机局域网 IP（绝不能用 127.0.0.1，会被当 martian 丢掉）。"""
        return divert.pick_target_ip(self.lan_ip, "")

    def enable_divert(self) -> dict:
        if not paths.caps()["divert"]:
            detail = ("容器里没有端口劫持。这个能力要改本机的网络栈，只能在 Windows 上跑。"
                      "容器版靠路由器把推流域名或端口转到这台机器，"
                      "加速器要开在路由器上，PS5 保持自动。")
            self.hijack_error = detail
            logbus.error(detail, tag="劫持")
            return {"ok": False, "detail": detail, "unavailable": True}

        if self.divert and self.divert.running:
            return {"ok": True, "detail": "端口劫持已在运行", **self.divert.snapshot()}

        if not netutil.is_admin():
            detail = "没有管理员权限，直播桥开不了。右键程序 → 以管理员身份运行。"
            self.hijack_error = detail
            logbus.error(detail, tag="劫持")
            return {"ok": False, "detail": detail}

        target = self._divert_target()
        if not target:
            detail = "没拿到本机 IP。先检查网络连接。"
            self.hijack_error = detail
            logbus.error(detail, tag="劫持")
            return {"ok": False, "detail": detail}

        # nginx 必须在听 1935，否则劫持过来的推流没人接，PS5 会一直重试
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
        # 只标记「重定向通了」，不替用户跳步骤：
        # 进度是给人看的，往前跳会把第一次用的人直接甩到第 3 页，
        # 而他还没看到第 1 页的说明。跳步由界面上的按钮决定。
        self.progress = paths.save_progress({"dns_verified": True, "lan_ip": self.lan_ip})
        logbus.ok("直播桥已开，%s 的连接都转到 %s"
                  % ("/".join(str(p) for p in ports), target), tag="劫持")
        if self.dns:
            # 两者同时开只会让 PS5 的 DNS 行为变得不可预测，DNS 主动让位
            self.dns.stop()
            self.dns = None
            logbus.info("内置 DNS 已停，直播桥模式下用不到", tag="DNS")
        return {"ok": True, "detail": "端口劫持已生效", **ic.snapshot()}

    def disable_divert(self) -> dict:
        if self.divert:
            self.divert.stop()
            self.divert = None
            logbus.info("端口劫持已停止（正在进行的推流不受影响）", tag="劫持")
        # 不在这里改 HIJACK_MODE：界面上的「重启直播桥」是先停再开，
        # 如果这里把模式写成 dns，重启后就不会自动开启了。
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
        # 没有劫持能力（容器）时一律报 router：界面据此只走「改路由器」那条分支，
        # 不会去渲染一个永远点不动的按钮。
        mode = self.cfg.get("HIJACK_MODE", "divert") if paths.caps()["divert"] else "router"
        return {
            "mode": mode,
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
        self.dns.answer_ip = want      # 下一次 resolve 就用新地址，不需要重新绑端口
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
        # 这里给的是「PS5 的 DNS 该填谁」，判断依据是「哪个地址能把查询送到本机 53」。
        # 实测：加速器网卡 IP（172.19.84.237）能收到 PS5 的查询，局域网 IP 作为备选。
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

    # ══════════════════════════════════════════════
    # 步骤 3：加速器与 PS5 网络
    # ══════════════════════════════════════════════
    def save_ps5_net(self, ip="", mask="", gateway="", dns="", accelerator="") -> dict:
        patch = {"PS5_NET": {"ip": ip, "mask": mask, "gateway": gateway, "dns": dns}}
        if accelerator:
            patch["ACCELERATOR"] = accelerator
        if dns:
            patch["DNS_UPSTREAM"] = dns
        self.cfg = paths.save_config(patch)
        answer_changed = False
        # 用户填了网关之后，加速器网卡往往才出现 —— 答复地址可能要跟着换
        if self.dns and self.dns.running:
            want = self._dns_answer_ip()
            if want and want != self.dns.answer_ip:
                logbus.warn("加速器网段已就绪，内置 DNS 的答复地址从 %s 换成 %s"
                            % (self.dns.answer_ip, want), tag="DNS")
                self.dns.answer_ip = want
                answer_changed = True
        # 内置 DNS 在跑：立刻把上游换成加速器 DNS
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

    # ══════════════════════════════════════════════
    # 步骤 4：扫码登录
    # ══════════════════════════════════════════════
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

    def save_area(self, area_id: str, title: str = "") -> dict:
        self.cfg = paths.save_config({"BILIBILI_AREA_ID": str(area_id),
                                      "BILIBILI_TITLE": title})
        if self.bili.logged_in:
            self.bili.update_room(area_id=str(area_id), title=title or None)
        return {"ok": True}

    # ══════════════════════════════════════════════
    # 步骤 5/6：直播流水线
    # ══════════════════════════════════════════════
    def _monitor_loop(self) -> None:
        """1 秒一拍：看 PS5 推没推、要不要自动开播。"""
        was_publishing = False
        last_pub = 0.0
        nginx_retry_at = 0.0
        while not self._stop:
            try:
                now = time.time()

                # 收流服务是整条链路的地基，它没了后面全是空转。
                # 容器里尤其重要：容器重启策略只保主进程，nginx 是子进程，
                # 崩了就再也不会自己回来。限频 30 秒，避免端口真被占时刷屏。
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
                    # 按「最后一次看到推流的时间」判断，而不是连续计数：
                    # 断流瞬间 /stat 会抖几下，连续计数被反复清零，状态会卡住不回落。
                    # error 也要收：开播失败（比如没登录）后 PS5 停流，
                    # 状态不回落的话界面会一直挂着一条已经不成立的错误。
                    if self.live["state"] in ("local", "waiting", "starting",
                                              "relaying", "live", "error"):
                        self._on_ps5_unpublish()
                    last_pub = 0.0

                was_publishing = publishing

                # 加速器网卡可能晚于 DNS 才出现（用户按顺序：先启 DNS、再开加速器），
                # 答复地址必须跟着换，否则界面显示的和 DNS 实际答的不是同一个 IP
                self._follow_dns_answer()

                # 转发中：确认真到 B 站了
                if self.live["state"] == "relaying":
                    self._confirm_bili_live()

            except Exception as e:
                logbus.warn("监控循环异常：%s" % e)
            time.sleep(1.0)

    def _on_ps5_publish(self, pub: dict) -> None:
        app = pub.get("app", "app")
        name = pub.get("name", "")
        res = "%dx%d" % (pub.get("width", 0), pub.get("height", 0)) \
            if pub.get("width") else "未知"
        logbus.ok("PS5 开始推流了：%s/%s（%s，%s kbps）"
                  % (app, name[:10] + "***" if len(name) > 10 else name, res, pub.get("bw_in", 0)),
                  tag="推流")

        # 本地取流模式：只把流留在这里，等 OBS 之类的软件来拉。
        # 不开 B 站、不转发，也不需要登录 B 站。
        if self.output_mode() == "local":
            with self.lock:
                self.live["ps5"] = pub
                self.live["state"] = "local"
                self.live["since"] = time.time()
                self.live["error"] = ""
                self.live["local"] = {"app": app, "name": name}
            logbus.ok("本地流已就绪：%s" % self.local_play_url(app, name), tag="推流")
            self.services.notify_ps5("画面已就绪，可在电脑上拉取")
            return

        with self.lock:
            self.live["ps5"] = pub
            self.live["state"] = "starting" if self.cfg.get("AUTO_START_LIVE", True) else "waiting"
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
            self.live["state"] = "idle"
            self.live["ps5"] = {}
            self.live["local"] = {}
            self.live["since"] = 0.0
            self.live["face_url"] = ""

    def _start_bili_and_relay(self, app: str, name: str) -> None:
        area = str(self.cfg.get("BILIBILI_AREA_ID", "236"))
        title = self.cfg.get("BILIBILI_TITLE", "")
        logbus.step("正在让 B 站开播（分区 %s）…" % area)

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
            }
            self.live["state"] = "relaying"

        if not self.cfg.get("AUTO_RELAY", True):
            logbus.info("自动转发已关闭。B 站推流地址：%s / 密钥末六位 %s"
                        % (addr, code[-6:]), tag="转发")
            return

        in_url = "rtmp://127.0.0.1:%d/%s/%s" % (int(self.cfg.get("RTMP_PORT", 1935)), app, name)
        out_url = addr.rstrip("/") + "/" + code.lstrip("/")

        # 关键：B 站推流服务器也用 1935。不把它的 IP 加进白名单，
        # 端口劫持会把 ffmpeg 推到 B 站的连接劫回本机，自己推给自己，
        # ffmpeg 报 "Error opening output ... I/O error"，直播永远起不来。
        self._protect_relay_target(out_url, addr)

        if self.relay.start(in_url, out_url):
            self.relay.start_watchdog(on_drop=lambda reason: self._fail_live(
                "转发中断：%s" % reason))
            self.services.notify_ps5("B 站已开播，画面正在同步")
        else:
            self._fail_live(self.relay.last_error or "转发启动失败")

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
                self.live["state"] = "live"
                self.live["since"] = time.time()
            logbus.ok("B 站直播间已开播 —— 现在可以看了", tag="直播")
            self.progress = paths.save_progress({"step": 6})
            self.services.notify_ps5("B 站直播间已开播")
        elif status == 2:
            logbus.info("B 站显示轮播中，等待切换为直播…", tag="直播")
        elif tries >= 30:
            logbus.warn("等了 30 秒 B 站仍显示未开播。可能是推流地址/密钥不对，"
                        "或 B 站审核延迟。", tag="直播")

    def _fail_live(self, msg: str) -> None:
        logbus.error(msg, tag="直播")
        with self.lock:
            self.live["state"] = "error"
            self.live["error"] = msg
            # since 一并清掉：开播没成，「已播」时长没有意义，
            # 留着的话界面会在没直播的时候一直跳秒。
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
            self.live["state"] = "face_verify"
            self.live["error"] = ""
            self.live["face_url"] = face_url
            self.live["since"] = 0.0
        self.services.notify_ps5("B 站要求人脸验证，请在手机上完成")
        # 后台重试线程
        if not getattr(self, "_face_retry_thread", None) or not self._face_retry_thread.is_alive():
            self._face_retry_thread = threading.Thread(
                target=self._face_verify_retry_loop, daemon=True, name="face-verify-retry")
            self._face_retry_thread.start()

    def _face_verify_retry_loop(self) -> None:
        """
        人脸验证自动重试：每 30 秒调一次 start_live。
        成功 → 接管推流；仍要验证 → 刷新 face_url；其他错 → 转 error。
        """
        max_attempts = 20   # 10 分钟
        for attempt in range(max_attempts):
            time.sleep(30)
            # PS5 停推了就不用再试
            if self.live.get("state") != "face_verify":
                return
            if not self.bili.logged_in:
                self._fail_live("登录已失效，请重新扫码（第 4 步）。")
                return

            area = str(self.cfg.get("BILIBILI_AREA_ID", "236"))
            title = self.cfg.get("BILIBILI_TITLE", "")
            logbus.info("人脸验证重试（第 %d 次）…" % (attempt + 1), tag="B站")
            r = self.bili.start_live(area_id=area, title=title)
            if r.get("ok"):
                logbus.ok("人脸验证通过，B 站已开播", tag="B站")
                # 复用 _start_bili_and_relay 的后半段
                threading.Thread(target=self._relay_after_start, args=(r,),
                                 daemon=True).start()
                return
            code = r.get("code")
            if code in (60024, 60043):
                url = r.get("face_url") or ""
                with self.lock:
                    self.live["face_url"] = url
                continue   # 继续等
            # 其他错误
            self._fail_live("B 站开播失败：%s（%s）" % (r.get("msg"), code))
            return
        # 超时
        self._fail_live("人脸验证等待超时（10 分钟）。请在手机上完成验证后点「手动开播」。")

    def _relay_after_start(self, r: dict) -> None:
        """start_live 成功后的转发启动（从 _start_bili_and_relay 后半段拆出来复用）。"""
        addr = r["rtmp_addr"]
        code = r["rtmp_code"]
        with self.lock:
            self.live["bili"] = {
                "addr": addr, "code_tail": code[-6:] if code else "",
                "room_id": r.get("room_id", self.bili.room_id),
            }
            self.live["state"] = "relaying"
            self.live["error"] = ""

        if not self.cfg.get("AUTO_RELAY", True):
            logbus.info("自动转发已关闭。B 站推流地址已获取。", tag="转发")
            return

        pub = self.live.get("ps5") or {}
        app = pub.get("app", "app")
        name = pub.get("name", "")
        in_url = "rtmp://127.0.0.1:%d/%s/%s" % (int(self.cfg.get("RTMP_PORT", 1935)), app, name)
        out_url = addr.rstrip("/") + "/" + code.lstrip("/")
        self._protect_relay_target(out_url, addr)

        if self.relay.start(in_url, out_url):
            self.relay.start_watchdog(on_drop=lambda reason: self._fail_live(
                "转发中断：%s" % reason))
            self.services.notify_ps5("B 站已开播，画面正在同步")
        else:
            self._fail_live(self.relay.last_error or "转发启动失败")

    def start_live_manual(self) -> dict:
        """手动开播（自动模式失败或用户关掉自动时用）。"""
        pub = self.nginx.publisher()
        if not pub.get("publishing"):
            return {"ok": False, "msg": "PS5 还没推流，先在 PS5 上开直播"}
        if not self.bili.logged_in:
            return {"ok": False, "msg": "请先扫码登录 B 站"}
        with self.lock:
            self.live["state"] = "starting"
            self.live["error"] = ""
        threading.Thread(target=self._start_bili_and_relay,
                         args=(pub.get("app", "app"), pub.get("name", "")),
                         name="start-live-manual", daemon=True).start()
        return {"ok": True}

    # ══════════════════════════════════════════════
    # 输出方式：直推 B 站 / 本地取流
    # ══════════════════════════════════════════════
    def output_mode(self) -> str:
        return "local" if self.cfg.get("OUTPUT_MODE") == "local" else "direct"

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

        # 切换时如果 PS5 正在推流，按新方式立刻接管一次，
        # 否则监控循环以为「推流没断过」，不会重新触发。
        try:
            pub = self.nginx.publisher() if self.nginx.running() else {"publishing": False}
            if pub.get("publishing"):
                self._on_ps5_publish(pub)
        except Exception as e:
            logbus.warn("切换输出方式后接管当前推流失败：%s" % e, tag="推流")
        return {"ok": True, "mode": mode}

    def stop_live(self, reason: str = "") -> dict:
        logbus.step("停止直播%s" % (("（%s）" % reason) if reason else ""))
        self.relay.stop()
        try:
            r = self.bili.stop_live()
        except Exception as e:
            r = {"ok": False, "msg": str(e)}
        with self.lock:
            self.live["state"] = "idle"
            self.live["bili"] = {}
            self.live["confirm_tries"] = 0
        self.services.notify_ps5("B 站已关播")
        return {"ok": True, "bili": r}

    # ══════════════════════════════════════════════
    # 弹幕
    # ══════════════════════════════════════════════
    def _on_danmaku(self, item: dict) -> None:
        self.danmaku_total += 1
        if item["kind"] in ("gift", "guard", "sc"):
            self.gift_total += 1
        self.danmaku_recent.appendleft(item)

        if not self.services.ps5_connected:
            return
        kind = item["kind"]
        if kind == "danmaku":
            self.services.inject(item["user"], item["text"])
        elif kind == "gift":
            self.services.inject(item["user"], "送出 %s" % item["text"])
        elif kind == "guard":
            self.services.inject(item["user"], item["text"])
        elif kind == "sc":
            self.services.inject(item["user"], "醒目留言 ¥%s：%s"
                                 % (item["price"] // 1000, item["text"]))

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

    # ══════════════════════════════════════════════
    # 快照（前端每秒拉一次）
    # ══════════════════════════════════════════════
    def snapshot(self) -> dict:
        with self.lock:
            live = dict(self.live)
        dns_state = self.dns_status()
        return {
            "caps": paths.caps(),
            "platform": paths.PLATFORM,
            "step": self.progress.get("step", 1),
            "progress_dns_ok": bool(self.progress.get("dns_verified"))
                               or bool(self.dns and self.dns.running)
                               or bool(self.divert and self.divert.running),
            "lan_ip": self.lan_ip,
            "gateway": netutil.default_gateway(),
            "dns_mode": self.cfg.get("DNS_MODE", "router"),
            "dns_answer": self.dns_answer_hint(),
            "hijack": self.hijack_state(),
            "services": {
                "dns": dns_state,
                "irc": {"running": self.services.irc.running,
                        "ps5": self.services.ps5_connected},
                "nginx": {"running": self.nginx.running(),
                          "publisher": live.get("ps5") or {}},
                "relay": {"running": self.relay.running, "uptime": self.relay.uptime,
                          "restarts": self.relay.restarts,
                          "error": self.relay.last_error},
                "danmaku": {"connected": self.services.danmaku_connected,
                            "count": self.danmaku_total, "gift": self.gift_total},
            },
            "bili": {
                "logged_in": self.bili.logged_in,
                "uname": self.bili.uname,
                "uid": self.bili.uid,
                "room_id": self.bili.room_id,
                "area_id": self.cfg.get("BILIBILI_AREA_ID", "236"),
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
                # 只有真的在播才算时长；idle / error 状态给 0，
                # 否则「已播」会在没开播的时候自己往上跳。
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

    # ══════════════════════════════════════════════
    # 服务开关（nginx / relay 起停）
    # ══════════════════════════════════════════════
    def ensure_nginx(self) -> dict:
        if self.nginx.running():
            return {"ok": True, "detail": "收流服务已在运行"}
        ok = self.nginx.start()
        return {"ok": ok, "detail": self.nginx.last_error or "收流服务已启动"}
