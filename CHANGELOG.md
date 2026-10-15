# Cisco 配置备份中心 v1.0.0

首个正式版。支持 Cisco / H3C / 华为 / 锐捷四家网络设备的配置自动备份。

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