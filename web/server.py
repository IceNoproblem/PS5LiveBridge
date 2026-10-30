from __future__ import annotations

import os
import sys
import threading

from flask import (Flask, Response, jsonify, render_template, request,
                     send_from_directory, stream_with_context)

from core import logbus, netutil, paths


def create_app(orch, on_exit=None) -> Flask:
    app = Flask(
        __name__,
        template_folder=paths.resource_path("web", "templates"),
        static_folder=paths.resource_path("web", "static"),
        static_url_path="/static",
    )
    app.config["JSON_AS_ASCII"] = False
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

    @app.get("/")
    def index():
        return render_template("wizard.html")

    @app.get("/favicon.ico")
    def favicon():
        try:
            return send_from_directory(app.static_folder, "favicon.ico")
        except Exception:
            return ("", 204)

    @app.get("/api/state")
    def api_state():
        try:
            return jsonify(orch.snapshot())
        except Exception as e:
            logbus.error("状态快照失败：%s" % e)
            return jsonify({"error": str(e)}), 500

    @app.get("/api/env")
    def api_env():
        if request.args.get("refresh") == "1":
            netutil.clear_cache()
            logbus.info("网络环境已重新探测")
        return jsonify(orch.env())

    @app.get("/api/logs")
    def api_logs():
        try:
            since = float(request.args.get("since", 0) or 0)
        except ValueError:
            since = 0
        items = [x for x in logbus.recent(300) if x["ts"] > since]
        return jsonify({
            "items": items,
            "last_ts": (items[-1]["ts"] if items else since),
        })

    @app.get("/api/guides")
    def api_guides():
        return jsonify(orch.guides())

    @app.post("/api/guides/export")
    def api_guides_export():
        files = orch.export_router_files()
        if files:
            logbus.info("已导出路由器配置：%s" % "、".join(files))
            return jsonify({"ok": True, "files": files})
        return jsonify({"ok": False, "msg": "写文件失败，请检查目录权限"})

    @app.post("/api/router/detect")
    def api_router_detect():
        body = request.get_json(silent=True) or {}
        return jsonify(orch.router_detect(ip=body.get("ip", "")))

    @app.post("/api/router/apply")
    def api_router_apply():
        body = request.get_json(silent=True) or {}
        return jsonify(orch.router_apply(
            ip=body.get("ip", ""),
            username=body.get("username", "root") or "root",
            password=body.get("password", "")))

    @app.post("/api/router/restore")
    def api_router_restore():
        body = request.get_json(silent=True) or {}
        return jsonify(orch.router_restore(
            ip=body.get("ip", ""),
            username=body.get("username", "root") or "root",
            password=body.get("password", "")))

    @app.post("/api/dns/verify")
    def api_verify():
        body = request.get_json(silent=True) or {}
        return jsonify(orch.verify_dns(target=body.get("target", ""),
                                       mode=body.get("mode", "router")))

    @app.get("/api/dns/status")
    def api_dns_status():
        return jsonify(orch.dns_status())

    @app.get("/api/divert/status")
    def api_divert_status():
        return jsonify(orch.divert_status())

    @app.post("/api/divert/start")
    def api_divert_start():
        return jsonify(orch.enable_divert())

    @app.post("/api/divert/stop")
    def api_divert_stop():
        return jsonify(orch.disable_divert())

    @app.get("/api/hijack")
    def api_hijack():
        return jsonify(orch.hijack_state())

    @app.post("/api/builtin/start")
    def api_builtin_start():
        return jsonify(orch.enable_builtin_dns())

    @app.post("/api/builtin/selfcheck")
    def api_builtin_selfcheck():
        if not orch.dns:
            return jsonify({"ok": False, "detail": "内置 DNS 未启动"})
        ok, detail = orch.dns.selftest()
        return jsonify({"ok": ok, "detail": detail,
                        "upstreams": orch.dns.upstreams})

    @app.post("/api/builtin/stop")
    def api_builtin_stop():
        return jsonify(orch.disable_builtin_dns())

    @app.post("/api/builtin/upstream")
    def api_builtin_upstream():
        body = request.get_json(silent=True) or {}
        if not orch.dns:
            return jsonify({"ok": False, "detail": "内置 DNS 未启动"})
        servers = [s.strip() for s in str(body.get("servers", "")).replace("，", ",").split(",")
                   if s.strip()]
        if not servers:
            return jsonify({"ok": False, "detail": "没有有效的上游地址"})
        orch.dns.set_upstreams(servers)
        logbus.info("DNS 上游改为：%s" % ", ".join(servers), tag="DNS")
        ok, detail = orch.dns.selftest()
        return jsonify({"ok": ok, "detail": detail, "upstreams": servers})

    @app.post("/api/ps5net")
    def api_ps5net():
        body = request.get_json(silent=True) or {}
        return jsonify(orch.save_ps5_net(
            ip=body.get("ip", ""), mask=body.get("mask", ""),
            gateway=body.get("gateway", ""), dns=body.get("dns", ""),
            accelerator=body.get("accelerator", "")))

    @app.get("/api/ps5net/autofill")
    def api_ps5net_autofill():
        return jsonify(orch.autofill_ps5_net())

    @app.post("/api/qr/new")
    def api_qr_new():
        return jsonify(orch.qr_new())

    @app.get("/api/qr/poll")
    def api_qr_poll():
        return jsonify(orch.qr_poll())

    @app.post("/api/logout")
    def api_logout():
        return jsonify(orch.logout())

    @app.get("/api/partitions")
    def api_partitions():
        return jsonify(orch.partition_choices())

    @app.post("/api/area")
    def api_area():
        body = request.get_json(silent=True) or {}
        return jsonify(orch.save_area(str(body.get("area_id", "236")),
                                      body.get("title", ""),
                                      body.get("twitch_channel"),
                                      body.get("twitch_proxy")))

    @app.post("/api/output/mode")
    def api_output_mode():
        body = request.get_json(silent=True) or {}
        return jsonify(orch.set_output_mode(body.get("mode", "direct")))

    @app.post("/api/live/start")
    def api_live_start():
        return jsonify(orch.start_live_manual())

    @app.post("/api/live/stop")
    def api_live_stop():
        body = request.get_json(silent=True) or {}
        return jsonify(orch.stop_live(reason=body.get("reason", "手动停止")))

    @app.post("/api/test/inject")
    def api_test_inject():
        body = request.get_json(silent=True) or {}
        return jsonify(orch.test_inject(body.get("text", "")))

    @app.post("/api/flags")
    def api_flags():
        body = request.get_json(silent=True) or {}
        return jsonify(orch.set_flags(
            auto_start=body.get("auto_start_live"),
            auto_relay=body.get("auto_relay"),
            twitch_channel=body.get("twitch_channel")))

    @app.post("/api/step")
    def api_step():
        body = request.get_json(silent=True) or {}
        return jsonify(orch.goto_step(int(body.get("step", 1))))

    @app.post("/api/nginx/ensure")
    def api_nginx_ensure():
        return jsonify(orch.ensure_nginx())

    @app.post("/api/shutdown")
    def api_shutdown():
        def _bye():
            try:
                orch.shutdown()
            finally:
                if on_exit:
                    on_exit()
        threading.Thread(target=_bye, daemon=True).start()
        return jsonify({"ok": True})

    @app.post("/api/hide")
    def api_hide():
        return jsonify({"ok": False, "msg": "浏览器模式无法隐藏窗口，直接关掉窗口即可"})

    from core import updater as updater_mod
    _up = updater_mod.get_updater(paths.APP_VERSION)

    @app.get("/api/logs/debug")
    def api_logs_debug_get():
        return jsonify({"on": logbus.debug_enabled()})

    @app.post("/api/logs/debug")
    def api_logs_debug_set():
        body = request.get_json(silent=True) or {}
        on = bool(body.get("on"))
        logbus.set_debug(on)
        try:
            if orch.cfg.get("DEBUG_LOG") != on:
                orch.cfg = paths.save_config({"DEBUG_LOG": on})
        except Exception as e:
            logbus.warn("保存调试开关失败：%s" % e, tag="日志")
        logbus.info("调试日志已%s" % ("打开" if on else "关闭"), tag="日志")
        if on:
            logbus.debug("这是一条调试日志，出现它说明开关生效了", tag="日志")
        return jsonify({"on": on})

    _OVERLAY_KEYS = (
        "OVERLAY_TO_BILI", "OVERLAY_TO_LOCAL",
        "OVERLAY_TEXT", "OVERLAY_FONT_SIZE", "OVERLAY_COLOR", "OVERLAY_ALPHA",
        "OVERLAY_BG_ALPHA", "OVERLAY_POS", "OVERLAY_X", "OVERLAY_Y",
    )

    @app.get("/api/overlay")
    def api_overlay_get():
        return jsonify({k: orch.cfg.get(k) for k in _OVERLAY_KEYS})

    @app.post("/api/overlay")
    def api_overlay_set():
        body = request.get_json(silent=True) or {}
        patch = {}
        for k in _OVERLAY_KEYS:
            if k in body:
                patch[k] = body[k]
        if not patch:
            return jsonify({"ok": False, "msg": "没有要改的"}), 400
        # 滤镜参数（位置/字号/颜色）是写死在 ffmpeg 滤镜串里的，
        # 只改配置不会让正在跑的那条链路重新生成 —— 必须重启转码才能生效。
        # 文本内容不同，它靠 reload 重读 txt，不用重启。
        # OVERLAY_TEXT 也在里面：drawtext 不再 reload，文字只在开播时读一次，
        # 所以改了内容必须重启转码才能看到。
        # OVERLAY_TO_BILI / OVERLAY_TO_LOCAL 也要重启：
        # 它们决定 ffmpeg 输出几路，改完必须重建进程才生效。
        _layout_keys = ("OVERLAY_TEXT", "OVERLAY_TO_BILI", "OVERLAY_TO_LOCAL",
                        "OVERLAY_X", "OVERLAY_Y", "OVERLAY_FONT_SIZE",
                        "OVERLAY_COLOR", "OVERLAY_ALPHA", "OVERLAY_BG_ALPHA",
                        )
        need_restart = any(k in patch for k in _layout_keys)

        orch.cfg = paths.save_config(patch)
        try:
            orch.overlay.write(orch.cfg)
        except Exception:
            pass

        restarted = False
        if need_restart and orch.live.get("state") in ("live", "relaying"):
            _addr = getattr(orch, "_last_addr", "")
            _code = getattr(orch, "_last_code", "")
            if _addr and _code:
                try:
                    # 只重启转码这一段，不重新调 B 站开播（会触发风控）
                    orch.relay.stop()
                    restarted = bool(orch._start_push(_addr, _code))
                except Exception as e:
                    logbus.warn("按新参数重启转码失败：%s" % e, tag="叠加")

        logbus.info("画面叠加设置已更新（%s%s）"
                    % ("开启" if orch.has_overlay() else "关闭",
                       "，已按新参数重启转码" if restarted else ""), tag="叠加")
        return jsonify({"ok": True, "active": orch.has_overlay(),
                        "restarted": restarted})

    @app.get("/api/netcheck")
    def api_netcheck():
        return jsonify(orch.netcheck(force=request.args.get("force") == "1"))

    @app.get("/api/outputs")
    def api_outputs_get():
        return jsonify({
            "push_bili": orch.push_bili(),
            "local_stream": orch.local_stream(),
        })

    @app.post("/api/outputs")
    def api_outputs_set():
        body = request.get_json(silent=True) or {}
        r = orch.set_outputs(body.get("push_bili"), body.get("local_stream"))
        return jsonify(r)

    @app.get("/api/paths")
    def api_paths():
        from core import paths as _p
        info = {
            "base_dir": _p.BASE_DIR,
            "frozen": _p.is_frozen(),
            "executable": sys.executable,
            "meipass": getattr(sys, "_MEIPASS", ""),
            "cwd": os.getcwd(),
            "rtmf_exe": getattr(orch.nginx, "exe", ""),
        }
        if hasattr(orch.nginx, "_bases"):
            info["candidates"] = [
                {"path": x, "exists": os.path.exists(x)}
                for x in orch.nginx._bases()
            ]
        return jsonify(info)

    @app.get("/api/rtmp_impl")
    def api_rtmp_impl_get():
        return jsonify({
            "impl": orch.cfg.get("RTMP_IMPL") or "nginx",
            "active": "rtmpf" if getattr(orch, "native_relay", False) else "nginx",
        })

    @app.post("/api/rtmp_impl")
    def api_rtmp_impl_set():
        body = request.get_json(silent=True) or {}
        impl = (body.get("impl") or "").strip().lower()
        if impl not in ("nginx", "rtmpf"):
            return jsonify({"ok": False, "error": "只能是 nginx 或 rtmpf"}), 400
        orch.cfg = paths.save_config({"RTMP_IMPL": impl})
        active = "rtmpf" if getattr(orch, "native_relay", False) else "nginx"
        logbus.info("收流实现已设为 %s（%s）"
                    % (impl, "重启后生效" if impl != active else "当前已生效"),
                    tag="收流")
        return jsonify({"ok": True, "impl": impl, "need_restart": impl != active})

    @app.get("/api/preview.flv")
    def api_preview_flv():
        """
        把收流端(rtmp-forwarder)的 HTTP-FLV 代理出来。

        不让前端直连它的端口：那个端口只听 127.0.0.1，而且直连会有跨域。
        代理之后前端只要请求同源的 /api/preview.flv 就能看画面。
        """
        # 预览始终拉 PS5 的原始画面（不烧字）。
        # 叠加效果由前端覆盖层画在上面，拖到哪儿就是哪儿，同时实时改后端。
        port = int(getattr(orch.nginx, "web_port", 0) or 0)
        if not port:
            return jsonify({"error": "当前收流实现不支持本地预览"}), 400
        import requests
        url = "http://127.0.0.1:%d/live/stream.flv" % port
        try:
            up = requests.get(url, stream=True, timeout=(3, None),
                              proxies={"http": None, "https": None})
            up.raise_for_status()
        except Exception as e:
            return jsonify({"ok": False, "error": "拉不到预览流：%s" % e}), 502

        def _pump():
            try:
                for chunk in up.iter_content(8192):
                    if chunk:
                        yield chunk
            finally:
                try:
                    up.close()
                except Exception:
                    pass

        resp = Response(stream_with_context(_pump()), mimetype="video/x-flv")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/api/diag")
    def api_diag():
        from core import diag
        try:
            return jsonify(diag.collect(orch))
        except Exception as e:
            logbus.error("诊断采集失败：%s" % e)
            return jsonify({"error": str(e), "issues": [], "local": {}}), 500

    @app.post("/api/sniff/start")
    def api_sniff_start():
        from core import sniff
        body = request.get_json(silent=True) or {}
        ip = str(body.get("ip") or "").strip() or sniff.guess_ps5_ip(orch)
        try:
            secs = int(body.get("seconds") or 30)
        except (TypeError, ValueError):
            secs = 30
        ok, err = sniff.get_sniffer().start(ip, secs)
        snap = sniff.get_sniffer().snapshot()
        snap["ok"] = ok
        if err:
            snap["error"] = err
        return jsonify(snap)

    @app.post("/api/sniff/stop")
    def api_sniff_stop():
        from core import sniff
        sniff.get_sniffer().stop()
        return jsonify(sniff.get_sniffer().snapshot())

    @app.get("/api/sniff/status")
    def api_sniff_status():
        from core import sniff
        snap = sniff.get_sniffer().snapshot()
        if not snap.get("ps5_ip"):
            snap["guess"] = sniff.guess_ps5_ip(orch)
        return jsonify(snap)

    @app.post("/api/diag/export")
    def api_diag_export():
        from core import diag
        try:
            r = diag.export(orch)
            return jsonify({"ok": True, "name": r["name"], "size": r["size"],
                            "url": "/api/diag/file/" + r["name"]})
        except Exception as e:
            logbus.error("诊断包导出失败：%s" % e)
            return jsonify({"ok": False, "msg": str(e)})

    @app.get("/api/diag/file/<name>")
    def api_diag_file(name):
        import os
        from flask import send_file
        if (not name.startswith("diag-") or not name.endswith(".zip")
                or "/" in name or "\\" in name or ".." in name):
            return jsonify({"ok": False, "msg": "非法文件名"}), 400
        p2 = os.path.join(paths.DATA_DIR, "diag", name)
        if not os.path.exists(p2):
            return jsonify({"ok": False, "msg": "文件不存在"}), 404
        try:
            return send_file(p2, as_attachment=True, download_name=name)
        except TypeError:
            return send_file(p2, as_attachment=True, attachment_filename=name)

    @app.get("/api/twitch/session")
    def api_twitch_session():
        from core import twitch_title
        ch = (orch.cfg.get("TWITCH_CHANNEL") or "").strip()
        px = (orch.cfg.get("TWITCH_PROXY") or "").strip()
        if not ch:
            pub = ((orch.snapshot().get("live") or {}).get("ps5") or {})
            ch = twitch_title.parse_broadcaster_id(pub.get("key") or "")
        try:
            return jsonify(twitch_title.fetch_session(ch, proxy=px, timeout=8.0))
        except Exception as e:
            return jsonify({"ok": False, "verdict": "unknown",
                            "error": "查询失败：%s" % e, "advice": "",
                            "channel": ch, "last_broadcast": {}, "local": []})

    @app.post("/api/proxy/check")
    def api_proxy_check():
        """
        代理检测。给两个输入框旁边的按钮用。

        不传 proxy（或传空）：先测直连，再扫本机代理，告诉用户要不要填。
        传了 proxy：只测这一条。
        """
        from core import twitch_title
        body = request.get_json(silent=True) or {}
        proxy = str(body.get("proxy") or "").strip()
        try:
            timeout = float(body.get("timeout") or 6.0)
        except (TypeError, ValueError):
            timeout = 6.0
        try:
            return jsonify(twitch_title.check(proxy, timeout=timeout))
        except Exception as e:
            return jsonify({"verdict": "error", "advice": "检测失败：%s" % e,
                            "mode": "explicit" if proxy else "auto",
                            "direct": None, "local": [], "chosen": "",
                            "result": {}})

    @app.get("/api/update/check")
    def api_update_check():
        body = request.args
        force = str(body.get("force", "")).lower() in ("1", "true", "yes")
        snap = _up.check(force=force)
        snap["platform"] = paths.PLATFORM
        snap["install_hint"] = ("docker compose pull && docker compose up -d"
                                if paths.PLATFORM == "docker" else "")
        return jsonify(snap)

    @app.get("/api/update/status")
    def api_update_status():
        snap = _up.snapshot()
        snap["platform"] = paths.PLATFORM
        return jsonify(snap)

    @app.post("/api/update/run")
    def api_update_run():
        if paths.PLATFORM == "docker":
            return jsonify({
                "ok": False,
                "detail": "容器版不能自己替换自己。在 NAS 上执行：\n\n"
                          "docker compose pull && docker compose up -d\n\n"
                          "想要全自动：compose 里启用 auto-update profile（watchtower），"
                          "每天凌晨 4 点自动拉新版重启。",
            })
        if paths.PLATFORM != "windows":
            return jsonify({"ok": False, "detail": "当前形态不支持自动安装，请手动更新"})

        def _before_exit():
            try:
                orch.shutdown()
            except Exception:
                pass

        return jsonify(_up.install(on_before_exit=_before_exit))

    @app.errorhandler(Exception)
    def on_error(e):
        logbus.error("接口异常：%s" % e)
        return jsonify({"error": str(e)}), 500

    return app


def serve(orch, host: str = "127.0.0.1", port: int = 5000, on_exit=None) -> None:
    """阻塞运行。Flask 自带的 werkzeug 开发服务器对本场景够用且无额外依赖。"""
    app = create_app(orch, on_exit=on_exit)
    import logging
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.ERROR)
    logbus.info("界面已就绪：http://%s:%d" % (host, port))
    app.run(host=host, port=port, threaded=True, use_reloader=False, debug=False)
