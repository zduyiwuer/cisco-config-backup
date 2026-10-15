"""备份文件打包导出。

支持三种粒度：
- 全部：所有设备的历史备份
- 按门店：某个门店下所有设备的历史备份
- 按设备：单台设备的历史备份

包内目录结构：
    cisco_backup_<时间戳>/
        manifest.csv                 备份清单（设备/命令/时间/SHA256）
        README.txt                   说明文件
        <门店名>/
            <设备名>/
                <YYYYmmdd-HHMMSS>/
                    show_running-config.txt
                    show_cdp_neighbors_detail.txt
                    _diff/
                        show_running-config.diff
"""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import datetime
from typing import Any, Iterable

from . import db


def _safe(name: str) -> str:
    """把字符串转成安全的文件/目录名，保留中文。"""
    if not name:
        return "_unnamed"
    bad = '<>:"/\\|?*\r\n\t'
    out = "".join("_" if ch in bad else ch for ch in str(name)).strip(" .")
    return (out or "_unnamed")[:80]


def _timestamp(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%Y%m%d-%H%M%S")
    except (TypeError, ValueError):
        return "unknown"


def content_disposition(filename: str) -> str:
    """生成支持中文文件名的 Content-Disposition 头。

    HTTP 头只能编码 latin-1，中文文件名必须用 RFC 5987 的 filename*=UTF-8'' 形式，
    同时保留一个 ASCII 回退名供老浏览器使用。
    """
    from urllib.parse import quote

    ascii_fallback = filename.encode("ascii", "ignore").decode() or "backup.zip"
    return (
        f'attachment; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{quote(filename)}"
    )


def _iter_backup_files(
    device_ids: Iterable[int],
    limit_per_device: int | None = None,
    since: str | None = None,
    until: str | None = None,
    commands: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """取出待导出的备份文件明细，按门店/设备/时间排序。"""
    conn = db.get_conn()
    ids = list(device_ids)
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    sql = f"""
        SELECT f.id AS file_id, f.backup_id, f.command, f.slug, f.content, f.sha256,
               f.is_new, f.size, f.created_at,
               b.started_at, b.status, b.trigger, b.os_version,
               d.id AS device_id, d.name AS device_name, d.host, d.site, d.site_id,
               s.name AS site_name
        FROM backup_files f
        JOIN backups b ON b.id = f.backup_id
        JOIN devices d ON d.id = f.device_id
        LEFT JOIN sites s ON s.id = d.site_id
        WHERE d.id IN ({placeholders})
          AND b.status = 'success'
    """
    params: list[Any] = list(ids)
    if since:
        sql += " AND b.started_at >= ?"
        params.append(since)
    if until:
        sql += " AND b.started_at <= ?"
        params.append(until)
    if commands:
        cmds = list(commands)
        sql += f" AND f.command IN ({','.join('?' * len(cmds))})"
        params.extend(cmds)
    sql += " ORDER BY COALESCE(s.name, d.site, '未归属门店'), d.name, b.started_at DESC, f.id"

    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    if limit_per_device:
        # 每台设备只保留最近 N 次备份（按 backup_id 计）
        seen: dict[int, int] = {}
        kept: list[dict[str, Any]] = []
        for row in rows:
            key = row["device_id"]
            count = seen.get(key, 0)
            if count < limit_per_device:
                kept.append(row)
                seen[key] = count + 1
        rows = kept
    return rows


def _collect_diffs(backup_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    conn = db.get_conn()
    ids = list(set(backup_ids))
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT * FROM backup_diffs WHERE backup_id IN ({placeholders})", ids
    ).fetchall()
    return {r["file_id"]: dict(r) for r in rows}


def build_zip(
    device_ids: list[int],
    filename_prefix: str = "cisco_backup",
    limit_per_device: int | None = None,
    since: str | None = None,
    until: str | None = None,
    commands: list[str] | None = None,
    include_diff: bool = True,
    include_readme: bool = True,
) -> tuple[bytes, str, dict[str, Any]]:
    """构造 zip 包，返回 (内容, 文件名, 统计)。"""
    rows = _iter_backup_files(device_ids, limit_per_device, since, until, commands)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    root = f"{_safe(filename_prefix)}_{stamp}"

    if not rows:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                f"{root}/README.txt",
                "没有符合条件的备份文件。\n请先在设备页执行备份，或放宽筛选条件。\n",
            )
        return buf.getvalue(), f"{root}_empty.zip", {"files": 0, "backups": 0, "devices": 0}

    diffs = _collect_diffs(r["backup_id"] for r in rows) if include_diff else {}

    buffer = io.BytesIO()
    site_names: set[str] = set()
    device_keys: set[tuple] = set()
    backup_keys: set[tuple] = set()
    manifest_rows: list[list[Any]] = []
    total_size = 0

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for row in rows:
            site_label = row["site_name"] or row["site"] or "未归属门店"
            site_names.add(site_label)
            device_keys.add((site_label, row["device_name"]))
            backup_keys.add((site_label, row["device_name"], row["started_at"]))
            total_size += row["size"]

            dir_path = (
                f"{root}/{_safe(site_label)}/{_safe(row['device_name'])}"
                f"/{_timestamp(row['started_at'])}"
            )
            fname = f"{dir_path}/{_safe(row['slug'])}.txt"
            zf.writestr(fname, row["content"])

            diff = diffs.get(row["file_id"])
            diff_count = None
            if include_diff and diff:
                zf.writestr(
                    f"{dir_path}/_diff/{_safe(row['slug'])}.diff", diff["diff_text"]
                )
                diff_count = f"+{diff['added']}/-{diff['removed']}"

            manifest_rows.append(
                [
                    row["site_name"] or row["site"] or "",
                    row["device_name"],
                    row["host"],
                    row["command"],
                    row["started_at"],
                    row["trigger"],
                    row["os_version"] or "",
                    row["sha256"],
                    row["size"],
                    "新版本" if row["is_new"] else "无变化",
                    diff_count or "",
                    fname,
                ]
            )

        # 清单 CSV（带 BOM，Excel 直接打开不乱码）
        buf_csv = io.StringIO()
        writer = csv.writer(buf_csv)
        writer.writerow(
            [
                "门店", "设备", "管理IP", "命令", "备份时间", "触发方式",
                "IOS版本", "SHA256", "字节数", "版本状态", "差异", "包内路径",
            ]
        )
        writer.writerows(manifest_rows)
        zf.writestr(f"{root}/manifest.csv", "\ufeff" + buf_csv.getvalue())

        if include_readme:
            zf.writestr(
                f"{root}/README.txt",
                _readme(stamp, manifest_rows, device_keys, site_names),
            )

    stats = {
        "files": len(rows),
        "backups": len(backup_keys),
        "devices": len(device_keys),
        "sites": len(site_names),
        "bytes": total_size,
        "diffs": len(diffs) if include_diff else 0,
    }
    return buffer.getvalue(), f"{root}.zip", stats


def _readme(
    stamp: str,
    manifest_rows: list[list[Any]],
    device_keys: set[tuple],
    site_names: set[str],
) -> str:
    lines = [
        "Cisco 配置备份导出包",
        "=" * 46,
        f"导出时间: {stamp}",
        f"门店数  : {len(site_names)}",
        f"设备数  : {len(device_keys)}",
        f"文件数  : {len(manifest_rows)}",
        "",
        "目录结构",
        "-" * 46,
        "  <门店>/<设备>/<备份时间>/<命令>.txt",
        "  <门店>/<设备>/<备份时间>/_diff/<命令>.diff   与上一版本的差异",
        "",
        "清单文件",
        "-" * 46,
        "  manifest.csv  记录每个文件的门店、设备、命令、时间与 SHA256，",
        "                用 Excel 打开不会乱码。",
        "",
        "说明",
        "-" * 46,
        "  * .txt 为设备命令原始输出（已去除命令回显与提示符）",
        "  * .diff 为该版本相对上一版本的 unified diff，+为新增、-为删除",
        "  * 目录时间即备份执行时间，按设备倒序排列，最新版本在最前",
        "",
    ]
    return "\n".join(lines)


def export_scope(
    scope: str = "all",
    site_id: int | None = None,
    device_ids: list[int] | None = None,
    limit_per_device: int | None = None,
    since: str | None = None,
    until: str | None = None,
    commands: list[str] | None = None,
    include_diff: bool = True,
) -> tuple[bytes, str, dict[str, Any]]:
    """按 scope 解析设备范围后打包。

    scope: all（全部设备）/ site（指定门店）/ device（指定设备）
    """
    if scope == "site" and site_id is not None:
        ids = db.site_device_ids(site_id, only_enabled=False)
        prefix = db.get_site(site_id)
        prefix_name = prefix["name"] if prefix else f"site_{site_id}"
    elif scope == "device":
        ids = list(device_ids or [])
        if ids:
            dev = db.get_device(ids[0])
            prefix_name = dev["name"] if dev else "device"
        else:
            prefix_name = "device"
    else:
        ids = [d["id"] for d in db.list_devices()]
        prefix_name = "全部门店"
    return build_zip(
        ids,
        filename_prefix=prefix_name,
        limit_per_device=limit_per_device,
        since=since,
        until=until,
        commands=commands,
        include_diff=include_diff,
    )