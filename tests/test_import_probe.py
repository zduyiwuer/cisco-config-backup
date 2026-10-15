"""导入与在线探测专项测试。"""

from __future__ import annotations

import csv
import io
import shutil
import sys
import tempfile
import time
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


def make_csv(rows: list[list], headers: list[str] | None = None) -> bytes:
    headers = headers or ["设备名称", "管理IP", "协议", "端口", "用户名", "登录密码", "门店", "备注"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    w.writerows(rows)
    return ("\ufeff" + buf.getvalue()).encode("utf-8")  # 带 BOM，模拟 Excel 导出


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cisco_import_"))
    from app import crypto, db, importer, probe, scheduler

    crypto.DATA_DIR = tmp
    crypto.KEY_PATH = tmp / ".secret_key"
    crypto._key()
    crypto._fernet.__init__(crypto.KEY_PATH.read_bytes())
    db.DATA_DIR = tmp
    db.DB_PATH = tmp / "t.db"
    db._local.__dict__.clear()
    db.init_db()
    # 本文件专注导入与探测逻辑，认证由 test_auth.py 专门覆盖
    db.update_settings({"auth_enabled": False})

    from fastapi.testclient import TestClient
    from app.main import app

    print("\n=== 1. 导入：CSV 中文表头 ===")
    content = make_csv([
        ["core-sw-01", "10.0.0.1", "ssh", "22", "cisco", "pw123", "上海南京西路店", "核心"],
        ["access-sw-02", "10.0.0.2", "telnet", "23", "cisco", "pw123", "上海南京西路店", "接入"],
        ["edge-rtr-03", "10.0.0.3", "ssh", "", "admin", "pw456", "深圳南山科技园店", "边界"],
    ])
    rows = importer.read_table(content, "devices.csv")
    check("CSV 解析出行数", len(rows) == 3, str(len(rows)))
    result = importer.import_devices(rows, mode="create")
    check("导入 3 台成功", result["summary"]["created"] == 3, str(result["summary"]))
    check("无失败行", result["summary"]["failed"] == 0)

    devices = db.list_devices()
    check("设备已入库", len(devices) == 3)
    by_name = {d["name"]: d for d in devices}
    check("端口按协议自动补全(telnet)", by_name["access-sw-02"]["port"] == 23)
    check("端口按协议自动补全(ssh)", by_name["edge-rtr-03"]["port"] == 22)
    check("门店自动创建", db.get_site_by_name("上海南京西路店") is not None)
    check("设备归属门店正确", by_name["core-sw-01"]["site"] == "上海南京西路店")
    check("密码加密存储", by_name["core-sw-01"]["password"] == "" and db.get_device(by_name["core-sw-01"]["id"], True)["password"] == "pw123")

    print("\n=== 2. 导入：英文表头与别名 ===")
    en = make_csv(
        [["sw-a", "1.1.1.1", "ssh", 22, "cisco", "pw", "北京朝阳门店", ""]],
        headers=["name", "ip_address", "protocol", "port", "username", "password", "site", "note"],
    )
    r = importer.import_devices(importer.read_table(en, "x.csv"))
    check("英文表头可识别", r["summary"]["created"] == 1, str(r["summary"]))

    print("\n=== 3. 导入：Excel (.xlsx) ===")
    try:
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.append(["设备名称", "管理IP", "协议", "端口", "用户名", "登录密码", "门店"])
        ws.append(["xlsx-sw-01", "2.2.2.2", "ssh", 22, "cisco", "pw", "广州天河店"])
        bio = io.BytesIO()
        wb.save(bio)
        rows_x = importer.read_table(bio.getvalue(), "devices.xlsx")
        check("Excel 解析出行数", len(rows_x) == 1, str(len(rows_x)))
        r = importer.import_devices(rows_x)
        check("Excel 导入成功", r["summary"]["created"] == 1, str(r["summary"]))
    except ImportError:
        check("openpyxl 可用", False, "未安装")

    print("\n=== 4. 导入：校验与容错 ===")
    bad = make_csv([
        ["good-01", "3.3.3.3", "ssh", 22, "cisco", "pw", "测试店", ""],
        ["", "3.3.3.4", "ssh", 22, "cisco", "pw", "测试店", ""],        # 缺名称
        ["no-ip", "", "ssh", 22, "cisco", "pw", "测试店", ""],            # 缺 IP
        ["bad-proto", "3.3.3.5", "http", 22, "cisco", "pw", "测试店", ""], # 协议非法
        ["bad-port", "3.3.3.6", "ssh", "abc", "cisco", "pw", "测试店", ""],  # 端口非法
        ["no-pass", "3.3.3.7", "ssh", 22, "cisco", "", "测试店", ""],      # 缺密码
        ["", "", "", "", "", "", "", ""],                                  # 空行
    ])
    r = importer.import_devices(importer.read_table(bad, "b.csv"))
    s = r["summary"]
    check("合法行被导入", s["created"] == 1, str(s))
    check("缺名称被拒", any("缺少设备名称" in d["message"] for d in r["details"]))
    check("缺 IP 被拒", any("缺少管理 IP" in d["message"] for d in r["details"]))
    check("非法协议被拒", any("协议" in d["message"] for d in r["details"]))
    check("非法端口被拒", any("端口" in d["message"] for d in r["details"]))
    check("缺密码被拒", any("缺少登录密码" in d["message"] for d in r["details"]))
    check("空行被跳过", all(d["name"] != "" for d in r["details"] if d["status"] == "failed"))

    print("\n=== 5. 导入：重复与更新模式 ===")
    dup = make_csv([["core-sw-01", "9.9.9.9", "ssh", 22, "cisco", "newpw", "上海南京西路店", "改了"]])
    r = importer.import_devices(importer.read_table(dup, "d.csv"), mode="create")
    check("create 模式重名跳过", r["summary"]["skipped"] == 1, str(r["summary"]))
    check("原设备 IP 未变", db.get_device_by_name("core-sw-01")["host"] == "10.0.0.1")

    r = importer.import_devices(importer.read_table(dup, "d.csv"), mode="upsert")
    check("upsert 模式更新成功", r["summary"]["updated"] == 1, str(r["summary"]))
    updated = db.get_device_by_name("core-sw-01")
    check("IP 已更新", updated["host"] == "9.9.9.9")
    check("密码已更新", updated["password"] == "newpw")

    r = importer.import_devices(
        importer.read_table(dup, "d.csv"), mode="upsert"
    )
    again = db.get_device_by_name("core-sw-01")
    check("upsert 空密码保持原值", again["password"] == "newpw", again["password"])

    print("\n=== 6. 导入：模板下载 ===")
    tpl = importer.template_csv()
    check("模板含中文表头", "设备名称" in tpl and "管理IP" in tpl)
    check("模板带 BOM", tpl.startswith("\ufeff"))
    check("模板含示例行", "core-sw-01" in tpl)

    print("\n=== 7. 在线探测 ===")
    # 起一个监听端口作为"在线设备"，再用未监听端口作为"离线设备"
    import socket as _socket

    listener = _socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    live_port = listener.getsockname()[1]

    db.create_device({
        "name": "online-sw", "host": "127.0.0.1", "port": 23,
        "protocol": "telnet", "username": "c", "password": "p",
        "commands": ["show running-config"],
    })
    db.create_device({
        "name": "offline-sw", "host": "127.0.0.1", "port": 9,
        "protocol": "ssh", "username": "c", "password": "p",
        "commands": ["show running-config"],
    })
    online_dev = db.get_device_by_name("online-sw")
    offline_dev = db.get_device_by_name("offline-sw")
    db.update_device(online_dev["id"], {"port": live_port})

    r = probe.probe_device(db.get_device(online_dev["id"]), timeout=3)
    check("可连设备探测为在线", r["online"] is True, str(r))
    check("记录延迟", r["latency_ms"] is not None and r["latency_ms"] >= 0)

    r = probe.probe_device(db.get_device(offline_dev["id"]), timeout=3)
    check("不可连设备探测为离线", r["online"] is False, str(r))
    check("离线有原因", bool(r["error"]))

    on_dev = db.get_device(online_dev["id"])
    off_dev = db.get_device(offline_dev["id"])
    check("在线状态已落库", on_dev["online"] == 1)
    check("离线状态已落库", off_dev["online"] == 0)
    check("记录最后在线时间", on_dev["last_seen_at"] is not None)
    check("记录探测时间", off_dev["last_probe_at"] is not None)

    # 连续失败计数
    for _ in range(3):
        probe.probe_device(db.get_device(offline_dev["id"]), timeout=2)
    check("累计连续失败次数", db.get_device(offline_dev["id"])["consecutive_fails"] == 4)

    # 离线清单（此时设备仍处于离线状态）
    overview = db.online_overview()
    check("在线概况统计正确", overview["total"] >= 2)
    check("在线设备被统计", overview["online"] >= 1)
    offline_names = {d["name"] for d in overview["offline_devices"]}
    check("离线清单包含设备", "offline-sw" in offline_names, str(offline_names))
    check("离线设备带连续失败数", any(
        d.get("consecutive_fails", 0) > 0 for d in overview["offline_devices"]
    ))

    # 把离线设备端口指向可用端口，模拟恢复后失败计数应清零
    db.update_device(offline_dev["id"], {"port": live_port})
    probe.probe_device(db.get_device(offline_dev["id"]), timeout=3)
    recovered = db.get_device(offline_dev["id"])
    check("恢复在线后失败计数清零", recovered["consecutive_fails"] == 0, str(recovered["consecutive_fails"]))
    check("恢复后标记为在线", recovered["online"] == 1)
    db.update_device(offline_dev["id"], {"port": 9})  # 复原为离线
    probe.probe_device(db.get_device(offline_dev["id"]), timeout=2)

    print("\n=== 8. 探测：批量并发 ===")
    summary = probe.probe_all(timeout=3)
    check("批量探测覆盖全部设备", summary["total"] >= 4, str(summary["total"]))
    check("批量探测返回结果明细", len(summary["results"]) == summary["total"])
    check("批量探测统计在线数", summary["online"] >= 1)

    print("\n=== 9. HTTP API ===")
    with TestClient(app) as client:
        r = client.get("/api/probe/status")
        check("GET /api/probe/status", r.status_code == 200)
        check("返回在线数", "online" in r.json() and "offline" in r.json())
        check("返回探测间隔", r.json()["probe_interval_minutes"] == 5)
        check("默认开启探测", r.json()["probe_enabled"] is True)

        r = client.post("/api/probe/run", json={})
        check("POST /api/probe/run 立即探测", r.status_code == 200 and r.json()["total"] > 0)

        did = db.get_device_by_name("offline-sw")["id"]
        r = client.get(f"/api/probe/{did}/history")
        check("GET 探测历史", r.status_code == 200 and len(r.json()["history"]) > 0)
        r = client.get("/api/probe/99999/history")
        check("不存在设备返回 404", r.status_code == 404)

        # 导入 API
        r = client.get("/api/import/template")
        check("GET 导入模板", r.status_code == 200 and "设备名称" in r.text)
        check("模板中文名可下载",
              "UTF-8''" in r.headers.get("content-disposition", ""))

        csv_bytes = make_csv([
            ["api-sw-01", "7.7.7.1", "ssh", 22, "cisco", "pw", "导入测试店", ""],
            ["api-sw-02", "7.7.7.2", "telnet", 23, "cisco", "pw", "导入测试店", ""],
        ])
        r = client.post(
            "/api/import/devices",
            files={"file": ("dev.csv", csv_bytes, "text/csv")},
            params={"params": "create"},
        )
        check("POST /api/import/devices", r.status_code == 200, r.text[:150])
        check("导入 API 新增 2 台", r.json()["summary"]["created"] == 2, r.text[:200])
        check("导入自动建门店", db.get_site_by_name("导入测试店") is not None)

        r = client.post(
            "/api/import/devices",
            files={"file": ("dev.csv", csv_bytes, "text/csv")},
            params={"params": "create"},
        )
        check("重复导入被跳过", r.json()["summary"]["skipped"] == 2)

        r = client.post(
            "/api/import/devices",
            files={"file": ("empty.csv", b"", "text/csv")},
        )
        check("空文件返回 400", r.status_code == 400)

        r = client.post(
            "/api/import/devices",
            files={"file": ("x.txt", b"not a table", "text/plain")},
        )
        check("非法文件有明确提示", r.status_code == 400, r.text[:120])

        # 状态接口含在线数据
        r = client.get("/api/status")
        check("状态含在线/离线数", "online" in r.json() and "offline" in r.json())

        # 设置项
        r = client.put("/api/settings", json={"probe_interval_minutes": 10, "probe_enabled": False})
        check("PUT 探测设置", r.status_code == 200 and r.json()["probe_interval_minutes"] == 10)
        r = client.put("/api/settings", json={"probe_enabled": True, "probe_interval_minutes": 5})
        check("恢复探测设置", r.json()["probe_enabled"] is True)

    print("\n=== 10. 调度：5 分钟探测任务 ===")
    with TestClient(app) as client:
        # 此时调度器处于运行状态，探测任务已注册
        all_jobs = scheduler.scheduler.get_jobs()
        # 精确匹配周期任务，排除 id 以 ":initial" 结尾的首次探测
        probe_jobs = [
            j for j in all_jobs
            if j.id == scheduler.PROBE_JOB_ID
        ]
        check("周期探测任务已注册", len(probe_jobs) == 1, str([j.id for j in all_jobs]))
        if probe_jobs:
            trig = probe_jobs[0].trigger
            check("探测触发器为 Interval", type(trig).__name__ == "IntervalTrigger",
                  type(trig).__name__)
            check("探测间隔为 5 分钟", str(getattr(trig, "interval", "")) == "0:05:00",
                  str(getattr(trig, "interval", None)))
            check("探测任务已排下次执行", probe_jobs[0].next_run_time is not None)

        db.update_settings({"probe_enabled": False})
        scheduler.sync_probe_job()
        jobs = [j for j in scheduler.scheduler.get_jobs() if j.id == scheduler.PROBE_JOB_ID]
        check("关闭后探测任务移除", len(jobs) == 0)

        db.update_settings({"probe_enabled": True, "probe_interval_minutes": 5})
        scheduler.sync_probe_job()
        jobs = [j for j in scheduler.scheduler.get_jobs() if j.id == scheduler.PROBE_JOB_ID]
        check("重新开启后任务恢复", len(jobs) == 1)

    listener.close()
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'=' * 46}\n  通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 46}\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())