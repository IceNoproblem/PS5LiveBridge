# PS5 直播桥

把 PS5 的原生直播画面转到 B 站，并把 B 站弹幕送回 PS5 画面。
不破解、不装采集卡，全部在网络层完成。

> 本想买采集卡的，后来发现不用，网络层就能搞。

两种跑法，功能一样，选一个：

| | 适合 | 加速器装在哪 |
|---|---|---|
| **Windows 桌面版** | 手边有一台常开的 Windows 电脑 | 电脑上、路由器上都可以 |
| **容器版** | 有 NAS / 常年开机的 Linux 机器 | 必须装在路由器上 |

桌面版成品在 [Releases](https://github.com/IceNoproblem/PS5LiveBridge/releases) 下载，解压即用。
容器版用本仓库的 `Dockerfile` 自己构建，见[容器版部署](#docker)。

| 第 1 步 · 配路由器 | 第 2 步 · 填 PS5 |
|---|---|
| ![](docs/screens/01-配路由器.png) | ![](docs/screens/02-填PS5.png) |

| 扫码登录 | 开播 |
|---|---|
| ![](docs/screens/03-扫码登录.png) | ![](docs/screens/04-开播.png) |

截图是 Windows 桌面版实际运行的界面。容器版界面相同，只是第 3 步换成确认页
（加速器在路由器上，PS5 不用改设置）。

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

在路由器上把 5 个推流域名指向运行本程序的机器，PS5 什么都不用改。

```
contribute.live-video.net
global-contribute.live-video.net
live.twitch.tv
irc.twitch.tv
tmi.twitch.tv
```

只改这几个域名，PS5 上网、PSN、连索尼服务器都不受影响。
界面里按路由器型号给了小米、OpenWrt、华硕、爱快等写法，可以直接复制。

**容器版只能用这一种。**

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

容器动不了宿主机的网络栈，做不了这件事，所以容器版没有这条路。

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

> 改 DNS 那条路真机上走不通，现在这套是试出来的。下面那些注意事项都是踩过的。

## 环境要求

**桌面版**

| 项 | 说明 |
|---|---|
| 系统 | Windows 10 / 11，需要管理员权限 |
| 内存 | 不转码，占用很小，几百 MB 就够 |
| 加速器 | UU、迅游、雷神等，装在电脑或路由器上 |
| B 站账号 | 已开通直播权限 |

**容器版**

| 项 | 说明 |
|---|---|
| 系统 | Linux + Docker（NAS、软路由、小主机都行） |
| 加速器 | 必须装在路由器上 |
| 网络 | 容器用 host 网络；1935 / 6667 / 5000 三个端口不能和别的服务撞 |
| B 站账号 | 已开通直播权限 |

两种形态共同的要求：PS5 和运行本程序的机器在同一局域网，中间不能有 AP 隔离或访客网络。

## 运行

### Windows 桌面版

发行版：解压后双击 `PS5LiveBridge.exe`，UAC 弹窗选「是」，浏览器窗口打开界面，按 5 个步骤走完。
以后每次只需打开软件，然后在 PS5 上开播。

`tools\ffmpeg.exe` 是转发用的，缺了就不会往 B 站推流。

源码：

```bat
python -m pip install -r requirements.txt
python main.py
```

`nginx-rtmp\` 和 `tools\ffmpeg.exe` 需要自行放入，不进仓库。

<a id="docker"></a>

### 容器版（NAS / Linux）

```bash
git clone https://github.com/IceNoproblem/PS5LiveBridge.git
cd PS5LiveBridge
docker compose up -d --build
```

起来后浏览器打开 `http://<这台机器的 IP>:5000`，界面和桌面版一样。

数据和登录状态都在 `./data` 里，备份或迁移只拷这个目录。

**只跑容器，不装数据库、不装别的服务**，镜像里自带 nginx、nginx-rtmp 和 ffmpeg。

端口说明：

| 端口 | 用途 | 能不能改 |
|---|---|---|
| 1935 | PS5 推流进来 | 能，但要和路由器 hosts 里那条对应 |
| 6667 | PS5 弹幕通道 | 同上 |
| 5000 | 界面 | 能，改 `PS5LB_WEB_PORT` |
| 8099 | nginx 状态，只给程序自己读 | 能，改 `PS5LB_STAT_PORT` |

常用环境变量（写进 `docker-compose.yml` 的 `environment`）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `PS5LB_LAN_IP` | 自动探测 | 这台机器的局域网 IP，多网卡探测错时手填 |
| `PS5LB_WEB_PORT` | 5000 | 界面端口 |
| `PS5LB_OUTPUT_MODE` | direct | `direct` 自动开播转发 / `local` 只收流给 OBS 拉 |
| `PS5LB_AREA_ID` | 236 | B 站分区，236 是主机游戏 |
| `PS5LB_AUTO_START_LIVE` | true | PS5 一推流就自动开播 |

> 改了环境变量要 `docker compose up -d` 重建容器才生效。
> 分区、标题这类能在界面里改的，直接改界面，不用动环境变量。

**容器版做不到的事**：加速器装在电脑上时用不了。那是 Windows 桌面版的
「直播桥」在 IP 层改写端口实现的，容器动不了宿主机的网络栈。

## 打包

```bat
scripts\build_exe.bat
```

产物在 `dist\PS5LiveBridge\`。脚本会把 pydivert 的 WinDivert 驱动一起打进去。

## 常见问题

| 现象 | 处理 |
|---|---|
| 检测显示路由器答的不是本机 IP | 检测页会直接写出路由器答的是哪个 IP，把 hosts 里的改成这台机器的 IP |
| 直播桥灯不绿 | 多半没给管理员权限，或被安全软件拦了驱动。右键以管理员身份运行 |
| 已接住连接一直是 0 | 正常，只有 PS5 新开一条推流连接才会跳 |
| PS5 提示有其他设备正在直播 | Twitch 侧有残留会话，在 PS5 上正常停止直播，等两三分钟再开 |
| B 站开播报人脸验证 | 在手机 B 站 App 完成认证 |

容器版另外几条：

| 现象 | 处理 |
|---|---|
| 容器起来了但界面打不开 | 换 `PS5LB_WEB_PORT`，或者看 5000 是不是被 NAS 上别的服务占了 |
| 界面里本机 IP 明显不对 | 多网卡时自动探测会挑错，在 compose 里写死 `PS5LB_LAN_IP` |
| 路由器 hosts 配了、检测却不过 | 路由器上的加速器可能接管了 DNS，dnsmasq / hosts 被绕过。把规则加到加速器的 DNS 上游，或改用端口转发 |
| 收流服务起不来 | 看 `docker logs ps5-live-bridge`，多半是 1935 被别的容器占了 |

日志在 `data/logs/` 下，界面右上角也能实时看。

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
│  ├─ nginxmgr.py         收流服务（Windows 用随包的 exe，Linux 用系统 nginx）
│  ├─ bili.py             B 站登录 / 开播 / 关播 / 弹幕
│  ├─ danmaku.py          弹幕 WebSocket
│  ├─ ircsrv.py           PS5 弹幕通道
│  ├─ relay.py            ffmpeg 转发
│  ├─ services.py         事件循环
│  ├─ netutil.py          网络探测
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

本项目使用 MIT 许可证，全文见 [LICENSE](LICENSE)。

分发包里还包含以下第三方组件，再分发时请保留各自的许可证与版权声明：

| 组件 | 许可证 |
|---|---|
| nginx 与 nginx-rtmp-module | BSD-2-Clause |
| ffmpeg | LGPL 2.1 或 GPL，取决于构建；可用 `ffmpeg -version` 查看 |
| WinDivert | LGPL 3.0 / GPL 2.0 双许可 |
| pydivert | LGPL 3.0 |

容器镜像另外基于 `python:3.11-slim-bookworm`，其中的 Debian 组件（nginx、
libnginx-mod-rtmp、ffmpeg 等）沿用各自发行版的许可证。

本项目的代码不包含上述组件的源码，仅以二进制形式随发行包分发。
