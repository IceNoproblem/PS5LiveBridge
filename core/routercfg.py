from __future__ import annotations

from . import paths

TWITCH_INGEST_NODES = [
    "contribute.live-video.net",
    "global-contribute.live-video.net",
    "ingest.global-contribute.live-video.net",
    "apn20.contribute.live-video.net",
    "hkg06.contribute.live-video.net",
    "hkg07.contribute.live-video.net",
    "dus01.contribute.live-video.net",
    "cph.contribute.live-video.net",
    "fra06.contribute.live-video.net",
    "sjc01.contribute.live-video.net",
    "dfw01.contribute.live-video.net",
    "iad05.contribute.live-video.net",
    "lax03.contribute.live-video.net",
    "lhr03.contribute.live-video.net",
    "nrt02.contribute.live-video.net",
    "sin03.contribute.live-video.net",
    "syd01.contribute.live-video.net",
    "tpe01.contribute.live-video.net",
    "icn01.contribute.live-video.net",
    "sea01.contribute.live-video.net",
]

_HOSTS_EXPAND = {
    "live-video.net": TWITCH_INGEST_NODES,
    "twitch.tv": ["irc.twitch.tv", "tmi.twitch.tv"],
    "ttvnw.net": [],
    "twitchcdn.net": [],
}


def _expand_for_hosts(domains) -> list:
    """把通配后缀展开成 hosts 能用的具体域名（去重、保序）。"""
    out = []
    for d in (domains or []):
        key = str(d).strip().lower()
        if key in _HOSTS_EXPAND:
            for sub in _HOSTS_EXPAND[key]:
                if sub not in out:
                    out.append(sub)
        else:
            if key and key not in out:
                out.append(key)
    return out


def hosts_snippet(ip: str, domains) -> str:
    """
    标准 hosts 格式：小米自定义 HOST / 斐讯 / 老毛子 / 梅林 通用。

    ⚠️ hosts 不支持通配符：写「live-video.net」不会匹配
    「hkg06.contribute.live-video.net」。所以这里必须把节点展开成具体条目。
    """
    real = _expand_for_hosts(domains)
    lines = ["# PS5 直播桥 · 路由器 hosts（本机 IP：%s）" % ip, ""]
    for d in real:
        lines.append("%s %s" % (ip, d))
    lines.append("")
    lines.append("# 说明：只重定向 PS5 的推流与弹幕通道，其余域名不受影响")
    lines.append("# 注意：hosts 不支持通配符，所以下面每个地区节点都单独列了一行。")
    lines.append("#       如果 PS5 连到了没列出的节点，会出现「开播了但没画面」。")
    return "\n".join(lines)


def dnsmasq_snippet(ip: str, domains) -> str:
    """OpenWrt / 梅林 / 老毛子 的 dnsmasq 写法。"""
    lines = ["# PS5 直播桥 · dnsmasq 配置（本机 IP：%s）" % ip, ""]
    for d in domains:
        lines.append("address=/%s/%s" % (d, ip))
    return "\n".join(lines)


def ikuai_rows(ip: str, domains) -> list:
    """爱快是表单式：域名 + IP 两列，直接给成表格数据。"""
    return [{"domain": d, "ip": ip} for d in domains]


def guides(ip: str, domains) -> list:
    """
    返回方案列表。第一个是主推（小米），第二个是软路由（OpenWrt），
    其余是备选，UI 收在「其他路由器」里。
    """
    domains = [d for d in (domains or [])
               if not any(x in str(d).lower()
                          for x in ("bilivideo.com", "bilibili.com", "biliapi"))]

    items = [
        {
            "key": "xiaomi",
            "title": "小米 / 红米路由器",
            "tag": "最常见",
            "primary": True,
            "steps": [
                "手机打开「小米 WiFi」App（或米家 App 里的路由器卡片），连上家里 WiFi。",
                "底部点「工具箱」→ 找到「自定义 HOST」（部分型号叫「Hosts 设置」）。",
                "把下面这段整段复制粘贴进输入框，保存。",
                "路由器会提示重启 DNS 或生效，等 10 秒即可。",
            ],
            "note": "小米路由器 Web 后台（192.168.31.1）没有这个入口，必须用 App。",
            "format": "hosts",
            "snippet": hosts_snippet(ip, domains),
        },
        {
            "key": "openwrt",
            "title": "OpenWrt / 软路由",
            "tag": "推荐，最稳",
            "primary": True,
            "steps": [
                "浏览器进路由器后台（默认 192.168.1.1）→ 登录。",
                "「网络」→「DHCP/DNS」→ 页面底部「自定义 DNS 解析」（或「静态地址」）。",
                "把下面这段粘进去（每行一条），点「保存并应用」。",
                "或者把同样的内容追加到 /etc/dnsmasq.conf 后执行 /etc/init.d/dnsmasq restart。",
            ],
            "note": "如果装了 AdGuard Home / MosDNS 之类接管了 53，要加到它们的上游里，"
                    "dnsmasq 这条会被绕过。",
            "format": "dnsmasq",
            "snippet": dnsmasq_snippet(ip, domains),
        },
        {
            "key": "asus",
            "title": "华硕 / 梅林固件",
            "primary": False,
            "steps": [
                "后台「系统管理」→「系统设置」，开启 JFFS 与 SSH，重启。",
                "SSH 登录后在 /jffs/configs 建 dnsmasq.conf.add，内容写：addn-hosts=/jffs/configs/hosts",
                "在 /jffs/configs/hosts 写入下面的 hosts 内容。",
                "执行 service restart_dnsmasq。",
            ],
            "format": "hosts",
            "snippet": hosts_snippet(ip, domains),
        },
        {
            "key": "ikuai",
            "title": "爱快 iKuai",
            "primary": False,
            "steps": [
                "后台「网络设置」→「DNS 设置」→「DNS 设置」标签。",
                "点「增加」，域名填左列、IP 填右列，逐条加完保存。",
            ],
            "format": "table",
            "rows": ikuai_rows(ip, domains),
            "snippet": hosts_snippet(ip, domains),
        },
        {
            "key": "padavan",
            "title": "老毛子 / Padavan",
            "primary": False,
            "steps": [
                "后台「内部网络(LAN)」→「DHCP 服务器」。",
                "「自定义 HOSTS 文件」里加下面的行，保存后重启路由器。",
            ],
            "format": "hosts",
            "snippet": hosts_snippet(ip, domains),
        },
        {
            "key": "hostsfile",
            "title": "实在不行：只改这台电脑",
            "primary": False,
            "steps": [
                "这个方案只对「本机自己」生效，PS5 不认，仅供排查用。",
                "以管理员身份打开记事本 → 打开 C:\\Windows\\System32\\drivers\\etc\\hosts。",
                "把下面内容追加进去，保存（不要带 .txt 后缀）。",
                "命令行执行 ipconfig /flushdns。",
            ],
            "note": "真正要让 PS5 生效，必须改路由器的解析，或者用本软件的内置 DNS。",
            "format": "hosts",
            "snippet": hosts_snippet(ip, domains),
        },
    ]

    if paths.PLATFORM == "docker":
        items = [x for x in items if x["key"] != "hostsfile"]
        for x in items:
            if x["key"] == "openwrt":
                x["note"] = ("如果路由器上开了加速器并接管了 53 端口，dnsmasq 这条会被绕过。"
                             "那种情况要么把规则加到加速器的 DNS 上游，要么改用端口转发——"
                             "把 1935 和 6667 两个端口转到 %s。" % ip)
    return items


def write_files(base_dir: str, ip: str, domains) -> list:
    """把两种格式各落一份文件，方便用户直接打开复制。返回写出的路径。"""
    import os
    out_dir = os.path.join(base_dir, "路由器配置")
    written = []
    try:
        os.makedirs(out_dir, exist_ok=True)
        p1 = os.path.join(out_dir, "hosts格式_直接粘贴.txt")
        with open(p1, "w", encoding="utf-8") as f:
            f.write(hosts_snippet(ip, domains))
        written.append(p1)
        p2 = os.path.join(out_dir, "dnsmasq格式_OpenWrt梅林.txt")
        with open(p2, "w", encoding="utf-8") as f:
            f.write(dnsmasq_snippet(ip, domains))
        written.append(p2)
    except Exception:
        pass
    return written
