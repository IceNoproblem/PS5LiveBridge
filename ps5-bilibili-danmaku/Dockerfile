# 推荐用Python官方镜像（Debian基础，稳定无兼容问题）
FROM python:3.12.12-slim

# 设置工作目录
WORKDIR /app

# ===== 核心修复：适配不同镜像的源修改逻辑 =====
# 先判断文件是否存在，不存在则跳过（避免报错）
RUN if [ -f /etc/apt/sources.list ]; then \
        sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list && \
        sed -i 's/security.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list; \
    elif [ -f /etc/apk/repositories ]; then \
        sed -i 's/dl-cdn.alpinelinux.org/mirrors.aliyun.com/g' /etc/apk/repositories; \
    fi

# 安装依赖（精简版，仅保留必要组件）
RUN apt-get update && apt-get install -y --no-install-recommends \
        net-tools \
        procps \
        && rm -rf /var/lib/apt/lists/*

# 安装Python依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
        flask \
        requests \
        asyncio \
        urllib3

# 复制应用代码
COPY . .

# 创建日志目录
RUN mkdir -p /app/logs && chmod 777 /app/logs

# 暴露端口（5000 Web，6667 IRC）
EXPOSE 5000 6667

# 设置时区
ENV TZ=Asia/Shanghai
ENV PYTHONUNBUFFERED=1
ENV PYTHONIOENCODING=utf-8

# 启动命令
CMD ["python", "danmaku_forward.py"]