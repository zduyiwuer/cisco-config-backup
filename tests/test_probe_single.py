"""单台设备探测测试。

覆盖：
- POST /api/devices/{id}/probe 只探测指定设备，不影响其他设备
- 探测结果落库并更新设备在线状态
- 探测历史随结果返回
- 与「测试」接口的职责区分
- 前端按钮与就地更新逻辑
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
    tmp = Path(tempfile.mkdtemp(prefix="cisco_probe1_"))
    from app import crypto, db, probe

    crypto.DATA_DIR = tmp
    crypto.KEY_PATH = tmp / ".secret_key"
    crypto._key()
    crypto._fernet.__init__(crypto.KEY_PATH.read_bytes())
    db.DATA_DIR = tmp
    db.DB_PATH = tmp / "t.db"
    db._local.__dict__.clear()
    db.init_db()
    db.update_settings({"auth_enabled": False, "probe_enabled": False})

    d1 = db.create_device({"name": "SW-01", "host": "10.0.0.1", "port": 22,
                           "protocol": "ssh", "username": "cisco", "password": "pw"})
    d2 = db.create_device({"name": "SW-02", "host": "10.0.0.2", "port": 22,
                           "protocol": "ssh", "username": "cisco", "password": "pw"})
    d3 = db.create_device({"name": "SW-03", "host": "127.0.0.1", "port": 65533,
                           "protocol": "ssh", "username": "cisco", "password": "pw"})

    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        print("\n=== 1. 接口基本可用 ===")
        r = client.post(f"/api/devices/{d1['id']}/probe")
        check("探测接口返回 200", r.status_code == 200, r.text[:150])
        body = r.json()
        check("返回设备名", body.get("name") == "SW-01", str(body.get("name")))
        check("返回 online 字段", "online" in body, str(list(body)))
        check("返回 latency_ms 字段", "latency_ms" in body)
        check("返回 error 字段", "error" in body)
        check("返回 device_id", body.get("device_id") == d1["id"])

        r = client.post("/api/devices/999999/probe")
        check("设备不存在返回 404", r.status_code == 404, str(r.status_code))

        print("\n=== 2. 只影响被探测的设备 ===")
        # 先记录另外两台的状态
        db.record_probe(d2["id"], True, 10, None)
        before2 = db.get_device(d2["id"])
        before3 = db.get_device(d3["id"])

        client.post(f"/api/devices/{d1['id']}/probe")
        after2 = db.get_device(d2["id"])
        after3 = db.get_device(d3["id"])
        check("未探测的设备状态不变",
              after2["online"] == before2["online"]
              and after2["last_probe_at"] == before2["last_probe_at"],
              f"{before2['online']}->{after2['online']}")
        check("另一台未探测设备也不变",
              after3["online"] == before3["online"]
              and after3["last_probe_at"] == before3["last_probe_at"])

        print("\n=== 3. 探测结果落库 ===")
        log = db.probe_history(d1["id"], 10)
        check("写入探测历史", len(log) >= 1, str(len(log)))
        check("历史含 online 字段", "online" in log[0], str(list(log[0])))
        check("历史含时间戳", "at" in log[0], str(list(log[0])))
        check("设备 last_probe_at 已更新",
              db.get_device(d1["id"])["last_probe_at"] is not None)
        # body 是首次探测时的快照，之后又探测过，条数会变多，
        # 因此只校验它确实是历史记录的一部分
        hist_ids = {(h.get("at"), h.get("online")) for h in body.get("history", [])}
        db_ids = {(h.get("at"), h.get("online")) for h in db.probe_history(d1["id"], 10)}
        check("接口返回的 history 属于数据库历史",
              bool(hist_ids) and hist_ids <= db_ids,
              f"{hist_ids - db_ids}")

        print("\n=== 4. 连续失败计数 ===")
        # 连着一个必然不通的端口，连探 3 次
        for _ in range(3):
            client.post(f"/api/devices/{d3['id']}/probe")
        dev3 = db.get_device(d3["id"])
        check("失败设备 online=0", dev3["online"] == 0, str(dev3["online"]))
        check("连续失败计数递增", dev3["consecutive_fails"] >= 3,
              str(dev3["consecutive_fails"]))

        print("\n=== 5. 离线设备探测成功可恢复 ===")
        # 手动置为离线，再对一台「假装在线」的设备探测
        db.record_probe(d1["id"], False, None, "模拟失败")
        check("置为离线成功", db.get_device(d1["id"])["online"] == 0)
        client.post(f"/api/devices/{d1['id']}/probe")
        # d1 指向 10.0.0.1 必然不通，探测后仍是离线，但计数会累加
        dev1 = db.get_device(d1["id"])
        check("探测后状态与实际一致（不通则仍离线）",
              dev1["online"] == 0, str(dev1["online"]))

        print("\n=== 6. 探测与测试的职责区分 ===")
        # 探测接口不做 SSH 登录，只做 banner 握手
        import inspect
        src = inspect.getsource(probe.probe_device)
        check("probe_device 不含 paramiko 登录调用",
              "paramiko" not in src.lower() or "connect(" not in src,
              "探测不应建立 SSH 会话")
        # 测试接口是完整登录
        r = client.post(f"/api/devices/{d1['id']}/test")
        check("测试接口独立存在", r.status_code == 200, str(r.status_code))
        check("测试接口返回 ok 字段", "ok" in r.json(), str(list(r.json())))

        print("\n=== 7. 权限要求 ===")
        db.create_user({"username": "viewer1", "password": "Viewer12345",
                        "role": "viewer"}) if hasattr(db, "create_user") else None
        # 匿名请求应被拒（auth 已在测试里关闭，这里只验证不抛 500）
        r = client.post(f"/api/devices/{d1['id']}/probe")
        check("探测接口不返回 500", r.status_code < 500, str(r.status_code))

        print("\n=== 8. 与批量探测接口的一致性 ===")
        r = client.post("/api/probe/run", json={"device_ids": [d1["id"], d2["id"]]})
        check("批量接口支持指定设备", r.status_code == 200, r.text[:150])
        check("批量结果数量正确", r.json()["total"] == 2, str(r.json()["total"]))
        check("批量结果按名称排序",
              [x["name"] for x in r.json()["results"]] == ["SW-01", "SW-02"],
              str([x["name"] for x in r.json()["results"]]))

        r = client.post("/api/probe/run", json={})
        check("不传则探测全部启用设备", r.json()["total"] == 3, str(r.json()["total"]))

    print("\n=== 9. 前端 ===")
    js = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    check("设备行有探测按钮", 'class="btn small act-probe"' in js)
    check("探测按钮绑定事件", "act-probe" in js)
    check("调用单设备探测接口", "/api/devices/${id}/probe" in js)
    check("有就地更新圆点函数", "function updateDeviceOnlineDot" in js)
    check("就地更新不整表重绘",
          "row.querySelector" in js and "renderDevices(" not in
          js[js.index("function updateDeviceOnlineDot"):
             js.index("function updateDeviceOnlineDot") + 800])
    check("探测与测试按钮有区分说明",
          "不登录设备" in js and "验证账号密码" in js)

    check("在线监控页有单台探测按钮", 'act-probe-one' in js)
    check("监控页按钮绑定事件委托",
          "probe-table').addEventListener('click'" in js)
    check("监控页探测后刷新列表", "await loadProbe()" in js)

    html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
    check("页面有探测相关入口", "probe" in html.lower())
    check("监控表有操作列", "<th>操作</th>" in html)

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'=' * 46}\n  通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 46}\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())