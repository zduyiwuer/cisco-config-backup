"""删除失败备份记录测试。

覆盖单条删除、批量删除、级联清理、权限与保护性约束。
"""

from __future__ import annotations

import shutil
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


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cisco_del_"))
    from app import crypto, db

    crypto.DATA_DIR = tmp
    crypto.KEY_PATH = tmp / ".secret_key"
    crypto._key()
    crypto._fernet.__init__(crypto.KEY_PATH.read_bytes())
    db.DATA_DIR = tmp
    db.DB_PATH = tmp / "t.db"
    db._local.__dict__.clear()
    db.init_db()
    db.update_settings({"auth_enabled": False, "probe_enabled": False})

    d1 = db.create_device({"name": "SW-01", "host": "10.0.0.1", "protocol": "ssh",
                           "username": "cisco", "password": "pw"})
    d2 = db.create_device({"name": "SW-02", "host": "10.0.0.2", "protocol": "ssh",
                           "username": "cisco", "password": "pw"})

    def make_backup(device, status, error=None, content=None):
        # create_backup 直接返回 backup id
        bid = db.create_backup(device["id"], "manual", device["host"])
        if content:
            import hashlib
            db.add_file(bid, device["id"], "show running-config", "cfg",
                        content, hashlib.sha256(content.encode()).hexdigest())
        db.finish_backup(bid, status, error=error, changed=(status == "success"),
                         duration_ms=100)
        return bid

    print("\n=== 1. 单条删除 ===")
    ok_id = make_backup(d1, "success", content="hostname SW-01")
    fail_id = make_backup(d1, "failed", error="SSH 认证失败")
    check("删除前记录存在", db.get_backup(fail_id) is not None)

    from app import db as dbm
    ok = dbm.delete_backup(fail_id)
    check("delete_backup 返回 True", ok is True)
    check("记录已删除", db.get_backup(fail_id) is None)
    check("成功记录不受影响", db.get_backup(ok_id) is not None)

    ok2 = dbm.delete_backup(999999)
    check("删除不存在的记录返回 False", ok2 is False)

    print("\n=== 2. 级联清理文件 ===")
    # 失败备份也可能已写入部分文件
    fail_with_file = make_backup(d1, "failed", error="采集中断", content="partial cfg")
    files_before = db.get_backup(fail_with_file)["files"]
    check("失败备份里有文件记录", len(files_before) >= 1, str(len(files_before)))
    dbm.delete_backup(fail_with_file)
    check("备份记录已删除", db.get_backup(fail_with_file) is None)
    remaining = db.get_conn().execute(
        "SELECT COUNT(*) AS c FROM backup_files WHERE backup_id = ?",
        (fail_with_file,),
    ).fetchone()["c"]
    check("关联文件已级联删除", remaining == 0, str(remaining))

    print("\n=== 3. 批量删除失败记录 ===")
    # d1: 2 条成功 3 条失败；d2: 1 条成功 1 条失败
    s1 = make_backup(d1, "success")
    f1 = make_backup(d1, "failed", error="超时")
    f2 = make_backup(d1, "failed", error="超时")
    f3 = make_backup(d1, "failed", error="认证失败")
    s2 = make_backup(d2, "success")
    f4 = make_backup(d2, "failed", error="不可达")

    n = dbm.delete_failed_backups()
    check("批量删除全部失败记录", n == 4, str(n))
    check("成功记录全部保留",
          db.get_backup(s1) is not None and db.get_backup(s2) is not None)
    for bid in (f1, f2, f3, f4):
        check(f"失败记录 {bid} 已删除", db.get_backup(bid) is None)

    print("\n=== 4. 按设备限定范围 ===")
    make_backup(d1, "failed", error="A")
    make_backup(d2, "failed", error="B")
    n = dbm.delete_failed_backups(device_id=d1["id"])
    check("只删指定设备的失败记录", n == 1, str(n))
    check("其他设备的失败记录还在",
          db.get_conn().execute(
              "SELECT COUNT(*) AS c FROM backups WHERE device_id = ? AND status='failed'",
              (d2["id"],)).fetchone()["c"] == 1)

    print("\n=== 5. 按时间限定范围 ===")
    make_backup(d1, "failed", error="C")
    before_count = db.get_conn().execute(
        "SELECT COUNT(*) AS c FROM backups WHERE status='failed'").fetchone()["c"]
    check("造数后有失败记录", before_count >= 1, str(before_count))

    # 先验证「早于某时间才删」：所有记录都晚于 2000 年，应一条都不删
    n = dbm.delete_failed_backups(before="2000-01-01T00:00:00")
    check("before 之前的都不删", n == 0, str(n))
    check("失败记录仍在",
          db.get_conn().execute(
              "SELECT COUNT(*) AS c FROM backups WHERE status='failed'").fetchone()["c"]
          == before_count)

    # 再验证「早于某时间全删」：2999 年晚于所有记录
    n = dbm.delete_failed_backups(before="2999-01-01T00:00:00")
    check("before 之后的全删", n == before_count, f"{n} vs {before_count}")
    check("失败记录已清空",
          db.get_conn().execute(
              "SELECT COUNT(*) AS c FROM backups WHERE status='failed'").fetchone()["c"] == 0)

    print("\n=== 6. 不误删进行中的记录 ===")
    running = db.create_backup(d1["id"], "manual", d1["host"])
    n = dbm.delete_failed_backups()
    check("批量删除不碰 running 记录", db.get_backup(running) is not None)

    print("\n=== 7. API ===")
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        fail_bid = make_backup(d1, "failed", error="API 测试失败")
        ok_bid = make_backup(d1, "success")

        r = client.delete(f"/api/backups/{fail_bid}")
        check("DELETE /api/backups/{id} 可用", r.status_code == 200, r.text[:150])
        check("返回 deleted=1", r.json().get("deleted") == 1, r.text[:100])
        check("记录已消失", db.get_backup(fail_bid) is None)

        r = client.delete("/api/backups/999999")
        check("删除不存在的返回 404", r.status_code == 404, str(r.status_code))

        # 批量
        make_backup(d1, "failed", error="x")
        make_backup(d2, "failed", error="y")
        r = client.post("/api/backups/delete-failed", json={})
        check("POST /api/backups/delete-failed 可用", r.status_code == 200, r.text[:150])
        check("返回删除条数", r.json()["deleted"] == 2, r.text[:100])
        check("成功的没被动", db.get_backup(ok_bid) is not None)

        # 按设备
        make_backup(d1, "failed", error="x")
        make_backup(d2, "failed", error="y")
        r = client.post("/api/backups/delete-failed", json={"device_id": d2["id"]})
        check("按设备批量删除", r.json()["deleted"] == 1, r.text[:100])
        left = db.get_conn().execute(
            "SELECT COUNT(*) AS c FROM backups WHERE status='failed'").fetchone()["c"]
        check("另一台的失败记录还在", left == 1, str(left))

        # 空 body 也要能处理
        r = client.post("/api/backups/delete-failed", json={})
        check("空范围不报错", r.status_code == 200 and r.json()["deleted"] == 1,
              r.text[:100])

    print("\n=== 8. 统计随删除更新 ===")
    stats = dbm.backup_stats()
    check("失败计数已归零或减少", stats["failed"] == 0, str(stats["failed"]))
    check("成功计数保留", stats["success"] >= 2, str(stats["success"]))

    print("\n=== 9. 前端 ===")
    html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
    check("有清除失败记录按钮", 'id="btn-delete-failed"' in html)
    js = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    check("调用批量删除接口", "/api/backups/delete-failed" in js)
    check("调用单条删除接口", "act-del-backup" in js)
    check("单条删除用 DELETE 方法", "method: 'DELETE'" in js)
    check("有二次确认", "confirm(" in js[js.index("btn-delete-failed"):][:600])
    check("删除后刷新列表", "await refreshHistory()" in js)

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'=' * 46}\n  通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 46}\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())