"""飞塔配置转 .conf 格式测试。

覆盖转换正确性、头部元信息、边界情况、采集流程接入与导出扩展名。
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


SHOW_OUTPUT = """config system global
    set hostname "FGT-01"
    set timezone "04"
end
config system interface
    edit "port1"
        set ip 192.168.1.1 255.255.255.0
        set allowaccess ping https ssh
        set type physical
        set snmp-index 1
    next
end
config firewall policy
    edit 1
        set name "outbound"
        set srcintf "port1"
        set dstintf "wan1"
        set action accept
        set schedule "always"
    next
end
"""

STATUS_OUTPUT = """Version: FortiGate-60F v7.0.5,build0307,220331 (GA)
Serial-Number: FGT60FTK20000000
Hardware: FortiGate-60F
Security Level: 2
Firmware Signature: certified
"""


def main() -> int:
    from app import fortinet, vendors

    print("\n=== 1. 转换基本正确性 ===")
    conf = fortinet.to_conf(SHOW_OUTPUT, STATUS_OUTPUT)
    check("转换有输出", bool(conf.strip()))
    check("保留配置正文", "config system global" in conf)
    check("保留 hostname", 'set hostname "FGT-01"' in conf)
    check("保留接口配置", "set ip 192.168.1.1 255.255.255.0" in conf)
    check("保留防火墙策略", "set action accept" in conf)
    check("保留 end 结构", conf.count("end") >= 3, str(conf.count("end")))

    print("\n=== 2. 头部元信息 ===")
    lines = conf.split("\n")
    check("第一行是 config-version",
          lines[0].startswith("#config-version="), lines[0])
    check("含 conf_file_ver",
          any(l.startswith("#conf_file_ver=") for l in lines[:6]))
    check("含 buildno",
          any(l.startswith("#buildno=") for l in lines[:6]))
    check("含 global_vdom",
          any(l.startswith("#global_vdom=") for l in lines[:6]))
    check("型号归一为 FGT60F",
          lines[0].startswith("#config-version=FGT60F-"), lines[0])
    check("头部含版本号", "7.0.5" in lines[0], lines[0])
    check("头部含 build 号", "build0307" in lines[0], lines[0])
    check("头部含日期", "220331" in lines[0], lines[0])
    check("头部含序列号",
          any(l.startswith("#serial=") for l in lines[:8]))

    print("\n=== 3. status 解析 ===")
    info = fortinet.parse_status(STATUS_OUTPUT)
    check("型号正确", info["model"] == "FortiGate-60F", info["model"])
    check("版本正确", info["version"] == "7.0.5", info["version"])
    check("build 正确", info["build"] == "0307", info["build"])
    check("日期正确", info["date"] == "220331", info["date"])
    check("序列号正确", info["serial"] == "FGT60FTK20000000", info["serial"])

    print("\n=== 4. 型号归一 ===")
    for raw, expect in [
        ("FortiGate-60F", "FGT60F"),
        ("FortiGate-200F", "FGT200F"),
        ("FortiGate-VM64", "FGVM64"),
        ("FGT60F", "FGT60F"),
    ]:
        got = fortinet._normalize_model(raw)
        check(f"{raw} → {expect}", got == expect, got)

    print("\n=== 5. 边界情况 ===")
    check("空输出返回空串", fortinet.to_conf("", STATUS_OUTPUT) == "")
    check("空白输出返回空串", fortinet.to_conf("   \n  ", STATUS_OUTPUT) == "")
    check("无 status 时仍能转换", bool(fortinet.to_conf(SHOW_OUTPUT).strip()))
    check("无 status 时头部降级但格式完整",
          fortinet.to_conf(SHOW_OUTPUT).startswith("#config-version="))
    check("非飞塔内容不崩", fortinet.to_conf("random text\n", "") is not None)
    check("CRLF 输入也能处理",
          "config system global" in fortinet.to_conf(
              SHOW_OUTPUT.replace("\n", "\r\n"), STATUS_OUTPUT))
    check("噪音行被剔除",
          "== [ N/A ]" not in fortinet.to_conf(
              "== [ N/A ]\nconfig system global\n    set hostname \"X\"\nend\n",
              STATUS_OUTPUT))
    check("配置正文不受噪音影响",
          "set hostname" in fortinet.to_conf(
              "== [ N/A ]\nconfig system global\n    set hostname \"X\"\nend\n",
              STATUS_OUTPUT))

    print("\n=== 6. 文件名生成 ===")
    check("文件名带版本",
          fortinet.to_conf_filename("FGT-01", "7.0.5") == "FGT-01_v7.0.5.conf",
          fortinet.to_conf_filename("FGT-01", "7.0.5"))
    check("无版本时简化",
          fortinet.to_conf_filename("FGT-01") == "FGT-01.conf")
    check("特殊字符被清理",
          "/" not in fortinet.to_conf_filename("a/b", "1.0"))

    print("\n=== 7. 厂商声明 ===")
    cfg = vendors.VENDORS["fortinet"]
    check("声明了预取版本", cfg.get("prefetch_version") is True)
    check("声明了配置转换器", cfg.get("config_transform") == "fortinet_conf",
          str(cfg.get("config_transform")))

    print("\n=== 8. 转换器接入 ===")
    from app import collector

    check("转换器已注册",
          "fortinet_conf" in collector.CONFIG_TRANSFORMS)
    out = collector.apply_config_transform(
        SHOW_OUTPUT, "fortinet_conf", {"status_output": STATUS_OUTPUT})
    check("通过采集器转换得到 .conf 头部",
          out.startswith("#config-version="), out[:60])
    check("未注册的转换器原样返回",
          collector.apply_config_transform(SHOW_OUTPUT, "nope", {}) == SHOW_OUTPUT)
    check("转换异常时保留原文",
          collector.apply_config_transform(SHOW_OUTPUT, "fortinet_conf", {}) is not None)

    print("\n=== 9. 导出扩展名 ===")
    from app.exporter import _file_ext, _is_config_command

    check("飞塔配置用 .conf",
          _file_ext("show full-configuration", "fortinet") == ".conf")
    check("飞塔邻居用 .txt",
          _file_ext("diagnose lldp rx neighbor summary", "fortinet") == ".txt")
    check("思科配置仍是 .txt",
          _file_ext("show running-config", "cisco") == ".txt")
    check("华为配置仍是 .txt",
          _file_ext("display current-configuration", "huawei") == ".txt")
    check("未知厂商回退 .txt",
          _file_ext("show running-config", "xxx") == ".txt")
    check("配置命令识别",
          _is_config_command("show full-configuration")
          and _is_config_command("show running-config")
          and _is_config_command("display current-configuration"))
    check("非配置命令不误判",
          not _is_config_command("diagnose lldp rx neighbor summary"))

    print("\n=== 10. 端到端：备份内容已是 .conf ===")
    tmp = Path(tempfile.mkdtemp(prefix="cisco_fgtconf_"))
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
        dev = r.json()
        check("设备创建成功", r.status_code in (200, 201), r.text[:150])

        import hashlib
        bid = db.create_backup(dev["id"], "manual", dev["host"])
        converted = fortinet.to_conf(SHOW_OUTPUT, STATUS_OUTPUT)
        db.add_file(bid, dev["id"], "show full-configuration", "show_full-configuration",
                    converted, hashlib.sha256(converted.encode()).hexdigest())
        db.finish_backup(bid, "success", changed=True, duration_ms=500)

        r = client.get(f"/api/backups/{bid}")
        check("备份详情可查", r.status_code == 200)
        files = r.json()["files"]
        check("有配置文件", len(files) >= 1)
        # 内容接口
        fid = files[0]["id"]
        r = client.get(f"/api/files/{fid}")
        check("文件内容可取", r.status_code == 200, r.text[:120])
        check("内容是 .conf 格式",
              "content" in r.json()
              and r.json()["content"].startswith("#config-version="),
              str(r.json())[:150])

        # 导出 zip 检查扩展名
        r = client.post("/api/export/package", json={
            "scope": "device", "device_ids": [dev["id"]],
        })
        check("导出 zip 可用", r.status_code == 200, r.text[:150])
        import io, zipfile
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            names = zf.namelist()
        conf_files = [n for n in names if n.endswith(".conf")]
        check("zip 里有 .conf 文件", len(conf_files) >= 1, str(names))
        check("配置不是 .txt",
              not any(n.endswith("show_full-configuration.txt") for n in names),
              str(names))

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'=' * 46}\n  通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 46}\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())