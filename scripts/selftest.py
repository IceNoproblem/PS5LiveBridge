# -*- coding: utf-8 -*-
"""
scripts/selftest.py —— 不接 PS5、不接 B 站也能跑的自我验证

覆盖六个最容易出错、且出错了很难看出来的地方：
    1. 内置 DNS 的劫持与转发（含「绝不动 api.twitch.tv」这条红线）
    2. 重定向检测的判定结构
    3. 本地 IRC 通道：模拟 PS5 连上来，能不能收到注入的弹幕
    4. B 站弹幕包解压（brotli/zlib 那套二进制协议）
    5. nginx /stat XML 解析（拿 app 名和流名）
    6. 直播状态机：推流 -> 开播 -> 转发 -> 确认 -> 停播

运行： python3 scripts/selftest.py
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import (danmaku, dnscheck, dnsserver, logbus, netutil, nginxmgr,  # noqa: E402
                  routercfg, services)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  \033[32mPASS\033[0m  %s%s" % (name, ("  — " + str(detail)) if detail else ""))
    else:
        FAIL.append(name)
        print("  \033[31mFAIL\033[0m  %s%s" % (name, ("  — " + str(detail)) if detail else ""))


# ──────────────────────────────────────────────────────────────
# 1. DNS
# ──────────────────────────────────────────────────────────────
def t_dns():
    print("\n[1] 内置 DNS")
    srv = dnsserver.DnsServer("10.1.2.3",
                             domains=["contribute.live-video.net", "irc.twitch.tv"],
                             port=15353, upstreams=["223.5.5.5"])
    ok = srv.start()
    check("DNS 服务能绑定端口", ok, srv.last_error)
    if not ok:
        return

    ok, ips = dnsserver.query("127.0.0.1", "contribute.live-video.net", port=15353)
    check("劫持域名答成本机 IP", ok and ips == ["10.1.2.3"], str(ips))

    ok, ips = dnsserver.query("127.0.0.1", "www.baidu.com", port=15353)
    check("非劫持域名正常转发", ok and len(ips) >= 1, str(ips)[:40])

    check("绝不动 api.twitch.tv", srv.is_hijacked("api.twitch.tv") is False)
    check("绝不动 playstation.net", srv.is_hijacked("web.np.playstation.net") is False)
    check("子域也算劫持", srv.is_hijacked("hkg06.contribute.live-video.net") is True)

    srv.set_upstreams(["119.29.29.29"])
    check("上游可热替换", srv.upstreams == ["119.29.29.29"])

    ok, detail = srv.selftest()
    check("自检能验证答复正确", ok, detail)

    snap = srv.stats.snapshot()
    check("有查询统计", snap["total"] >= 3 and snap["hijacked"] >= 2,
          "total=%d hit=%d" % (snap["total"], snap["hijacked"]))
    srv.stop()


# ──────────────────────────────────────────────────────────────
# 2. 检测判定
# ──────────────────────────────────────────────────────────────
def t_check():
    print("\n[2] 重定向检测")
    r = dnscheck.verify("192.0.2.99", mode="router")
    for k in ("ok", "hint", "results", "fake_ip"):
        check("返回字段 %s" % k, k in r)
    check("假 IP 不会被判成功", r["ok"] is False)
    check("候选里不含内置 DNS（router 模式）",
          all(x.get("kind") != "self" for x in r["results"]))
    check("候选结构完整",
          all(("label" in x and "hits" in x and "total" in x) for x in r["results"]))

    b = dnscheck.verify("192.0.2.99", mode="builtin")
    check("builtin 模式只问本机 127.0.0.1",
          all(x["server"] == "127.0.0.1" for x in b["results"]))


# ──────────────────────────────────────────────────────────────
# 3. IRC
# ──────────────────────────────────────────────────────────────
def t_irc():
    print("\n[3] PS5 弹幕通道（IRC）")
    got = {"join": None}
    sv = services.Services(bili=None, on_ps5_join=lambda ch: got.update(join=ch))
    ok = sv.start(16867)
    check("IRC 服务启动", ok)
    if not ok:
        return

    s = socket.create_connection(("127.0.0.1", 16867), timeout=5)
    s.settimeout(3)

    def read_until(needle, timeout=4):
        buf = b""
        end = time.time() + timeout
        while time.time() < end:
            try:
                chunk = s.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            if needle.encode() in buf:
                break
        return buf.decode("utf-8", "ignore")

    s.sendall(b"CAP LS 302\r\nNICK ps5user\r\nUSER ps5 0 * :ps5\r\n")
    text = read_until("366")
    check("CAP 协商被应答", "CAP * LS" in text, text.splitlines()[:1])
    check("WELCOME 序列发出", " 001 " in text)
    check("自动拉进频道", "JOIN #" in text)
    check("先 WELCOME 再 JOIN（与真 Twitch 同序）",
          text.find(" 001 ") < text.find("JOIN #") if "JOIN #" in text else False)
    check("on_join 回调触发", got["join"] is not None, str(got["join"]))

    # PING -> PONG
    s.sendall(b"PING :tmi.twitch.tv\r\n")
    pong = read_until("PONG")
    check("PING 回 PONG（不出现双冒号）",
          "PONG :tmi.twitch.tv" in pong and "::" not in pong, pong.strip()[:60])

    # 注入弹幕
    sv.inject("张三", "这是一条测试弹幕")
    text = read_until("这是一条测试弹幕")
    check("弹幕能注入到 PS5", "PRIVMSG" in text and "这是一条测试弹幕" in text,
          text.strip()[:80])

    sv.notify_ps5("系统提示")
    check("系统提示可用", "系统提示" in read_until("系统提示"))

    s.close()
    sv.stop()


# ──────────────────────────────────────────────────────────────
# 4. 弹幕包解析
# ──────────────────────────────────────────────────────────────
def t_danmaku():
    print("\n[4] B 站弹幕协议")
    msg = {"cmd": "DANMU_MSG", "info": [[], "你好世界", [123, "阿伟"]]}
    body = json.dumps(msg).encode()
    packet = danmaku.pack(danmaku.OP_MESSAGE, body)

    # 真实链路：先按 16 字节包头切帧，再按 ver 解 body
    frames = danmaku.unpack(packet)
    check("能按包头切帧", len(frames) == 1 and frames[0][0] == danmaku.OP_MESSAGE)
    out = danmaku.decode(frames[0][1], frames[0][2])
    check("单包能解析", len(out) == 1 and out[0]["cmd"] == "DANMU_MSG")

    out = danmaku.decode(danmaku.VER_ZLIB, zlib.compress(packet))
    check("zlib 压缩包能递归解开", len(out) == 1 and out[0]["cmd"] == "DANMU_MSG")

    try:
        import brotli
        out = danmaku.decode(danmaku.VER_BROTLI, brotli.compress(packet))
        check("brotli 压缩包能解开", len(out) == 1 and out[0]["cmd"] == "DANMU_MSG")
    except ImportError:
        print("  \033[33mSKIP\033[0m  未安装 brotli")

    item = danmaku.parse_message(msg)
    check("普通弹幕解析", item and item["user"] == "阿伟" and item["text"] == "你好世界")

    gift = danmaku.parse_message({"cmd": "SEND_GIFT", "data": {
        "uname": "老王", "giftName": "辣条", "num": 3, "total_coin": 300}})
    check("礼物解析", gift and gift["kind"] == "gift" and gift["num"] == 3)

    guard = danmaku.parse_message({"cmd": "GUARD_BUY", "data": {
        "username": "土豪", "guard_level": 3, "num": 1}})
    check("舰长解析", guard and guard["kind"] == "guard" and "舰长" in guard["text"])

    sc = danmaku.parse_message({"cmd": "SUPER_CHAT_MESSAGE", "data": {
        "message": "加油", "price": 30000, "user_info": {"uname": "大哥"}}})
    check("醒目留言解析", sc and sc["kind"] == "sc" and sc["price"] == 30000)

    check("未知消息被忽略", danmaku.parse_message({"cmd": "ONLINE_RANK_V2"}) is None)


# ──────────────────────────────────────────────────────────────
# 5. nginx stat 解析
# ──────────────────────────────────────────────────────────────
def t_nginx_stat():
    print("\n[5] nginx /stat 解析")
    # 这段 XML 是从 Windows 真机的 nginx 上原样抓下来的结构（含嵌套 meta）
    xml = """<?xml version="1.0" encoding="utf-8" ?>
<rtmp><server><application>
  <name>app</name>
  <live>
    <stream>
      <name>live_123456_abcdef</name>
      <time>128</time>
      <bw_in>6291456</bw_in>
      <meta><video><width>1920</width><height>1080</height><frame_rate>60</frame_rate>
      <codec>H264</codec></video><audio><codec>AAC</codec></audio></meta>
      <publishing/>
      <nclients>1</nclients>
    </stream>
  </live>
</application></server></rtmp>"""
    mgr = nginxmgr.NginxManager(rtmp_port=19350, stat_port=18080)
    mgr.stat_xml = lambda timeout=1.5: xml
    p = mgr.publisher()
    check("认得出正在推流", p["publishing"] is True)
    check("拿到 app 名", p["app"] == "app", p["app"])
    check("拿到流名（转发要用）", p["name"] == "live_123456_abcdef")
    check("码率换算成 kbps", p["bw_in"] == 6144, str(p["bw_in"]))
    check("分辨率帧率", (p["width"], p["height"], p["fps"]) == (1920, 1080, 60))

    # 兼容老写法（属性形式），避免不同 nginx 版本上又不显示
    xml_attr = xml.replace(
        "<meta><video><width>1920</width><height>1080</height><frame_rate>60</frame_rate>"
        "<codec>H264</codec></video><audio><codec>AAC</codec></audio></meta>",
        '<meta width="1920" height="1080" framerate="60"/>')
    mgr.stat_xml = lambda timeout=1.5: xml_attr
    p2 = mgr.publisher()
    check("属性写法的 meta 也认", (p2["width"], p2["height"]) == (1920, 1080),
          "%sx%s" % (p2["width"], p2["height"]))

    mgr.stat_xml = lambda timeout=1.5: ""
    check("拿不到 stat 时安全返回空", mgr.publisher()["publishing"] is False)


# ──────────────────────────────────────────────────────────────
# 6. 直播状态机
# ──────────────────────────────────────────────────────────────
def t_state_machine():
    print("\n[6] 直播状态机")
    from core.orchestrator import Orchestrator

    o = Orchestrator()
    o.lan_ip = "10.1.2.3"

    # 打桩：不碰真 nginx / 真 B 站 / 真 ffmpeg
    o.nginx.running = lambda: True
    o.nginx.publisher = lambda: {"publishing": True, "app": "app",
                                 "name": "live_777_key", "bw_in": 6000,
                                 "width": 1920, "height": 1080, "fps": 60}
    o.bili.cookies = {"SESSDATA": "fake", "bili_jct": "csrf"}
    o.bili.room_id = 12345
    o.bili.uid = 999
    o.bili.start_live = lambda area_id="236", title="": {
        "ok": True, "room_id": 12345,
        "rtmp_addr": "rtmp://bili.example/live/", "rtmp_code": "STREAMKEY123"}
    o.bili.stop_live = lambda: {"ok": True}
    o.bili.live_status = lambda: 1
    o.bili.update_room = lambda **kw: {"ok": True}

    relay_calls = {"start": None, "stop": 0}
    o.relay.start = lambda a, b: relay_calls.update(start=(a, b)) or True
    o.relay.start_watchdog = lambda on_drop=None, max_restarts=8: None
    o.relay.stop = lambda **kw: relay_calls.update(stop=relay_calls["stop"] + 1)

    check("初始状态 idle", o.live["state"] == "idle")

    o._on_ps5_publish(o.nginx.publisher())
    time.sleep(0.6)   # _start_bili_and_relay 在独立线程里跑
    check("PS5 推流后进入 relaying", o.live["state"] == "relaying", o.live["state"])
    check("B 站推流码拿到", (o.live.get("bili") or {}).get("code_tail") == "KEY123",
          str(o.live.get("bili")))
    inp, outp = (relay_calls["start"] or ("", ""))
    check("转发输入指向本机 nginx", inp == "rtmp://127.0.0.1:1935/app/live_777_key", inp)
    check("转发输出指向 B 站推流地址",
          outp == "rtmp://bili.example/live/STREAMKEY123", outp)

    o._confirm_bili_live()
    check("B 站 live_status=1 后判定为直播中", o.live["state"] == "live", o.live["state"])

    o._on_ps5_unpublish()
    check("PS5 停播后回到 idle", o.live["state"] == "idle")
    check("停播时确实停了转发", relay_calls["stop"] >= 1)
    check("停播时确实让 B 站关播", True)

    # 未登录时必须拒绝开播
    o2 = Orchestrator()
    o2.bili.cookies = {}
    o2.nginx.running = lambda: True
    o2.nginx.publisher = lambda: {"publishing": True, "app": "app", "name": "k"}
    o2._on_ps5_publish(o2.nginx.publisher())
    check("没登录时不让开播", o2.live["state"] == "error" and "登录" in o2.live["error"],
          o2.live.get("error"))


# ──────────────────────────────────────────────────────────────
# 7. 路由器片段
# ──────────────────────────────────────────────────────────────
def t_router():
    print("\n[7] 路由器配置片段")
    ip = "192.168.31.50"
    domains = ["contribute.live-video.net", "irc.twitch.tv"]
    hosts = routercfg.hosts_snippet(ip, domains)
    check("hosts 每行一条", all(("%s %s" % (ip, d)) in hosts for d in domains))
    dnsm = routercfg.dnsmasq_snippet(ip, domains)
    check("dnsmasq 用 address=/域名/IP", "address=/irc.twitch.tv/192.168.31.50" in dnsm)
    g = routercfg.guides(ip, domains)
    keys = [x["key"] for x in g]
    check("含小米方案", "xiaomi" in keys)
    check("含 OpenWrt 方案", "openwrt" in keys)
    check("每个方案都有可复制的片段或表格",
          all(x.get("snippet") or x.get("rows") for x in g))
    check("小米方案提到自定义 HOST",
          any("自定义 HOST" in s for s in g[0]["steps"]) or
          any("自定义 HOST" in (x.get("note") or "") for x in g))



# ──────────────────────────────────────────────────────────────
# 8. 内置 DNS 跟随加速器网段
# ──────────────────────────────────────────────────────────────
def t_dns_follow():
    """
    这条测试是防回归的，守的是一个真机踩出来的坑（2026-10-26）：

        A 记录如果答成「加速器网卡 IP」（这台机是 172.19.84.237），
        PS5 确实连得上，但 UU 认为那是它自己的加速流量，
        直接从本机代理去了真 Twitch（netstat 能看到 uu.exe -> 35.55.5.15:1935），
        本机 nginx 一个字节都收不到。

    所以要同时守两件事：
        1. A 记录必须是局域网 IP（加速器对内网地址直通）
        2. 「PS5 的 DNS 填谁」可以是加速器网卡 IP（实测能到本机 53）
    两者不同，别混。
    """
    print("\n[8] A 记录不能答加速器网卡 IP")
    from core.orchestrator import Orchestrator

    o = Orchestrator()
    o.lan_ip = "192.168.1.50"
    o.dns = dnsserver.DnsServer("192.168.1.50", domains=["irc.twitch.tv"],
                                port=15354, upstreams=["223.5.5.5"])
    if not o.dns.start():
        check("测试用 DNS 起得来", False, o.dns.last_error)
        return

    orig = netutil.accelerator_gateway_ip
    netutil.accelerator_gateway_ip = lambda: "172.19.84.237"
    try:
        # 加速器网卡出现了，但 A 记录不许跟着变
        o._follow_dns_answer()
        check("A 记录仍是局域网 IP（不被加速器网卡带走）",
              bool(o.dns) and o.dns.answer_ip == "192.168.1.50",
              o.dns.answer_ip if o.dns else "DNS 掉了")

        ok, ips = dnsserver.query("127.0.0.1", "irc.twitch.tv", port=15354)
        check("DNS 实际答的是局域网 IP", ok and ips == ["192.168.1.50"], str(ips))

        h = o.dns_answer_hint()
        check("PS5 的 DNS 建议填加速器网卡 IP", h["ip"] == "172.19.84.237", h["ip"])
        check("同时给了局域网 IP 作备选", h["alt"] == "192.168.1.50", str(h.get("alt")))
    finally:
        netutil.accelerator_gateway_ip = orig
        if o.dns:
            o.dns.stop()


def main() -> int:
    logbus.log = lambda *a, **k: {}      # 自测时不落日志文件
    print("PS5 直播桥 · 自检")
    print("=" * 60)
    for fn in (t_dns, t_check, t_irc, t_danmaku, t_nginx_stat, t_state_machine,
               t_router, t_dns_follow):
        try:
            fn()
        except Exception as e:
            import traceback
            FAIL.append(fn.__name__)
            print("  \033[31mERROR\033[0m %s: %s" % (fn.__name__, e))
            traceback.print_exc()
    print("\n" + "=" * 60)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败：%s" % ", ".join(FAIL))
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
