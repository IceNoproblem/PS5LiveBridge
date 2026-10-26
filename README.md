# PS5 直播桥

把 PS5 的原生直播画面转到 B 站，并把 B 站弹幕送回 PS5 画面。
不破解、不装采集卡，全部在网络层完成。

成品在 [Releases](https://github.com/IceNoproblem/PS5LiveBridge/releases) 下载，解压即用。

| 第 1 步 · 配路由器 | 第 2 步 · 填 PS5 |
|---|---|
| ![](docs/screens/01-配路由器.png) | ![](docs/screens/02-填PS5.png) |

| 扫码登录 | 开播 |
|---|---|
| ![](docs/screens/03-扫码登录.png) | ![](docs/screens/04-开播.png) |

截图是在 Windows 11 虚拟机里实际运行的界面。

---

## 功能

- PS5 点「开始直播」后，自动在 B 站开播并转发画面，电脑上不需要额外操作
- B 站弹幕实时显示在 PS5 画面上，走 PS5 自带的 Twitch 弹幕通道
- PS5 停播后自动让 B 站关播
- 流复制转发，不转码，延迟 1~2 秒
- 可设置 B 站分区与标题

## 两种接入方式

PS5 开 Twitch 直播要经过加速器。加速器装在哪，决定了用哪种方式。

### 方式一：加速器装在路由器上

在路由器上把 5 个推流域名指向本机，PS5 什么都不用改。

```
contribute.live-video.net
global-contribute.live-video.net
live.twitch.tv
irc.twitch.tv
tmi.twitch.tv
```

只改这几个域名，PS5 上网、PSN、连索尼服务器都不受影响。
界面里按路由器型号给了小米、OpenWrt、华硕、爱快等写法，可以直接复制。

### 方式二：加速器装在电脑上

加速器装在电脑上时，PS5 的域名解析由加速器接管，改路由器不生效。
这种情况用软件内置的「直播桥」：在网络层把发往远端 1935 / 6667 的连接转到本机。

PS5 上进 设定 → 网络 → 设定互联网连接 → 当前网络 → Options → 高级设定，
把 IP 地址设置改成手动，再照抄加速器显示的 IP、掩码、网关、DNS 四项。
注意 PS5 的限制：IP 设为手动后 DNS 也必须手动填。

> 为什么电脑加速器不能用改 DNS 的办法：此时 PS5 在加速器的虚拟网段里，
> 电脑的局域网地址不在同一网段，包会被隧道接走；若把域名答成加速器网卡的地址，
> 加速器会当成自己的流量直接转发到 Twitch，本机收不到。
> 所以在 IP 层按端口改写，不碰 DNS。

## 两种输出方式

开播页可以选画面怎么出去，随时切换，切了立刻生效。

| 方式 | 做什么 | 需要登录 B 站 |
|---|---|---|
| 直接推流到 B 站 | 收流后自动开播并转发，直播间直接看到画面 | 需要 |
| 本地取流 | 只收流，给出一个 RTMP 地址，用 OBS 等软件自己拉 | 不需要 |

本地取流会给出类似 `rtmp://192.168.1.50:1935/app/live_xxxx` 的地址：
OBS 里加「媒体源」→ 取消勾选「本地文件」→ 填入地址，再由 OBS 推流到你想要的平台。
这样也能接其它直播平台，或者自己加转场、字幕、多路合成。

## 工作原理

```
① 握手   PS5 → 索尼鉴权 → 拿到 Twitch 推流地址      走加速器
② 推流   PS5 → contribute.live-video.net:1935       这一段转到本机
```

```
PS5 ──推流──→ 本机 nginx-rtmp ──→ ffmpeg -c copy ──→ B 站
                    │
                    └─→ 分辨率 / 码率 / 时长

B 站弹幕 ──WebSocket──→ 本程序 ──IRC 6667──→ PS5 画面
```

开播接口走 B 站直播姬通道，因此不受网页端人脸验证限制。

## 环境要求

| 项 | 说明 |
|---|---|
| 系统 | Windows 10 / 11，需要管理员权限 |
| PS5 与电脑 | 同一局域网，中间不能有 AP 隔离或访客网络 |
| 加速器 | UU、迅游、雷神等，装在电脑或路由器上 |
| B 站账号 | 已开通直播权限 |
| 端口 | 1935 收流、6667 弹幕、5000 界面、8080 状态 |

## 运行

### 发行版

1. 解压后双击 `PS5LiveBridge.exe`，UAC 弹窗选「是」
2. 浏览器窗口打开界面，按 5 个步骤走完
3. 以后每次只需打开软件，然后在 PS5 上开播

`tools\ffmpeg.exe` 是转发用的，缺了就不会往 B 站推流。

### 源码

```bat
python -m pip install -r requirements.txt
python main.py
```

`nginx-rtmp\` 和 `tools\ffmpeg.exe` 需要自行放入，不进仓库。

## 打包

```bat
scripts\build_exe.bat
```

产物在 `dist\PS5LiveBridge\`。脚本会把 pydivert 的 WinDivert 驱动一起打进去。

## 常见问题

| 现象 | 处理 |
|---|---|
| 检测显示路由器答的不是本机 IP | 检测页会直接写出路由器答的是哪个 IP，把 hosts 里的改成电脑 IP |
| 直播桥灯不绿 | 多半没给管理员权限，或被安全软件拦了驱动。右键以管理员身份运行 |
| 已接住连接一直是 0 | 正常，只有 PS5 新开一条推流连接才会跳 |
| PS5 提示有其他设备正在直播 | Twitch 侧有残留会话，在 PS5 上正常停止直播，等两三分钟再开 |
| B 站开播报人脸验证 | 在手机 B 站 App 完成认证 |

日志在程序目录 `logs\` 下，界面右上角也能实时看。

## 目录

```
PS5LiveBridge\
├─ main.py                启动、提权、单实例、防火墙
├─ core\
│  ├─ divert.py           端口级劫持（WinDivert）
│  ├─ dnsserver.py        内置 DNS
│  ├─ dnscheck.py         重定向检测
│  ├─ routercfg.py        路由器配置文案
│  ├─ nginxmgr.py         收流服务
│  ├─ bili.py             B 站登录 / 开播 / 关播 / 弹幕
│  ├─ danmaku.py          弹幕 WebSocket
│  ├─ ircsrv.py           PS5 弹幕通道
│  ├─ relay.py            ffmpeg 转发
│  ├─ services.py         事件循环
│  ├─ netutil.py          网络探测
│  ├─ paths.py            配置与路径
│  └─ orchestrator.py     状态机
├─ web\                   界面
├─ ui\window.py           窗口（Edge app 模式）
├─ scripts\               自检与打包
└─ logs\                  日志与进度
```

## 自检

```bat
python scripts\selftest.py
```

覆盖 DNS 劫持与转发、检测判定、IRC 全流程、弹幕解包、收流状态解析、直播状态机。

## 许可证

本项目使用 MIT 许可证，全文见 [LICENSE](LICENSE)。

分发包里还包含以下第三方组件，再分发时请保留各自的许可证与版权声明：

| 组件 | 许可证 |
|---|---|
| nginx 与 nginx-rtmp-module | BSD-2-Clause |
| ffmpeg | LGPL 2.1 或 GPL，取决于构建；可用 `ffmpeg -version` 查看 |
| WinDivert | LGPL 3.0 / GPL 2.0 双许可 |
| pydivert | LGPL 3.0 |

本项目的代码不包含上述组件的源码，仅以二进制形式随发行包分发。
