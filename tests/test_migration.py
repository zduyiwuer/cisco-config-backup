"""数据库迁移专项测试：验证从旧版库（无 sites 表、无 site_id 列）平滑升级。"""

from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = 0, 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


# 旧版库结构：devices 表没有 site_id，也没有 sites 表
LEGACY_SCHEMA = """
CREATE TABLE devices (
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
    note              TEXT    DEFAULT '',
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL,
    last_backup_at    TEXT,
    last_status       TEXT,
    last_message      TEXT
);
CREATE TABLE backups (
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
CREATE TABLE backup_files (
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
CREATE TABLE backup_diffs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    backup_id  INTEGER NOT NULL REFERENCES backups(id) ON DELETE CASCADE,
    file_id    INTEGER NOT NULL REFERENCES backup_files(id) ON DELETE CASCADE,
    command    TEXT    NOT NULL,
    diff_text  TEXT    NOT NULL,
    added      INTEGER NOT NULL DEFAULT 0,
    removed    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX idx_backups_device ON backups(device_id, id DESC);
CREATE INDEX idx_files_backup  ON backup_files(backup_id);
CREATE INDEX idx_files_lookup  ON backup_files(device_id, command, id DESC);
"""


def build_legacy_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    ts = "2026-10-01T10:00:00+08:00"
    legacy_devices = [
        ("core-sw-01", "10.0.0.1", "上海南京西路店"),
        ("access-sw-02", "10.0.0.2", "上海南京西路店"),
        ("edge-sw-03", "10.0.0.3", "深圳南山科技园店"),
        ("orphan-sw-04", "10.0.0.4", ""),  # 未归属
    ]
    for name, host, site in legacy_devices:
        conn.execute(
            """
            INSERT INTO devices (name, host, port, protocol, username, password_enc,
                                 enable_password_enc, device_type, commands, enabled,
                                 schedule_enabled, cron, site, note, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (name, host, 22, "ssh", "cisco", "ENC", None, "cisco_ios", '["show running-config"]',
             1, 0, "0 2 * * *", site, "", ts, ts),
        )
    # 一条历史备份与文件
    conn.execute(
        "INSERT INTO backups (device_id, trigger, status, started_at, finished_at, changed, host) "
        "VALUES (1,'manual','success',?,?,1,'10.0.0.1')",
        (ts, ts),
    )
    conn.execute(
        "INSERT INTO backup_files (backup_id, device_id, command, slug, content, size, sha256, is_new, created_at) "
        "VALUES (1,1,'show running-config','show_running_config','hostname CORE-SW-01',22,'abc',1,?)",
        (ts,),
    )
    conn.execute("INSERT INTO settings (key,value) VALUES ('concurrency','4')")
    conn.commit()
    conn.close()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cisco_migrate_"))
    db_path = tmp / "legacy.db"
    build_legacy_db(db_path)

    from app import crypto, db

    crypto.DATA_DIR = tmp
    crypto.KEY_PATH = tmp / ".secret_key"
    crypto._key()
    crypto._fernet.__init__(crypto.KEY_PATH.read_bytes())
    db.DATA_DIR = tmp
    db.DB_PATH = db_path
    db._local.__dict__.clear()

    print("\n=== 旧库迁移 ===")
    try:
        db.init_db()
        check("旧库 init_db 不抛异常", True)
    except Exception as exc:  # noqa: BLE001
        check("旧库 init_db 不抛异常", False, f"{type(exc).__name__}: {exc}")
        print(f"\n  通过 {PASS} 项，失败 {FAIL} 项\n")
        return 1

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}

    check("已创建 sites 表", "sites" in tables)
    check("devices 已补 site_id 列", "site_id" in {r[1] for r in conn.execute("PRAGMA table_info(devices)")})
    check("已创建 site 索引", "idx_devices_site" in indexes)
    check("已创建 sites 排序索引", "idx_sites_order" in indexes)

    sites = conn.execute("SELECT * FROM sites ORDER BY name").fetchall()
    check("旧 site 文本已提升为门店实体", len(sites) == 2, f"got {[s['name'] for s in sites]}")
    names = {s["name"] for s in sites}
    check("门店名称正确", names == {"上海南京西路店", "深圳南山科技园店"}, str(names))

    sw = conn.execute("SELECT name, site, site_id FROM devices ORDER BY name").fetchall()
    by_name = {r["name"]: r for r in sw}
    check("历史设备未丢失", len(sw) == 4, f"got {len(sw)}")
    check(
        "同店设备指向同一门店",
        by_name["core-sw-01"]["site_id"] == by_name["access-sw-02"]["site_id"] is not None,
    )
    check(
        "不同门店指向不同实体",
        by_name["core-sw-01"]["site_id"] != by_name["edge-sw-03"]["site_id"],
    )
    check("未归属设备 site_id 为空", by_name["orphan-sw-04"]["site_id"] is None)

    bids = conn.execute("SELECT COUNT(*) c FROM backups").fetchone()["c"]
    fids = conn.execute("SELECT COUNT(*) c FROM backup_files").fetchone()["c"]
    check("历史备份记录保留", bids == 1, str(bids))
    check("历史配置内容保留", fids == 1, str(fids))
    conn.close()

    print("\n=== 迁移后功能可用 ===")
    devices = db.list_devices()
    check("list_devices 正常", len(devices) == 4)
    check("设备带出门店名", devices[0]["site"] != "")
    check("list_sites 含统计", len(db.list_sites()) == 2)
    site_list = db.list_sites()
    sh = next(s for s in site_list if s["name"] == "上海南京西路店")
    check("门店设备数统计正确", sh["device_count"] == 2, str(sh["device_count"]))
    check("门店最近备份时间被统计", sh["last_backup_at"] is not None)
    check("可按门店过滤设备", len(db.list_devices(site_id=sh["id"])) == 2)
    check("可查未归属设备", len(db.list_devices(site_id=0)) == 1)
    check("可按关键字搜索", len(db.list_devices(keyword="edge")) == 1)

    from app import exporter
    content, filename, stats = exporter.export_scope(scope="all")
    check("迁移后可打包导出", content[:2] == b"PK", filename)
    check("导出统计到文件", stats["files"] == 1, str(stats))
    import io
    import zipfile

    zf = zipfile.ZipFile(io.BytesIO(content))
    names_in_zip = zf.namelist()
    check("导出包按门店分层", any("/上海南京西路店/" in n for n in names_in_zip), str(names_in_zip))
    check("导出包含清单", any(n.endswith("manifest.csv") for n in names_in_zip))

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'=' * 46}\n  通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 46}\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())