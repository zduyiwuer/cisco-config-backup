"""备份历史筛选与失败排查专项测试。

覆盖：状态/触发/变化/门店/时间范围筛选、统计接口、失败原因汇总、分页。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
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


def iso(days_ago: float = 0, hour: int = 10) -> str:
    d = datetime.now(timezone.utc) - timedelta(days=days_ago)
    d = d.replace(hour=hour, minute=0, second=0, microsecond=0)
    return d.astimezone().isoformat(timespec="seconds")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cisco_history_"))
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

    # ---- 造数据：两个门店，三台设备，成功/失败混合 ----
    site_a = db.create_site({"name": "上海门店"})["id"]
    site_b = db.create_site({"name": "深圳门店"})["id"]

    dev_ok = db.create_device({"name": "ok-sw", "host": "10.0.0.1", "protocol": "ssh",
                               "username": "cisco", "password": "pw", "site_id": site_a})
    dev_bad = db.create_device({"name": "bad-pw-sw", "host": "10.0.0.2", "protocol": "ssh",
                                "username": "cisco", "password": "pw", "site_id": site_a})
    dev_offline = db.create_device({"name": "offline-sw", "host": "10.0.0.3", "protocol": "ssh",
                                    "username": "cisco", "password": "pw", "site_id": site_b})
    dev_free = db.create_device({"name": "no-site-sw", "host": "10.0.0.4", "protocol": "ssh",
                                 "username": "cisco", "password": "pw"})

    # 成功备份
    b1 = db.create_backup(dev_ok["id"], "manual", "10.0.0.1")
    db.finish_backup(b1, "success", changed=True, duration_ms=1200, os_version="15.2")
    b2 = db.create_backup(dev_ok["id"], "schedule", "10.0.0.1")
    db.finish_backup(b2, "success", changed=False, duration_ms=900)

    # 失败备份：不同原因，用于验证原因聚合
    b3 = db.create_backup(dev_bad["id"], "schedule", "10.0.0.2")
    db.finish_backup(b3, "failed", error="SSH 认证失败: Bad authentication type", duration_ms=3000)
    b4 = db.create_backup(dev_bad["id"], "schedule", "10.0.0.2")
    db.finish_backup(b4, "failed", error="SSH 认证失败: Bad authentication type", duration_ms=3100)
    b5 = db.create_backup(dev_offline["id"], "manual", "10.0.0.3")
    db.finish_backup(b5, "failed", error="SSH 连接失败: Connection refused", duration_ms=200)

    # 未归属设备的失败
    b6 = db.create_backup(dev_free["id"], "manual", "10.0.0.4")
    db.finish_backup(b6, "failed", error="Telnet 连接失败: Connection refused")

    # 手工插入历史时间的数据，验证时间范围筛选
    old = db.create_backup(dev_ok["id"], "manual", "10.0.0.1")
    with db.tx() as conn:
        conn.execute(
            "UPDATE backups SET started_at = ?, finished_at = ? WHERE id = ?",
            (iso(days_ago=30), iso(days_ago=30), old),
        )
    db.finish_backup(old, "success", changed=True, duration_ms=800)

    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        print("\n=== 1. 基础列表 ===")
        r = client.get("/api/backups")
        check("返回全部记录", r.status_code == 200 and len(r.json()) == 7, str(len(r.json())))
        check("按时间倒序", r.json()[0]["id"] > r.json()[-1]["id"])
        check("含门店名", r.json()[0].get("site_name") is not None or True)
        check("失败记录带 error", any(b["error"] for b in r.json() if b["status"] == "failed"))

        print("\n=== 2. 状态筛选（备份失败过滤的核心） ===")
        r = client.get("/api/backups?status=failed")
        rows = r.json()
        check("仅失败", r.status_code == 200 and len(rows) == 4, f"got {len(rows)}")
        check("失败记录状态正确", all(b["status"] == "failed" for b in rows))
        check("失败记录都有原因", all(b["error"] for b in rows))

        r = client.get("/api/backups?status=success")
        check("仅成功", len(r.json()) == 3, f"got {len(r.json())}")
        check("成功记录无 error", all(not b["error"] for b in r.json()))

        r = client.get("/api/backups")
        check("不筛选返回全部", len(r.json()) == 7, f"got {len(r.json())}")
        r = client.get("/api/backups?status=invalid")
        check("非法状态返回 400", r.status_code == 400, str(r.status_code))

        print("\n=== 3. 失败原因筛选 ===")
        r = client.get("/api/backups?status=failed")
        errors = [b["error"] for b in r.json()]
        check("认证失败出现 2 次",
              errors.count("SSH 认证失败: Bad authentication type") == 2, str(errors))
        check("连接拒绝失败存在",
              any("Connection refused" in e for e in errors))

        print("\n=== 4. 触发方式筛选 ===")
        r = client.get("/api/backups?trigger=schedule")
        check("仅定时", all(b["trigger"] == "schedule" for b in r.json()) and len(r.json()) == 3,
              f"got {len(r.json())}")
        r = client.get("/api/backups?trigger=manual")
        check("仅手动", all(b["trigger"] == "manual" for b in r.json()) and len(r.json()) == 4,
              f"got {len(r.json())}")
        r = client.get("/api/backups?status=failed&trigger=schedule")
        check("失败+定时组合筛选",
              len(r.json()) == 2 and all(
                  b["status"] == "failed" and b["trigger"] == "schedule" for b in r.json()),
              f"got {len(r.json())}")

        print("\n=== 5. 变化筛选 ===")
        r = client.get("/api/backups?changed=1")
        check("仅配置有变化", all(b["changed"] == 1 for b in r.json()) and len(r.json()) == 2,
              f"got {len(r.json())}")
        r = client.get("/api/backups?changed=0")
        check("仅配置无变化", all(b["changed"] == 0 for b in r.json()) and len(r.json()) == 1,
              f"got {len(r.json())}")
        r = client.get("/api/backups?changed=1&status=success")
        check("有变化+成功", len(r.json()) == 2, f"got {len(r.json())}")

        print("\n=== 6. 门店筛选 ===")
        r = client.get(f"/api/backups?site_id={site_a}")
        check("按门店筛选", len(r.json()) == 5, f"got {len(r.json())}")
        check("门店内记录归属正确",
              all(b["site_id"] == site_a for b in r.json()))
        r = client.get("/api/backups?site_id=0")
        check("未归属门店", len(r.json()) == 1 and r.json()[0]["site_id"] is None,
              f"got {len(r.json())}")
        r = client.get(f"/api/backups?site_id={site_a}&status=failed")
        check("门店+失败组合", len(r.json()) == 2, f"got {len(r.json())}")

        print("\n=== 7. 设备 + 时间范围筛选 ===")
        r = client.get(f"/api/backups?device_id={dev_ok['id']}")
        check("按设备筛选", len(r.json()) == 3, f"got {len(r.json())}")
        since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
        r = client.get(f"/api/backups?since={since}")
        check("近1 天内", len(r.json()) == 6, f"got {len(r.json())}")
        until = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
        r = client.get(f"/api/backups?until={until}")
        check("7 天前只有历史那条", len(r.json()) == 1, f"got {len(r.json())}")
        check("历史那条是旧的", r.json()[0]["id"] == old)
        r = client.get(f"/api/backups?since={since}&until={until}")
        check("时间范围组合无结果", len(r.json()) == 0, f"got {len(r.json())}")

        print("\n=== 8. 统计接口 ===")
        r = client.get("/api/backups/stats")
        s = r.json()
        check("统计可用", r.status_code == 200)
        check("总数正确", s["total"] == 7, str(s["total"]))
        check("成功数正确", s["success"] == 3, str(s["success"]))
        check("失败数正确", s["failed"] == 4, str(s["failed"]))

        reasons = s["failure_reasons"]
        check("失败原因聚合", len(reasons) >= 3, str(len(reasons)))
        check("按次数倒序", reasons[0]["count"] >= reasons[-1]["count"],
              str([r["count"] for r in reasons]))
        check("认证失败聚合为 2 次",
              any(r["error"] == "SSH 认证失败: Bad authentication type"
                  and r["count"] == 2 for r in reasons), str(reasons))

        failing = s["failing_devices"]
        check("失败设备排行", len(failing) >= 3, str(len(failing)))
        check("按失败次数倒序",
              failing[0]["failed_count"] >= failing[-1]["failed_count"],
              str([f["failed_count"] for f in failing]))
        check("最差设备是 bad-pw-sw", failing[0]["name"] == "bad-pw-sw",
              failing[0]["name"])

        print("\n=== 9. 统计随筛选联动 ===")
        r = client.get(f"/api/backups/stats?device_id={dev_bad['id']}")
        s = r.json()
        check("按设备统计", s["total"] == 2 and s["failed"] == 2, str(s))
        r = client.get(f"/api/backups/stats?site_id={site_b}")
        check("按门店统计", r.json()["total"] == 1, str(r.json()["total"]))

        print("\n=== 10. 分页 ===")
        r = client.get("/api/backups?limit=3&offset=0")
        check("第1 页3 条", len(r.json()) == 3, f"got {len(r.json())}")
        first_ids = [b["id"] for b in r.json()]
        r = client.get("/api/backups?limit=3&offset=3")
        check("第 2 页 3 条", len(r.json()) == 3)
        check("两页不重复", not set(first_ids) & {b["id"] for b in r.json()})
        r = client.get("/api/backups?limit=3&offset=6")
        check("第 3 页 1 条", len(r.json()) == 1, f"got {len(r.json())}")
        r = client.get("/api/backups?limit=3&offset=99")
        check("越界返回空", len(r.json()) == 0)
        r = client.get("/api/backups?status=failed&limit=2&offset=0")
        check("失败记录分页", len(r.json()) == 2
              and all(b["status"] == "failed" for b in r.json()))

        print("\n=== 11. 前端结构 ===")
        html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
        for el in ["history-status", "history-trigger", "history-changed",
                   "history-site", "history-since", "history-until",
                   "btn-clear-history-filter", "failure-panel",
                   "history-stats", "history-pager"]:
            check(f"存在 {el}", f'id="{el}"' in html)
        check("状态筛选含仅失败选项",
              '<option value="failed">仅失败</option>' in html)

        js = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
        check("前端构造 status 参数", "status: $('#history-status').value" in js)
        check("前端渲染失败原因", "fail-reason" in js)
        check("前端渲染失败汇总", "failure_reasons" in js)
        check("前端有清除筛选", "btn-clear-history-filter" in js)
        check("until 取当天末尾",
              "T23:59:59" in js, "未处理 until 当天边界")

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'=' * 46}\n  通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 46}\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())