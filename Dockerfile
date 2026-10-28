
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LANG=C.UTF-8 \
    TZ=Asia/Shanghai \
    PS5LB_DATA_DIR=/data \
    PS5LB_WEB_HOST=0.0.0.0

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
        tzdata \
    && rm -rf /var/lib/apt/lists/* /etc/nginx/sites-enabled/default

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -i ${PIP_INDEX} -r requirements.txt

COPY . .

RUN mkdir -p /data/logs /data/nginx/conf \
    && chmod -R 755 /data

VOLUME ["/data"]

EXPOSE 1935 6667 8080 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/state' % os.environ.get('PS5LB_WEB_PORT','5000'), timeout=3)" || exit 1

CMD ["python", "main.py", "--headless"]
