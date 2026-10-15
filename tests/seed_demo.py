"""造演示数据：两家门店 + 若干设备 + 真实 mock 备份，便于查看界面与打包效果。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import collector, db
from tests import mock_device as m

SITES = [
    ("上海南京西路店", "SH001", "华东"),
    ("深圳南山科技园店", "SZ002", "华南"),
    ("北京朝阳门店", "BJ003", "华北"),
]

# (设备名, 门店索引, 协议)
DEVICES = [
    ("sh-core-sw-01", 0, "ssh"),
    ("sh-access-sw-02", 0, "ssh"),
    ("sh-edge-rtr-03", 0, "telnet"),
    ("sz-core-sw-01", 1, "ssh"),
    ("sz-access-sw-02", 1, "telnet"),
    ("bj-store-sw-01", 2, "ssh"),
]


def main() -> int:
    db.init_db()
    sites = []
    for name, code, region in SITES:
        existing = db.get_site_by_name(name)
        sites.append(existing or db.create_site(
            {"name": name, "code": code, "region": region}
        ))
    print(f"门店: {len(sites)} 个")

    ssh = m.MockSSHServer()
    ssh.start(); ssh.ready.wait(5); time.sleep(0.4)
    tl = m.MockTelnetServer()
    tl.start(); tl.ready.wait(5); time.sleep(0.3)

    created = 0
    for name, site_idx, protocol in DEVICES:
        if db.get_device_by_name(name):
            continue
        server = ssh if protocol == "ssh" else tl
        db.create_device({
            "name": name,
            "host": server.host,
            "port": server.port,
            "protocol": protocol,
            "username": m.USERNAME,
            "password": m.PASSWORD,
            "enable_password": m.ENABLE_PASSWORD,
            "site_id": sites[site_idx]["id"],
            "commands": ["show running-config", "show cdp neighbors detail"],
        })
        created += 1
    print(f"新增设备: {created} 台")

    devices = [d for d in db.list_devices() if d["site_id"]]
    print(f"开始备份 {len(devices)} 台设备…")
    results = collector.backup_devices([d["id"] for d in devices], trigger="manual")
    ok = sum(1 for r in results if r["status"] == "success")
    print(f"备份成功 {ok}/{len(results)}")

    # 第二轮：制造配置变化，验证 diff 与新版本
    m.mutate()
    print("制造一次配置变更后再次备份…")
    results2 = collector.backup_devices([d["id"] for d in devices], trigger="schedule")
    changed = sum(1 for r in results2 if r.get("changed"))
    print(f"检测到变化: {changed} 台")

    ssh.stop(); tl.stop()
    print("\n演示数据准备完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())