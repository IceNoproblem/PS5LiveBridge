# PS5 直播桥 · 容器版
#
# 和桌面版的区别只有一处：这里没有 WinDivert，也没有窗口。
# 收流、转发、B 站开播、弹幕回注的逻辑完全一样。
# 所以加速器必须开在路由器上，PS5 保持「自动获取」，不需要改任何网络设置。
#
# 构建：
#   docker build -t ps5livebridge .
# 用 compose 更省事，见 docker-compose.yml

FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LANG=C.UTF-8 \
    TZ=Asia/Shanghai \
    PS5LB_DATA_DIR=/data \
    PS5LB_WEB_HOST=0.0.0.0

# 换国内源。deb.debian.org 在 NAS 上拉 8 MB 索引要几分钟，换完是几秒。
# 在国外的机器上构建，用 --build-arg APT_MIRROR=deb.debian.org 换回来。
ARG APT_MIRROR=mirrors.aliyun.com
ARG PIP_INDEX=https://mirrors.aliyun.com/pypi/simple/
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list; \
    fi

# nginx + rtmp 模块负责收流，ffmpeg 负责把流转到 B 站。
# libnginx-mod-rtmp 是 Debian 官方包，不用自己编译。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        nginx \
        libnginx-mod-rtmp \
        ffmpeg \
        ca-certificates \
        tzdata \
    && rm -rf /var/lib/apt/lists/* /etc/nginx/sites-enabled/default

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -i ${PIP_INDEX} -r requirements.txt

COPY . .

RUN mkdir -p /data/logs /data/nginx/conf \
    && chmod -R 755 /data

VOLUME ["/data"]

# 1935 收流 / 6667 弹幕 / 8080 nginx 状态 / 5000 界面
EXPOSE 1935 6667 8080 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/state' % os.environ.get('PS5LB_WEB_PORT','5000'), timeout=3)" || exit 1

CMD ["python", "main.py", "--headless"]
