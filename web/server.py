from __future__ import annotations

import threading

from flask import Flask, jsonify, render_template, request, send_from_directory

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
