#!/usr/bin/env bash
# ============================================================
#  开机自启与健康守护
#
#  用法：
#    sudo ./deploy/install_autostart.sh              # 安装（自动识别用 Docker）
#    sudo ./deploy/install_autostart.sh --systemd    # 改用 systemd 部署
#
#  作用：
#    1. 确保 Docker 开机自启（很多服务器装完 Docker 是没开自启的）
#    2. 创建 cisco-backup-backup.service，用 Docker 方式启动容器
#    3. 每 2 分钟检查一次，容器停止或健康检查失败则拉起
# ============================================================
set -euo pipefail

cd "$(dirname "$0")/.."
INSTALL_DIR="$(pwd)"
SVC_NAME="cisco-backup-backup"
SVC_FILE="/etc/systemd/system/${SVC_NAME}.service"
USE_SYSTEMD_DEPLOY=false

[[ "${1:-}" == "--systemd" ]] && USE_SYSTEMD_DEPLOY=true

if [[ $EUID -ne 0 ]]; then
    echo "请用 root 或 sudo 运行"
    exit 1
fi

echo "==========================================="
echo " 配置开机自启"
echo "==========================================="

# ------------------------------------------------------------ 1. Docker 自启
if [[ "$USE_SYSTEMD_DEPLOY" == "false" ]]; then
    if ! command -v docker >/dev/null; then
        echo "[错误] 未找到 docker，请先安装 Docker"
        exit 1
    fi

    echo "[1/3] 启用 Docker 开机自启..."
    if command -v systemctl >/dev/null && systemctl list-unit-files docker.service >/dev/null 2>&1; then
        systemctl enable docker >/dev/null 2>&1 && echo "      ✓ docker.service 已设为开机启动"
    elif [[ "$(uname -s)" == "Linux" ]] && command -v update-rc.d >/dev/null; then
        update-rc.d docker defaults >/dev/null 2>&1 && echo "      ✓ docker 已加入开机启动项"
    fi

    # 容器自身的重启策略
    if grep -q "restart: unless-stopped" docker-compose.yml 2>/dev/null; then
        echo "[2/3] 容器重启策略：unless-stopped（已配置）"
    else
        echo "[2/3] 提示：docker-compose.yml 中未找到 restart: unless-stopped"
    fi

    # ------------------------------------------------------------ 2. 健康守护服务
    echo "[3/3] 创建健康守护服务 ${SVC_NAME}..."
    cat > "$SVC_FILE" <<EOF
[Unit]
Description=网络配置备份中心（Docker 容器守护）
After=docker.service network-online.target
Requires=docker.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=${INSTALL_DIR}
ExecStart=/usr/bin/docker compose up -d --no-build
ExecStop=/usr/bin/docker compose stop
TimeoutStartSec=300

# 守护：每 2 分钟检查，异常则重启（由 timer 触发）
[Install]
WantedBy=multi-user.target
EOF

    # 每 2 分钟巡检一次
    cat > "/etc/systemd/system/${SVC_NAME}-check.timer" <<EOF
[Unit]
Description=每 2 分钟检查备份服务健康状态

[Timer]
OnBootSec=2min
OnUnitActiveSec=2min
AccuracySec=30s

[Install]
WantedBy=timers.target
EOF

    # 巡检脚本单独成文件，避免在 heredoc 里混用 shell 与 docker 变量
    CHECK_SCRIPT="/usr/local/bin/${SVC_NAME}-check.sh"
    cat > "$CHECK_SCRIPT" <<'CHECKEOF'
#!/usr/bin/env bash
# 备份服务健康巡检：容器不在则拉起，健康检查异常则重启
set -uo pipefail

INSTALL_DIR="__INSTALL_DIR__"
CONTAINER="cisco-backup"

cd "$INSTALL_DIR" || exit 1

if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    echo "[$(date '+%F %T')] 容器未运行，拉起中..."
    docker compose up -d --no-build
    exit 0
fi

STATUS="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
    "$CONTAINER" 2>/dev/null || echo unknown)"

if [ "$STATUS" = "unhealthy" ] || [ "$STATUS" = "unknown" ]; then
    echo "[$(date '+%F %T')] 健康状态=$STATUS，重启容器..."
    docker compose restart
elif [ "$STATUS" = "starting" ]; then
    echo "[$(date '+%F %T')] 容器仍在启动中（$STATUS），暂不处理"
fi
CHECKEOF
    # 替换目录占位符（此时由 shell 展开，路径被固化）
    sed -i "s|__INSTALL_DIR__|${INSTALL_DIR}|g" "$CHECK_SCRIPT"
    chmod +x "$CHECK_SCRIPT"

    cat > "/etc/systemd/system/${SVC_NAME}-check.service" <<EOF
[Unit]
Description=检查并拉起备份服务
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
ExecStart=${CHECK_SCRIPT}
EOF

    systemctl daemon-reload
    systemctl enable ${SVC_NAME}.service >/dev/null 2>&1
    systemctl enable ${SVC_NAME}-check.timer >/dev/null 2>&1
    echo "      ✓ ${SVC_NAME}.service 已设为开机启动"
    echo "      ✓ ${SVC_NAME}-check.timer 每 2 分钟巡检"

    systemctl start ${SVC_NAME}.service || true

else
    # ------------------------------------------------------------ systemd 部署
    echo "[1/3] 使用 systemd 部署..."
    if [[ ! -f /etc/systemd/system/cisco-backup.service ]]; then
        echo "      未找到 cisco-backup.service，请先运行 install_systemd.sh"
        exit 1
    fi
    systemctl daemon-reload
    systemctl enable cisco-backup >/dev/null 2>&1
    echo "      ✓ cisco-backup 已设为开机启动"
    systemctl restart cisco-backup || true
fi

# ------------------------------------------------------------ 磁盘检查提醒
echo ""
echo "检查磁盘空间..."
DISK=$(df -h / | awk 'NR==2 {print $5}')
if [[ "${DISK%\%}" -ge 90 ]]; then
    echo -e "  [警告] 根分区使用率 ${DISK}，空间紧张！"
    echo "         磁盘满会导致服务无法启动，请清理空间："
    echo "         docker builder prune -af && docker image prune -af"
else
    echo "  ✓ 根分区使用率 ${DISK}"
fi

echo ""
echo "==========================================="
echo " 开机自启配置完成"
echo "==========================================="
echo ""
echo "常用命令："
if [[ "$USE_SYSTEMD_DEPLOY" == "false" ]]; then
    echo "  查看状态    systemctl status ${SVC_NAME}"
    echo "  查看定时器  systemctl list-timers | grep ${SVC_NAME}"
    echo "  手动启动    cd ${INSTALL_DIR} && docker compose up -d"
    echo "  查看日志    docker compose logs -f"
else
    echo "  查看状态    systemctl status cisco-backup"
    echo "  查看日志    journalctl -u cisco-backup -f"
fi
echo ""
echo "验证自启是否生效："
echo "  sudo reboot    # 重启后检查上述命令"
echo ""