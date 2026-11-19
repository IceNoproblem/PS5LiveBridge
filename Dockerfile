
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LANG=C.UTF-8 \
    TZ=Asia/Shanghai \
    PS5LB_DATA_DIR=/data \
    PS5LB_WEB_HOST=0.0.0.0 \
    PS5LB_RTMP_IMPL=auto

ARG APT_MIRROR=mirrors.aliyun.com
ARG PIP_INDEX=https://mirrors.aliyun.com/pypi/simple/
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list; \
    fi

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        nginx \
        libnginx-mod-rtmp \
        ffmpeg \
        ca-certificates \
        curl \
        tzdata \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/* /etc/nginx/sites-enabled/default

# 收流端：rtmp-forwarder（Rust，Apache-2.0，上游直接发 Linux 静态二进制）。
# 装上以后容器和桌面版走同一条路：一份流复制到多个平台，不用为每个平台
# 单起一个 ffmpeg。没下到就跳过，收流会自动退回 nginx-rtmp。
ARG TARGETARCH
ARG RTMF_VERSION=v0.1.1
# 每个 curl 都带连接和总超时：许可证副本只是附带的，取不到不该把构建挂死
RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
        arm64|aarch64) _arch=aarch64-unknown-linux-musl ;; \
        *)             _arch=x86_64-unknown-linux-musl ;; \
    esac; \
    _base="https://github.com/nanpuyue/rtmp-forwarder/releases/download/${RTMF_VERSION}"; \
    if curl -fsSL --connect-timeout 10 --max-time 60 --retry 3 --retry-delay 2 -o /tmp/rtmf "${_base}/rtmp-forwarder-${_arch}"; then \
        if python -c "import sys;sys.exit(0 if open('/tmp/rtmf','rb').read(4)==b'\x7fELF' else 1)"; then \
            install -m 0755 /tmp/rtmf /usr/local/bin/rtmf; \
            mkdir -p /usr/share/doc/rtmp-forwarder; \
            curl -fsSL --connect-timeout 5 --max-time 20 -o /usr/share/doc/rtmp-forwarder/LICENSE \
                "https://raw.githubusercontent.com/nanpuyue/rtmp-forwarder/${RTMF_VERSION}/LICENSE" || true; \
            echo "rtmp-forwarder ${RTMF_VERSION} (${_arch}) 装入完成"; \
        else \
            echo "警告：下载到的不是 Linux 可执行文件，跳过"; \
        fi; \
        rm -f /tmp/rtmf; \
    else \
        echo "警告：rtmp-forwarder 没下载到，收流将回退 nginx-rtmp"; \
    fi

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -i ${PIP_INDEX} -r requirements.txt

COPY . .

RUN mkdir -p /data/logs /data/nginx/conf \
    && chmod -R 755 /data

VOLUME ["/data"]

# 1935 收流 / 6667 弹幕 IRC / 8098 本地取流(HTTP-FLV) / 8080 nginx 统计 / 5000 界面
EXPOSE 1935 6667 8080 8098 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/state' % os.environ.get('PS5LB_WEB_PORT','5000'), timeout=3)" || exit 1

CMD ["python", "main.py", "--headless"]
