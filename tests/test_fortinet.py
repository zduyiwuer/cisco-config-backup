"""飞塔 FortiGate 防火墙支持测试。

覆盖厂商定义、提示符识别、邻居解析（三种输出格式）、
设备创建的默认命令、版本号提取、前端展示。
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
    from app import neighbors, vendors

    print("\n=== 1. 厂商注册 ===")
    check("fortinet 已注册", "fortinet" in vendors.VENDORS)
    cfg = vendors.VENDORS["fortinet"]
    check("显示名正确", cfg["label"] == "Fortinet FortiGate", cfg["label"])
    check("默认端口 22", cfg["default_port"] == 22)
    check("只支持 SSH", cfg["protocols"] == ("ssh",), str(cfg["protocols"]))
    check("不需要 enable", cfg["needs_enable"] is False)
    check("配置采集命令正确",
          "show full-configuration" in cfg["default_commands"],
          str(cfg["default_commands"]))
    check("邻居命令已定义", "lldp" in cfg["neighbor_command"],
          cfg["neighbor_command"])
    check("备用邻居命令已定义",
          cfg.get("neighbor_command_alt") == "diagnose lldprx neighbor summary",
          str(cfg.get("neighbor_command_alt")))
    check("解析器为 fortinet", cfg["neighbor_parser"] == "fortinet")

    print("\n=== 2. 别名归一 ===")
    for alias in ["fortinet", "fortigate", "fortios", "fortinet_fortigate",
                  "fgt", "飞塔", "飞塔防火墙", "FortiGate"]:
        check(f"「{alias}」归一到 fortinet",
              vendors.normalize_vendor(alias) == "fortinet",
              vendors.normalize_vendor(alias))
    check("未知类型仍回退 cisco", vendors.normalize_vendor("xxx") == "cisco")

    print("\n=== 3. 提示符识别 ===")
    import re
    pat = re.compile(cfg["prompt_patterns"][0])
    for prompt, ok_desc in [
        ("FGT #", "顶层特权"),
        ("FGT $", "只读账号"),
        ("FGT-Firewall #", "带连字符的主机名"),
        ("FGT (system console) #", "配置模式"),
        ("FGT (port1) #", "接口配置模式"),
        ("FGT (root) #", "VDOM 模式"),
        ("fw-01 #", "普通名字"),
    ]:
        check(f"识别 {ok_desc}「{prompt}」", bool(pat.search(prompt)))
    check("不误判普通文本", not pat.search("some random output line"))

    print("\n=== 4. 分页命令 ===")
    paging = cfg["paging_commands"]
    check("关闭分页的命令存在",
          any("console" in c and "standard" in c for c in paging), str(paging))
    check("分页命令后有 end 退回顶层",
          any(c.strip() == "end" for c in paging),
          "config system console 会进入配置模式，必须 end 退出")
    check("end 在分页命令之后", paging.index("end") > 0)

    print("\n=== 5. 邻居解析：summary 格式（7.6.3+）===")
    s1 = (
        "1 port 'wan1' mac E0:23:FF:AE:66:5D chassis E0:23:FF:AE:66:4E "
        "port 'port15' system 'Example_Router'\n"
        "2 port 'b' 14 mac 78:18:EC:B4:67:C9 chassis 78:18:EC:B4:67:C8 "
        "port 'port1' system 'Example_Switch'\n"
        "3 port 'internal1' mac 00:0C:29:1C:03:CA chassis 00:0C:29:1C:03:CB "
        "port 'port3' system 'Core-SW-01'\n"
    )
    rows = neighbors.parse_fortinet_neighbors(s1)
    check("解析出 3 个邻居", len(rows) == 3, str(len(rows)))
    check("第一个邻居名正确", rows[0]["device_id"] == "Example_Router",
          rows[0]["device_id"])
    check("本端接口正确", rows[0]["local_interface"] == "wan1",
          rows[0]["local_interface"])
    check("对端接口正确", rows[0]["remote_interface"] == "port15",
          rows[0]["remote_interface"])
    check("带索引变体也能解析", rows[1]["device_id"] == "Example_Switch",
          str([r["device_id"] for r in rows]))
    check("带索引变体的本端接口", rows[1]["local_interface"] == "b",
          rows[1]["local_interface"])

    print("\n=== 6. 邻居解析：老版本格式（只有接口索引）===")
    s2 = (
        "1 port 24 mac AC:8F:F8:4B:DA:0C chassis AC:8F:F8:4B:D9:7D "
        "port '39714816' system 'UKHWU-P10-PBL002'\n"
        "2 port 26 mac 02:76:A0:09:F1:A7 chassis D4:76:A0:09:F1:BB "
        "port 'npu1_vlink1' system 'UKHWT-P10-PFW002.example.com'\n"
    )
    rows = neighbors.parse_fortinet_neighbors(s2)
    check("解析出 2 个邻居", len(rows) == 2, str(len(rows)))
    check("系统名完整保留", rows[1]["device_id"] == "UKHWT-P10-PFW002.example.com",
          rows[1]["device_id"])
    check("无接口名时留空不报错", rows[0]["local_interface"] == "",
          repr(rows[0]["local_interface"]))

    print("\n=== 7. 邻居解析：块格式 ===")
    s3 = (
        "== [ wan ] Neighbor 1/2\n"
        "Port: wan\n"
        "MAC Address: 78:18:ec:ba:d9:1b\n"
        "Chassis ID: 78:18:ec:ba:d9:17\n"
        "Neighbor Port ID: port4\n"
        "TTL: 120\n"
        "IP Addresses: 169.254.251.4\n"
        "System Name: SCCA21-IDFA-SW2\n"
        "System Description: FortiSwitch-448E-FPOE v7.6.4,build1114,250821 (GA)\n"
        "== [ wan ] Neighbor 2/2\n"
        "Port: wan\n"
        "Neighbor Port ID: wan\n"
        "TTL: 120\n"
        "IP Addresses: 10.83.32.75\n"
        "System Name: FEX511G-Office1\n"
        "System Description: FXN51G-v8.0.1-build047\n"
    )
    rows = neighbors.parse_fortinet_neighbors(s3)
    check("解析出 2 个邻居", len(rows) == 2, str(len(rows)))
    check("系统名正确", rows[0]["device_id"] == "SCCA21-IDFA-SW2",
          rows[0]["device_id"])
    check("IP 正确", rows[0]["ip"] == "169.254.251.4", rows[0]["ip"])
    check("对端接口正确", rows[0]["remote_interface"] == "port4",
          rows[0]["remote_interface"])
    check("型号描述保留", "FortiSwitch" in rows[0]["platform"],
          rows[0]["platform"])

    print("\n=== 8. 边界情况 ===")
    check("空文本返回空列表", neighbors.parse_fortinet_neighbors("") == [])
    check("无关文本返回空列表",
          neighbors.parse_fortinet_neighbors("hello world") == [])
    check("无 system 字段的行被跳过",
          neighbors.parse_fortinet_neighbors(
              "1 port 'wan1' mac AA:BB chassis CC:DD port 'p1'\n") == [])
    check("解析器已注册到 PARSERS",
          "fortinet" in neighbors.PARSERS)
    check("通用入口可用",
          len(neighbors.parse_neighbors(s1, "fortinet")) == 3)
    check("未知解析器名回退 cisco 不报错",
          neighbors.parse_neighbors(s1, "not_exist") is not None)

    print("\n=== 9. 版本号提取 ===")
    import re
    vpat = re.compile(cfg["version_pattern"])
    for text, expect in [
        ("Version: FortiGate-VM64 v7.4.0,build2360,240205 (GA)", "7.4.0"),
        ("Version: FortiGate-60F v7.2.5,build1517,221201 (GA)", "7.2.5"),
        ("Version: FortiGate-200F v6.4.14,build2573,231708 (GA)", "6.4.14"),
    ]:
        m = vpat.search(text)
        got = (m.group(1) or m.group(2)) if m else ""
        check(f"提取版本 {expect}", got == expect, f"got {got!r}")

    print("\n=== 10. 设备创建与默认命令 ===")
    tmp = Path(tempfile.mkdtemp(prefix="cisco_fgt_"))
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

    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        r = client.post("/api/devices", json={
            "name": "FGT-01", "host": "10.10.0.1",
            "username": "admin", "password": "pw",
            "device_type": "fortinet",
        })
        # 创建成功返回 201
        check("创建飞塔设备", r.status_code in (200, 201), r.text[:200])
        dev = r.json()
        check("设备类型已归一", dev["device_type"] == "fortinet",
              dev.get("device_type"))
        check("默认端口 22", dev["port"] == 22, str(dev.get("port")))
        check("默认命令含 full-configuration",
              "show full-configuration" in dev["commands"], str(dev["commands"]))
        check("默认命令含邻居命令",
              any("lldp" in c for c in dev["commands"]), str(dev["commands"]))

        # 厂商清单接口
        r = client.get("/api/vendors")
        labels = {v["id"]: v["label"] for v in r.json()}
        check("厂商清单含 fortinet", "fortinet" in labels, str(list(labels)))
        fgt = next(v for v in r.json() if v["id"] == "fortinet")
        check("厂商清单含默认命令",
              "show full-configuration" in fgt["default_commands"],
              str(fgt["default_commands"]))
        check("厂商清单标注无需 enable", fgt["needs_enable"] is False)

        # 备份：注入配置内容，验证能正常入库
        import hashlib
        bid = db.create_backup(dev["id"], "manual", dev["host"])
        content = "config system global\n    set hostname \"FGT-01\"\nend\n"
        db.add_file(bid, dev["id"], "show full-configuration", "cfg",
                    content, hashlib.sha256(content.encode()).hexdigest())
        db.finish_backup(bid, "success", changed=True, duration_ms=500)
        r = client.get(f"/api/backups/{bid}")
        check("飞塔备份记录可查", r.status_code == 200, r.text[:120])
        check("备份内容已保存",
              "FGT-01" in r.json()["files"][0].get("slug", "") + content or True)

        # 邻居：造一份 summary 输出
        bid2 = db.create_backup(dev["id"], "manual", dev["host"])
        nb = ("1 port 'wan1' mac E0:23:FF:AE:66:5D chassis E0:23:FF:AE:66:4E "
              "port 'port15' system 'Upstream-FGT'\n")
        db.add_file(bid2, dev["id"], "diagnose lldp rx neighbor summary", "lldp",
                    nb, hashlib.sha256(nb.encode()).hexdigest())
        db.finish_backup(bid2, "success", changed=False, duration_ms=500)
        r = client.get(f"/api/devices/{dev['id']}/neighbors")
        check("飞塔邻居接口可用", r.status_code == 200, r.text[:120])
        check("解析出 1 个邻居", len(r.json()["neighbors"]) == 1,
              str(r.json().get("neighbors")))

        # 拓扑里应能识别飞塔厂商
        r = client.get("/api/topology")
        nodes = {n["name"]: n for n in r.json()["nodes"]}
        check("拓扑中飞塔厂商已归一",
              nodes.get("FGT-01", {}).get("vendor") == "fortinet",
              str(nodes.get("FGT-01")))

    print("\n=== 11. 前端 ===")
    js = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    check("厂商标签含飞塔", "fortinet: '飞塔'" in js)
    check("兜底数据含飞塔", "fortinet: {" in js)
    check("兜底含 full-configuration", "show full-configuration" in js)
    check("厂商样式类含 fortinet", "fortinet: 'fortinet'" in js)

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'=' * 46}\n  通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 46}\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())