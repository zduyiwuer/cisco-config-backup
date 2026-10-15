"""设备页备份结果筛选测试。

覆盖 summary 的 failed 字段、备份筛选前端逻辑、以及与后端字段的配合。
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
    tmp = Path(tempfile.mkdtemp(prefix="cisco_devfilter_"))
    from app import crypto, db

    crypto.DATA_DIR = tmp
    crypto.KEY_PATH = tmp / ".secret_key"
    crypto._key()
    crypto._fernet.__init__(crypto.KEY_PATH.read_bytes())
    db.DATA_DIR = tmp
    db.DB_PATH = tmp / "t.db"
    db._local.__dict__.clear()
    db.init_db()
    db.update_settings({"auth_enabled": False})

    print("\n=== 1. summary 含失败统计 ===")
    # 三类设备：最近失败 / 最近成功 / 从未备份
    d_fail = db.create_device({"name": "fail-sw", "host": "10.0.0.1", "protocol": "ssh",
                              "username": "cisco", "password": "pw"})
    d_ok = db.create_device({"name": "ok-sw", "host": "10.0.0.2", "protocol": "ssh",
                             "username": "cisco", "password": "pw"})
    d_never = db.create_device({"name": "new-sw", "host": "10.0.0.3", "protocol": "ssh",
                                "username": "cisco", "password": "pw"})
    # 曾经失败但后来成功
    d_recover = db.create_device({"name": "recover-sw", "host": "10.0.0.4", "protocol": "ssh",
                                  "username": "cisco", "password": "pw"})

    # fail-sw：两次失败
    b = db.create_backup(d_fail["id"], "schedule", "10.0.0.1")
    db.finish_backup(b, "failed", error="SSH 认证失败")
    db.set_device_result(d_fail["id"], "failed", "SSH 认证失败")
    b = db.create_backup(d_fail["id"], "schedule", "10.0.0.1")
    db.finish_backup(b, "failed", error="SSH 认证失败")
    db.set_device_result(d_fail["id"], "failed", "SSH 认证失败")

    # ok-sw：成功且配置有变化
    b = db.create_backup(d_ok["id"], "manual", "10.0.0.2")
    db.finish_backup(b, "success", changed=True)
    db.set_device_result(d_ok["id"], "success", "1 条命令采集完成")

    # recover-sw：先失败后成功
    b = db.create_backup(d_recover["id"], "manual", "10.0.0.4")
    db.finish_backup(b, "failed", error="Telnet 超时")
    db.set_device_result(d_recover["id"], "failed", "Telnet 超时")
    b = db.create_backup(d_recover["id"], "manual", "10.0.0.4")
    db.finish_backup(b, "success", changed=False)
    db.set_device_result(d_recover["id"], "success", "1 条命令采集完成")

    s_fail = db.device_backup_summary(d_fail["id"])
    check("失败设备 failed=2", s_fail["failed"] == 2, str(s_fail))
    check("失败设备 success=0", s_fail["success"] == 0)
    check("失败设备有 last_failed_at", s_fail["last_failed_at"] is not None)
    check("失败设备无 latest_backup_id", s_fail["latest_backup_id"] is None)

    s_ok = db.device_backup_summary(d_ok["id"])
    check("成功设备 success=1", s_ok["success"] == 1)
    check("成功设备 failed=0", s_ok["failed"] == 0)
    check("成功设备 changed=1", s_ok["changed"] == 1, str(s_ok))
    check("成功设备有 latest_backup_id", s_ok["latest_backup_id"] is not None)

    s_never = db.device_backup_summary(d_never["id"])
    check("未备份设备 total=0", s_never["total"] == 0)
    check("未备份设备 failed=0", s_never["failed"] == 0)
    check("未备份设备无 last_failed_at", s_never["last_failed_at"] is None)

    s_rec = db.device_backup_summary(d_recover["id"])
    check("恢复设备 success=1", s_rec["success"] == 1)
    check("恢复设备保留失败计数 failed=1", s_rec["failed"] == 1, str(s_rec))
    check("恢复设备有 last_failed_at", s_rec["last_failed_at"] is not None)

    print("\n=== 2. changed 统计不含失败记录 ===")
    # 给 ok-sw 加一条失败记录，changed 应仍为 1（失败的 changed=0 不计入）
    b = db.create_backup(d_ok["id"], "schedule", "10.0.0.2")
    db.finish_backup(b, "failed", error="超时")
    s_ok2 = db.device_backup_summary(d_ok["id"])
    check("失败记录不计入 changed", s_ok2["changed"] == 1, str(s_ok2["changed"]))
    check("失败记录计入 failed", s_ok2["failed"] == 1)

    print("\n=== 3. API 返回 summary ===")
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        r = client.get("/api/devices")
        check("设备列表可获取", r.status_code == 200)
        devices = {d["name"]: d for d in r.json()}
        check("fail-sw 带 failed=2",
              devices["fail-sw"]["summary"]["failed"] == 2,
              str(devices["fail-sw"]["summary"]))
        check("API 返回 last_status",
              devices["fail-sw"]["last_status"] == "failed",
              devices["fail-sw"]["last_status"])
        check("API 返回 last_message",
              bool(devices["fail-sw"]["last_message"]))

        r = client.get(f"/api/devices/{d_fail['id']}")
        check("设备详情带 summary",
              r.status_code == 200 and r.json()["summary"]["failed"] == 2)

        r = client.get(f"/api/devices/{d_never['id']}")
        check("未备份设备 summary 为空值",
              r.status_code == 200 and r.json()["summary"]["total"] == 0)

    print("\n=== 4. 前端筛选逻辑 ===")
    js = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    check("存在 matchBackupFilter", "function matchBackupFilter" in js)
    check("renderDevices 调用该函数", "matchBackupFilter(d, bkFilter)" in js)
    check("绑定备份筛选 change 事件",
          "device-backup-filter').addEventListener('change'" in js)
    check("支持最近失败", "case 'failed':" in js)
    check("支持最近成功", "case 'success':" in js)
    check("支持曾失败", "case 'ever_failed':" in js)
    check("支持从未备份", "case 'never':" in js)
    check("统计卡可点击筛选", "stat-clickable" in js and "stat-failed" in js)
    check("最近备份列展示失败次数", "次失败" in js)

    html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
    check("HTML 含备份筛选下拉", 'id="device-backup-filter"' in html)
    for opt in ["最近备份失败", "最近备份成功", "曾备份失败", "从未备份", "配置有变化"]:
        check(f"下拉含「{opt}」", opt in html)

    print("\n=== 5. 模拟前端筛选（对齐 JS 逻辑） ===")

    def match(d, mode):
        s = d.get("summary") or {}
        if not mode:
            return True
        if mode == "failed":
            return d.get("last_status") == "failed"
        if mode == "success":
            return d.get("last_status") == "success"
        if mode == "ever_failed":
            return (s.get("failed") or 0) > 0
        if mode == "never":
            return not d.get("last_backup_at")
        if mode == "changed":
            return (s.get("changed") or 0) > 0
        return True

    all_devs = [
        {"name": "fail-sw", "last_status": "failed",
         "last_backup_at": "2026-01-01", "summary": {"failed": 2, "changed": 0}},
        {"name": "ok-sw", "last_status": "success",
         "last_backup_at": "2026-01-01", "summary": {"failed": 1, "changed": 1}},
        {"name": "recover-sw", "last_status": "success",
         "last_backup_at": "2026-01-01", "summary": {"failed": 1, "changed": 0}},
        {"name": "new-sw", "last_status": None,
         "last_backup_at": None, "summary": {"failed": 0, "changed": 0}},
    ]

    check("全部筛选返回 4 台", len([d for d in all_devs if match(d, "")]) == 4)
    check("最近失败筛出 1 台",
          [d["name"] for d in all_devs if match(d, "failed")] == ["fail-sw"])
    check("最近成功筛出 2 台",
          len([d for d in all_devs if match(d, "success")]) == 2)
    check("曾失败筛出 3 台",
          len([d for d in all_devs if match(d, "ever_failed")]) == 3,
          str([d["name"] for d in all_devs if match(d, "ever_failed")]))
    check("从未备份筛出 1 台",
          [d["name"] for d in all_devs if match(d, "never")] == ["new-sw"])
    check("配置有变化筛出 1 台",
          [d["name"] for d in all_devs if match(d, "changed")] == ["ok-sw"])
    check("最近失败与曾失败不同（fail-sw 有2 次失败但仅按最近一次判）",
          match(all_devs[0], "failed") and not match(all_devs[2], "failed"))

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'=' * 46}\n  通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 46}\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())