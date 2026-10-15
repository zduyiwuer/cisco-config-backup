"""设备批量导入。

支持 CSV / Excel(xlsx, xlsm) 两种格式，含以下能力：
- 中英文列名自动识别（设备名称/device/name…）
- 门店可填名称，不存在则自动创建
- 逐行校验，失败行不影响其他行，最终返回成功/失败明细
- 支持"仅新增"与"按名称更新已有设备"两种模式
- 可导出标准模板，方便用户照着填
"""

from __future__ import annotations

import csv
import io
from typing import Any

from . import db, vendors

def _norm(text: Any) -> str:
    """规范化列名：转小写并去掉空格/下划线/连字符，使 ip_address 与 IP ADDRESS 等价。"""
    return str(text or "").strip().lower().replace(" ", "").replace("_", "").replace("-", "")


# 列名别名 -> 内部字段（键为规范化后的形式，与 _norm 处理一致）
COLUMN_ALIASES: dict[str, str] = {
    # 设备名
    "设备名称": "name", "设备名": "name", "名称": "name", "主机名": "name",
    "name": "name", "device": "name", "hostname": "name", "switchname": "name",
    # 管理 IP
    "管理ip": "host", "管理地址": "host", "ip": "host", "ip地址": "host",
    "地址": "host", "host": "host", "ipaddress": "host", "mgmtip": "host",
    "managementip": "host",
    # 协议与端口
    "协议": "protocol", "protocol": "protocol", "连接方式": "protocol",
    "端口": "port", "port": "port", "端口号": "port",
    # 厂商
    "厂商": "device_type", "设备厂商": "device_type", "品牌": "device_type",
    "devicetype": "device_type", "vendor": "device_type", "型号类型": "device_type",
    # 凭据
    "用户名": "username", "username": "username", "user": "username",
    "登录密码": "password", "密码": "password", "password": "password",
    "enable密码": "enable_password", "enablepassword": "enable_password",
    "enable口令": "enable_password", "特权密码": "enable_password",
    # 归属与描述
    "门店": "site", "所属门店": "site", "site": "site", "store": "site",
    "门店名称": "site", "分店": "site",
    "备注": "note", "说明": "note", "note": "note", "remark": "note",
    "描述": "note", "description": "note",
    # 备份策略
    "定时备份": "schedule_enabled", "启用定时": "schedule_enabled",
    "cron": "cron", "cron表达式": "cron", "定时表达式": "cron",
    "采集命令": "commands", "commands": "commands", "命令": "commands",
}

TEMPLATE_HEADERS = [
    "设备名称", "管理IP", "厂商", "协议", "端口", "用户名", "登录密码",
    "Enable密码", "门店", "定时备份", "Cron", "采集命令", "备注",
]

# 模板中的示例行，覆盖四种厂商
TEMPLATE_ROWS = [
    [
        "core-sw-01", "10.0.0.1", "cisco", "ssh", "22", "cisco", "cisco123",
        "enable123", "上海南京西路店", "是", "0 2 * * *",
        "show running-config;show cdp neighbors detail", "思科核心交换机",
    ],
    [
        "h3c-sw-02", "10.0.0.2", "h3c", "ssh", "22", "admin", "h3c123",
        "", "上海南京西路店", "否", "", "", "新华三接入交换机",
    ],
    [
        "huawei-sw-03", "10.0.0.3", "huawei", "ssh", "22", "admin", "hw123",
        "", "深圳南山科技园店", "是", "0 3 * * *",
        "display current-configuration;display lldp neighbor brief", "华为接入交换机",
    ],
    [
        "access-sw-04", "10.0.0.4", "cisco", "telnet", "23", "cisco", "cisco123",
        "", "深圳南山科技园店", "否", "", "show running-config", "老设备走 telnet",
    ],
]


def _map_headers(row: dict[str, Any]) -> dict[str, Any]:
    """把原始表头映射为内部字段名。"""
    mapped: dict[str, Any] = {}
    for raw_key, value in row.items():
        field = COLUMN_ALIASES.get(_norm(raw_key))
        if field:
            mapped[field] = value
    return mapped


def _to_bool(value: Any) -> bool:
    return _norm(value) in ("1", "true", "yes", "y", "是", "开", "启用", "on")


def _split_commands(value: Any) -> list[str] | None:
    if not value:
        return None
    text = str(value)
    parts = [p.strip() for p in text.replace("；", ";").replace(",", ";").split(";") if p.strip()]
    if len(parts) == 1:
        parts = [p.strip() for p in text.splitlines() if p.strip()]
    return parts or None


def read_table(content: bytes | str, filename: str) -> list[dict[str, Any]]:
    """读取上传的 CSV 或 Excel 内容为字典列表。兼容 bytes 与 str 输入。"""
    if isinstance(content, str):
        text_content = content
    else:
        text_content = None

    name = filename.lower()
    if not name.endswith((".csv", ".txt")) and not text_content:
        try:
            from openpyxl import load_workbook

            wb = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            wb.close()
            if not rows:
                return []
            headers = [str(h or "") for h in rows[0]]
            return [
                {headers[i]: (row[i] if i < len(row) else None) for i in range(len(headers))}
                for row in rows[1:]
                if any(c is not None and str(c).strip() for c in row)
            ]
        except Exception:
            # 不是有效的 Excel，当作 CSV 再试一次
            pass

    # CSV：先尝试 utf-8-sig（Excel 导出的中文 CSV），失败则用 gbk
    if text_content is None:
        for encoding in ("utf-8-sig", "gbk", "utf-8"):
            try:
                text_content = content.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if text_content is None:
            raise ValueError("文件编码无法识别，请另存为 UTF-8 或 GBK 编码的 CSV")
    return list(csv.DictReader(io.StringIO(text_content)))


def template_csv() -> str:
    """生成导入模板（含示例行）。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(TEMPLATE_HEADERS)
    writer.writerows(TEMPLATE_ROWS)
    return "\ufeff" + buffer.getvalue()


def template_xlsx() -> bytes:
    """生成 Excel 版导入模板，带列宽、表头样式与批注说明。"""
    from openpyxl import Workbook
    from openpyxl.comments import Comment
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "设备清单"

    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill("solid", fgColor="2563EB")
    center = Alignment(horizontal="center", vertical="center")

    ws.append(TEMPLATE_HEADERS)
    for cell in ws[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center
    ws.row_dimensions[1].height = 22

    for row in TEMPLATE_ROWS:
        ws.append(row)

    # 列宽与列批注
    widths = [16, 14, 12, 8, 7, 12, 14, 14, 20, 10, 14, 40, 18]
    hints = [
        "必填。设备唯一名称，如 core-sw-01",
        "必填。管理 IP 或主机名",
        "厂商：cisco / h3c / huawei / ruijie，留空默认 cisco。\n"
        "决定采集命令、提示符识别与邻居解析方式",
        "ssh 或 telnet，留空默认 ssh",
        "留空按协议自动补：ssh=22，telnet=23",
        "登录用户名",
        "新增时必填。留空（更新模式）则保持原密码",
        "仅 Cisco 需要（enable 提权），华为/H3C 留空",
        "留空则为未归属；填写的门店不存在会自动创建",
        "是/否、1/0、true/false",
        "5段式 cron，如 0 2 * * * 表示每天 02:00",
        "多条命令用分号分隔；留空则按厂商自动套用默认命令",
        "任意备注",
    ]
    for idx, (width, hint) in enumerate(zip(widths, hints), start=1):
        col = ws.cell(row=1, column=idx)
        col.comment = Comment(hint, "备份中心")
        ws.column_dimensions[col.column_letter].width = width

    # 说明页
    ws2 = wb.create_sheet("填写说明")
    notes = [
        ["设备导入模板使用说明", ""],
        ["", ""],
        ["1", "第一张表「设备清单」填写数据，第一行为表头，请勿修改表头文字。"],
        ["2", "必填项：设备名称、管理 IP、登录密码（新增时）。"],
        ["3", "门店列填写门店名称，不存在会自动创建，无需事先在系统里建门店。"],
        ["4", "端口留空时按协议自动补全：ssh=22，telnet=23。"],
        ["5", "定时备份填「是」，并填写 Cron 表达式，即可导入后自动生效。"],
        ["6", "采集命令多条用分号分隔；留空则按厂商自动套用默认命令。"],
        ["7", "导入方式：仅新增（重名跳过）或按名称更新（密码留空保持原值）。"],
        ["8", "导入时逐行校验，出错的行会单独列出原因，不影响其他行。"],
        ["", ""],
        ["厂商支持", "厂商列决定采集命令、提示符识别与邻居解析方式，留空默认为 cisco。"],
        ["", "cisco   → show running-config / show cdp neighbors detail"],
        ["", "h3c        → display current-configuration / display lldp neighbor-information"],
        ["", "huawei     → display current-configuration / display lldp neighbor brief"],
        ["", "ruijie     → show running-config / show lldp neighbors"],
        ["", "支持中文：思科、新华三/H3C、华为、锐捷"],
        ["", ""],
        ["Enable 密码", "仅 Cisco 需要（用于 enable 提权），华为与 H3C 登录后即特权视图，留空即可。"],
        ["", ""],
        ["列名兼容", "中文与英文表头均可识别，例如 管理IP / ip / ip_address / host 都能识别。"],
    ]
    for row in notes:
        ws2.append(row)
    ws2.column_dimensions["A"].width = 12
    ws2.column_dimensions["B"].width = 76
    ws2["A1"].font = Font(bold=True, size=13, color="1D4ED8")
    for r in range(3, ws2.max_row + 1):
        ws2.cell(row=r, column=2).alignment = Alignment(wrap_text=True, vertical="top")

    # 冻结表头
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{ws.cell(row=1, column=len(TEMPLATE_HEADERS)).coordinate}"

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _resolve_vendor(raw: str) -> str:
    """把填写的厂商标识归一为内部 vendor id。

    支持英文（cisco / h3c / huawei / ruijie）与中文
    （思科 / 华为 / 新华三 / H3C / 锐捷），识别不了则回退 cisco。
    """
    from . import vendors

    text = (raw or "").strip().lower()
    if not text:
        return "cisco"
    # 中文与常见别名映射
    cn_map = {
        "思科": "cisco", "cisco": "cisco", "ios": "cisco", "cisco ios": "cisco",
        "华为": "huawei", "huawei": "huawei", "vrp": "huawei",
        "新华三": "h3c", "h3c": "h3c", "华三": "h3c", "comware": "h3c",
        "锐捷": "ruijie", "ruijie": "ruijie", "rgos": "ruijie",
    }
    key = text.replace(" ", "_")
    if key in cn_map:
        return cn_map[key]
    if key in vendors.VENDORS:
        return key
    # 走 vendors 自己的别名表（如 h3c_comware、huawei_vrp）
    resolved = vendors.normalize_vendor(key)
    return resolved


def import_devices(
    rows: list[dict[str, Any]], mode: str = "create", default_site: str = ""
) -> dict[str, Any]:
    """执行导入。

    mode: create（仅新增，重名跳过）/ upsert（按名称更新已有设备）
    """
    if mode not in ("create", "upsert"):
        raise ValueError("mode 只能是 create 或 upsert")

    results: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    existing = {d["name"]: d for d in db.list_devices()}
    seen_names.update(existing)

    for index, raw in enumerate(rows, start=2):  # 2 = 表头之后的第一行
        item = _map_headers(raw)
        name = str(item.get("name") or "").strip()
        host = str(item.get("host") or "").strip()

        if not name and not host:
            continue  # 空行静默跳过
        if not name:
            results.append({"row": index, "status": "failed", "name": host, "message": "缺少设备名称"})
            continue
        if not host:
            results.append({"row": index, "status": "failed", "name": name, "message": "缺少管理 IP"})
            continue
        if name in seen_names and mode == "create":
            results.append({"row": index, "status": "skipped", "name": name, "message": "设备名已存在，已跳过"})
            continue

        protocol = str(item.get("protocol") or "ssh").strip().lower()
        if protocol not in ("ssh", "telnet"):
            results.append({"row": index, "status": "failed", "name": name, "message": f"协议只能是 ssh 或 telnet，当前为 {protocol}"})
            continue

        # 厂商：支持中文与英文标识，未知则回退 cisco
        vendor_raw = str(item.get("device_type") or "").strip()
        vendor = _resolve_vendor(vendor_raw)

        port_raw = item.get("port")
        if port_raw in (None, ""):
            port = vendors.default_port_for(vendor)
        else:
            try:
                port = int(port_raw)
            except (TypeError, ValueError):
                results.append({"row": index, "status": "failed", "name": name, "message": f"端口格式错误: {port_raw}"})
                continue
            if not (1 <= port <= 65535):
                results.append({"row": index, "status": "failed", "name": name, "message": f"端口超出范围: {port}"})
                continue

        site_name = str(item.get("site") or default_site or "").strip()
        payload: dict[str, Any] = {
            "name": name,
            "host": host,
            "protocol": protocol,
            "port": port,
            "device_type": vendor,
            "username": str(item.get("username") or "").strip(),
            "password": str(item.get("password") or ""),
            "enable_password": str(item.get("enable_password") or ""),
            "site": site_name,
            "note": str(item.get("note") or ""),
        }
        commands = _split_commands(item.get("commands"))
        # 未填命令时按厂商套用默认命令集（关键：不同厂商命令不同）
        payload["commands"] = commands or vendors.default_commands_for(vendor)
        payload["schedule_enabled"] = _to_bool(item.get("schedule_enabled"))
        if payload["schedule_enabled"]:
            payload["cron"] = str(item.get("cron") or "0 2 * * *").strip() or "0 2 * * *"

        try:
            if mode == "upsert" and name in existing:
                # 更新模式：密码留空则保持原值
                if not payload["password"]:
                    payload.pop("password")
                    payload.pop("enable_password", None)
                db.update_device(existing[name]["id"], payload)
                results.append({"row": index, "status": "updated", "name": name, "message": "已更新"})
            else:
                if not payload["password"]:
                    results.append({"row": index, "status": "failed", "name": name, "message": "缺少登录密码"})
                    continue
                db.create_device(payload)
                seen_names.add(name)
                results.append({"row": index, "status": "created", "name": name, "message": "导入成功"})
        except ValueError as exc:
            results.append({"row": index, "status": "failed", "name": name, "message": str(exc)})
        except Exception as exc:  # noqa: BLE001
            results.append({"row": index, "status": "failed", "name": name, "message": f"{type(exc).__name__}: {exc}"})

    summary = {
        "total": len(results),
        "created": sum(1 for r in results if r["status"] == "created"),
        "updated": sum(1 for r in results if r["status"] == "updated"),
        "skipped": sum(1 for r in results if r["status"] == "skipped"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
    }
    return {"summary": summary, "details": results}