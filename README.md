# Cisco 配置备份中心

通过 **SSH / Telnet** 登录 Cisco 交换机，自动采集 `show running-config`、`show cdp neighbors detail` 等命令输出，存入本地 SQLite，保留历史版本并高亮展示配置差异。支持按门店归类，备份文件可打包下载到本地。带 Web 管理界面和定时备份。

## 功能

| 能力 | 说明 |
|---|---|
| **登录认证** | 本地账号 + AD 域账号双入口，全局强制登录 |
| **角色权限** | 管理员/操作员/只读三级，按接口校验 |
| 双协议采集 | SSH（paramiko）与 Telnet（自研IAC 协商客户端），自动进入 enable 模式 |
| 自定义命令 | 每台设备可配置任意命令列表，默认 `show run` + `show cdp ne de` |
| 差异版本 | 内容无变化不产生新版本；发生变化时自动生成 unified diff |
| **批量导入** | CSV / Excel 批量导入设备，逐行校验并给出失败原因 |
| **在线探测** | 每 5 分钟自动探测设备在线状态，记录延迟与失败原因 |
| **门店归类** | 门店为独立实体，设备归属门店；支持门店级批量备份与打包 |
| **打包下载** | 按全部门店/单门店/单设备打包 zip，含 manifest.csv 清单与 .diff 差异 |
| 定时备份 | 每台设备独立 crontab 表达式，APScheduler 调度，支持并发 |
| 凭据加密 | 密码 PBKDF2 派生；设备与 AD 密码 Fernet 加密落盘 |
| CDP 邻居 | 解析 `show cdp neighbors detail`，前端展示邻居卡片 |
| 版本清理 | 按设备保留最近 N 个变化版本 |

## 快速开始

### Windows 一键启动（推荐日常使用）

双击 **`启动服务.bat`**，浏览器会自动打开管理界面。

首次运行会自动创建运行环境并安装依赖（约 1-2 分钟），之后启动只需几秒。窗口保持打开，关闭窗口即停止服务。

### Linux 部署

完整说明见 **[deploy/DEPLOYMENT.md](deploy/DEPLOYMENT.md)**，两种方式任选。

#### Docker（推荐）

```bash
git clone https://github.com/zduyiwuer/cisco-config-backup.git
cd cisco-config-backup
cp .env.example .env      # 可选：预设管理员密码
vim .env
docker compose up -d
```

访问 `http://<服务器IP>:8077`。数据存放在 `./data`，容器重建不丢。

```bash
docker compose ps                # 查看状态（应为 healthy）
docker compose logs -f           # 跟踪日志
docker compose down              # 停止
docker compose up -d --build     # 升级
```

#### systemd

```bash
sudo ./deploy/install_systemd.sh   # 装到 /opt/cisco-backup，自动建用户并启动
```

```bash
systemctl status cisco-backup      # 查看状态
journalctl -u cisco-backup -f      # 实时日志
systemctl restart cisco-backup     # 重启
```

首次安装会显示一次性管理员密码并写入 `/etc/cisco-backup/env`（权限 600）。

> 生产环境建议前置 Nginx + HTTPS，配置示例见 `deploy/nginx.conf.example`。

### 手动启动

```bash
cd cisco-backup

# 1. 创建虚拟环境（需 Python 3.10+）
python3 -m venv .venv

# 2. 安装依赖
./.venv/bin/pip install -r requirements.txt         # Linux/macOS
./.venv/Scripts/pip install -r requirements.txt      # Windows

# 3. 启动（默认自动打开浏览器）
./.venv/bin/python run.py
```

打开 <http://127.0.0.1:8077>，用 admin 登录后添加设备。

### 环境变量

| 变量 | 说明 |
|---|---|
| `CBS_HOST` | 监听地址，默认 `127.0.0.1`，服务器部署用 `0.0.0.0` |
| `CBS_PORT` | 监听端口，默认 `8077` |
| `CBS_NO_BROWSER` | 设为 `1` 不自动开浏览器 |
| `CBS_ADMIN_PASSWORD` | 初始管理员密码，留空则随机生成并打印在日志 |

### 开机自启（可选）

若希望服务常驻、开机即启动，以管理员身份运行 **`安装开机自启.bat`**。

它用 [NSSM](https://nssm.cc/download) 把程序注册为真正的 Windows 服务，支持崩溃自动重启（5 秒后重试，最多 10 次）、后台静默运行、日志写入 `data/service.log`。

常用管理命令：

```bat
sc query CiscoConfigBackup      查看状态
nssm stop CiscoConfigBackup     停止
nssm start CiscoConfigBackup    启动
nssm delete CiscoConfigBackup   卸载
```

> 前提：把 `nssm.exe` 下载后放到项目目录，脚本会自动检测。

常用启动参数：

```bash
python run.py --port 9000        # 换端口
python run.py --host 0.0.0.0     # 允许局域网访问
python run.py --no-browser       # 不自动开浏览器
python run.py --reload           # 开发模式，改代码自动重载
```

## 批量导入设备

设备少时逐台添加即可；几十上百台时用「导入」，设备页点「导入」按钮，支持拖拽文件。

**支持格式**：Excel（.xlsx，推荐，含模板）与 CSV（UTF-8 / GBK，Excel 导出即可）

**Excel 模板**：导入弹窗点「下载 Excel 模板」即可拿到 `设备导入模板.xlsx`，包含两张工作表：

- **设备清单** —— 12 列表头（蓝底加粗、冻结首行、启用筛选），每列都带鼠标悬停批注说明填写要求，附 2 行示例数据
- **填写说明** —— 必填项、端口自动补全规则、门店自动创建、导入模式等完整说明

模板下载后直接在上面填写，保存为 .xlsx 再拖回导入区即可——**模板本身就能直接导入**，不需要改表头。

**列名自动识别** —— 中文英文混用也能认出来：

| 含义 | 可用列名 | 必填 |
|---|---|---|
| 设备名称 | 设备名称 / name / hostname | 是 |
| 管理 IP | 管理IP / ip / ip_address / host | 是 |
| 协议 | 协议 / protocol（ssh 或 telnet） | 否，默认 ssh |
| 端口 | 端口 / port | 否，按协议自动补 22/23 |
| 用户名 | 用户名 / username | 否 |
| 登录密码 | 登录密码 / 密码 / password | 新增时必填 |
| Enable密码 | Enable密码 / enable_password | 否 |
| 门店 | 门店 / site | 否，填了会自动创建门店 |
| 定时备份 | 定时备份（是/否/1/0/true） | 否 |
| Cron | cron / Cron表达式 | 否，默认每天 02:00 |
| 采集命令 | 采集命令（分号分隔多条） | 否 |
| 备注 | 备注 / note | 否 |

**两种模式**：

- **仅新增**：设备名已存在则跳过，不覆盖原有配置
- **按名称更新**：按设备名匹配并更新；密码留空则保持原密码不变

**容错**：逐行校验，某一行的错误不会影响其他行。导入结束后会列出每行的处理结果（新增/更新/跳过/失败）与失败原因，例如「协议只能是 ssh 或 telnet，当前为 http」「缺少管理 IP」「端口格式错误」。

不确定格式时，点弹窗里的「下载 Excel 模板」照着填最省事，模板可直接导入。

## 在线探测

「在线监控」标签页查看全部设备的在线状态，设备列表也有在线列并可按在线/离线筛选。

- **默认每 5 分钟自动探测一轮**，服务启动时会立即跑一次
- 探测只做 TCP 连接（SSH 额外校验 banner），**不做完整登录认证**，开销极低，不影响备份任务
- 记录延迟、最后在线时间、连续失败次数；每台设备保留最近 200 条探测历史
- 连续失败计数用于识别「偶发抖动」与「真的掉线」，恢复在线后自动清零
- 探测间隔与超时可在「设置」里调整，也可手动点「立即探测」

探测方式对 SSH 会校验 banner：端口开放但不返回 SSH 协议头的设备会被标为「非 SSH 服务」而不是简单算在线，避免端口被其他服务占用时误判。

## 登录与权限

服务默认**强制登录**，除登录页与健康检查外全部接口都需要有效会话。未登录访问会跳转到 `/login`。

### 首次登录

首次启动会自动创建管理员 `admin`，**随机生成密码并打印在启动窗口的控制台里（仅显示一次）**：

```
  已创建默认管理员账号：
      用户名: admin
      密码  : xxxxxxxxxxxx
  该密码仅显示这一次，请立即登录并修改。
```

忘记密码时可从项目目录运行重置脚本：

```bash
./.venv/Scripts/python scripts/reset_admin_password.py admin MyNewPass123
```

重置后旧密码与现有会话立即失效。

### 角色权限

| 角色 | 权限 |
|---|---|
| **管理员** admin | 全部权限，含用户管理、域账号配置、系统设置、删除设备/门店 |
| **操作员** operator | 增删改设备与门店、执行备份、导入、探测；不能改系统设置与删设备 |
| **只读** viewer | 仅查看与导出打包，不能做任何修改 |

右上角用户菜单可修改自己的密码、打开用户管理、退出登录。

### 域账号（AD / LDAP）

登录页在启用后会多出「域账号」入口。支持三种账号写法：`域\用户名`、`用户名@域名`、`用户名`（自动补域名）。

配置入口：右上角用户菜单 →「域账号配置」，填好后可点「测试连接」验证。

关键配置项：

| 项 | 说明 |
|---|---|
| 域服务器地址 / 端口 | 如 `dc01.corp.local`，LDAPS 默认 636 |
| 使用 LDAPS | **强烈建议开启**，否则域密码会明文传输 |
| 搜索基准 DN | 如 `OU=Users,DC=corp,DC=local` |
| 绑定账号 / 密码 | 服务账号，用于搜索用户 DN；直接绑定模式可留空 |
| 绑定方式 | 搜索后绑定（推荐）/ 直接绑定 |
| 管理员组 | 该组成员自动成为管理员，支持完整 DN 或纯组名 |
| 其他用户默认角色 | 不在管理员组的域用户默认权限 |
| CA 证书路径 | 域控用自签名证书时填写 |

**安全约束**：

- 密码用 PBKDF2-HMAC-SHA256（26 万次迭代）派生，数据库不存明文
- 会话 token 数据库里只存 SHA256 哈希，数据库泄露也无法冒用会话
- 连续 5 次密码错误锁定账号 15 分钟
- 登录失败提示不区分"用户不存在"与"密码错误"，防用户名枚举
- 默认拒绝明文 LDAP（389），必须显式勾选允许
- **不提供"跳过证书校验"开关**，LDAPS 始终校验证书
- AD 服务账号密码加密落盘，接口只返回"是否已设置"
- LDAP 过滤器特殊字符全部转义，防注入

域用户首次登录自动建档，之后登录会同步显示名与邮箱，并按管理员组重新计算角色。

### 用户管理

管理员在「用户管理」里可新建本地账号、改角色、启用/禁用、删除。禁用后该用户的会话立即失效。

## 门店归类

「门店」是独立实体（不是自由文本），一个门店下有多台设备：

- **新增门店**：「门店」标签页 → 「+ 新增门店」，填名称、编号、区域、地址
- **归属设备**：新增/编辑设备时选「所属门店」；设备列表可按门店筛选，也可只看「未归属」
- **门店级操作**：每张门店卡片有「备份本店」「打包下载」「查看设备」三个按钮
- **删除门店**：设备不会被连带删除，可自动迁移到另一个门店，或置为未归属

门店卡片实时显示：设备数、正常数、异常数、最近备份时间。

老版本里 `devices.site` 是自由文本，已自动迁移为门店实体，同名文本会合并为同一门店。

## 打包下载

「打包下载」标签页可按范围导出 zip：

- **范围**：全部设备（所有门店）/ 指定门店 / 指定设备
- **版本数**：全部历史，或每台设备只保留最近 1/3/7/30 次
- **时间范围**：可选起止时间
- **命令**：不选则导出全部命令
- **差异**：可附带各版本相对上一版本的 `.diff`

导出门店快捷入口：设备列表行内「打包」按钮、门店卡片「打包下载」按钮。

包内结构：

```
全部门店_20261014-172340/
├── manifest.csv                备份清单（门店/设备/命令/时间/SHA256，Excel 直接打开）
├── README.txt                  导出说明
├── 上海南京西路店/
│   ├── sh-core-sw-01/
│   │   ├── 20261014-172232/
│   │   │   ├── show_running_config.txt
│   │   │   ├── show_cdp_neighbors_detail.txt
│   │   │   └── _diff/
│   │   │       └── show_running_config.diff
│   │   └── 20261014-172147/
│   └── ...
└── 深圳南山科技园店/
```

未归属门店的设备统一放在 `未归属门店/` 目录下。

## 添加设备

在界面点「新增设备」，填写：

- **协议**：SSH（默认端口 22）或 Telnet（默认端口 23）
- **用户名 / 登录密码**：必填，密码加密存储
- **Enable 密码**：设备要求 enable 时填写
- **所属门店**：可选，选定后可在门店页统一管理
- **采集命令**：每行一条，默认两条
- **定时备份**：勾选后填 crontab，如 `0 2 * * *`（每天 02:00）、`30 3 * * 1`（每周一 03:30）

保存后先用「测试」按钮验证连通性，再点「备份」。

## 目录结构

```
cisco-backup/
├── app/
│   ├── main.py                FastAPI 路由 + 鉴权中间件
│   ├── auth.py                用户、会话、密码哈希、登录审计
│   ├── ad.py                  Active Directory / LDAP 认证
│   ├── collector.py           备份编排：登录→执行→清洗→入库→diff
│   ├── db.py                  SQLite 存储层（含旧库迁移）
│   ├── exporter.py            打包导出（门店/设备/全量）
│   ├── importer.py            设备批量导入（CSV/Excel）
│   ├── probe.py               在线探测
│   ├── crypto.py              敏感数据加密
│   ├── diffing.py             差异计算
│   ├── cdp.py                 CDP 邻居解析
│   ├── scheduler.py           定时调度（备份 + 探测）
│   ├── drivers/
│   │   ├── base.py            公共接口 + 输出清洗
│   │   ├── ssh_driver.py      SSH 采集
│   │   └── telnet_driver.py   Telnet 采集
│   └── static/                前端页面（含 login.html/css/js）
├── scripts/
│   └── reset_admin_password.py 管理员密码重置
├── deploy/                    Linux 部署
│   ├── DEPLOYMENT.md          部署文档（Docker / systemd）
│   ├── cisco-backup.service   systemd 服务模板
│   ├── install_systemd.sh     systemd 一键安装脚本
│   └── nginx.conf.example     Nginx 反代示例
├── Dockerfile                 容器构建（多阶段）
├── docker-compose.yml         容器编排
├── .env.example               环境变量模板
├── tests/
│   ├── fixtures.py            测试公共夹具
│   ├── mock_device.py         模拟 Cisco 设备（SSH + Telnet）
│   ├── test_e2e.py            端到端自检（87 项）
│   ├── test_migration.py      旧库迁移专项测试（25 项）
│   ├── test_import_probe.py   导入与探测测试（70 项）
│   ├── test_xlsx_template.py  Excel 模板测试（45 项）
│   ├── test_auth.py           登录与权限测试（110 项）
│   ├── js_smoke.js            前端脚本冒烟测试
│   └── seed_demo.py           生成演示数据
├── data/                      数据库与密钥（运行时生成）
├── 启动服务.bat               Windows 一键启动（自动装依赖+开浏览器）
├── 安装开机自启.bat            注册为 Windows 开机自启服务（需 NSSM）
└── run.py                     启动脚本
```

## 自检

无需真实设备，测试内置了模拟 Cisco 设备（走真实的 SSH 握手、Telnet IAC 协商、enable 交互）：

```bash
./.venv/Scripts/python -m tests.test_e2e           # 87 项：采集/差异/API/门店/打包/调度
./.venv/Scripts/python -m tests.test_migration      # 25 项：旧库平滑升级
./.venv/Scripts/python -m tests.test_import_probe   # 70 项：CSV/Excel 导入 + 在线探测
./.venv/Scripts/python -m tests.test_xlsx_template  # 45 项：Excel 模板生成与回导
./.venv/Scripts/python -m tests.test_auth           # 110 项：登录/会话/权限/AD

# 前端脚本冒烟测试（用 Node 跑，无需浏览器）
node tests/js_smoke.js
```

想先体验界面可以造一批演示数据（3 门店 6 设备，含两轮备份与配置变更）：

```bash
./.venv/Scripts/python -m tests.seed_demo
```

覆盖：密码加解密、输出清洗、CDP 解析、SSH/Telnet 采集、幂等性、差异生成、错误处理、HTTP API、门店归类、打包导出、旧库迁移、定时调度。

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/login` | 登录页 |
| POST | `/api/auth/login` | 登录（`mode=local` / `ad`） |
| POST | `/api/auth/logout` | 退出登录 |
| GET | `/api/auth/me` | 当前用户信息 |
| GET | `/api/auth/ad-status` | 登录页用的认证状态 |
| POST | `/api/auth/change-password` | 修改自己的密码 |
| GET | `/api/auth/users` | 用户列表（管理员） |
| POST | `/api/auth/users` | 新建用户（管理员） |
| PATCH | `/api/auth/users/{id}` | 改角色/状态（管理员） |
| DELETE | `/api/auth/users/{id}` | 删除用户（管理员） |
| PUT | `/api/auth/ad` | 配置 AD（管理员） |
| POST | `/api/auth/ad/test` | 测试 AD 连通性（管理员） |
| GET | `/api/auth/sessions` | 活跃会话（管理员） |
| GET | `/api/auth/login-log` | 登录审计（管理员） |
| GET | `/api/import/template` | 下载 CSV 导入模板 |
| GET | `/api/import/template.xlsx` | 下载 Excel 导入模板（含批注与说明页） |
| POST | `/api/import/devices` | 批量导入设备（上传 CSV/Excel） |
| GET | `/api/probe/status` | 在线概况 + 离线设备清单 |
| POST | `/api/probe/run` | 立即执行一轮探测 |
| GET | `/api/probe/{id}/history` | 单设备探测历史 |
| GET | `/api/sites` | 门店列表（含设备数与最近备份） |
| POST | `/api/sites` | 新增门店 |
| GET | `/api/sites/{id}` | 门店详情（含设备明细） |
| PATCH | `/api/sites/{id}` | 更新门店 |
| DELETE | `/api/sites/{id}?reassign_to=` | 删除门店，`reassign_to` 指定设备迁移目标 |
| POST | `/api/sites/{id}/backup` | 批量备份该门店下启用设备 |
| GET | `/api/devices?site_id=&keyword=` | 设备列表（`site_id=0` 只看未归属） |
| POST | `/api/devices` | 新增设备 |
| PATCH | `/api/devices/{id}` | 更新设备 |
| DELETE | `/api/devices/{id}` | 删除设备及其历史 |
| POST | `/api/devices/{id}/test` | 测试连通性 |
| POST | `/api/devices/{id}/backup` | 立即备份一台 |
| POST | `/api/backups` | 批量备份（后台执行） |
| GET | `/api/backups?device_id=` | 备份历史 |
| GET | `/api/backups/{id}` | 备份详情（含文件与diff 摘要） |
| GET | `/api/files/{id}/raw` | 配置原文 |
| GET | `/api/files/{id}/diff` | 与上一版本的差异 |
| GET | `/api/backup/{id}/download` | 单次备份打包下载 |
| POST | `/api/export/package` | **按门店/设备/全量打包下载** |
| GET | `/api/export/sites/{id}/package` | 门店打包快捷入口 |
| GET | `/api/export/preview` | 导出规模预览 |
| GET | `/api/devices/{id}/neighbors` | CDP 邻居 |
| GET/PUT | `/api/settings` | 全局设置 |
| GET | `/api/status` | 汇总状态与调度信息 |

打包下载示例：

```bash
# 全部门店
curl -X POST http://127.0.0.1:8077/api/export/package \
     -H "Content-Type: application/json" \
     -d '{"scope":"all","include_diff":true}' \
     -o backup.zip

# 指定门店，每台设备只要最近 3 次
curl -X POST http://127.0.0.1:8077/api/export/package \
     -H "Content-Type: application/json" \
     -d '{"scope":"site","site_id":1,"limit_per_device":3}' \
     -o sh_store.zip
```

## 实现要点

**输出清洗**：设备会回显命令并以提示符结尾，还可能在终端宽度不足时插入 `--More--`。驱动层统一处理这些情况，只保留干净的命令输出。

**分页处理**：登录后自动发送 `terminal length 0` 和 `terminal width 511`；若设备仍分页，采集时检测 `--More--` 并自动发送空格翻页。

**提示符识别**：正则 `[\w.\-()]+(\(...\))?[>#]$` 同时匹配 `SW>`、`SW#`、`SW(config)#`、`switch(config-if)#` 等形态。发送命令前清空缓冲区，确保输出归属于当前命令。

**Telnet 实现**：Python 3.13 移除了标准库 `telnetlib`，因此用 socket 自研客户端，处理 IAC 协商（DO/DONT/WILL/WONT/SB-SE）并拒绝所有选项请求，避免卡在协商阶段。

**版本策略**：以命令输出内容的 SHA256 判断是否变化。无变化时标记为非新版本（`is_new=0`），不占用「保留版本数」配额；变化时写入新版本并与上一版本生成 diff。

**参数兼容性**：paramiko 需锁在 `<4.0`，因为 paramiko 4.0/5.0 调用了 Python 3.14 才有的线程接口，在 3.13 上会抛 `TypeError: '_thread._ThreadHandle' object is not callable`。

**门店与设备的一致性**：`devices.site_id` 是归属关系的唯一依据，`devices.site` 文本作为冗余同步保留，便于搜索与导出。绑定门店时传 `site_id`；只传 `site` 文本则自动创建或复用同名门店。

**旧库迁移**：`init_db()` 分三步执行——先建表（不含索引）、再补 `site_id` 列、最后建索引。顺序不能颠倒，否则旧库上执行 `CREATE INDEX ... ON devices(site_id)` 会因列不存在而失败。随后把 `devices.site` 中已有的文本值去重后提升为 `sites` 实体，同名文本合并到同一门店。

**中文文件名下载**：HTTP 头只能编码 latin-1，中文文件名必须用 RFC 5987 的 `filename*=UTF-8''` 形式，同时保留 ASCII 回退名，否则会抛 `UnicodeEncodeError`。zip 包内部目录名则是 UTF-8，Excel/WinRAR 均可正常打开。

**列名规范化**：导入时列名会先转小写并去掉空格、下划线、连字符再查别名表，因此 `ip_address`、`IP ADDRESS`、`IpAddress` 等价。别名表的键也必须是规范化后的形式，否则会匹配不上。

**探测与备份分离**：探测只做 TCP 层判断（SSH 额外读 banner 确认对端确实是 SSH 服务），不做认证，因此不会因为 enable 密码错误、设备繁忙等原因把「在线」误判为「离线」。连续失败次数单独记录，可区分网络抖动与真正掉线。

**调度器启动顺序**：APScheduler 的 `add_job` 要求调度器已启动，所以必须先 `scheduler.start()` 再注册任务，顺序颠倒会导致任务注册被静默跳过。

**依赖锁定**：`requirements.txt` 里 paramiko 必须锁 `<4.0`（原因见上），另需 `openpyxl`（Excel 导入）与 `python-multipart`（文件上传）。

**静态资源禁用缓存**：前端 JS/CSS 由 `NoCacheStaticFiles` 挂载，统一带 `Cache-Control: no-store`，并在 HTML 引用处加了版本号参数。运维工具改动频繁，若沿用默认缓存策略会出现「代码明明改了、浏览器跑的还是旧逻辑」这类极难定位的问题——尤其是新加的按钮事件绑定在旧 JS 里根本不存在，表现为点击毫无反应且无任何报错。

**密码与会话安全**：本地密码用 PBKDF2-HMAC-SHA256（26 万次迭代 + 16 字节随机盐）存储；会话 token 数据库里只存 SHA256 哈希。两者都不可逆推，数据库文件泄露也无法直接冒用。会话 Cookie 设 `HttpOnly` + `SameSite=Lax`，仅 HTTPS 下才加 `Secure`。

**设置项双通道**：`get_settings()` 解密敏感字段供内部使用，`get_public_settings()` 额外脱敏后供 API 返回。AD 服务账号密码走后者，接口只返回 `ad_bind_password_set: true`，更新时留空表示不修改。

**AD 组名匹配**：先按完整 DN 比对，再退化到只比 CN。配置写 `CN=NetOps,DC=corp,DC=local` 时只认这个 DN，`CN=Users,...` 不会被误判为管理员。多个候选用换行或分号分隔（不能用逗号，会拆坏DN 内部结构）。LDAP 过滤器特殊字符按 RFC 4515 转义。

**测试与认证的关系**：认证上线后所有 API 都需要登录态。`test_auth.py` 专注认证本身并关闭全局拦截，其余测试文件（`test_e2e` 等）显式设置 `auth_enabled=False` 专注业务逻辑，避免相互干扰。

## 注意事项

- `data/.secret_key` 是解密密码的唯一依据，**不要删除**，否则历史设备密码无法解密。
- Telnet 传输不加密，仅用于内网老设备；新设备建议用 SSH。
- 设备侧建议配置只读或限制 enable 权限的备份账号。
- 首次连接 IOS 设备时可能因 `known_hosts` 缺失而跳过校验（`AutoAddPolicy`），生产环境可改为固定主机密钥。