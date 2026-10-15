#!/usr/bin/env bash
# ============================================================
#  网络配置备份中心 - systemd 一键安装（Linux）
#
#  用法：
#    sudo ./deploy/install_systemd.sh              # 克隆到 /opt/cisco-backup
#    sudo ./deploy/install_systemd.sh /srv/myapp   # 装到自定义目录
#
#  完成后：
#    systemctl status cisco-backup
#    journalctl -u cisco-backup -f      # 看日志
#    systemctl restart cisco-backup
# ============================================================
set -euo pipefail

INSTALL_DIR="${1:-/opt/cisco-backup}"
SERVICE_NAME="cisco-backup"
RUN_USER="cisco-backup"
ENV_DIR="/etc/cisco-backup"
ENV_FILE="${ENV_DIR}/env"
PORT="${CBS_PORT:-8077}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN ]${NC} $*"; }
err()   { echo -e "${RED}[ERR ]${NC} $*" >&2; }

[[ $EUID -ne 0 ]] && { err "请用 root 或 sudo 运行"; exit 1; }
[[ "$(uname -s)" == "Linux" ]] || { err "仅支持 Linux"; exit 1; }

# --------------------------------------------------------------- 环境检查
info "检查运行环境..."
command -v python3 >/dev/null || { err "未找到 python3，请先安装"; exit 1; }

PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
info "检测到 Python ${PY_VER}"
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'; then
    err "需要 Python 3.10 及以上（paramiko/新版依赖要求）"
    exit 1
fi

for cmd in curl systemctl; do
    command -v $cmd >/dev/null || warn "未找到 ${cmd}，部分功能可能不可用"
done

# --------------------------------------------------------------- 创建用户
if id -u "$RUN_USER" >/dev/null 2>&1; then
    info "服务用户 ${RUN_USER} 已存在"
else
    info "创建服务用户 ${RUN_USER}（无登录 shell）"
    useradd --system --shell /usr/sbin/nologin --home-dir "$INSTALL_DIR" "$RUN_USER"
fi

# --------------------------------------------------------------- 放置代码
if [[ "$INSTALL_DIR" == "$(dirname "$(readlink -f "$0")")/.." ]]; then
    SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
    info "安装到 ${INSTALL_DIR}"
    mkdir -p "$INSTALL_DIR"
    # 排除数据目录，避免覆盖服务器上的现有备份
    rsync -a --delete \
        --exclude 'data/' --exclude '.venv/' --exclude '.git/' \
        "$SRC_DIR"/ "$INSTALL_DIR"/ 2>/dev/null || {
        info "rsync 不可用，改用 cp"
        cp -r "$SRC_DIR"/app "$SRC_DIR"/scripts "$SRC_DIR"/deploy \
              "$SRC_DIR"/run.py "$SRC_DIR"/requirements.txt \
              "$SRC_DIR"/README.md "$INSTALL_DIR"/
    }
else
    info "克隆仓库到 ${INSTALL_DIR}"
    git clone https://github.com/zduyiwuer/cisco-config-backup.git "$INSTALL_DIR"
fi

mkdir -p "$INSTALL_DIR/data"
chown -R "${RUN_USER}:${RUN_USER}" "$INSTALL_DIR"

# --------------------------------------------------------------- 虚拟环境
info "创建虚拟环境并安装依赖（首次约需 1-3 分钟）..."
sudo -u "$RUN_USER" python3 -m venv "$INSTALL_DIR/.venv"
sudo -u "$RUN_USER" "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip -q
sudo -u "$RUN_USER" "$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q

# --------------------------------------------------------------- 环境变量
mkdir -p "$ENV_DIR"
if [[ ! -f "$ENV_FILE" ]]; then
    info "生成环境变量文件 ${ENV_FILE}"
    RANDOM_PWD=$(python3 -c "import secrets; print(secrets.token_urlsafe(12))")
    cat > "$ENV_FILE" <<EOF
# 初始管理员密码，仅首次启动生效
CBS_ADMIN_PASSWORD=${RANDOM_PWD}
CBS_PORT=${PORT}
EOF
    chmod 600 "$ENV_FILE"
    echo
    echo -e "${YELLOW}  初始管理员账号${NC}"
    echo "      用户名: admin"
    echo "      密码  : ${RANDOM_PWD}"
    echo -e "      ${YELLOW}登录后请立即修改！${NC}"
    echo
fi
chown -R "${RUN_USER}:${RUN_USER}" "$ENV_DIR" 2>/dev/null || true

# --------------------------------------------------------------- systemd
info "安装 systemd 服务..."
sed "s|/opt/cisco-backup|${INSTALL_DIR}|g" \
    "$INSTALL_DIR/deploy/cisco-backup.service" > "/etc/systemd/system/${SERVICE_NAME}.service"
#若模板里用户名是占位符，按实际替换
sed -i "s/^User=.*/User=${RUN_USER}/; s/^Group=.*/Group=${RUN_USER}/" \
    "/etc/systemd/system/${SERVICE_NAME}.service"
sed -i "s|--port 8077|--port ${PORT}|" "/etc/systemd/system/${SERVICE_NAME}.service"
chmod 644 "/etc/systemd/system/${SERVICE_NAME}.service"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
systemctl restart "$SERVICE_NAME"

# --------------------------------------------------------------- 结果
sleep 3
if systemctl is-active --quiet "$SERVICE_NAME"; then
    IP=$(hostname -I 2>/dev/null | awk '{print $1}')
    echo
    echo -e "${GREEN}========================================${NC}"
    echo -e "${GREEN} 部署成功，服务已启动${NC}"
    echo -e "${GREEN}========================================${NC}"
    echo "  访问地址: http://${IP:-<服务器IP>}:${PORT}"
    echo
    echo "  常用命令:"
    echo "    systemctl status ${SERVICE_NAME}      查看状态"
    echo "    journalctl -u ${SERVICE_NAME} -f跟踪日志"
    echo "    systemctl restart ${SERVICE_NAME}     重启"
    echo "    systemctl stop ${SERVICE_NAME}        停止"
    echo
    echo -e "${YELLOW}  提示：${PORT} 端口若被防火墙拦截，需放行${NC}"
    echo "    firewall-cmd --add-port=${PORT}/tcp --permanent && firewall-cmd --reload"
    echo "    或 iptables -A INPUT -p tcp --dport ${PORT} -j ACCEPT"
    echo
    echo -e "${YELLOW}  生产环境建议前置 Nginx + HTTPS${NC}"
    echo "  参考 deploy/nginx.conf.example"
else
    err "服务启动失败，请查看日志：journalctl -u ${SERVICE_NAME} -n 50"
    exit 1
fi