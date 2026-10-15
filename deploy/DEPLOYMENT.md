# ============================================================
#  Linux 部署指南
#  提供两种方式：Docker（推荐）与 systemd + venv
# ============================================================

## 一、Docker 部署（推荐）

### 前置要求

- Docker 20.10+ 与 Docker Compose v2
- 服务器能访问交换机所在网络（在线探测与备份需要）

```bash
# CentOS / RHEL
sudo yum install -y docker docker-compose-plugin
sudo systemctl enable --now docker

# Ubuntu / Debian
sudo apt update && sudo apt install -y docker.io docker-compose-v2
sudo systemctl enable --now docker
```

### 部署

```bash
git clone https://github.com/YOUR_NAME/cisco-config-backup.git
cd cisco-config-backup

# 可选：预设管理员密码（留空则自动生成随机密码）
cp .env.example .env
vim .env          # 填 CBS_ADMIN_PASSWORD

# 启动
docker compose up -d
```

### 验证

```bash
docker compose ps# 查看状态，应为 healthy
docker compose logs -f              # 跟踪日志
curl http://127.0.0.1:8077/api/health   # 应返回 {"status":"ok"}
```

浏览器访问 `http://<服务器IP>:8077`，用 admin 登录。

### 常用运维

```bash
docker compose restart         # 重启
docker compose down            # 停止
docker compose up -d --build   # 拉取新代码后重新构建
docker compose logs --tail=100 cisco-backup

# 进入容器排查
docker compose exec cisco-backup bash
```

### 数据与备份

所有数据在宿主机的 `./data` 目录：

```
data/
├── cisco_backup.db      数据库（设备、备份历史、用户）
├── .secret_key加密密钥（务必与数据库一起备份）
└── ...
```

**`.secret_key` 丢失会导致所有已保存的设备密码无法解密**，迁移或备份时务必一起复制。

备份数据：

```bash
# 打包数据目录
tar czf cisco-backup-data-$(date +%Y%m%d).tar.gz data/
```

## 二、systemd 部署

适合已有 Python 环境的服务器，不需要 Docker。

### 一键安装

```bash
git clone https://github.com/YOUR_NAME/cisco-config-backup.git
cd cisco-config-backup
sudo ./deploy/install_systemd.sh
```

脚本会自动完成：检查环境 → 创建服务用户 → 安装到 `/opt/cisco-backup` → 建虚拟环境装依赖 → 写入 systemd 服务 → 启动。

自定义安装目录：

```bash
sudo ./deploy/install_systemd.sh /srv/cisco-backup
```

安装时会生成并**显示一次**初始管理员密码，同时写入 `/etc/cisco-backup/env`（权限 600）。

### 常用命令

```bash
systemctl status cisco-backup     # 查看状态
systemctl restart cisco-backup    # 重启
systemctl stop cisco-backup# 停止
journalctl -u cisco-backup -f     # 实时日志
journalctl -u cisco-backup -n 100 # 最近 100 行
```

### 修改配置

```bash
sudo vim /etc/cisco-backup/env    # 改端口、管理员密码
sudo systemctl restart cisco-backup
```

### 更新版本

```bash
cd /opt/cisco-backup
sudo -u cisco-backup git pull
sudo -u cisco-backup .venv/bin/pip install -r requirements.txt
sudo systemctl restart cisco-backup
```

> `install_systemd.sh` 会排除 `data/` 目录，覆盖安装不会丢备份。

## 三、端口与防火墙

默认端口 8077。若被拦截：

```bash
# firewalld（CentOS）
sudo firewall-cmd --add-port=8077/tcp --permanent
sudo firewall-cmd --reload

# ufw（Ubuntu）
sudo ufw allow 8077/tcp

# 或直接指定内网网段
sudo firewall-cmd --add-source=192.168.1.0/24 --add-port=8077/tcp --permanent
```

## 四、生产环境建议

### 1. 配HTTPS（Nginx 反代）

服务本身提供 HTTP，生产环境建议前端 Nginx：

```bash
sudo cp deploy/nginx.conf.example /etc/nginx/conf.d/cisco-backup.conf
sudo vim /etc/nginx/conf.d/cisco-backup.conf   # 改server_name 与证书路径
sudo certbot --nginx -d backup.corp.local
sudo nginx -t && sudo systemctl reload nginx
```

HTTPS 生效后登录更安全——本地登录与域账号密码都经由浏览器传输。

### 2. 限制访问来源

备份内容包含设备配置与密码，仅允许内网或运维网段访问：

```bash
sudo ufw allow from 192.168.1.0/24 to any port 8077
sudo ufw deny 8077
```

Nginx 侧也可用 `allow` / `deny`（配置示例里有注释说明）。

### 3. 用低权限账号

安装脚本已创建 `cisco-backup` 系统用户（无登录 shell）。不要用 root 运行服务。

### 4. 数据备份

`data/` 目录就是全部状态，纳入常规备份：

```cron
# 每天 03:30 备份，保留 14 天
30 3 * * * cd /opt/cisco-backup && tar czf /backup/cbs-$(date +\%Y\%m\%d).tar.gz data/ && find /backup -name 'cbs-*.tar.gz' -mtime +14 -delete
```

### 5. 日志轮转

Docker 已限制单文件 10MB × 3。systemd 走 journald，建议：

```bash
sudo vim /etc/systemd/journald.conf
# SystemMaxUse=500M
sudo systemctl restart systemd-journald
```

## 五、常见问题

**Q：Docker 里生成的密码在哪看？**
```bash
docker compose logs cisco-backup | grep -A4 "默认管理员"
```

**Q：忘记密码？**
```bash
# Docker 方式
docker compose exec cisco-backup python scripts/reset_admin_password.py admin NewPass123
# systemd 方式
cd /opt/cisco-backup && sudo -u cisco-backup .venv/bin/python scripts/reset_admin_password.py admin NewPass123
```

**Q：容器里探测不到设备？**
容器网络与宿主机不同。交换机在内网时用 host 网络模式：
```yaml
# docker-compose.yml
    network_mode: host
```
或用 `extra_hosts` 把设备主机名映射到宿主机可达的 IP。

**Q：备份大文件/长时间任务超时？**
Nginx 侧已把 `proxy_read_timeout` 调到 600 秒。若直连访问，在设置里调大「命令超时」。

**Q：服务器没有 Python 3.10+？**
Docker 方式不依赖服务器 Python，直接用 `Dockerfile` 里的 3.12。

## 六、升级

```bash
# Docker
git pull && docker compose up -d --build

# systemd
cd /opt/cisco-backup && sudo -u cisco-backup git pull
sudo -u cisco-backup .venv/bin/pip install -r requirements.txt
sudo systemctl restart cisco-backup
```

数据库结构变更会自动迁移，无需手动处理，老数据完整保留。