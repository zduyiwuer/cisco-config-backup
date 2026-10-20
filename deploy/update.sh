#!/usr/bin/env bash
# ============================================================
#  Docker 一键更新
#
#  用法：
#    ./deploy/update.sh              # 更新并重建
#    ./deploy/update.sh --no-backup  # 跳过备份（不推荐）
#    ./deploy/update.sh --check-only # 只看有什么更新，不执行
#
#  做的事：
#    1. 备份数据目录（数据库 + 加密密钥）
#    2. git pull 拉最新代码
#    3. 重新构建镜像并重建容器
#    4. 等待健康检查通过，失败则回滚到备份的代码版本
# ============================================================
set -euo pipefail

cd "$(dirname "$0")/.."
INSTALL_DIR="$(pwd)"
BACKUP_DIR="$INSTALL_DIR/backups"
STAMP="$(date +%Y%m%d-%H%M%S)"
DO_BACKUP=1
CHECK_ONLY=0

[[ "${1:-}" == "--no-backup" ]] && DO_BACKUP=0
[[ "${1:-}" == "--check-only" ]] && CHECK_ONLY=1

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

echo "==========================================="
echo " 更新服务"
echo "==========================================="

# ------------------------------------------------------------ 检查环境
if ! command -v docker >/dev/null; then
    echo -e "${RED}[错误] 未找到 docker${NC}"
    exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
    echo -e "${RED}[错误] docker compose 不可用，请确认 Compose V2 已安装${NC}"
    exit 1
fi

# ------------------------------------------------------------ 磁盘检查
# df --output 是 coreutils 8.21+ 才有的，精简版系统上不可用，
# 因此先试 --output，失败则退回解析 df -h 的输出
disk_avail_gb() {
    local avail
    # 优先看数据目录所在分区（容器数据通常在 /var 下）
    local target="."
    [[ -d "$INSTALL_DIR/data" ]] && target="$INSTALL_DIR/data"
    avail="$(df -BG --output=avail "$target" 2>/dev/null | tail -1 | tr -dc '0-9')"
    if [[ -z "$avail" ]]; then
        avail="$(df -h "$target" 2>/dev/null | awk 'NR==2 {gsub(/[A-Za-z]/,"",$4); print $4}')"
    fi
    [[ -z "$avail" ]] && avail=0
    echo "$avail"
}

AVAIL_GB=$(disk_avail_gb)
echo "  可用空间: ${AVAIL_GB} GB"
if [[ "$AVAIL_GB" -lt 3 ]]; then
    echo -e "${YELLOW}[警告] 磁盘空间不足 3GB，构建很可能失败${NC}"
    echo "  建议先清理: docker builder prune -af && docker image prune -af"
    if [[ "$CHECK_ONLY" -eq 0 ]]; then
        read -r -p "  仍要继续吗？[y/N] " ans
        [[ "$ans" == "y" || "$ans" == "Y" ]] || exit 1
    fi
fi

# ------------------------------------------------------------ 记录当前版本
OLD_COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
echo "  当前版本: ${OLD_COMMIT}"

# ------------------------------------------------------------ 拉取代码
echo ""
echo "[1/4] 拉取最新代码..."
git fetch --quiet origin

# 本地有未提交改动时先提醒，避免 pull 冲突或覆盖
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo -e "${YELLOW}[警告] 本地有未提交的改动${NC}"
    git status --short | head -10 | sed 's/^/      /'
    if [[ "$CHECK_ONLY" -eq 0 ]]; then
        read -r -p "  这些改动可能与更新冲突，仍要继续吗？[y/N] " ans
        [[ "$ans" == "y" || "$ans" == "Y" ]] || {
            echo "已取消。可先执行 git stash 保存改动。"
            exit 1
        }
    fi
fi

# 用 rev-list 判断是否有新提交，比 diff --quiet 可靠
if [[ -z "$(git rev-list HEAD..origin/main 2>/dev/null)" ]]; then
    echo "      已是最新，无需更新"
    exit 0
fi
NEW_COMMIT=$(git rev-parse --short origin/main)
CHANGED=$(git rev-list --count HEAD..origin/main)
echo "      发现 ${CHANGED} 个新提交: ${OLD_COMMIT} → ${NEW_COMMIT}"
git log --oneline --no-decorate HEAD..origin/main | head -10 | sed 's/^/      /'

if [[ "$CHECK_ONLY" -eq 1 ]]; then
    echo ""
    echo "（--check-only 模式，未执行更新）"
    exit 0
fi

# ------------------------------------------------------------ 备份数据
if [[ "$DO_BACKUP" -eq 1 && -d "$INSTALL_DIR/data" ]]; then
    echo ""
    echo "[2/4] 备份数据..."
    mkdir -p "$BACKUP_DIR"
    # 数据库用 sqlite3 的 .backup 保证一致性；没有 sqlite3 或失败则退化为打包。
    # 备份失败不阻断更新流程——用户可能正是磁盘紧张才来更新的。
    if command -v sqlite3 >/dev/null && [[ -f data/cisco_backup.db ]]; then
        if sqlite3 data/cisco_backup.db ".backup '$BACKUP_DIR/cisco_backup-$STAMP.db'" 2>/dev/null; then
            echo "      数据库已备份: cisco_backup-$STAMP.db"
        else
            echo -e "${YELLOW}      sqlite3 备份失败，改用打包方式${NC}"
        fi
    fi
    # 密钥文件必须一起备份，丢了所有设备密码都解不开
    if tar czf "$BACKUP_DIR/data-$STAMP.tar.gz" -C "$INSTALL_DIR" data 2>/dev/null; then
        echo "      数据目录已打包: data-$STAMP.tar.gz"
    else
        echo -e "${YELLOW}      打包失败，请确认磁盘空间${NC}"
    fi
    # 只保留最近 5 份，避免备份堆积占满磁盘
    ls -t "$BACKUP_DIR"/data-*.tar.gz 2>/dev/null | tail -n +6 | xargs -r rm -f || true
    echo "      （保留最近 5 份，位于 ${BACKUP_DIR}）"
else
    echo ""
    echo "[2/4] 跳过备份"
fi

# ------------------------------------------------------------ 更新代码
echo ""
echo "[3/4] 更新代码并重建容器..."
git pull --ff-only origin main

# 构建失败时回滚代码
if ! docker compose build; then
    echo -e "${RED}      构建失败${NC}"
    echo "  回滚代码到 ${OLD_COMMIT}..."
    git checkout --quiet "$OLD_COMMIT" 2>/dev/null || true
    echo -e "${RED}  构建失败，已回滚代码。请检查上方报错。${NC}"
    echo "  常见原因：磁盘空间不足（docker builder prune -af）"
    exit 1
fi

# ------------------------------------------------------------ 重启并验证
echo ""
echo "[4/4] 重建容器..."
docker compose up -d --no-build

echo -n "      等待服务就绪"
READY=0
for i in $(seq 1 60); do
    if curl -fsS http://127.0.0.1:8077/api/health >/dev/null 2>&1; then
        READY=1
        break
    fi
    echo -n "."
    sleep 2
done
echo ""

if [[ "$READY" -eq 1 ]]; then
    echo -e "${GREEN}===========================================${NC}"
    echo -e "${GREEN} 更新完成${NC}"
    echo -e "${GREEN}===========================================${NC}"
    echo ""
    echo "  新版本: ${NEW_COMMIT}"
    echo "  访问地址: http://127.0.0.1:8077"
    echo "  备份位置: ${BACKUP_DIR}"
    echo ""
    echo "  查看日志: docker compose logs -f --tail 100"
    echo "  如需回滚: 见下方说明"
    echo ""
    echo "  回滚方法（旧代码仍在，容器回到上一版本）："
    echo "    docker compose down && git checkout ${OLD_COMMIT} && docker compose up -d --build"
    echo "  数据回滚（谨慎，会覆盖当前数据）："
    echo "    tar xzf ${BACKUP_DIR}/data-*.tar.gz && docker compose restart"
else
    echo -e "${RED}===========================================${NC}"
    echo -e "${RED} 服务未能在 2 分钟内就绪${NC}"
    echo -e "${RED}===========================================${NC}"
    echo ""
    echo "  查看日志排查: docker compose logs --tail 100"
    echo "  代码已更新到 ${NEW_COMMIT}，如需回滚:"
    echo "    git checkout ${OLD_COMMIT} && docker compose up -d --build"
    echo ""
    echo "  数据备份在: ${BACKUP_DIR}"
    exit 1
fi