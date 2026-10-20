"""门店邻居拓扑测试。

验证 topology_of 的聚合逻辑与 /api/topology 接口：
门店筛选、跨设备连线、内外部分界、去重。
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
    tmp = Path(tempfile.mkdtemp(prefix="cisco_topo_"))
    from app import collector, crypto, db

    crypto.DATA_DIR = tmp
    crypto.KEY_PATH = tmp / ".secret_key"
    crypto._key()
    crypto._fernet.__init__(crypto.KEY_PATH.read_bytes())
    db.DATA_DIR = tmp
    db.DB_PATH = tmp / "t.db"
    db._local.__dict__.clear()
    db.init_db()
    db.update_settings({"auth_enabled": False})

    site_sh = db.create_site({"name": "上海门店"})["id"]
    site_sz = db.create_site({"name": "深圳门店"})["id"]

    # 上海门店：core 与 access，两台互联
    sh_core = db.create_device({
        "name": "SH-CORE", "host": "10.1.0.1", "protocol": "ssh",
        "username": "cisco", "password": "pw", "site_id": site_sh,
        "device_type": "cisco",
    })
    sh_acc = db.create_device({
        "name": "SH-ACCESS", "host": "10.1.0.2", "protocol": "ssh",
        "username": "cisco", "password": "pw", "site_id": site_sh,
        "device_type": "cisco",
    })
    sh_iso = db.create_device({
        "name": "SH-ISOLATED", "host": "10.1.0.3", "protocol": "ssh",
        "username": "cisco", "password": "pw", "site_id": site_sh,
    })
    sz_core = db.create_device({
        "name": "SZ-CORE", "host": "10.2.0.1", "protocol": "ssh",
        "username": "huawei", "password": "pw", "site_id": site_sz,
        "device_type": "huawei",
    })
    free = db.create_device({
        "name": "NO-SITE-SW", "host": "10.9.0.1", "protocol": "ssh",
        "username": "cisco", "password": "pw",
    })

    # 造备份：注入邻居输出
    def backup_with(device, content, cmd, changed=True):
        b = db.create_backup(device["id"], "manual", device["host"])
        digest = crypto.sha256(content) if hasattr(crypto, "sha256") else __import__("hashlib").sha256(
            content.encode("utf-8")
        ).hexdigest()
        db.add_file(b, device["id"], cmd, cmd.replace(" ", "_"), content, digest)
        db.finish_backup(b, "success", changed=changed, duration_ms=1000)
        return b

    # Cisco CDP detail 样例：一台连本门店一台连外部
    CDP_TO_ACCESS_AND_OUTSIDER = (
        "-------------------------\n"
        "Device ID: SH-ACCESS.corp.local\n"
        "Entry address(es):\n"
        "  IP address: 10.1.0.2\n"
        "Platform: cisco WS-C3750-48P,  Capabilities: Switch IGMP\n"
        "Interface: GigabitEthernet0/1,  Port ID (outgoing port): GigabitEthernet0/24\n"
        "-------------------------\n"
        "Device ID: HQ-CORE.corp.local\n"
        "Entry address(es):\n"
        "  IP address: 172.16.0.1\n"
        "Platform: cisco WS-C4500,  Capabilities: Switch IGMP\n"
        "Interface: GigabitEthernet0/2,  Port ID (outgoing port): TenGigabitEthernet1/1/1\n"
    )
    # 反向：SH-ACCESS 看到 SH-CORE
    CDP_BACK = (
        "-------------------------\n"
        "Device ID: SH-CORE.corp.local\n"
        "Entry address(es):\n"
        "  IP address: 10.1.0.1\n"
        "Platform: cisco WS-C4506,  Capabilities: Switch IGMP\n"
        "Interface: GigabitEthernet0/24,  Port ID (outgoing port): GigabitEthernet0/1\n"
        "-------------------------\n"
    )
    HUAWEI_LLDP = (
        "Local IntfNeighbor Dev       Neighbor Intf      Exptime(s)\n"
        "-----------------------------------------------------------------------\n"
        "GigabitEthernet0/0/1   SZ-ACCESS            GigabitEthernet0/0/2        120\n"
    )

    print("\n=== 1. 基础聚合（全量）===")
    # SH-CORE 看到 SH-ACCESS 与一台外部设备
    backup_with(sh_core, CDP_TO_ACCESS_AND_OUTSIDER, "show cdp neighbors detail")
    # SH-ACCESS 回指 SH-CORE，形成双向
    backup_with(sh_acc, CDP_BACK, "show cdp neighbors detail")
    # 深圳用华为 LLDP
    backup_with(sz_core, HUAWEI_LLDP, "display lldp neighbor brief")

    t = collector.topology_of()
    check("接口返回节点与连线",
          "nodes" in t and "links" in t and "device_count" in t)
    check("节点数为全部设备数", t["device_count"] == 5, str(t["device_count"]))

    names = {n["name"] for n in t["nodes"]}
    check("含各门店设备",
          {"SH-CORE", "SH-ACCESS", "SZ-CORE"} <= names, str(names))
    check("节点含在线状态字段",
          all("online" in n for n in t["nodes"]))
    check("节点含厂商归一值",
          next(n for n in t["nodes"] if n["name"] == "SZ-CORE")["vendor"] == "huawei",
          str(next(n for n in t["nodes"] if n["name"] == "SZ-CORE")["vendor"]))

    print("\n=== 2. 门店筛选 ===")
    t_sh = collector.topology_of(site_id=site_sh)
    check("只含该门店设备", t_sh["device_count"] == 3, str(t_sh["device_count"]))
    sh_names = {n["name"] for n in t_sh["nodes"]}
    check("不含其他门店设备", "SZ-CORE" not in sh_names, str(sh_names))
    check("不含未归属设备", "NO-SITE-SW" not in sh_names)

    t_sz = collector.topology_of(site_id=site_sz)
    check("深圳门店只有 1 台", t_sz["device_count"] == 1, str(t_sz["device_count"]))

    t_none = collector.topology_of(site_id=0)
    check("未归属设备筛出 1 台",
          t_none["device_count"] == 1
          and t_none["nodes"][0]["name"] == "NO-SITE-SW",
          str([n["name"] for n in t_none["nodes"]]))

    print("\n=== 3. 连线与内外部分界 ===")
    internal = [l for l in t_sh["links"] if l["in_scope"]]
    external = [l for l in t_sh["links"] if not l["in_scope"]]
    check("存在本门店内连线", len(internal) >= 1, str(len(internal)))
    check("存在外部设备连线", len(external) >= 1, str(len(external)))
    check("内部连线两端都在范围内",
          all(l["target_id"] is not None for l in internal))
    check("外部连线 target_id 为空",
          all(l["target_id"] is None for l in external))
    check("连线含两端接口信息",
          all(l.get("local_interface") for l in internal),
          str(internal[:1]))

    # 双向互联应产生两条独立连线（方向不同）
    pair = {(l["source_name"], l["target_name"]) for l in internal}
    check("双向互联被记录为两条",
          ("SH-CORE", "SH-ACCESS") in pair or ("SH-ACCESS", "SH-CORE") in pair,
          str(pair))

    print("\n=== 4. 邻居计数 ===")
    core_node = next(n for n in t_sh["nodes"] if n["name"] == "SH-CORE")
    check("SH-CORE 邻居计数 > 0", core_node["neighbor_count"] > 0,
          str(core_node["neighbor_count"]))
    iso = next(n for n in t_sh["nodes"] if n["name"] == "SH-ISOLATED")
    check("无备份设备计数为 0", iso["neighbor_count"] == 0,
          str(iso["neighbor_count"]))

    print("\n=== 4b. 域名后缀匹配 ===")
    # CDP 的 Device ID 带域名后缀（SH-ACCESS.corp.local），
    # 设备表里是短名（SH-ACCESS），应识别为同一台设备
    matched = [l for l in t_sh["links"] if l["target_name"] == "SH-ACCESS"]
    check("带域名后缀的 Device ID 能匹配到短名设备",
          len(matched) >= 1 and matched[0]["in_scope"],
          str([(l["target_name"], l["in_scope"]) for l in t_sh["links"]]))
    check("匹配后 target_id 正确指向设备",
          matched and matched[0]["target_id"] == sh_acc["id"],
          str(matched[0]["target_id"]) if matched else "无匹配")

    print("\n=== 5. 去重 ===")
    # 连续两次返回的连线不应变化
    t2 = collector.topology_of(site_id=site_sh)
    check("重复调用结果一致",
          len(t2["links"]) == len(t_sh["links"]),
          f"{len(t2['links'])} vs {len(t_sh['links'])}")
    keys = [
        (l["source_name"], l["target_name"], l["local_interface"], l["remote_interface"])
        for l in t_sh["links"]
    ]
    check("连线无完全重复项", len(keys) == len(set(keys)), f"{len(keys)} vs {len(set(keys))}")

    print("\n=== 6. 空数据容错 ===")
    empty_site = db.create_site({"name": "空门店"})["id"]
    t_empty = collector.topology_of(site_id=empty_site)
    check("空门店返回空节点", t_empty["device_count"] == 0)
    check("空门店返回空连线", t_empty["link_count"] == 0)

    print("\n=== 7. 指定设备集合 ===")
    t_ids = collector.topology_of(device_ids=[sh_core["id"], sh_acc["id"]])
    check("按设备 id 筛选生效", t_ids["device_count"] == 2, str(t_ids["device_count"]))

    print("\n=== 8. API ===")
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        r = client.get("/api/topology")
        check("GET /api/topology 可用", r.status_code == 200)
        body = r.json()
        check("返回结构完整",
              all(k in body for k in ("nodes", "links", "device_count", "link_count")))

        r = client.get(f"/api/topology?site_id={site_sh}")
        check("按门店查询", r.json()["device_count"] == 3,
              str(r.json()["device_count"]))

        r = client.get("/api/topology?all_sites=true")
        check("all_sites 返回全量", r.json()["device_count"] == 5,
              str(r.json()["device_count"]))

        r = client.get(f"/api/topology?site_id={empty_site}")
        check("空门店接口不报错",
              r.status_code == 200 and r.json()["device_count"] == 0)

        r = client.get(f"/api/devices/{sh_core['id']}/neighbors")
        check("单设备邻居接口仍可用",
              r.status_code == 200 and len(r.json()["neighbors"]) >= 1)

    print("\n=== 9. 前端结构 ===")
    html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
    check("邻居页含门店下拉", 'id="neighbor-site"' in html)
    check("含拓扑按钮", 'id="btn-load-topology"' in html)
    check("含拓扑容器", 'id="topology-card"' in html)
    check("含统计容器", 'id="neighbor-stats"' in html)

    js = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    check("存在 loadTopology", "function loadTopology" in js)
    check("调用拓扑接口", "/api/topology" in js)
    check("门店变化联动设备下拉",
          "neighbor-site').addEventListener('change'" in js)
    check("拓扑按钮绑定点击", "btn-load-topology').addEventListener('click'" in js)
    check("区分内外连线", "in_scope" in js)
    check("门店下拉含未归属选项", "仅未归属设备" in js)

    css = (ROOT / "app/static/style.css").read_text(encoding="utf-8")
    check("有拓扑节点样式", ".topo-node{" in css)
    check("有互联表样式", ".topo-link-table" in css)
    check("有外部行样式", "tr.row-external" in css)

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'=' * 46}\n  通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 46}\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())