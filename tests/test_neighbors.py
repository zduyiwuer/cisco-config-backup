"""多厂商邻居解析专项测试：使用各厂商真实输出格式的样例。"""

from __future__ import annotations

import sys
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


# ------------------- 各厂商真实输出样例 -------------------
CISCO_CDP = """
-------------------------
Device ID: SW-CORE-02.corp.local
Entry address(es):
  IP address: 10.1.1.3
Platform: cisco WS-C3750-48P,  Capabilities: Switch IGMP
Interface: GigabitEthernet1/0/24,  Port ID (outgoing port): GigabitEthernet0/24
-------------------------
Device ID: AP-FLOOR3-01.corp.local
Entry address(es):
  IP address: 10.1.3.50
Platform: cisco AIR-AP2802I-E,  Capabilities: Bridge AirStation
Interface: GigabitEthernet0/2,  Port ID (outgoing port): GigabitEthernet0
-------------------------
"""

H3C_LLDP = """
LLDP neighbor-information of port 1[GigabitEthernet1/0/1]
  Neighbor index :1
  Device type     :LLDP device
  System name     :SW-CORE-02
  System description :
    H3C S5560X-54C-EI, Software Version 7.1.070, Release 3111P05
  Management address :
    IPv4 address :10.1.1.3
  Port description :GigabitEthernet1/0/24
  Interface index :2
  TTL :120
  Capability :
    Supported :Bridge, Router
    Enabled   :Bridge, Router

  System MAC: 0023-8927-afdc
LLDP neighbor-information of port 2[GigabitEthernet1/0/2]
  Neighbor index :1
  Device type     :LLDP device
  System name     :AP-FLOOR3-01
  System description :
    H3C WA4320, Software Version 7.1.070
  Management address :
    IPv4 address :10.1.3.50
  Port description :GigabitEthernet1
  Interface index :2
  TTL :120
  Capability :
    Supported :Bridge
    Enabled   :Bridge
"""

HUAWEI_BRIEF = """Local Intf            Neighbor Dev       Neighbor Intf      Exptime(s)
-------------------------------------------------------------------------------
10GE1/0/1             SW-CORE-02         10GE1/0/24         120
10GE1/0/2             SW-CORE-03         10GE1/0/25         120
"""

HUAWEI_DETAIL = """10GE1/0/1 has 1 neighbor(s):
Neighbor index :1
Chassis type :macAddress
Chassis ID :0025-9e95-7c20
Port ID type :interfaceName
Port ID :10GE1/0/24
Port description :HUAWEI, CloudEngine Series, 10GE1/0/24
Interface //Description of the neighbor device interface.
System name :SW-CORE-02
System description :Huawei Versatile Routing Platform Software VRP (R) software, Version 8.180 (CE6865 V200R023C10)
Management address type :ipv4
Management address :10.1.1.3
Expired time :111s
"""

RUIJIE_LLDP = """Interface  Chassis ID  Port ID                     System Name
GigabitEthernet0/1  4c1f-cc12-3456GigabitEthernet0/24       SW-CORE-02
GigabitEthernet0/2  4c1f-cc12-7890  GigabitEthernet0/25       SW-CORE-03
"""


def main() -> int:
    from app import neighbors as nb
    from app import vendors

    print("\n=== 1. 厂商命令集 ===")
    check("Cisco 默认命令", vendors.default_commands_for("cisco_ios")[0] == "show running-config")
    check("H3C 配置命令",
          "display current-configuration" in vendors.default_commands_for("h3c_comware"))
    check("H3C 邻居命令",
          "display lldp neighbor-information" in vendors.default_commands_for("h3c_comware"))
    check("华为配置命令",
          "display current-configuration" in vendors.default_commands_for("huawei_vrp"))
    check("华为邻居命令",
          "display lldp neighbor brief" in vendors.default_commands_for("huawei_vrp"))
    check("H3C 默认端口", vendors.default_port_for("h3c_comware") == 22)
    check("未知厂商回退 cisco", vendors.normalize_vendor("unknown_xyz") == "cisco")
    check("空类型回退 cisco", vendors.normalize_vendor(None) == "cisco")
    check("别名 h3c_comware", vendors.normalize_vendor("h3c_comware") == "h3c")
    check("别名 huawei_vrp", vendors.normalize_vendor("huawei_vrp") == "huawei")
    check("Cisco 需要 enable", vendors.vendor_config("cisco_ios")["needs_enable"] is True)
    check("H3C 无需 enable", vendors.vendor_config("h3c_comware")["needs_enable"] is False)
    check("华为无需 enable", vendors.vendor_config("huawei_vrp")["needs_enable"] is False)
    check("H3C 分页命令", "screen-length disable" in vendors.vendor_config("h3c")["paging_commands"])
    check("华为分页命令", "screen-length 0 temporary" in vendors.vendor_config("huawei")["paging_commands"])

    print("\n=== 2. Cisco CDP 解析（回归） ===")
    rows = nb.parse_neighbors(CISCO_CDP, "cisco")
    check("解析出 2 个邻居", len(rows) == 2, str(len(rows)))
    check("设备名正确", rows[0]["device_id"] == "SW-CORE-02.corp.local")
    check("IP 正确", rows[0]["ip"] == "10.1.1.3")
    check("平台正确", rows[0]["platform"] == "cisco WS-C3750-48P")
    check("本端接口正确", rows[0]["local_interface"] == "GigabitEthernet1/0/24")
    check("对端接口正确", rows[0]["remote_interface"] == "GigabitEthernet0/24")

    print("\n=== 3. H3C LLDP 解析 ===")
    rows = nb.parse_neighbors(H3C_LLDP, "h3c")
    check("解析出 2 个邻居", len(rows) == 2, f"got {len(rows)}")
    if rows:
        r = rows[0]
        check("系统名正确", r["device_id"] == "SW-CORE-02", r["device_id"])
        check("管理 IP 正确", r["ip"] == "10.1.1.3", r["ip"])
        check("型号提取正确", r["platform"] == "H3C S5560X-54C-EI", r["platform"])
        check("版本提取正确", r["os_version"] == "7.1.070", r["os_version"])
        check("本端接口正确", r["local_interface"] == "GigabitEthernet1/0/1", r["local_interface"])
        check("对端接口正确", r["remote_interface"] == "GigabitEthernet1/0/24", r["remote_interface"])
        check("能力字段", "Bridge" in r["capabilities"], r["capabilities"])
    if len(rows) > 1:
        check("第二个邻居也被解析", rows[1]["device_id"] == "AP-FLOOR3-01", rows[1]["device_id"])
        check("第二邻居接口正确", rows[1]["local_interface"] == "GigabitEthernet1/0/2")

    print("\n=== 4. 华为 brief 解析 ===")
    rows = nb.parse_neighbors(HUAWEI_BRIEF, "huawei")
    check("解析出 2 个邻居", len(rows) == 2, f"got {len(rows)}")
    if len(rows) == 2:
        check("本端接口正确", rows[0]["local_interface"] == "10GE1/0/1", rows[0]["local_interface"])
        check("设备名正确", rows[0]["device_id"] == "SW-CORE-02", rows[0]["device_id"])
        check("对端接口正确", rows[0]["remote_interface"] == "10GE1/0/24", rows[0]["remote_interface"])
        check("第二邻居正确", rows[1]["device_id"] == "SW-CORE-03")
        check("brief 模式无 IP 是正常的", rows[0]["ip"] == "")

    print("\n=== 5. 华为详细格式解析（自动优先） ===")
    rows = nb.parse_neighbors(HUAWEI_DETAIL, "huawei")
    check("解析出 1 个邻居", len(rows) == 1, f"got {len(rows)}")
    if rows:
        r = rows[0]
        check("系统名正确", r["device_id"] == "SW-CORE-02", r["device_id"])
        check("管理 IP 正确", r["ip"] == "10.1.1.3", r["ip"])
        check("对端接口正确", r["remote_interface"] == "10GE1/0/24", r["remote_interface"])
        check("本端接口正确", r["local_interface"] == "10GE1/0/1", r["local_interface"])
        check("版本提取正确", "8.180" in r["os_version"], r["os_version"])
        check("平台信息非空", bool(r["platform"]), r["platform"])

    print("\n=== 6. 锐捷解析 ===")
    rows = nb.parse_neighbors(RUIJIE_LLDP, "ruijie")
    check("锐捷解析有结果", len(rows) >= 1, f"got {len(rows)}")

    print("\n=== 7. 健壮性 ===")
    check("空文本返回空列表", nb.parse_neighbors("", "huawei") == [])
    check("None 输入不崩", nb.parse_neighbors(None or "", "h3c") == [])
    check("未知厂商回退 cisco", nb.parse_neighbors(CISCO_CDP, "brand_new") != [])
    check("华为空表头不崩", nb.parse_huawei_brief("Local Intf  Neighbor Dev\n---\n") == [])
    check("H3C 残缺块不崩",
          isinstance(nb.parse_h3c_neighbors("LLDP neighbor-information of port 1[Gi1/0/1]\n"), list))
    check("解析结果字段统一",
          all(set(r.keys()) == {"device_id", "ip", "platform", "capabilities",
                                "local_interface", "remote_interface", "os_version"}
              for r in nb.parse_neighbors(HUAWEI_DETAIL, "huawei")))
    check("过滤全空行",
          all(r["device_id"] or r["local_interface"]
              for r in nb.parse_neighbors(HUAWEI_BRIEF, "huawei")))

    print(f"\n{'=' * 46}\n  通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 46}\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())