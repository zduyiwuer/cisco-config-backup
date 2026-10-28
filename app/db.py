"""本地 SQLite 存储层。

设计要点：
- sites 表是门店实体，设备通过 site_id 归属门店（门店级批量备份/打包都基于它）。
- devices 表保存设备清单与连接参数，密码加密后落盘。
- backups 表一次备份任务一条记录。
- backup_files 表保存每条命令的原始输出（show run / cdp ne de / ...），
  只有内容发生变化时才产生新的"版本"，并记录与上一版本的 diff。
- schedule 字段直接存 crontab 表达式，由调度器读取。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .crypto import decrypt, encrypt

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "cisco_backup.db"

DEFAULT_COMMANDS = [
    "show running-config",
    "show cdp neighbors detail",
]

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    code        TEXT    NOT NULL DEFAULT '',
    region      TEXT    NOT NULL DEFAULT '',
    address     TEXT    NOT NULL DEFAULT '',
    note        TEXT    NOT NULL DEFAULT '',
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT    NOT NULL UNIQUE,
    host              TEXT    NOT NULL,
    port              INTEGER NOT NULL,
    protocol          TEXT    NOT NULL DEFAULT 'ssh',
    username          TEXT    NOT NULL,
    password_enc      TEXT    NOT NULL,
    enable_password_enc TEXT,
    device_type       TEXT    NOT NULL DEFAULT 'cisco_ios',
    commands          TEXT    NOT NULL DEFAULT '[]',
    enabled           INTEGER NOT NULL DEFAULT 1,
    schedule_enabled  INTEGER NOT NULL DEFAULT 0,
    cron              TEXT    NOT NULL DEFAULT '0 2 * * *',
    site              TEXT    DEFAULT '',
    site_id           INTEGER REFERENCES sites(id) ON DELETE SET NULL,
    note              TEXT    DEFAULT '',
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL,
    last_backup_at    TEXT,
    last_status       TEXT,
    last_message      TEXT,
    online            INTEGER,
    last_seen_at      TEXT,
    last_probe_at     TEXT,
    probe_latency_ms  INTEGER,
    consecutive_fails INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS backups (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id    INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    trigger      TEXT    NOT NULL DEFAULT 'manual',
    status       TEXT    NOT NULL,
    started_at   TEXT    NOT NULL,
    finished_at  TEXT,
    duration_ms  INTEGER,
    changed      INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    host         TEXT,
    os_version   TEXT
);

CREATE TABLE IF NOT EXISTS backup_files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    backup_id   INTEGER NOT NULL REFERENCES backups(id) ON DELETE CASCADE,
    device_id   INTEGER NOT NULL,
    command     TEXT    NOT NULL,
    slug        TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    size        INTEGER NOT NULL,
    sha256      TEXT    NOT NULL,
    is_new      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS backup_diffs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    backup_id  INTEGER NOT NULL REFERENCES backups(id) ON DELETE CASCADE,
    file_id    INTEGER NOT NULL REFERENCES backup_files(id) ON DELETE CASCADE,
    command    TEXT    NOT NULL,
    diff_text  TEXT    NOT NULL,
    added      INTEGER NOT NULL DEFAULT 0,
    removed    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS users (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    username             TEXT    NOT NULL UNIQUE,
    password_hash        TEXT,
    role                 TEXT    NOT NULL DEFAULT 'viewer',
    source               TEXT    NOT NULL DEFAULT 'local',
    display_name         TEXT    NOT NULL DEFAULT '',
    email                TEXT    NOT NULL DEFAULT '',
    ad_dn                TEXT    NOT NULL DEFAULT '',
    status               TEXT    NOT NULL DEFAULT 'active',
    must_change_password INTEGER NOT NULL DEFAULT 0,
    failed_attempts      INTEGER NOT NULL DEFAULT 0,
    locked_until         TEXT,
    last_login_at        TEXT,
    created_at           TEXT    NOT NULL,
    updated_at           TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT    NOT NULL UNIQUE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT    NOT NULL,
    expires_at TEXT    NOT NULL,
    ip         TEXT    DEFAULT '',
    user_agent TEXT    DEFAULT ''
);

CREATE TABLE IF NOT EXISTS login_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT    NOT NULL,
    success  INTEGER NOT NULL,
    source   TEXT    NOT NULL DEFAULT 'local',
    ip       TEXT    DEFAULT '',
    message  TEXT    DEFAULT '',
    at       TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS probe_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    at          TEXT    NOT NULL,
    online      INTEGER NOT NULL,
    latency_ms  INTEGER,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_backups_device ON backups(device_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_files_backup  ON backup_files(backup_id);
CREATE INDEX IF NOT EXISTS idx_files_lookup  ON backup_files(device_id, command, id DESC);
CREATE INDEX IF NOT EXISTS idx_devices_site   ON devices(site_id);
CREATE INDEX IF NOT EXISTS idx_sites_order    ON sites(sort_order, name);
CREATE INDEX IF NOT EXISTS idx_probe_device   ON probe_log(device_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_user  ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_logintime      ON login_log(id DESC);
"""

DEFAULT_SETTINGS: dict[str, Any] = {
    "default_commands": DEFAULT_COMMANDS,
    "concurrency": 4,
    "connect_timeout": 15,
    "command_timeout": 45,
    "keep_versions": 50,
    "paging_fix": True,
    # 在线探测
    "probe_enabled": True,
    "probe_interval_minutes": 5,
    "probe_timeout": 5,
    "probe_workers": 16,
    # 认证
    "auth_enabled": True,
    "session_hours": 12,
    "allow_password_login": True,
    # Active Directory / LDAP
    "ad_enabled": False,
    "ad_host": "",
    "ad_port": 636,
    "ad_use_ssl": True,
    "ad_domain": "",
    "ad_base_dn": "",
    "ad_bind_dn": "",
    "ad_bind_password": "",
    "ad_auth_strategy": "search",
    "ad_use_ntlm": False,
    "ad_admin_group": "",
    "ad_default_role": "operator",
    "ad_allow_plaintext": False,
    "ad_ca_cert": "",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_conn() -> sqlite3.Connection:
    """每线程一个连接，避免 sqlite3 跨线程使用限制。"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _conn()
        _local.conn = conn
    return conn


@contextmanager
def tx():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate(conn: sqlite3.Connection) -> None:
    """老库平滑升级：devices 补 site_id 列与在线探测相关列。"""
    cols = _table_columns(conn, "devices")
    if "site_id" not in cols:
        conn.execute("ALTER TABLE devices ADD COLUMN site_id INTEGER")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_devices_site ON devices(site_id)"
        )
    # 在线探测字段
    probe_columns = {
        "online": "INTEGER",
        "last_seen_at": "TEXT",
        "last_probe_at": "TEXT",
        "probe_latency_ms": "INTEGER",
        "consecutive_fails": "INTEGER NOT NULL DEFAULT 0",
    }
    for column, ddl in probe_columns.items():
        if column not in _table_columns(conn, "devices"):
            conn.execute(f"ALTER TABLE devices ADD COLUMN {column} {ddl}")


def _backfill_sites(conn: sqlite3.Connection) -> None:
    """把devices.site 里已有的门店文本提升为 sites 表中的实体记录。"""
    rows = conn.execute(
        "SELECT DISTINCT site FROM devices WHERE site IS NOT NULL AND TRIM(site) != ''"
    ).fetchall()
    ts = now_iso()
    for row in rows:
        name = (row["site"] or "").strip()
        if not name:
            continue
        existing = conn.execute("SELECT id FROM sites WHERE name = ?", (name,)).fetchone()
        if existing:
            site_id = existing["id"]
        else:
            cur = conn.execute(
                "INSERT INTO sites (name, sort_order, created_at, updated_at) VALUES (?,?,?,?)",
                (name, 100, ts, ts),
            )
            site_id = int(cur.lastrowid)
        conn.execute(
            "UPDATE devices SET site_id = ? WHERE TRIM(site) = ? AND site_id IS NULL",
            (site_id, name),
        )


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    # 先建表（不含索引），再补列，最后建索引：
    # 旧库里 devices 没有 site_id 列，直接执行带该列的索引会报错。
    base_schema = SCHEMA.split("CREATE INDEX")[0]
    conn.executescript(base_schema)
    _migrate(conn)
    conn.executescript(
        "CREATE INDEX" + "CREATE INDEX".join(SCHEMA.split("CREATE INDEX")[1:])
    )
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
            (key, json.dumps(value, ensure_ascii=False)),
        )
    conn.commit()
    _backfill_sites(conn)
    conn.commit()


# --------------------------------------------------------------------------- settings
def get_settings() -> dict[str, Any]:
    conn = get_conn()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    result = dict(DEFAULT_SETTINGS)
    for row in rows:
        try:
            result[row["key"]] = json.loads(row["value"])
        except json.JSONDecodeError:
            result[row["key"]] = row["value"]
    # AD 服务账号密码解密后供ad.py 使用
    if result.get("ad_bind_password"):
        result["ad_bind_password"] = decrypt(result["ad_bind_password"])
    return result


def get_public_settings() -> dict[str, Any]:
    """对外暴露的设置：敏感字段脱敏，绝不返回密码。"""
    result = get_settings()
    if result.get("ad_bind_password"):
        result["ad_bind_password"] = ""
        result["ad_bind_password_set"] = True
    else:
        result["ad_bind_password_set"] = False
    return result


def update_settings(patch: dict[str, Any]) -> dict[str, Any]:
    """更新设置。AD 服务账号密码加密存储；留空表示不修改。"""
    with tx() as conn:
        for key, value in patch.items():
            if key not in DEFAULT_SETTINGS:
                continue
            if key == "ad_bind_password":
                if not value:
                    continue  # 留空则保持原值
                stored = encrypt(str(value))
            else:
                stored = json.dumps(value, ensure_ascii=False)
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, stored),
            )
    return get_public_settings()


# --------------------------------------------------------------------------- sites
def _row_to_site(row: sqlite3.Row) -> dict[str, Any]:
    site = {
        "id": row["id"],
        "name": row["name"],
        "code": row["code"] or "",
        "region": row["region"] or "",
        "address": row["address"] or "",
        "note": row["note"] or "",
        "sort_order": row["sort_order"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    # list_sites 会附带聚合列，单条查询时不存在
    for key in ("device_count", "ok_count", "failed_count", "last_backup_at"):
        if key in row.keys():
            site[key] = row[key]
    return site


def list_sites() -> list[dict[str, Any]]:
    """门店列表，附带设备数量与最近备份概况。"""
    rows = get_conn().execute(
        """
        SELECT s.*,
               (SELECT COUNT(*) FROM devices d WHERE d.site_id = s.id) AS device_count,
               (SELECT COUNT(*) FROM devices d
                 WHERE d.site_id = s.id AND d.last_status = 'success') AS ok_count,
               (SELECT COUNT(*) FROM devices d
                 WHERE d.site_id = s.id AND d.last_status = 'failed') AS failed_count,
               (SELECT MAX(b.started_at) FROM backups b
                 JOIN devices d2 ON d2.id = b.device_id
                WHERE d2.site_id = s.id AND b.status = 'success') AS last_backup_at
        FROM sites s
        ORDER BY s.sort_order, s.name
        """
    ).fetchall()
    return [_row_to_site(r) for r in rows]


def get_site(site_id: int) -> dict[str, Any] | None:
    row = get_conn().execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()
    return _row_to_site(row) if row else None


def get_site_by_name(name: str) -> dict[str, Any] | None:
    row = get_conn().execute("SELECT * FROM sites WHERE name = ?", (name.strip(),)).fetchone()
    return _row_to_site(row) if row else None


def create_site(payload: dict[str, Any]) -> dict[str, Any]:
    ts = now_iso()
    name = payload["name"].strip()
    with tx() as conn:
        if conn.execute("SELECT 1 FROM sites WHERE name = ?", (name,)).fetchone():
            raise ValueError(f"门店「{name}」已存在")
        cur = conn.execute(
            """
            INSERT INTO sites (name, code, region, address, note, sort_order, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                name,
                payload.get("code", ""),
                payload.get("region", ""),
                payload.get("address", ""),
                payload.get("note", ""),
                int(payload.get("sort_order") or 100),
                ts,
                ts,
            ),
        )
        site_id = int(cur.lastrowid)
    return get_site(site_id)  # type: ignore[return-value]


def update_site(site_id: int, payload: dict[str, Any]) -> dict[str, Any] | None:
    existing = get_conn().execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()
    if not existing:
        return None
    fields, values = [], []
    for key in ("name", "code", "region", "address", "note"):
        if key in payload:
            fields.append(f"{key} = ?")
            values.append(payload[key])
    if "sort_order" in payload:
        fields.append("sort_order = ?")
        values.append(int(payload["sort_order"]))
    if fields:
        fields.append("updated_at = ?")
        values.extend([now_iso(), site_id])
        with tx() as conn:
            conn.execute(f"UPDATE sites SET {', '.join(fields)} WHERE id = ?", values)
    return get_site(site_id)


def delete_site(site_id: int, reassign_to: int | None = None) -> int:
    """删除门店。reassign_to 指定时把其下设备迁移到目标门店，否则设备变为未归属。"""
    with tx() as conn:
        if reassign_to:
            moved = conn.execute(
                "UPDATE devices SET site_id = ? WHERE site_id = ?", (reassign_to, site_id)
            ).rowcount
        else:
            moved = conn.execute(
                "UPDATE devices SET site_id = NULL WHERE site_id = ?", (site_id,)
            ).rowcount
        conn.execute("DELETE FROM sites WHERE id = ?", (site_id,))
    return moved


def site_device_ids(site_id: int, only_enabled: bool = True) -> list[int]:
    sql = "SELECT id FROM devices WHERE site_id = ?"
    if only_enabled:
        sql += " AND enabled = 1"
    sql += " ORDER BY name"
    return [r["id"] for r in get_conn().execute(sql, (site_id,)).fetchall()]


def unassigned_device_ids() -> list[int]:
    rows = get_conn().execute(
        "SELECT id FROM devices WHERE site_id IS NULL AND enabled = 1 ORDER BY name"
    ).fetchall()
    return [r["id"] for r in rows]


# --------------------------------------------------------------------------- devices
def _row_to_device(row: sqlite3.Row, reveal: bool = False) -> dict[str, Any]:
    commands = json.loads(row["commands"] or "[]")
    cols = row.keys()
    device = {
        "id": row["id"],
        "name": row["name"],
        "host": row["host"],
        "port": row["port"],
        "protocol": row["protocol"],
        "username": row["username"],
        "password": decrypt(row["password_enc"]) if reveal else "",
        "enable_password": (
            decrypt(row["enable_password_enc"])
            if reveal and row["enable_password_enc"]
            else ""
        ),
        "device_type": row["device_type"],
        "commands": commands,
        "enabled": bool(row["enabled"]),
        "schedule_enabled": bool(row["schedule_enabled"]),
        "cron": row["cron"],
        "site_id": row["site_id"] if "site_id" in cols else None,
        "site": row["site"] or "",
        "note": row["note"] or "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_backup_at": row["last_backup_at"],
        "last_status": row["last_status"],
        "last_message": row["last_message"],
    }
    for key in ("online", "last_seen_at", "last_probe_at", "probe_latency_ms", "consecutive_fails"):
        if key in cols:
            device[key] = row[key]
    return device


def list_devices(site_id: int | None = None, keyword: str | None = None) -> list[dict[str, Any]]:
    """设备列表，可按门店与关键字过滤。site_id 传 0 表示只看未归属设备。"""
    sql = "SELECT * FROM devices"
    clauses, params = [], []
    if site_id is not None:
        if site_id == 0:
            clauses.append("site_id IS NULL")
        else:
            clauses.append("site_id = ?")
            params.append(site_id)
    if keyword:
        clauses.append("(name LIKE ? OR host LIKE ? OR site LIKE ? OR note LIKE ?)")
        like = f"%{keyword}%"
        params.extend([like, like, like, like])
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY name"
    rows = get_conn().execute(sql, params).fetchall()
    return [_row_to_device(r) for r in rows]


def get_device(device_id: int, reveal: bool = False) -> dict[str, Any] | None:
    row = get_conn().execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    return _row_to_device(row, reveal=reveal) if row else None


def get_device_by_name(name: str) -> dict[str, Any] | None:
    row = get_conn().execute("SELECT * FROM devices WHERE name = ?", (name,)).fetchone()
    return _row_to_device(row, reveal=True) if row else None


def _resolve_site(conn: sqlite3.Connection, payload: dict[str, Any]) -> tuple[int | None, str]:
    """确定设备的门店归属，返回 (site_id, site 文本)。

    - 传了 site_id：取该门店名，并同步 devices.site 文本（冗余便于导出与搜索）。
    - 只传了 site 文本：自动创建或复用同名门店。
    """
    site_id = payload.get("site_id")
    if site_id in ("", None):
        site_name = (payload.get("site") or "").strip()
        if not site_name:
            return None, ""
        row = conn.execute("SELECT id, name FROM sites WHERE name = ?", (site_name,)).fetchone()
        if row:
            return int(row["id"]), row["name"]
        ts = now_iso()
        cur = conn.execute(
            "INSERT INTO sites (name, sort_order, created_at, updated_at) VALUES (?,?,?,?)",
            (site_name, 100, ts, ts),
        )
        return int(cur.lastrowid), site_name

    site_id = int(site_id)
    row = conn.execute("SELECT id, name FROM sites WHERE id = ?", (site_id,)).fetchone()
    if not row:
        raise ValueError(f"门店 id={site_id} 不存在")
    return int(row["id"]), row["name"]


def create_device(payload: dict[str, Any]) -> dict[str, Any]:
    ts = now_iso()
    with tx() as conn:
        site_id, site_name = _resolve_site(conn, payload)
        cur = conn.execute(
            """
            INSERT INTO devices (name, host, port, protocol, username, password_enc,
                                 enable_password_enc, device_type, commands, enabled,
                                 schedule_enabled, cron, site, site_id, note,
                                 created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                payload["name"],
                payload["host"],
                int(payload.get("port") or (23 if payload["protocol"] == "telnet" else 22)),
                payload["protocol"],
                payload.get("username", ""),
                encrypt(payload.get("password", "")),
                encrypt(payload["enable_password"]) if payload.get("enable_password") else None,
                payload.get("device_type", "cisco_ios"),
                json.dumps(payload.get("commands") or DEFAULT_COMMANDS, ensure_ascii=False),
                1 if payload.get("enabled", True) else 0,
                1 if payload.get("schedule_enabled") else 0,
                payload.get("cron", "0 2 * * *"),
                site_name,
                site_id,
                payload.get("note", ""),
                ts,
                ts,
            ),
        )
        device_id = int(cur.lastrowid)
    return get_device(device_id)  # type: ignore[return-value]


def update_device(device_id: int, payload: dict[str, Any]) -> dict[str, Any] | None:
    existing = get_conn().execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    if not existing:
        return None

    fields: list[str] = []
    values: list[Any] = []
    simple = ("name", "host", "username", "cron", "note")
    for key in simple:
        if key in payload:
            fields.append(f"{key} = ?")
            values.append(payload[key])
    if "site" in payload or "site_id" in payload:
        site_id, site_name = _resolve_site(get_conn(), payload)
        fields.extend(["site_id = ?", "site = ?"])
        values.extend([site_id, site_name])
    # 厂商变更时：若设备用的还是旧厂商的默认命令集，则同步为新厂商的命令集
    if "device_type" in payload:
        from . import vendors

        old_vendor = vendors.normalize_vendor(existing["device_type"])
        new_vendor = vendors.normalize_vendor(payload["device_type"])
        if new_vendor != old_vendor:
            fields.append("device_type = ?")
            values.append(new_vendor)
            old_default = vendors.default_commands_for(old_vendor)
            current_cmds = json.loads(existing["commands"] or "[]")
            # 只在命令仍等于旧默认（或为空）时才替换，避免覆盖用户自定义
            if not current_cmds or current_cmds == old_default:
                fields.append("commands = ?")
                values.append(
                    json.dumps(vendors.default_commands_for(new_vendor), ensure_ascii=False)
                )
    if "port" in payload:
        fields.append("port = ?")
        values.append(int(payload["port"]))
    if "protocol" in payload:
        fields.append("protocol = ?")
        values.append(payload["protocol"])
    if "commands" in payload:
        fields.append("commands = ?")
        values.append(json.dumps(payload["commands"], ensure_ascii=False))
    for flag in ("enabled", "schedule_enabled"):
        if flag in payload:
            fields.append(f"{flag} = ?")
            values.append(1 if payload[flag] else 0)
    if payload.get("password"):
        fields.append("password_enc = ?")
        values.append(encrypt(payload["password"]))
    if "enable_password" in payload:
        value = payload["enable_password"]
        fields.append("enable_password_enc = ?")
        values.append(encrypt(value) if value else None)

    if fields:
        fields.append("updated_at = ?")
        values.extend([now_iso(), device_id])
        with tx() as conn:
            conn.execute(f"UPDATE devices SET {', '.join(fields)} WHERE id = ?", values)
    return get_device(device_id)


def delete_device(device_id: int) -> None:
    with tx() as conn:
        conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))


def set_device_result(device_id: int, status: str, message: str) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE devices SET last_backup_at = ?, last_status = ?, last_message = ? WHERE id = ?",
            (now_iso(), status, message[:500], device_id),
        )


# --------------------------------------------------------------------------- 探测
def record_probe(
    device_id: int, online: bool, latency_ms: int | None, error: str | None
) -> None:
    """记录一次探测结果，并更新设备的在线状态。

    连续失败次数用于判断"疑似掉线"，避免网络抖动导致状态来回跳。
    """
    ts = now_iso()
    with tx() as conn:
        conn.execute(
            "INSERT INTO probe_log (device_id, at, online, latency_ms, error) VALUES (?,?,?,?,?)",
            (device_id, ts, 1 if online else 0, latency_ms, (error or "")[:300] or None),
        )
        if online:
            conn.execute(
                "UPDATE devices SET online = 1, last_seen_at = ?, last_probe_at = ?, "
                "probe_latency_ms = ?, consecutive_fails = 0 WHERE id = ?",
                (ts, ts, latency_ms, device_id),
            )
        else:
            conn.execute(
                "UPDATE devices SET online = 0, last_probe_at = ?, consecutive_fails = "
                "consecutive_fails + 1 WHERE id = ?",
                (ts, device_id),
            )
        # 控制日志体积：每台设备只留最近 200 条
        conn.execute(
            """
            DELETE FROM probe_log WHERE device_id = ? AND id NOT IN (
                SELECT id FROM probe_log WHERE device_id = ? ORDER BY id DESC LIMIT 200
            )
            """,
            (device_id, device_id),
        )


def probe_history(device_id: int, limit: int = 50) -> list[dict[str, Any]]:
    rows = get_conn().execute(
        "SELECT * FROM probe_log WHERE device_id = ? ORDER BY id DESC LIMIT ?",
        (device_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def online_overview() -> dict[str, Any]:
    """全局在线概况，供仪表盘与监控使用。"""
    row = get_conn().execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN online = 1 THEN 1 ELSE 0 END) AS online,
               SUM(CASE WHEN online = 0 THEN 1 ELSE 0 END) AS offline,
               SUM(CASE WHEN online IS NULL THEN 1 ELSE 0 END) AS unknown
        FROM devices
        """
    ).fetchone()
    offline_list = get_conn().execute(
        """
        SELECT id, name, host, site, last_seen_at, consecutive_fails, last_message
        FROM devices WHERE online = 0 ORDER BY name
        """
    ).fetchall()
    return {
        "total": row["total"] or 0,
        "online": row["online"] or 0,
        "offline": row["offline"] or 0,
        "unknown": row["unknown"] or 0,
        "offline_devices": [dict(r) for r in offline_list],
    }


# --------------------------------------------------------------------------- backups
def create_backup(device_id: int, trigger: str, host: str) -> int:
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO backups (device_id, trigger, status, started_at, host) "
            "VALUES (?,?,'running',?,?)",
            (device_id, trigger, now_iso(), host),
        )
        return int(cur.lastrowid)


def previous_file(device_id: int, command: str, before_backup_id: int) -> sqlite3.Row | None:
    return get_conn().execute(
        """
        SELECT * FROM backup_files
        WHERE device_id = ? AND command = ? AND backup_id < ?
        ORDER BY backup_id DESC LIMIT 1
        """,
        (device_id, command, before_backup_id),
    ).fetchone()


def add_file(
    backup_id: int,
    device_id: int,
    command: str,
    slug: str,
    content: str,
    sha256: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """写入一份命令输出，返回 (新文件, diff信息)。内容相同则不产生新版本。"""
    data = content.encode("utf-8", errors="replace")
    with tx() as conn:
        prev = conn.execute(
            """
            SELECT * FROM backup_files
            WHERE device_id = ? AND command = ? AND backup_id < ?
            ORDER BY backup_id DESC LIMIT 1
            """,
            (device_id, command, backup_id),
        ).fetchone()

        is_new = 1
        diff_meta: dict[str, Any] | None = None
        if prev and prev["sha256"] == sha256:
            is_new = 0

        cur = conn.execute(
            """
            INSERT INTO backup_files (backup_id, device_id, command, slug, content, size,
                                      sha256, is_new, created_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (backup_id, device_id, command, slug, content, len(data), sha256, is_new, now_iso()),
        )
        file_id = int(cur.lastrowid)

        if prev and is_new == 1:
            from .diffing import make_diff

            diff_text, added, removed = make_diff(prev["content"], content)
            conn.execute(
                "INSERT INTO backup_diffs (backup_id, file_id, command, diff_text, added, removed) "
                "VALUES (?,?,?,?,?,?)",
                (backup_id, file_id, command, diff_text, added, removed),
            )
            diff_meta = {"added": added, "removed": removed, "previous_file_id": prev["id"]}

        row = conn.execute("SELECT * FROM backup_files WHERE id = ?", (file_id,)).fetchone()
        result = {
            "id": file_id,
            "backup_id": backup_id,
            "command": command,
            "slug": slug,
            "size": len(data),
            "sha256": sha256,
            "is_new": bool(is_new),
            "created_at": row["created_at"],
        }
    return result, diff_meta


def finish_backup(
    backup_id: int,
    status: str,
    error: str | None = None,
    changed: bool = False,
    duration_ms: int = 0,
    os_version: str | None = None,
) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE backups SET status=?, finished_at=?, duration_ms=?, changed=?, error=?, "
            "os_version=COALESCE(?, os_version) WHERE id = ?",
            (status, now_iso(), duration_ms, 1 if changed else 0, error, os_version, backup_id),
        )


def list_backups(
    device_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
    status: str | None = None,
    trigger: str | None = None,
    changed: str | None = None,
    site_id: int | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[dict[str, Any]]:
    """备份历史查询。

    status: success / failed / running
    changed: 1（有变化）/ 0（无变化）
    site_id: 传 0 表示未归属门店的设备
    """
    conn = get_conn()
    sql = (
        "SELECT b.*, d.name AS device_name, d.host AS device_host, d.site, d.site_id, "
        "s.name AS site_name "
        "FROM backups b JOIN devices d ON d.id = b.device_id "
        "LEFT JOIN sites s ON s.id = d.site_id"
    )
    clauses: list[str] = []
    params: list[Any] = []

    if device_id:
        clauses.append("b.device_id = ?")
        params.append(device_id)
    if site_id is not None:
        if site_id == 0:
            clauses.append("d.site_id IS NULL")
        else:
            clauses.append("d.site_id = ?")
            params.append(site_id)
    if status:
        clauses.append("b.status = ?")
        params.append(status)
    if trigger:
        clauses.append("b.trigger = ?")
        params.append(trigger)
    if changed in ("0", "1"):
        # 只对成功的备份做变化筛选：失败的备份没有"配置是否变化"的概念
        clauses.append("b.changed = ? AND b.status = 'success'")
        params.append(int(changed))
    if since:
        clauses.append("b.started_at >= ?")
        params.append(since)
    if until:
        clauses.append("b.started_at <= ?")
        params.append(until)

    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY b.id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def backup_stats(
    device_id: int | None = None, site_id: int | None = None
) -> dict[str, Any]:
    """备份统计，用于历史页顶部概览与失败汇总。"""
    conn = get_conn()
    sql = (
        "SELECT b.status, COUNT(*) AS c FROM backups b "
        "JOIN devices d ON d.id = b.device_id LEFT JOIN sites s ON s.id = d.site_id"
    )
    clauses, params = [], []
    if device_id:
        clauses.append("b.device_id = ?")
        params.append(device_id)
    if site_id is not None:
        if site_id == 0:
            clauses.append("d.site_id IS NULL")
        else:
            clauses.append("d.site_id = ?")
            params.append(site_id)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " GROUP BY b.status"

    counts = {"success": 0, "failed": 0, "running": 0}
    for row in conn.execute(sql, params).fetchall():
        counts[row["status"]] = row["c"]

    # 失败原因 Top 5，便于快速定位共性问题（如密码错误、认证失败）
    fail_sql = (
        "SELECT b.error, COUNT(*) AS c FROM backups b "
        "JOIN devices d ON d.id = b.device_id LEFT JOIN sites s ON s.id = d.site_id"
        + (" WHERE " + " AND ".join(clauses + ["b.status = 'failed'"])
           if clauses else " WHERE b.status = 'failed'")
        + " AND b.error IS NOT NULL AND TRIM(b.error) != '' "
        "GROUP BY b.error ORDER BY c DESC LIMIT 5"
    )
    reasons = [
        {"error": r["error"], "count": r["c"]}
        for r in conn.execute(fail_sql, params).fetchall()
    ]

    # 连续失败的设备（最近 3 次里有失败），优先排查对象
    device_sql = """
        SELECT d.id, d.name, d.host, COUNT(*) AS failed_count
        FROM backups b JOIN devices d ON d.id = b.device_id
        WHERE b.status = 'failed'
        GROUP BY d.id
        HAVING failed_count > 0
        ORDER BY failed_count DESC, d.name
        LIMIT 20
    """
    failing = [dict(r) for r in conn.execute(device_sql).fetchall()]

    return {
        "total": sum(counts.values()),
        **counts,
        "failure_reasons": reasons,
        "failing_devices": failing,
    }


def delete_backup(backup_id: int) -> bool:
    """删除一条备份记录及其关联的文件、差异。

    backup_files / backup_diffs 都是 ON DELETE CASCADE，
    外键开启时会自动级联清理，这里只负责 backups 主记录。
    返回是否真的删掉了。
    """
    with tx() as conn:
        cur = conn.execute("DELETE FROM backups WHERE id = ?", (backup_id,))
        return cur.rowcount > 0


def delete_failed_backups(
    device_id: int | None = None, before: str | None = None
) -> int:
    """批量删除失败的备份记录。

    只删status='failed' 的，成功与进行中的记录一律保留。
    device_id 传 None 表示所有设备；before 传时间则只删该时间之前的。
    返回删除的条数。
    """
    clauses = ["status = 'failed'"]
    params: list[Any] = []
    if device_id is not None:
        clauses.append("device_id = ?")
        params.append(device_id)
    if before:
        clauses.append("started_at < ?")
        params.append(before)
    with tx() as conn:
        cur = conn.execute(
            f"DELETE FROM backups WHERE {' AND '.join(clauses)}", params
        )
        return cur.rowcount


def get_backup(backup_id: int) -> dict[str, Any] | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT b.*, d.name AS device_name FROM backups b JOIN devices d ON d.id=b.device_id "
        "WHERE b.id = ?",
        (backup_id,),
    ).fetchone()
    if not row:
        return None
    backup = dict(row)
    files = conn.execute(
        "SELECT id, command, slug, size, sha256, is_new, created_at FROM backup_files "
        "WHERE backup_id = ? ORDER BY id",
        (backup_id,),
    ).fetchall()
    diffs = {
        d["file_id"]: dict(d) for d in conn.execute(
            "SELECT * FROM backup_diffs WHERE backup_id = ?", (backup_id,)
        ).fetchall()
    }
    items = []
    for f in files:
        item = dict(f)
        diff = diffs.get(f["id"])
        item["diff"] = (
            {"added": diff["added"], "removed": diff["removed"]} if diff else None
        )
        items.append(item)
    backup["files"] = items
    backup["changed"] = bool(backup["changed"])
    return backup


def get_file_content(file_id: int) -> dict[str, Any] | None:
    row = get_conn().execute("SELECT * FROM backup_files WHERE id = ?", (file_id,)).fetchone()
    return dict(row) if row else None


def get_diff(file_id: int) -> dict[str, Any] | None:
    row = get_conn().execute("SELECT * FROM backup_diffs WHERE file_id = ?", (file_id,)).fetchone()
    return dict(row) if row else None


def device_backup_summary(device_id: int) -> dict[str, Any]:
    conn = get_conn()
    total = conn.execute(
        "SELECT COUNT(*) AS c FROM backups WHERE device_id = ?", (device_id,)
    ).fetchone()["c"]
    ok = conn.execute(
        "SELECT COUNT(*) AS c FROM backups WHERE device_id = ? AND status='success'",
        (device_id,),
    ).fetchone()["c"]
    failed = conn.execute(
        "SELECT COUNT(*) AS c FROM backups WHERE device_id = ? AND status='failed'",
        (device_id,),
    ).fetchone()["c"]
    changed = conn.execute(
        "SELECT COUNT(*) AS c FROM backups WHERE device_id = ? "
        "AND status='success' AND changed=1",
        (device_id,),
    ).fetchone()["c"]
    latest = conn.execute(
        "SELECT id FROM backups WHERE device_id = ? AND status='success' ORDER BY id DESC LIMIT 1",
        (device_id,),
    ).fetchone()
    # 最近一次失败时间，供「最近备份失败」筛选与列表展示
    last_failed = conn.execute(
        "SELECT started_at FROM backups WHERE device_id = ? AND status='failed' "
        "ORDER BY id DESC LIMIT 1",
        (device_id,),
    ).fetchone()
    return {
        "total": total,
        "success": ok,
        "failed": failed,
        "changed": changed,
        "latest_backup_id": latest["id"] if latest else None,
        "last_failed_at": last_failed["started_at"] if last_failed else None,
    }


def latest_config(device_id: int, command: str) -> sqlite3.Row | None:
    return get_conn().execute(
        """
        SELECT f.* FROM backup_files f
        JOIN backups b ON b.id = f.backup_id
        WHERE f.device_id = ? AND f.command = ? AND b.status = 'success'
        ORDER BY f.id DESC LIMIT 1
        """,
        (device_id, command),
    ).fetchone()


def prune_versions(device_id: int, keep: int) -> int:
    """仅保留最近 keep 个成功版本中"发生过变化"的文件内容。"""
    if keep <= 0:
        return 0
    conn = get_conn()
    ids = [
        r["backup_id"]
        for r in conn.execute(
            """
            SELECT DISTINCT backup_id FROM backup_files
            WHERE device_id = ? AND is_new = 1 ORDER BY backup_id DESC
            """,
            (device_id,),
        ).fetchall()
    ][keep:]
    removed = 0
    with tx() as c:
        for backup_id in ids:
            removed += c.execute(
                "DELETE FROM backup_files WHERE backup_id = ?", (backup_id,)
            ).rowcount
    return removed


def bulk_fetch(where: str, params: Iterable[Any]) -> list[dict[str, Any]]:
    rows = get_conn().execute(
        f"SELECT * FROM backup_files WHERE {where} ORDER BY id DESC LIMIT 5000", tuple(params)
    ).fetchall()
    return [dict(r) for r in rows]