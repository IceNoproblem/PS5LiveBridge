#!/bin/bash

set -e

NAS_IP="${1:-192.168.110.99}"
NAS_USER="${2:-yinpengyu}"
REMOTE_BASE="/vol3/1000/system/docker/ps5-live-bridge"
LOCAL_BASE="$(cd "$(dirname "$0")/.." && pwd)"
NAS_PASS="131302426aA.."

echo "=== PS5LiveBridge NAS 部署 ==="
echo "本地: $LOCAL_BASE"
echo "远程: $NAS_USER@$NAS_IP:$REMOTE_BASE"

echo ""
echo "[1/4] 打包..."
cd "$LOCAL_BASE"
tar czf /tmp/ps5lb_deploy.tar.gz \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.git' \
    --exclude='.DS_Store' \
    --exclude='logs/' \
    --exclude='data/' \
    --exclude='config.json' \
    --exclude='bili_credential.json' \
    --exclude='*.log' \
    --exclude='dist/' \
    --exclude='build/' \
    --exclude='*.bak' \
    --exclude='nginx-rtmp/nginx.exe' \
    --exclude='启动轨迹.txt' \
    --exclude='崩溃日志.txt' \
    --exclude='rtmp_progress.txt' \
    --exclude='ps5livebridge-crash.log' \
    .
echo "打包完成: $(ls -lh /tmp/ps5lb_deploy.tar.gz | awk '{print $5}')"

echo ""
echo "[2/4] 上传..."
expect << EXPECT_EOF
set timeout 60
spawn scp /tmp/ps5lb_deploy.tar.gz $NAS_USER@$NAS_IP:$REMOTE_BASE/
expect {
    "password:" { send "$NAS_PASS\r" }
    timeout { puts "SCP timeout"; exit 1 }
}
expect eof
EXPECT_EOF

echo ""
echo "[3/4] 解压 + 重建镜像..."
expect << EXPECT_EOF
set timeout 600
spawn ssh $NAS_USER@$NAS_IP "cd $REMOTE_BASE && tar xzf ps5lb_deploy.tar.gz && rm ps5lb_deploy.tar.gz && echo '解压完成' && docker compose down && docker compose build 2>&1 | tail -3 && docker compose up -d 2>&1 | tail -3"
expect {
    "password:" { send "$NAS_PASS\r" }
    timeout { puts "SSH timeout"; exit 1 }
}
expect eof
EXPECT_EOF

echo ""
echo "[4/4] 验证..."
expect << EXPECT_EOF
set timeout 30
spawn ssh $NAS_USER@$NAS_IP "docker exec ps5-live-bridge python3 -c \"from core.statemachine import LiveState; print('v5.0 状态机:', LiveState.IDLE.value)\" && docker logs ps5-live-bridge 2>&1 | tail -3"
expect {
    "password:" { send "$NAS_PASS\r" }
    timeout { puts "SSH timeout"; exit 1 }
}
expect eof
EXPECT_EOF

echo ""
echo "=== 部署完成 ==="
echo "界面: http://$NAS_IP:5000"
