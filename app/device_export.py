"""设备清单导出。

导出当前系统的全部设备（或按门店/厂商筛选），格式与「批量导入」模板保持一致，
导出的文件可以直接改完再导回来。

两种格式：
- CSV：UTF-8 BOM，Excel/WPS 直接打开不乱码
- XLSX：带表头样式、列宽、冻结首行、自动筛选

关于密码
--------
默认**不导出密码**，密码列留空。这样导出文件可以放心发给他人，
也不会以明文形式落在磁盘上。

确实需要带密码时传 include_password=True（需 admin 权限），
此时密码列填真实值，导出文件的敏感级别由使用者自行把控。
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any

from . import db, vendors

# 与导入模板一致的列（可回导）
BASE_HEADERS = [
    "设备名称", "管理IP", "厂商", "协议", "端口", "用户名", "登录密码",
    "Enable密码", "门店", "定时备份", "Cron", "采集命令", "备注",
]

# 额外的只读信息列（用于查看，导入时会忽略）
EXTRA_HEADERS = ["在线状态", "最后备份时间", "最后备份结果", "设备ID"]

# 厂商 id -> 导入模板里用的中文名，保证导出的清单能被正确识别
VENDOR_LABELS = {
    "cisco": "cisco",
    "h3c": "h3c",
    "huawei": "huawei",
    "ruijie": "ruijie",
    "fortinet": "fortinet",
}

# 厂商 id -> 模板里额外接受的中文写法
VENDOR_ALIASES = {
    "cisco": "cisco",
    "h3c": "h3c",
    "huawei": "huawei",
    "ruijie": "ruijie",
    "fortinet": "fortinet",
}


def _bool_cn(value: Any) -> str:
    """布尔转成导入模板认得的 是/否。"""
    return "是" if value else "否"


def _device_row(dev: dict[str, Any], include_password: bool) -> list[str]:
    """把一台设备转成一行导出数据。"""
    vendor = (dev.get("device_type") or "cisco").strip().lower()
    commands = dev.get("commands") or []
    if isinstance(commands, str):
        try:
            import json

            commands = json.loads(commands)
        except Exception:
            commands = []
    # 采集命令列用分号分隔，与导入模板一致
    cmd_text = ";".join(commands) if commands else ""
    password = dev.get("password") or "" if include_password else ""
    enable_pw = dev.get("enable_password") or "" if include_password else ""

    return [
        dev.get("name") or "",
        dev.get("host") or "",
        VENDOR_LABELS.get(vendor, vendor),
        (dev.get("protocol") or "ssh").lower(),
        str(dev.get("port") or ""),
        dev.get("username") or "",
        password,
        enable_pw,
        dev.get("site") or "",
        _bool_cn(dev.get("schedule_enabled")),
        dev.get("cron") or "",
        cmd_text,
        dev.get("note") or "",
    ]


def _extra_cells(dev: dict[str, Any]) -> list[str]:
    """只读信息列。"""
    online = dev.get("online")
    if online is None:
        status = "未探测"
    elif online in (1, True):
        status = "在线"
    else:
        status = "离线"
    last_status = {
        "success": "成功",
        "failed": "失败",
        "running": "进行中",
    }.get(dev.get("last_status") or "", dev.get("last_status") or "—")
    return [
        status,
        dev.get("last_backup_at") or "从未备份",
        last_status,
        str(dev.get("id") or ""),
    ]


def collect_devices(
    site_id: int | None = None,
    vendor: str | None = None,
    keyword: str | None = None,
    reveal: bool = False,
) -> list[dict[str, Any]]:
    """按条件取设备。

    reveal=True 时逐台解密密码（list_devices 默认不返回明文，
    且返回值里没有 password_enc 字段可供判断，只能重新按 id 取）。
    """
    devices = db.list_devices(site_id=site_id, keyword=keyword)
    if vendor:
        want = vendors.normalize_vendor(vendor)
        devices = [d for d in devices
                   if vendors.normalize_vendor(d.get("device_type")) == want]
    if reveal:
        out = []
        for d in devices:
            full = db.get_device(d["id"], reveal=True)
            if full:
                out.append(full)
        return out
    return devices


def export_csv(
    site_id: int | None = None,
    vendor: str | None = None,
    keyword: str | None = None,
    include_password: bool = False,
    include_extra: bool = True,
) -> tuple[bytes, str, dict[str, Any]]:
    """导出设备清单为 CSV。返回 (内容, 文件名, 统计)。"""
    devices = collect_devices(site_id, vendor, keyword, reveal=include_password)
    headers = list(BASE_HEADERS) + (list(EXTRA_HEADERS) if include_extra else [])

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(headers)
    for dev in devices:
        row = _device_row(dev, include_password)
        if include_extra:
            row += _extra_cells(dev)
        writer.writerow(row)

    # BOM 让 Excel / WPS 正确识别 UTF-8
    text = "﻿" + buffer.getvalue()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"设备清单_{stamp}.csv"
    stats = {
        "devices": len(devices),
        "sites": len({d.get("site") or "未归属" for d in devices}),
        "include_password": include_password,
    }
    return text.encode("utf-8"), filename, stats


def export_xlsx(
    site_id: int | None = None,
    vendor: str | None = None,
    keyword: str | None = None,
    include_password: bool = False,
    include_extra: bool = True,
) -> tuple[bytes, str, dict[str, Any]]:
    """导出设备清单为 XLSX。返回 (内容, 文件名, 统计)。"""
    from openpyxl import Workbook
    from openpyxl.comments import Comment
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    devices = collect_devices(site_id, vendor, keyword, reveal=include_password)
    headers = list(BASE_HEADERS) + (list(EXTRA_HEADERS) if include_extra else [])

    wb = Workbook()
    ws = wb.active
    ws.title = "设备清单"

    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill("solid", fgColor="2563EB")
    center = Alignment(horizontal="center", vertical="center")

    ws.append(headers)
    for cell in ws[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center
    ws.row_dimensions[1].height = 22

    for dev in devices:
        row = _device_row(dev, include_password)
        if include_extra:
            row += _extra_cells(dev)
        ws.append(row)

    widths = [16, 14, 12, 8, 7, 12, 14, 14, 20, 10, 14, 40, 18, 10, 20, 12, 8]
    hints = [
        "设备唯一名称",
        "管理 IP 或主机名",
        "厂商：cisco / h3c / huawei / ruijie / fortinet",
        "ssh 或 telnet",
        "端口",
        "登录用户名",
        "登录密码" + ("" if include_password else "（未导出，如需回导请在系统中重新填写）"),
        "仅 Cisco 需要 enable 提权",
        "门店名称，留空为未归属",
        "是否启用定时备份",
        "5 段式 cron",
        "多条命令用分号分隔",
        "备注",
        "只读列，导入时忽略",
        "只读列，导入时忽略",
        "只读列，导入时忽略",
        "只读列，导入时忽略",
    ]
    for idx, (width, hint) in enumerate(zip(widths, hints), start=1):
        cell = ws.cell(row=1, column=idx)
        cell.comment = Comment(hint, "备份中心")
        ws.column_dimensions[get_column_letter(idx)].width = width

    # 冻结首行 + 自动筛选，方便查看
    ws.freeze_panes = "A2"
    if devices:
        ws.auto_filter.ref = (
            f"A1:{get_column_letter(len(headers))}{len(devices) + 1}"
        )

    # 说明页
    ws2 = wb.create_sheet("填写说明")
    notes = [
        ["设备清单导出说明", ""],
        ["", ""],
        ["导出时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
        ["设备数量", str(len(devices))],
        ["门店数量", str(len({d.get("site") or "未归属" for d in devices}))],
        ["", ""],
        ["用途", "本表列结构与「批量导入」模板一致，改完可直接导回系统。"],
        ["密码", "默认不导出密码，密码列为空；如需回导请在系统中重新填写。"],
        ["只读列", "末尾的在线状态、最后备份时间等列导入时会自动忽略。"],
        ["", ""],
        ["厂商取值", "cisco / h3c / huawei / ruijie / fortinet"],
    ]
    for row in notes:
        ws2.append(row)
    ws2.column_dimensions["A"].width = 16
    ws2.column_dimensions["B"].width = 60

    buffer = io.BytesIO()
    wb.save(buffer)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"设备清单_{stamp}.xlsx"
    stats = {
        "devices": len(devices),
        "sites": len({d.get("site") or "未归属" for d in devices}),
        "include_password": include_password,
    }
    return buffer.getvalue(), filename, stats
