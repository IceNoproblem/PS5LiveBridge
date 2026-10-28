# PS5 直播桥

<p align="center"><img src="assets/icon.png" width="104" alt="PS5 直播桥"></p>

<p align="center">
  <a href="https://space.bilibili.com/2250922"><img src="https://img.shields.io/badge/Bilibili-主页-00A1D6?logo=bilibili&logoColor=white" alt="Bilibili"></a>
  <img src="https://img.shields.io/badge/QQ群-1092104797-12B7F5?logo=tencentqq&logoColor=white" alt="QQ 群 1092104797">
</p>

PS5 点「开始直播」，画面自动转到 B 站，弹幕送回 PS5。

不破解、不装采集卡，全在网络层做。

> 本来原来的也能用，但是很多人弄不明白那个dns劫持，索性用分步引导的方式重新做了一个windows版，毕竟会弄容器的还是少数，所以这个项目以后以win版为主。
> 之前的容器版同步进行的升级。

## 装哪个

两个版本功能一样，看你有啥。

| 你手上 | 装这个 | 加速器装在哪 |
|---|---|---|
| 一台常开的 Windows 电脑 | **桌面版**，解压双击 | 电脑、路由器都行 |
| NAS / 常年开机的 Linux | **容器版**，后台常驻 | 只能装路由器 |

开发重心在 Windows 版，因为会弄容器的人少。但你要是已经有 NAS，容器版更省事——装一次，之后全天候在后台等着，PS5 开机就能播，不用去开电脑。

桌面版成品在 [Releases](https://github.com/IceNoproblem/PS5LiveBridge/releases) 下载，解压即用。
容器版用仓库里的 `Dockerfile` 自己构建，见[容器版部署](#docker)。

| 第 1 步 · 配路由器 | 第 2 步 · 填 PS5 |
|---|---|
| ![](docs/screens/01-配路由器.png) | ![](docs/screens/02-填PS5.png) |

| 扫码登录 | 开播 |
|---|---|
| ![](docs/screens/03-扫码登录.png) | ![](docs/screens/04-开播.png) |

图是 Windows 桌面版实际跑的界面。容器版一样，只是第 3 步换成确认页。

---

## 它能干什么

- PS5 点开播，B 站自动开播并转发画面，电脑上不用再操作
- B 站弹幕实时显示在 PS5 画面上，走 PS5 自带的 Twitch 弹幕通道
- PS5 停播，B 站自动关播
- 流复制转发，不转码，延迟 1~2 秒
- 分区、标题可设

## 怎么工作

```
① 握手   PS5 → 索尼鉴权 → 拿到 Twitch 推流地址     走加速器
② 推流   PS5 → contribute.live-video.net:1935      这一段转到本机
```

```
PS5 ──推流──→ 本机 nginx-rtmp ──→ ffmpeg -c copy ──→ B 站
                    │
                    └─→ 分辨率 / 码率 / 时长

B 站弹幕 ──WebSocket──→ 本程序 ──IRC 6667──→ PS5 画面
```

开播走 B 站直播姬通道，所以不受网页端人脸验证限制。

## 两种接入方式

PS5 开 Twitch 直播要过加速器。加速器装哪，决定你用哪种。

### 加速器装在路由器上

把 5 个推流域名指向跑本程序的机器，PS5 什么都不用改。

```
contribute.live-video.net
global-contribute.live-video.net
live.twitch.tv
irc.twitch.tv
tmi.twitch.tv
```

只改这 5 个。PS5 上网、PSN、连索尼服务器都不受影响。
界面里按路由器型号给了小米、OpenWrt、华硕、爱快等写法，直接复制。

**容器版只能这样。**

### 加速器装在电脑上

这时候 PS5 的域名由加速器接管，改路由器没用。用软件里的「直播桥」，在网络层把发往远端 1935 / 6667 的连接转到本机。

PS5 上：设定 → 网络 → 设定互联网连接 → 当前网络 → Options → 高级设定，IP 地址设成手动，照抄加速器显示的 IP、掩码、网关、DNS。

**注意那个坑**：IP 设成手动之后，DNS 也必须手动填，PS5 不给留自动。

为什么不改 DNS 解决：这时 PS5 在加速器的虚拟网段里，电脑的局域网地址不在同段，包会被隧道接走；把域名答成加速器网卡地址，加速器又当成自己的流量直接转去 Twitch，本机收不到。所以只能在 IP 层按端口改写。

容器动不了宿主机网络栈，做不了这件事。容器版没有这条路。

## 两种输出方式

开播页选，随时切。

| 方式 | 做什么 | 要登录 B 站 |
|---|---|---|
| 直接推流到 B 站 | 收流后自动开播转发，直播间直接看到画面 | 要 |
| 本地取流 | 只收流，给一个 RTMP 地址，用 OBS 拉 | 不要 |

本地取流给的是 `rtmp://192.168.1.50:1935/app/live_xxxx` 这种地址。
OBS 里加「媒体源」→ 取消勾选「本地文件」→ 填地址，再推到你想要的平台。
这样也能接别的平台，或者自己加转场、字幕、多路合成。

## 环境要求

**桌面版**：Windows 10 / 11 + 管理员权限。不转码，内存几百 MB 就够。
**容器版**：Linux + Docker（NAS、软路由、小主机都行）。容器用 host 网络，1935 / 6667 / 5000 不能和别的服务撞。
**共同**：B 站账号要开直播权限；PS5 和跑程序的机器在同一局域网，中间不能有 AP 隔离或访客网络。

## 跑起来

### Windows 桌面版

解压后双击 `PS5LiveBridge.exe`，UAC 选是，浏览器打开界面，按 5 步走完。
以后每次只需要开软件，然后在 PS5 上开播。

`tools\ffmpeg.exe` 是转发用的，缺了不会往 B 站推流。

源码跑：

```bat
python -m pip install -r requirements.txt
python main.py
```

`nginx-rtmp\` 和 `tools\ffmpeg.exe` 自己放进去，不进仓库。

<a id="docker"></a>

### 容器版

```bash
git clone https://github.com/IceNoproblem/PS5LiveBridge.git
cd PS5LiveBridge
docker compose up -d --build
```

起来后开 `http://<这台机器的 IP>:5000`，界面和桌面版一样。

数据和登录状态都在 `./data`，备份或搬家只拷这个目录。

只跑容器，不装数据库。镜像里自带 nginx、nginx-rtmp、ffmpeg。

端口：

| 端口 | 用途 | 能改吗 |
|---|---|---|
| 1935 | PS5 推流进来 | 能，但要和路由器 hosts 那条对应 |
| 6667 | PS5 弹幕通道 | 同上 |
| 5000 | 界面 | 能，`PS5LB_WEB_PORT` |
| 8099 | nginx 状态，程序自己读 | 能，`PS5LB_STAT_PORT` |

环境变量，写在 compose 的 `environment` 里：

| 变量 | 默认 | 说明 |
|---|---|---|
| `PS5LB_LAN_IP` | 自动探测 | 多网卡探测错的时候手填 |
| `PS5LB_WEB_PORT` | 5000 | 界面端口 |
| `PS5LB_OUTPUT_MODE` | direct | `direct` 自动开播转发，`local` 只收流 |
| `PS5LB_AREA_ID` | 236 | 分区，236 是主机游戏 |
| `PS5LB_AUTO_START_LIVE` | true | PS5 一推流就自动开播 |

改了环境变量要 `docker compose up -d` 重建才生效。分区、标题这种能在界面改的，直接改界面。

## 更新

**桌面版**自己会查新版，启动半小时后第一次，之后每 12 小时一次。有新版界面顶部出提示，点「立即更新」自动完成：下载 → 备份 → 换文件 → 重启。

`config.json`、B 站登录、日志都不会动。旧版备份在 `data\backup-v*-*\`，出问题手动拷回去。不想让它查就在 `config.json` 里把 `AUTO_UPDATE` 改成 `false`。

**容器版**不能替换自己，走镜像。推荐一次性开自动更新：

```bash
docker compose --profile auto-update up -d
```

之后每天凌晨 4 点自动拉新版重启，`./data` 不受影响。不开也行，界面发现新版会给命令：

```bash
docker compose pull && docker compose up -d
```

镜像是 GitHub Actions 构建的，发布到 `ghcr.io/icenoproblem/ps5livebridge`，跟 `main` 走，打 `v*` 标签时也发一份带版本号的。

## 打包

```bat
scripts\build_exe.bat
```

产物在 `dist\PS5LiveBridge\`，会把 pydivert 的 WinDivert 驱动一起打进去。

## 卡住了看这里

**双击后窗口一闪就没** —— 先看程序目录的 `启动轨迹.txt`（死在哪步写在哪步）和 `崩溃日志.txt`。v4.5 起浏览器开不出小窗会自动改开默认浏览器，一般不闪退了。还闪退多半是这三个：在压缩包里直接双击了（必须先解压整个文件夹）；只拷了 exe 没拷旁边的 `_internal`；杀软拦了网络驱动（整个文件夹加信任）。

**检测显示路由器答的不是本机 IP** —— 检测页会写出路由器答的是哪个 IP，把 hosts 里那条改掉。

**直播桥灯不绿** —— 多半没给管理员权限，或者被安全软件拦了驱动。右键以管理员运行。

**已接住连接一直是 0** —— 正常。只有 PS5 新开一条推流连接才会跳。

**PS5 提示有其他设备正在直播** —— Twitch 侧有残留会话。在 PS5 上正常停止直播，等两三分钟。

**B 站开播要人脸验证** —— 手机 B 站 App 里认证。

容器版另外几条：

**容器起来了界面打不开** —— 换 `PS5LB_WEB_PORT`，或者 5000 被 NAS 上别的服务占了。

**界面里本机 IP 不对** —— 多网卡探测挑错了，compose 里写死 `PS5LB_LAN_IP`。

**路由器 hosts 配了、检测不过** —— 路由器上的加速器可能接管了 DNS，dnsmasq / hosts 被绕过。把规则加到加速器的 DNS 上游，或者改用端口转发。

**收流起不来** —— 看 `docker logs ps5-live-bridge`，多半 1935 被别的容器占了。

日志在 `data/logs/`，界面右上角也能实时看。

## 目录

```
PS5LiveBridge\
├─ main.py                启动、提权、单实例、防火墙
├─ Dockerfile             容器版镜像
├─ docker-compose.yml     容器版部署
├─ core\
│  ├─ divert.py           端口级劫持（WinDivert，仅 Windows）
│  ├─ dnsserver.py        内置 DNS
│  ├─ dnscheck.py         重定向检测
│  ├─ routercfg.py        路由器配置文案
│  ├─ nginxmgr.py         收流服务
│  ├─ bili.py             B 站登录 / 开播 / 关播 / 弹幕
│  ├─ danmaku.py          弹幕 WebSocket
│  ├─ ircsrv.py           PS5 弹幕通道
│  ├─ relay.py            ffmpeg 转发
│  ├─ services.py         事件循环
│  ├─ netutil.py          网络探测、降权启动浏览器
│  ├─ updater.py          自动更新
│  ├─ paths.py            配置与路径，以及当前平台能做什么
│  └─ orchestrator.py     状态机
├─ web\                   界面
├─ ui\window.py           窗口（Edge app 模式，仅桌面版）
├─ scripts\               自检与打包
└─ logs\                  日志与进度
```

## 自检

```bat
python scripts\selftest.py
```

覆盖 DNS 劫持与转发、检测判定、IRC 全流程、弹幕解包、收流状态解析、直播状态机。

> 自己用的，糙。卡住开个 issue，看到会回。

## 许可证

MIT，全文见 [LICENSE](LICENSE)。

分发包里还带着这些第三方组件，再分发时保留各自的许可证和版权声明：

| 组件 | 许可证 |
|---|---|
| nginx 与 nginx-rtmp-module | BSD-2-Clause |
| ffmpeg | LGPL 2.1 或 GPL，取决于构建，`ffmpeg -version` 可查 |
| WinDivert | LGPL 3.0 / GPL 2.0 双许可 |
| pydivert | LGPL 3.0 |

容器镜像基于 `python:3.11-slim-bookworm`，里面的 Debian 组件（nginx、libnginx-mod-rtmp、ffmpeg 等）沿用各自发行版许可证。

本项目的代码不含上述组件源码，只以二进制形式随发行包分发。
