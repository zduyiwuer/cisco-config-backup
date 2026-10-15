"""端到端自检：用 mock 设备跑通 SSH / Telnet 采集、加密、diff、CDP 解析。

运行： ./.venv/Scripts/python.exe -m tests.test_e2e
"""

from __future__ import annotations

import re
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


def main() -> int:
    # 让 db / crypto 使用临时目录，避免污染真实数据
    tmp = Path(tempfile.mkdtemp(prefix="cisco_backup_test_"))
    from app import crypto, db

    crypto.DATA_DIR = tmp
    crypto.KEY_PATH = tmp / ".secret_key"
    crypto._key()
    crypto._fernet.__init__(crypto.KEY_PATH.read_bytes())

    db.DATA_DIR = tmp
    db.DB_PATH = tmp / "test.db"
    db._local.__dict__.clear()
    db.init_db()
    # 本文件专注业务逻辑，认证由 test_auth.py 专门覆盖
    db.update_settings({"auth_enabled": False})

    from app import collector
    from app.cdp import parse_cdp_neighbors
    from app.drivers.base import clean_output, sha256_text
    from tests import mock_device

    print("\n=== 1. 单元：加密 / 清洗 / CDP 解析 / diff ===")
    enc = crypto.encrypt("Passw0rd!")
    check("密码加密后不含明文", "Passw0rd!" not in enc)
    check("密码可解密还原", crypto.decrypt(enc) == "Passw0rd!")
    check("空密码处理", crypto.encrypt("") == "" and crypto.decrypt("") == "")

    raw = "CORE-SW#show running-config\r\nBuilding configuration...\r\n!\r\nhostname X\r\n!\r\n\r\n\r\nCORE-SW#"
    cleaned = clean_output(raw, "show running-config", "CORE-SW#")
    check("清洗去掉命令回显", not cleaned.strip().startswith("show running"))
    check("清洗去掉提示符", "CORE-SW#" not in cleaned)
    check("清洗压缩多余空行", "\n\n\n" not in cleaned)

    nbs = parse_cdp_neighbors(mock_device.CDP_DETAIL)
    check("CDP 解析出 2 个邻居", len(nbs) == 2, f"got {len(nbs)}")
    check(
        "CDP 邻居字段完整",
        nbs[0]["device_id"] == "SW-CORE-02.corp.local"
        and nbs[0]["ip"] == "10.1.1.3"
        and nbs[0]["local_interface"] == "GigabitEthernet0/1"
        and nbs[0]["remote_interface"] == "GigabitEthernet0/24",
        str(nbs[0]),
    )
    check("空输入解析不报错", parse_cdp_neighbors("") == [])

    print("\n=== 2. SSH 采集（mock 设备） ===")
    ssh_server = mock_device.MockSSHServer()
    ssh_server.start()
    ssh_server.ready.wait(5)
    time.sleep(0.5)  # 预热 RSA 主机密钥的首次加解密

    dev = db.create_device(
        {
            "name": "core-sw-01",
            "host": ssh_server.host,
            "port": ssh_server.port,
            "protocol": "ssh",
            "username": mock_device.USERNAME,
            "password": mock_device.PASSWORD,
            "enable_password": mock_device.ENABLE_PASSWORD,
            "commands": ["show running-config", "show cdp neighbors detail", "show version"],
        }
    )
    stored = db.get_device(dev["id"], reveal=True)
    check("设备入库且密码可解密", stored["password"] == mock_device.PASSWORD)
    check("API 层不回显明文密码", db.get_device(dev["id"])["password"] == "")

    res = collector.backup_device(dev["id"], "manual")
    check("SSH 备份成功", res["status"] == "success", str(res.get("error")))
    detail = db.get_backup(res["backup_id"])
    files = {f["command"]: f for f in detail["files"]}
    check("采集到 3 条命令输出", len(detail["files"]) == 3)
    check(
        "show run 内容完整",
        files.get("show running-config")
        and "hostname CORE-SW-01" in db.get_file_content(files["show running-config"]["id"])["content"],
    )
    check(
        "show cdp neighbors detail 内容完整",
        files.get("show cdp neighbors detail")
        and "SW-CORE-02.corp.local" in db.get_file_content(files["show cdp neighbors detail"]["id"])["content"],
    )
    check("解析出 IOS 版本", detail["os_version"] == "15.2(4)E7", str(detail["os_version"]))
    check("首次备份全部标记为新版本", all(f["is_new"] for f in detail["files"]))

    print("\n=== 3. 幂等：无变化时不应产生新版本 ===")
    res2 = collector.backup_device(dev["id"], "manual")
    d2 = db.get_backup(res2["backup_id"])
    check("第二次备份成功", res2["status"] == "success")
    check("无变化时 changed=False", res2["changed"] is False)
    check("无变化时文件标记非新版本", all(not f["is_new"] for f in d2["files"]))

    print("\n=== 4. 差异：配置变更后生成 diff ===")
    mock_device.mutate()
    res3 = collector.backup_device(dev["id"], "manual")
    d3 = db.get_backup(res3["backup_id"])
    run_file = next(f for f in d3["files"] if f["command"] == "show running-config")
    check("配置变化后 changed=True", res3["changed"] is True)
    check("变化文件标记为新版本", run_file["is_new"])
    diff = db.get_diff(run_file["id"])
    check("生成 diff 记录", diff is not None)
    check("diff 含新增行", diff and diff["added"] > 0, str(diff and diff["added"]))
    check(
        "diff 定位到变更内容",
        diff and "access-port-printer" in diff["diff_text"],
    )
    check("CDP 输出无变化时不生成 diff", db.get_diff(
        next(f for f in d3["files"] if f["command"] == "show cdp neighbors detail")["id"]
    ) is None)

    print("\n=== 5. Telnet 采集（mock 设备，含 IAC 协商） ===")
    tl = mock_device.MockTelnetServer()
    tl.start()
    tl.ready.wait(5)
    time.sleep(0.3)
    dev_tl = db.create_device(
        {
            "name": "access-sw-tl",
            "host": tl.host,
            "port": tl.port,
            "protocol": "telnet",
            "username": mock_device.USERNAME,
            "password": mock_device.PASSWORD,
            "enable_password": mock_device.ENABLE_PASSWORD,
            "commands": ["show running-config", "show cdp neighbors detail"],
        }
    )
    res_tl = collector.backup_device(dev_tl["id"], "manual")
    check("Telnet 备份成功", res_tl["status"] == "success", str(res_tl.get("error")))
    if res_tl["status"] == "success":
        d_tl = db.get_backup(res_tl["backup_id"])
        f_tl = next(f for f in d_tl["files"] if f["command"] == "show running-config")
        content = db.get_file_content(f_tl["id"])["content"]
        check("Telnet 拿到完整配置", "hostname CORE-SW-01" in content)
        check("Telnet 无 IAC 残留字节", "\xff" not in content)

    print("\n=== 6. 错误处理 ===")
    bad = db.create_device(
        {
            "name": "bad-device",
            "host": "127.0.0.1",
            "port": 9,
            "protocol": "ssh",
            "username": "x",
            "password": "y",
            "commands": ["show running-config"],
        }
    )
    res_bad = collector.backup_device(bad["id"], "manual")
    check("连接失败被捕获而非抛异常", res_bad["status"] == "failed")
    check("失败原因已记录", bool(res_bad.get("error")))
    dev_state = db.get_device(bad["id"])
    check("设备最近状态标记为 failed", dev_state["last_status"] == "failed")

    wrong = db.create_device(
        {
            "name": "wrong-pass",
            "host": ssh_server.host,
            "port": ssh_server.port,
            "protocol": "ssh",
            "username": "cisco",
            "password": "wrong",
            "commands": ["show run"],
        }
    )
    res_wrong = collector.backup_device(wrong["id"], "manual")
    check("认证失败被捕获", res_wrong["status"] == "failed")
    check("认证失败提示清晰", "认证" in (res_wrong.get("error") or ""), str(res_wrong.get("error")))

    print("\n=== 7. HTTP API ===")
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = client.get("/api/health")
        check("GET /api/health", r.status_code == 200 and r.json()["status"] == "ok")
        r = client.get("/api/devices")
        check("GET /api/devices 返回列表", r.status_code == 200 and len(r.json()) == 4)
        check("设备列表不回显密码", all(d["password"] == "" for d in r.json()))

        r = client.post("/api/devices", json={"name": "api-made", "host": "1.1.1.1", "protocol": "ssh",
                                              "username": "u", "password": "p", "port": 22})
        check("POST /api/devices 创建成功", r.status_code == 201, r.text[:120])
        new_id = r.json()["id"]

        r = client.patch(f"/api/devices/{new_id}", json={"cron": "30 3 * * *", "schedule_enabled": True})
        check("PATCH 更新定时策略", r.status_code == 200 and r.json()["cron"] == "30 3 * * *")

        r = client.get("/api/backups?limit=10")
        check("GET /api/backups", r.status_code == 200 and len(r.json()) >= 3)

        bid = client.post(f"/api/devices/{dev['id']}/backup").json()["backup_id"]
        r = client.get(f"/api/backups/{bid}")
        check("GET /api/backups/{id} 含文件明细", r.status_code == 200 and len(r.json()["files"]) == 3)
        fid = r.json()["files"][0]["id"]
        r = client.get(f"/api/files/{fid}/raw")
        check("GET 原始配置为 text/plain", r.status_code == 200 and "hostname" in r.text)
        r = client.get(f"/api/backup/{bid}/download")
        check("GET 打包 zip 下载", r.status_code == 200 and r.content[:2] == b"PK")

        r = client.get(f"/api/devices/{dev['id']}/neighbors")
        check("CDP 邻居 API", r.status_code == 200 and len(r.json()["neighbors"]) == 2)

        r = client.get("/api/status")
        check("GET /api/status", r.status_code == 200 and r.json()["devices"] == 5)

        r = client.put("/api/settings", json={"concurrency": 8, "keep_versions": 20})
        check("PUT /api/settings", r.status_code == 200 and r.json()["concurrency"] == 8)

        r = client.delete(f"/api/devices/{new_id}")
        check("DELETE 设备", r.status_code == 200 and r.json()["ok"])
        r = client.get("/api/devices/99999")
        check("不存在的设备返回 404", r.status_code == 404)

        r = client.get("/")
        check("GET / 返回前端页面", r.status_code == 200 and "网络配置备份中心" in r.text)
        # 静态资源版本号由后端按 mtime 注入，避免手工维护 ?v= 漏改导致缓存旧 JS
        check("JS 引用带自动版本号",
              re.search(r"/static/app\.js\?v=\d+", r.text) is not None,
              r.text[:200])
        check("CSS 引用带自动版本号",
              re.search(r"/static/style\.css\?v=\d+", r.text) is not None)

        # 为 core-sw-01 开启定时，供下一节断言调度器
        r = client.patch(
            f"/api/devices/{dev['id']}",
            json={"schedule_enabled": True, "cron": "30 3 * * *"},
        )
        check("开启 core-sw-01 定时备份", r.status_code == 200 and r.json()["schedule_enabled"])

        print("\n=== 8. 门店归类 ===")
        r = client.post("/api/sites", json={"name": "上海南京西路店", "code": "SH001", "region": "华东"})
        check("POST /api/sites 创建门店", r.status_code == 201, r.text[:150])
        site_sh = r.json()["id"]
        r = client.post("/api/sites", json={"name": "深圳南山科技园店", "code": "SZ002", "region": "华南"})
        site_sz = r.json()["id"]
        r = client.post("/api/sites", json={"name": "上海南京西路店"})
        check("同名门店被拒绝", r.status_code == 400)

        # 把 core-sw-01 归到上海店，access-sw-tl 归到深圳店
        r = client.patch(f"/api/devices/{dev['id']}", json={"site_id": site_sh})
        check("设备绑定门店", r.status_code == 200 and r.json()["site_id"] == site_sh)
        check("设备同步门店文本", r.json()["site"] == "上海南京西路店")
        r = client.patch(f"/api/devices/{dev_tl['id']}", json={"site_id": site_sz})
        check("第二台设备绑定另一门店", r.json()["site"] == "深圳南山科技园店")

        # 通过 site 文本自动建店
        r = client.patch(f"/api/devices/{wrong['id']}", json={"site": "北京朝阳门店"})
        check("按文本自动创建门店", r.json()["site_id"] is not None and r.json()["site"] == "北京朝阳门店")

        r = client.get("/api/sites")
        check("GET /api/sites 列出全部门店", r.status_code == 200 and len(r.json()) >= 3)
        sites = {s["name"]: s for s in r.json()}
        check("门店统计设备数", sites["上海南京西路店"]["device_count"] == 1)
        check("门店记录最近备份时间", sites["上海南京西路店"]["last_backup_at"] is not None)

        r = client.get(f"/api/devices?site_id={site_sh}")
        check("按门店过滤设备", r.status_code == 200 and len(r.json()) == 1)
        r = client.get("/api/devices?keyword=core")
        check("按关键字过滤设备", r.status_code == 200 and len(r.json()) == 1)
        r = client.get(f"/api/sites/{site_sh}")
        check("GET /api/sites/{id} 含设备明细", r.status_code == 200 and len(r.json()["devices"]) == 1)

        r = client.post(f"/api/sites/{site_sh}/backup")
        check("按门店批量备份", r.status_code == 200 and r.json()["queued"] == 1)

        print("\n=== 9. 打包导出 ===")
        import io as _io
        import zipfile as _zip

        r = client.post("/api/export/preview" if False else "/api/export/package",
                        json={"scope": "all"})
        check("POST /api/export/package 全量导出", r.status_code == 200, r.text[:150])
        check("导出返回 zip", r.content[:2] == b"PK")
        zf = _zip.ZipFile(_io.BytesIO(r.content))
        names = zf.namelist()
        check("包内含manifest.csv", any(n.endswith("manifest.csv") for n in names))
        check("包内含 README", any(n.endswith("README.txt") for n in names))
        check("目录按门店分层", any("/上海南京西路店/" in n for n in names), names[:5])
        check("目录按设备分层", any("/core-sw-01/" in n for n in names))
        check("目录按备份时间分层", any(
            __import__("re").search(r"/\d{8}-\d{6}/", n) for n in names
        ))
        check("包内含配置正文", any(
            n.endswith("show_running_config.txt") for n in names
        ))
        run_txt = next(n for n in names if n.endswith("show_running_config.txt"))
        check("配置内容非空", "hostname" in zf.read(run_txt).decode("utf-8"))
        manifest = next(n for n in names if n.endswith("manifest.csv"))
        csv_text = zf.read(manifest).decode("utf-8-sig")
        check("清单含表头", "门店" in csv_text and "SHA256" in csv_text)
        check("清单记录门店与设备", "上海南京西路店" in csv_text and "core-sw-01" in csv_text)

        r = client.post("/api/export/package", json={"scope": "site", "site_id": site_sz})
        check("按门店导出", r.status_code == 200)
        zf2 = _zip.ZipFile(_io.BytesIO(r.content))
        check("门店包只含本店设备",
              all("深圳南山科技园店" in n for n in zf2.namelist() if not n.endswith((".csv", ".txt"))),
              [n for n in zf2.namelist()][:4])

        r = client.post("/api/export/package",
                        json={"scope": "device", "device_ids": [dev["id"]]})
        zf3 = _zip.ZipFile(_io.BytesIO(r.content))
        check("按设备导出", r.status_code == 200 and
              any("core-sw-01" in n for n in zf3.namelist()))

        r = client.post("/api/export/package",
                        json={"scope": "all", "commands": ["show cdp neighbors detail"]})
        zf4 = _zip.ZipFile(_io.BytesIO(r.content))
        check("按命令过滤导出", all(
            n.endswith(("show_cdp_neighbors_detail.txt", ".csv", ".txt")) for n in zf4.namelist()
        ), [n for n in zf4.namelist() if not n.endswith((".csv", ".txt", ".diff"))][:4])

        r = client.get(f"/api/export/sites/{site_sh}/package")
        check("GET 门店快捷导出", r.status_code == 200 and r.content[:2] == b"PK")

        r = client.get("/api/export/preview?scope=all")
        check("GET 导出预览", r.status_code == 200 and r.json()["files"] > 0)
        check("预览统计门店数", r.json()["sites"] >= 2)

        r = client.delete(f"/api/sites/{site_sz}?reassign_to={site_sh}")
        check("删除门店并迁移设备", r.status_code == 200 and r.json()["moved_devices"] == 1)
        r = client.get(f"/api/devices/{dev_tl['id']}")
        check("设备已迁移到目标门店", r.json()["site_id"] == site_sh)

        r = client.get("/api/status")
        check("状态含门店数", r.status_code == 200 and r.json()["sites"] >= 2)

    
    print("\n=== 10. 调度器 ===")
    from app import scheduler

    with TestClient(app):
        jobs = [j for j in scheduler.scheduler.get_jobs() if j.id.startswith("device-backup:")]
        check("定时任务已注册", len(jobs) == 1, f"got {len(jobs)}")
        check("任务下次执行时间已计算", jobs[0].next_run_time is not None)
        db.update_device(dev["id"], {"schedule_enabled": False})
        scheduler.sync_schedule()
        jobs = [j for j in scheduler.scheduler.get_jobs() if j.id.startswith("device-backup:")]
        check("关闭定时后任务被移除", len(jobs) == 0)

    ssh_server.stop()
    tl.stop()
    shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'=' * 46}\n  通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 46}\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())