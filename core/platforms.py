"""
多平台的定义。界面、配置、日志都从这里取，不要各处各写一份。

现在支持的四个新平台都走 RTMP，用户从对应后台把推流地址整段粘过来，
程序只负责把同一路流转发过去。B 站不一样，它有开播接口，地址是运行时拿的。
"""

# 键名同时用作配置键、接口路径参数和日志标签，别随便改
#
# server 是这个平台固定的推流服务器地址。填了它，界面上就只要用户粘「推流密钥」，
# 不用连服务器地址一起复制。抖音和斗鱼的地址每场都可能变，所以留空，整条粘。
PLATFORMS = {
    "bilibili": {
        "name": "B 站",
        "tag": "B站",
        "manual_url": False,
        "hint": "",
        "server": "",
        "dm_hint": "登录 B 站后自动接",
    },
    "douyin": {
        "name": "抖音",
        "tag": "抖音",
        "manual_url": True,
        # 抖音的第三方推流要在直播伴侣或创作者中心里开，有粉丝门槛
        "hint": "直播伴侣 → 推流码，或创作者中心 → 直播管理 → 电脑直播",
        # 弹幕只要直播间号；填直播间链接也行
        "dm_hint": "直播间链接后面那串数字，例如 live.douyin.com/123456789",
        "gate": "第三方推流一般要 1000 粉或企业号，够不到就先用「直播伴侣」推",
        "host": "push-rtmp.douyin.com",
        # 地址里带 expire，每场都不一样，没法固定
        "server": "",
    },
    "douyu": {
        "name": "斗鱼",
        "tag": "斗鱼",
        "manual_url": True,
        "hint": "主播中心 → 直播管理 → 推流设置，密钥默认隐藏，点显示要二次验证",
        "dm_hint": "房间号，靓号也认，例如 6657",
        "gate": "",
        "host": "tx.douyu.com",
        # danmupro / sendtc 两个域名都见过，不固定
        "server": "",
    },
    "kuaishou": {
        "name": "快手",
        "tag": "快手",
        "manual_url": True,
        "hint": "快手直播中心 → OBS 推流，复制推流码；或创作者服务平台",
        "dm_hint": "暂不支持",
        "gate": "",
        "host": "",
        "server": "rtmp://live.kuaishou.com/live",
    },
    "xiaohongshu": {
        "name": "小红书",
        "tag": "小红书",
        "manual_url": True,
        # 小红书的密钥必须用手机 App 的 6 位码去换，直接给的是过期串
        "hint": "手机 App 预直播页 →「电脑」→ 拿 6 位数字，去 xiaohongshu.com/zhibo 换串流密钥",
        "dm_hint": "暂不支持",
        "gate": "要先在手机端开通直播权限",
        "host": "",
        "server": "rtmp://live-push.xhscdn.com/live",
    },
}

# 顺序决定界面上怎么排，B 站放最前面
ORDER = ("bilibili", "douyin", "douyu", "kuaishou", "xiaohongshu")

# 需要用户自己填推流地址的平台
MANUAL = tuple(k for k in ORDER if PLATFORMS[k]["manual_url"])


def name(key: str) -> str:
    return (PLATFORMS.get(key) or {}).get("name") or key


def tag(key: str) -> str:
    return (PLATFORMS.get(key) or {}).get("tag") or key


def full_url(key: str, typed: str) -> str:
    """
    把用户粘的东西整理成完整推流地址。

    小红书和快手的服务器地址是固定的，用户只需要粘密钥，这里补上前缀。
    用户整条粘过来也认，原样返回。抖音、斗鱼没有固定服务器，只能整条粘，
    少了 rtmp:// 就说明粘错了，直接返回原值让上层去报错。
    """
    s = (typed or "").strip()
    if not s:
        return ""
    if s.lower().startswith(("rtmp://", "rtmps://", "rtmpt://")):
        return s
    base = (PLATFORMS.get(key) or {}).get("server") or ""
    if not base:
        return s
    return base.rstrip("/") + "/" + s.lstrip("/")
