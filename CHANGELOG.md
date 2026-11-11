# 多厂商网络设备配置备份中心 v1.3.0

新增设备清单导出，可按门店、厂商、关键字筛选，导出为 Excel 或 CSV。

## 新增

### 设备清单导出

「打包下载」标签页新增「导出设备清单」：

- **Excel 与 CSV 两种格式**，CSV 带 UTF-8 BOM，Excel / WPS 直接打开不乱码
- **列结构与「批量导入」模板完全一致**，导出的文件改完可以直接导回系统
- 可按**门店、厂商、关键字**（设备名 / IP / 门店）筛选
- Excel 版带表头样式、列宽、冻结首行、自动筛选与列批注说明
- 末尾可附带只读列：在线状态、最后备份时间、最后备份结果、设备 ID
  （导入时自动忽略）
- 新增接口：
  - `GET /api/export/devices.csv`
  - `GET /api/export/devices.xlsx`

### 密码保护

设备清单**默认不导出密码**，密码列留空，导出文件可以放心分发。

确实需要时勾选「包含登录密码」（仅 admin 权限），前端有二次确认，
后端也会校验角色，避免误操作导致明文密码落到磁盘上。

## 修复

- `db.create_device` 未指定命令时使用全局默认命令集（那是思科的），
  导致绕过 API 层创建的设备（如导入器、脚本）拿到了错误的采集命令。
  现按厂商取默认命令集。
- `list_devices` 返回的设备信息里没有 `password_enc` 字段，
  导致按「密码为空则重新解密」的判断永远不成立。

---

# 多厂商网络设备配置备份中心 v1.2.0

飞塔 FortiGate 的配置自动转换为标准 FortiOS `.conf` 格式。

## 新增

### 飞塔配置导出为标准 .conf

`show full-configuration` 的输出原先直接存成 `.txt`，现在自动转换成
FortiOS 备份文件格式，并补全头部元信息：

```
#config-version=FGT60F-7.0.5-FW-build0307-220331:opmode=0:vdom=0
#conf_file_ver=202201,201912
#buildno=0307,220331
#global_vdom=1
#serial=FGT60FTK20000000
config system global
    set hostname "FGT-01"
    ...
```

- 型号归一：`FortiGate-60F` → `FGT60F`，`FortiGate-VM64` → `FGVM64`
- 采集前先取一次 `get system status`，头部才有真实版本号
- 导出打包时飞塔配置为 `.conf`，其他厂商仍是 `.txt`
- 转换器做成可注册的机制（`CONFIG_TRANSFORMS`），后续其他厂商要转换可直接接入

> ⚠️ FortiGate 官方明确说明：`show` 系列的输出**不能用于恢复配置**，
> 真正可恢复的备份需要 `execute backup config`（要把文件推到 flash / FTP /
> TFTP / 管理站）。本次转换产出的是规范化、可归档、可对比的配置文件，
> 补上了版本头信息，在同型号同固件上可作为配置基线，
> 但不含证书私钥等 `execute backup` 才有的内容。

---

# 多厂商网络设备配置备份中心 v1.1.0

新增**飞塔 FortiGate 防火墙**支持，同时完善了历史记录管理与排查能力。

## 新增

### 飞塔 FortiGate 防火墙支持

- 配置采集命令：`show full-configuration`
- 邻居命令：`diagnose lldp rx neighbor summary`（7.6.3+），
  自动兼容旧版本的 `diagnose lldprx neighbor summary`
- 提示符识别：`FGT #`（特权）、`FGT $`（只读账号）、
  `FGT (system console) #`（配置模式）、`FGT (port1) #`（接口模式）
- 只支持 SSH（FortiGate 官方不提供 Telnet 管理接口），
  选择其他协议时自动纠正
- 邻居解析独立实现，覆盖三种输出格式：
  - summary 单行格式（7.6.3+）
  - summary 带接口索引格式（7.0.4+）
  - `get system lldp neighbor` 块格式
- 版本号提取：`get system status` 的 `Version:` 字段

### 备份历史页

- 多维度筛选：结果 / 触发方式 / 配置变化 / 门店 / 时间范围 / 设备
- 失败排查面板：常见失败原因 Top 5（条形图对比）、失败设备排行、
  一键「只看失败记录」
- **删除失败记录**：单条删除与批量清理，只删失败记录，成功与进行中的一律保留
- 分页浏览，每页 50 条

### 设备页

- **备份结果筛选**：最近备份失败 / 成功 / 曾失败 / 从未备份 / 配置有变化
- **单台设备在线探测**：设备列表与在线监控页均可单独探测，
  只做 TCP/SSH banner 握手，不登录设备
- 探测后就地更新在线状态，不整表重绘

### 邻居拓扑

- **按门店查看互联关系**，区分本门店内与跨店外部链接
- 修复 CDP `Device ID` 带域名后缀时无法匹配设备的问题

## 改进

- Docker 构建瘦身：去掉不必要的编译工具链，新增 `.dockerignore`，
  修复 `*.bat` 被误忽略导致启动脚本从未进仓库的问题
- 新增一键更新脚本 `deploy/update.sh`：备份 → 拉取 → 构建 → 健康检查，
  失败自动回滚
- 新增开机自启与健康守护脚本 `deploy/install_autostart.sh`
- Windows 支持后台启动与一键关闭（`后台启动服务.bat` / `关闭服务.bat`）
- 飞塔分页命令需在配置模式下执行，采集前会先 `end` 退回顶层

## 修复

- 创建设备时未传 `protocol` 会因 `exclude_unset` 丢失该字段而报错
- 切换厂商时未同步纠正协议与默认命令
- 变化筛选会把失败记录算进「配置无变化」（失败的 `changed` 恒为 0）

---

# 多厂商网络设备配置备份中心 v1.0.0

首个正式版。支持 Cisco / H3C / 华为 / 锐捷四家网络设备的配置自动备份。

> 应用名称：原「Cisco 配置备份中心」→ 「网络配置备份中心」→ **「多厂商网络设备配置备份中心」**。
> 支持多厂商后不再带厂商前缀。仓库名与 systemd 服务名（`cisco-backup`）保持不变，部署命令不受影响。

## 功能特性

### 多厂商设备采集

| 厂商 | 配置命令 | 邻居命令 | Enable |
|---|---|---|---|
| Cisco IOS | `show running-config` | `show cdp neighbors detail` | 需要 |
| H3C Comware | `display current-configuration` | `display lldp neighbor-information` | 不需要 |
| 华为 VRP | `display current-configuration` | `display lldp neighbor brief` | 不需要 |
| 锐捷 RGOS | `show running-config` | `show lldp neighbors` | 不需要 |

各厂商的差异已全部适配：

- **双协议**：SSH 与 Telnet，Python 3.13 移除了 `telnetlib`，Telnet 客户端为自研（含完整 IAC 协商）
- **提示符识别**：Cisco 的 `SW>` / `SW#`，华为与 H3C 的 `<Sysname>` / `[Sysname-GigabitEthernet0/0/1]`
- **分页处理**：`terminal length 0` / `screen-length disable` / `screen-length 0 temporary`
- **邻居解析**：四家输出格式完全不同，独立解析器
- **版本提取**：各厂商版本号格式差异的正则适配

### 配置备份

- **自定义命令**：每台设备可配任意命令列表，默认为厂商适配命令
- **差异版本**：以 SHA256 判断变化，无变化不产生新版本；变化时生成 unified diff 并高亮展示
- **定时备份**：每台设备独立 crontab，支持并发
- **版本清理**：按设备保留最近 N 个变化版本

### 门店归类

- 门店为独立实体，设备归属门店
- 门店级批量备份与打包下载
- 门店卡片实时显示设备数、正常/异常数、最近备份时间

### 批量导入

- CSV / Excel 批量导入，中英文列名自动识别
- 逐行校验，出错的行单独列出原因，不影响其他行
- Excel 模板含列批注与填写说明，覆盖四家厂商示例
- 两种模式：仅新增 / 按名称更新（密码留空保持原值）

### 在线探测

- 默认每 5 分钟自动探测，服务启动时立即跑一次
- 只做 TCP 层判断（SSH 额外校验 banner），开销极低，不干扰备份任务
- 记录延迟、最后在线时间、连续失败次数，区分网络抖动与真正掉线

### 打包下载

- 按全部门店 / 单门店 / 单设备导出 zip
- 包内按 `<门店>/<设备>/<备份时间>/<命令>.txt` 组织
- 附 `manifest.csv` 清单与各版本 `.diff`

### 登录认证

- 本地账号 + AD 域账号双入口，全局强制登录
- 三级角色：管理员 / 操作员 / 只读，按接口校验

安全措施：

- 密码 PBKDF2-HMAC-SHA256（26 万次迭代 + 随机盐）
- 会话 token 数据库仅存 SHA256 哈希
- 连续 5 次失败锁定 15 分钟
- 登录失败提示不区分用户不存在与密码错误
- AD 默认要求 LDAPS，**不提供跳过证书校验的开关**
- LDAP 过滤器按 RFC 4515 转义防注入
- AD 管理员组匹配严格区分完整 DN 与纯组名

## 部署

### Docker（推荐）

```bash
git clone https://github.com/zduyiwuer/cisco-config-backup.git
cd cisco-config-backup
cp .env.example .env      # 可选：预设管理员密码
vim .env
docker compose up -d
```

访问 `http://<服务器IP>:8077`。数据在 `./data`，容器重建不丢。

### systemd

```bash
sudo ./deploy/install_systemd.sh
```

自动创建低权限用户、装依赖、注册服务并启动。首次安装显示一次性管理员密码。

### Windows

双击 `启动服务.bat`。

生产环境建议前置 Nginx + HTTPS，示例见 `deploy/nginx.conf.example`。

详细说明见 [deploy/DEPLOYMENT.md](deploy/DEPLOYMENT.md)。

## 首次使用

1. 启动服务，用控制台打印的 admin 密码登录（**仅显示一次**）
2. 立即在「用户管理」里修改密码
3. 「门店」页新建门店
4. 「设备」页新增设备，或用「导入」批量导入（先下载 Excel 模板）
5. 点「测试」验证连通性，再点「备份」
6. 需要自动备份时，在设备上勾选「启用定时备份」并填 crontab

忘记密码：

```bash
./.venv/bin/python scripts/reset_admin_password.py admin 新密码
```

## 测试

458 项自动化测试全部通过：

```bash
./.venv/bin/python -m tests.test_e2e           # 87 项  采集/差异/API/打包
./.venv/bin/python -m tests.test_vendors       # 61 项  多厂商端到端
./.venv/bin/python -m tests.test_auth# 110 项 登录/会话/权限/AD
./.venv/bin/python -m tests.test_import_probe   # 70 项  导入 + 在线探测
./.venv/bin/python -m tests.test_neighbors      # 52 项  四家厂商邻居解析
./.venv/bin/python -m tests.test_xlsx_template  # 53 项  Excel 模板
./.venv/bin/python -m tests.test_migration      # 25 项  旧库平滑升级
```

测试内置模拟设备，走真实 SSH 握手与 Telnet IAC 协商，无需真实网络设备。

## 注意事项

- `data/.secret_key` 是解密设备密码的唯一依据，**不要删除**，迁移时务必与数据库一起备份
- Telnet 不加密，仅建议用于内网老设备
- 设备侧建议配置只读或限制 enable 权限的备份账号
- AD 默认拒绝明文 LDAP(389)，需显式开启才允许
- 升级时数据库结构自动迁移，老数据完整保留

## 许可

MIT