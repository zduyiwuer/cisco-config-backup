"""多厂商端到端测试：H3C / 华为 / 锐捷 走完整备份链路。

用 mock 设备（真实 SSH 握手、Telnet IAC 协商、各厂商提示符与命令）
验证备份、diff、邻居解析、版本号提取全流程。
"""

from __future__ import annotations

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


VENDOR_CASES = [
    {
        "vendor": "h3c",
        "device_type": "h3c_comware",
        "label": "H3C",
        "protocol": "ssh",
        "expect_cmd": "display current-configuration",
        "neighbor_cmd": "display lldp neighbor-information",
        "expect_cfg": "sysname H3C-CORE-SW",
        "expect_neighbor": "H3C-ACCESS-SW",
        "expect_neighbor_ip": "10.1.1.3",
        "expect_version": "7.1.070",
    },
    {
        "vendor": "huawei",
        "device_type": "huawei_vrp",
        "label": "华为",
        "protocol": "ssh",
        "expect_cmd": "display current-configuration",
        "neighbor_cmd": "display lldp neighbor brief",
        # 华为 mock 未提供 config 输出，配置内容退化为错误提示，只验证命令与邻居
        "expect_cfg": None,
        "expect_neighbor": "HUAWEI-CORE-02",
        "expect_neighbor_ip": "",
        "expect_version": None,
    },
    {
        "vendor": "h3c",
        "device_type": "h3c_comware",
        "label": "H3C-Telnet",
        "protocol": "telnet",
        "expect_cmd": "display current-configuration",
        "neighbor_cmd": "display lldp neighbor-information",
        "expect_cfg": "sysname H3C-CORE-SW",
        "expect_neighbor": "H3C-ACCESS-SW",
        "expect_neighbor_ip": "10.1.1.3",
        "expect_version": "7.1.070",
    },
    {
        "vendor": "ruijie",
        "device_type": "ruijie",
        "label": "锐捷",
        "protocol": "telnet",
        "expect_cmd": "show running-config",
        "neighbor_cmd": "show lldp neighbors",
        "expect_cfg": None,
        "expect_neighbor": "RG-CORE-02",
        "expect_neighbor_ip": "",
        "expect_version": None,
    },
]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cisco_vendor_"))
    from app import collector, crypto, db, neighbors, vendors
    from tests import mock_device as m

    crypto.DATA_DIR = tmp
    crypto.KEY_PATH = tmp / ".secret_key"
    crypto._key()
    crypto._fernet.__init__(crypto.KEY_PATH.read_bytes())
    db.DATA_DIR = tmp
    db.DB_PATH = tmp / "t.db"
    db._local.__dict__.clear()
    db.init_db()
    db.update_settings({"auth_enabled": False})

    # 每种厂商起一台mock 设备
    servers: dict[str, object] = {}
    print("\n=== 1. 启动多厂商 mock 设备 ===")
    for vendor in ("h3c", "huawei", "ruijie"):
        ssh = m.MockSSHServer(vendor=vendor)
        ssh.start(); ssh.ready.wait(5); time.sleep(0.3)
        servers[f"{vendor}-ssh"] = ssh
        tl = m.MockTelnetServer(vendor=vendor)
        tl.start(); tl.ready.wait(5); time.sleep(0.2)
        servers[f"{vendor}-telnet"] = tl
    check("启动 6 台 mock 设备", len(servers) == 6)

    print("\n=== 2. 厂商识别与命令集 ===")
    check("H3C 厂商归一", vendors.normalize_vendor("h3c_comware") == "h3c")
    check("华为厂商归一", vendors.normalize_vendor("huawei_vrp") == "huawei")
    check("H3C 默认端口", vendors.default_port_for("h3c_comware") == 22)
    check("H3C 分页命令为 screen-length disable",
          vendors.vendor_config("h3c")["paging_commands"][0] == "screen-length disable")
    check("华为分页命令为 screen-length 0 temporary",
          vendors.vendor_config("huawei")["paging_commands"][0] == "screen-length 0 temporary")
    check("锐捷命令为 show 风格",
          "show running-config" in vendors.default_commands_for("ruijie"))

    print("\n=== 3. 各厂商备份全链路 ===")
    results = {}
    for idx, case in enumerate(VENDOR_CASES):
        name = f"{case['vendor']}-{case['protocol']}-{idx}"
        key = f"{case['vendor']}-{case['protocol']}"
        server = servers[key]
        dev = db.create_device({
            "name": name,
            "host": server.host,
            "port": server.port,
            "protocol": case["protocol"],
            "username": m.USERNAME,
            "password": m.PASSWORD,
            "enable_password": m.ENABLE_PASSWORD,
            "device_type": case["device_type"],
            "commands": vendors.default_commands_for(case["device_type"]),
            "note": case["label"],
        })
        result = collector.backup_device(dev["id"], "manual")
        results[name] = (dev["id"], result)

        check(f"{case['label']} 备份成功", result["status"] == "success",
              str(result.get("error")))
        if result["status"] != "success":
            continue

        detail = db.get_backup(result["backup_id"])
        cmds = [f["command"] for f in detail["files"]]
        check(f"{case['label']} 执行了厂商配置命令",
              case["expect_cmd"] in cmds, str(cmds))
        check(f"{case['label']} 执行了厂商邻居命令",
              case["neighbor_cmd"] in cmds, str(cmds))

        if case["expect_cfg"]:
            cfg_file = next(
                (f for f in detail["files"] if "current-configuration" in f["command"]), None
            )
            if cfg_file:
                content = db.get_file_content(cfg_file["id"])["content"]
                check(f"{case['label']} 配置内容完整",
                      case["expect_cfg"] in content, content[:80])
                check(f"{case['label']} 无命令回显残留",
                      not content.strip().startswith(case["expect_cmd"]))
                check(f"{case['label']} 无提示符残留", ">" not in content.split("\n")[0])

        if case["expect_version"]:
            check(f"{case['label']} 版本号提取正确",
                  detail["os_version"] == case["expect_version"],
                  str(detail["os_version"]))

    print("\n=== 4. 多厂商邻居解析 ===")
    for idx, case in enumerate(VENDOR_CASES):
        name = f"{case['vendor']}-{case['protocol']}-{idx}"
        if name not in results or results[name][1]["status"] != "success":
            continue
        dev_id = results[name][0]
        rows = collector.neighbors_of(dev_id)
        check(f"{case['label']} 解析出邻居", len(rows) >= 1, f"got {len(rows)}")
        if rows and case["expect_neighbor"]:
            ids = [r["device_id"] for r in rows]
            check(f"{case['label']} 邻居设备名正确",
                  case["expect_neighbor"] in ids, str(ids))
            check(f"{case['label']} 邻居有本端接口",
                  all(r["local_interface"] for r in rows))
            if case["expect_neighbor_ip"]:
                hit = next(r for r in rows if r["device_id"] == case["expect_neighbor"])
                check(f"{case['label']} 邻居 IP 正确",
                      hit["ip"] == case["expect_neighbor_ip"], hit["ip"])

    print("\n=== 5. 多厂商 diff（配置变更） ===")
    m.mutate()  # 切换 H3C 配置到 V2
    h3c_dev = db.get_device_by_name("h3c-ssh-0")
    if h3c_dev:
        r2 = collector.backup_device(h3c_dev["id"], "manual")
        check("H3C 二次备份成功", r2["status"] == "success", str(r2.get("error")))
        check("检测到配置变化", r2.get("changed") is True)
        d2 = db.get_backup(r2["backup_id"])
        cfg = next((f for f in d2["files"] if "current-configuration" in f["command"]), None)
        if cfg:
            diff = db.get_diff(cfg["id"])
            check("生成了 diff", diff is not None)
            if diff:
                check("diff 含变更内容",
                      "uplink-to-core-printer" in diff["diff_text"],
                      diff["diff_text"][:150])
                check("diff 有增删统计", diff["added"] > 0, str(diff["added"]))

    print("\n=== 6. API 层厂商接口 ===")
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        r = client.get("/api/vendors")
        check("GET /api/vendors", r.status_code == 200)
        vlist = r.json()
        check("返回 4 个厂商", len(vlist) == 4, str(len(vlist)))
        ids = {v["id"] for v in vlist}
        check("厂商 id 齐全",
              {"cisco", "h3c", "huawei", "ruijie"} <= ids, str(ids))
        h3c = next(v for v in vlist if v["id"] == "h3c")
        check("H3C 默认命令正确",
              "display current-configuration" in h3c["default_commands"],
              str(h3c["default_commands"]))
        check("H3C 标记为无需 enable", h3c["needs_enable"] is False)
        cisco = next(v for v in vlist if v["id"] == "cisco")
        check("Cisco 标记为需要 enable", cisco["needs_enable"] is True)

        # 新建设备时不指定命令，应按厂商自动套用
        r = client.post("/api/devices", json={
            "name": "api-h3c-test", "host": "10.20.0.1", "protocol": "ssh",
            "username": "admin", "password": "pw", "device_type": "h3c_comware",
        })
        check("POST 设备成功", r.status_code == 201, r.text[:150])
        if r.status_code == 201:
            dev = r.json()
            check("厂商被归一为 h3c", dev["device_type"] == "h3c", dev["device_type"])
            check("自动套用 H3C 命令",
                  "display current-configuration" in dev["commands"],
                  str(dev["commands"]))
            check("端口自动补全", dev["port"] == 22, str(dev["port"]))

            # 改厂商时应同步命令（h3c 与 huawei 配置命令相同，
            # 但邻居命令不同：h3c 用 neighbor-information，华为用 neighbor brief）
            r = client.patch(f"/api/devices/{dev['id']}",
                             json={"device_type": "huawei"})
            check("改厂商成功", r.status_code == 200)
            if r.status_code == 200:
                check("厂商已更新", r.json()["device_type"] == "huawei",
                      r.json()["device_type"])
                check("命令随厂商更新为华为邻居命令",
                      "display lldp neighbor brief" in r.json()["commands"],
                      str(r.json()["commands"]))

        # 设备列表带厂商信息
        r = client.get("/api/devices")
        check("设备列表含厂商字段",
              all("device_type" in d for d in r.json()))

        # 邻居 API
        if h3c_dev:
            r = client.get(f"/api/devices/{h3c_dev['id']}/neighbors")
            check("邻居 API 可用", r.status_code == 200 and len(r.json()["neighbors"]) >= 1)

    for s in servers.values():
        s.stop()
    shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'=' * 46}\n  通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 46}\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())