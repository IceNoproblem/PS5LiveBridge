# PS5BiliDanmaku
让Ps5游戏机不使用采集卡进行直播（B站），把 Bilibili 直播弹幕转发到 PS5屏幕上的轻量工具，全 Web 配置、Docker 一键部署。

BiliPS5

一款轻量级工具，通过IRC协议将B站直播弹幕转发到PS5，支持全Web界面配置，Docker一键部署。

✨ 功能特性
- 使用PS5内置直播程序进行直播（目前仅B站）使用了bao3/playstation 感谢

- 实时抓取B站直播间弹幕

- 转换弹幕为IRC协议，转发至PS5

- 全Web可视化配置面板（无需修改代码）

- Docker一键部署，运行稳定

- 自动重连、异常容错，保障长期运行

- 完整日志记录，方便问题排查

📋 部署要求

- 已安装Docker和Docker Compose

- PS5与部署机器处于同一局域网

- 5000端口（Web配置）、6667端口（IRC服务）未被占用

🚀 快速部署

1. 克隆项目

将本项目克隆到部署机器，或下载核心文件（danmaku_forward.py、Dockerfile、docker-compose.yml）至同一目录。

2. 启动服务

进入项目目录，执行以下命令：

docker compose up -d --build

3. 查看运行状态

docker compose logs -f

⚙️ 配置说明

Web配置面板

服务启动后，通过浏览器访问Web管理面板：

http://[部署机器IP]:5000

面板支持修改所有配置项，包括：

配置项

说明

默认值

BILIBILI_ROOM_ID

B站直播间数字ID

669827

TWITCH_CHANNEL

PS5 IRC频道名

yu332506767

IRC_HOST

IRC服务监听地址

0.0.0.0

IRC_PORT

IRC服务端口

6667

WEB_PORT

Web配置面板端口

5000

DANMAKU_POLL_INTERVAL

弹幕抓取间隔（秒）

3

MAX_SEEN_DANMAKU

最大弹幕缓存数（去重用）

1000

HEARTBEAT_TIMEOUT

PS5连接超时（秒）

300

USER_AGENT

B站接口请求头

Chrome浏览器标识

🎮 PS5配置说明

1. 在PS5上打开支持IRC弹幕的应用

2. 配置IRC服务器，参数如下：
        

  - 服务器地址：部署机器的局域网IP

  - 端口：6667

  - 频道名：与Web配置中的TWITCH_CHANNEL一致（需加#前缀）

3. 连接后即可实时接收B站弹幕

🐛 常见问题

Q1: Web面板无法访问

- 检查5000端口是否被占用

- 查看容器日志排查错误：docker compose logs -f

- 确认Docker端口映射正确（5000:5000）

Q2: 无法抓取弹幕

- 确认B站直播间ID为数字ID（非短ID）

- 检查部署机器能否访问B站API

- 查看日志中“抓取弹幕失败”的错误信息

Q3: PS5无法连接IRC

- 检查6667端口是否被占用

- 确认PS5与部署机器在同一局域网

- 检查防火墙是否放行6667端口

📁 项目结构

BiliPS5/
├── danmaku_forward.py    # 核心程序（弹幕抓取 + IRC服务 + Web管理）
├── Dockerfile            # Docker镜像构建配置
├── docker-compose.yml    # Docker容器编排配置
├── config.json           # 配置文件（自动生成）
└── logs/                 # 日志目录（自动生成）

📄 许可证

本项目采用MIT许可证开源 - 详见 LICENSE 文件。

📞 反馈与维护

- 问题反馈：提交GitHub Issue报告bug或需求

- 日志查看：docker compose logs -f ps5-bilibili-danmaku

- 重启服务：docker compose restart

✨ 版本更新

- v1.0: 基础功能 - 弹幕抓取+IRC转发

- v1.1: 新增Web配置面板，支持所有参数可视化修改

- v1.2: 修复弹幕抓取异常、变量作用域等问题，优化稳定性
