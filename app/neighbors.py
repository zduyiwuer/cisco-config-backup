"""多厂商邻居信息解析。

各厂商的邻居输出格式差异很大，需要分别解析：

===== Cisco CDP =====
-------------------------
Device ID: SW-CORE-01.corp.local
Entry address(es):
  IP address: 10.1.1.2
Platform: cisco WS-C3750-48P,  Capabilities: Switch IGMP
Interface: GigabitEthernet1/0/24,  Port ID (outgoing port): GigabitEthernet0/24
-------------------------

===== H3C LLDP（display lldp neighbor-information verbose）=====
LLDP neighbor-information of port 1 [GigabitEthernet1/0/1]
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

===== 华为 LLDP（display lldp neighbor brief）=====
Local Intf Neighbor Dev            Neighbor Intf                 Exptime(s)
10GE1/0/1     SwitchB10GE1/0/2                          100
10GE1/0/2     SwitchA               10GE1/0/1                    95

（简要格式只有接口和系统名，无IP 与型号）
"""

from __future__ import annotations

import re
from typing import Any

from .cdp import parse_cdp_neighbors

# --------------------------------------------------------------------- Cisco
# 沿用原有的 CDP 解析


# ----------------------------------------------------------------------- H3C
_H3C_PORT_SPLIT = re.compile(r"^LLDP neighbor-information of port \d+\s*\[(.+?)\]\s*$", re.M)
_H3C_BLOCK = re.compile(
    r"^LLDP neighbor-information of port \d+\s*\[(?P<local_if>.+?)\]\s*$(?P<body>.*?)(?=^LLDP neighbor-information|\Z)",
    re.M | re.S,
)
_H3C_SYSNAME = re.compile(r"^\s*System name\s*:\s*(.+)$", re.M)
_H3C_SYSDESC = re.compile(r"^\s*System description\s*:\s*(.+)$", re.M)
_H3C_DESC_LINE2 = re.compile(r"^\s{4,}(\S.*)$", re.M)
_H3C_IPV4 = re.compile(r"IPv4 address\s*:\s*([0-9a-fA-F.:]+)")
_H3C_IPV6 = re.compile(r"IPv6 address\s*:\s*([0-9a-fA-F:]+)")
_H3C_PORTDESC = re.compile(r"^\s*Port description\s*:\s*(.+)$", re.M)
_H3C_CAP = re.compile(r"^\s*Supported\s*:\s*(.+)$", re.M)
# 设备型号：从 System description 里提取 "H3C S5560X-54C-EI" 这类
_H3C_MODEL = re.compile(r"(H3C\s+\S+|HPE\s+\S+|H3C\s+Comware\S*)")
_H3C_VERSION = re.compile(r"Software Version\s+([^\s,]+)")


def _clean_model(text: str) -> str:
    """去掉型号尾部的标点，如 'H3C S5560X-54C-EI,' -> 'H3C S5560X-54C-EI'。"""
    return text.strip().rstrip(",;。").strip()


def parse_h3c_neighbors(text: str) -> list[dict[str, Any]]:
    """解析 H3C `display lldp neighbor-information` 输出。"""
    if not text:
        return []
    neighbors: list[dict[str, Any]] = []
    for match in _H3C_BLOCK.finditer(text):
        body = match.group("body")
        local_if = match.group("local_if").strip()

        sysname = _H3C_SYSNAME.search(body)
        sysdesc = _H3C_SYSDESC.search(body)
        # System description 可能换行显示型号，取紧跟其后的一行
        platform = ""
        version = ""
        if sysdesc:
            first_line = sysdesc.group(1).strip()
            if first_line:
                platform = first_line
            # 续行（缩进更深的非字段行）
            tail = body[sysdesc.end():]
            for line in tail.splitlines():
                stripped = line.strip()
                if not stripped or re.match(r"^\w[\w\s]*\s*:\s*", stripped):
                    break
                platform = (platform + " " + stripped).strip() if platform else stripped
                break
            mv = _H3C_VERSION.search(platform)
            if mv:
                version = mv.group(1)
            mm = _H3C_MODEL.search(platform)
            if mm:
                platform = _clean_model(mm.group(1))

        ipv4 = _H3C_IPV4.search(body)
        ipv6 = _H3C_IPV6.search(body)
        portdesc = _H3C_PORTDESC.search(body)
        cap = _H3C_CAP.search(body)

        neighbors.append(
            {
                "device_id": sysname.group(1).strip() if sysname else "",
                "ip": ipv4.group(1).strip() if ipv4 else (ipv6.group(1).strip() if ipv6 else ""),
                "platform": platform,
                "capabilities": cap.group(1).strip() if cap else "",
                "local_interface": local_if,
                # H3C 的 Port description 即对端接口名
                "remote_interface": portdesc.group(1).strip() if portdesc else "",
                "os_version": version,
            }
        )
    return neighbors


# --------------------------------------------------------------------- 华为
# display lldp neighbor brief
#   Local Intf  Neighbor Dev Neighbor Intf  Exptime(s)
#   10GE1/0/1   SwitchB      10GE1/0/2        100
_HW_BRIEF_HEAD = re.compile(
    r"Local\s+Intf.*?Neighbor\s+Dev.*?(?:Neighbor\s+Intf)?.*?Exptime", re.I
)
_HW_BRIEF_ROW = re.compile(
    r"^\s*(?P<local>\S+)\s+(?P<dev>\S+)\s+(?P<remote>\S+)\s+(?P<exptime>\d+)\s*$"
)

# display lldp neighbor interface 10ge 1/0/1（详细信息）
_HW_NEIGHBOR_BLOCK = re.compile(
    r"(?P<local_if>\S+)\s+has\s+\d+\s+neighbor\(s\):(?P<body>.*?)(?=^\S+\s+has\s+\d+\s+neighbor|^\s*<|\Z)",
    re.M | re.S,
)
_HW_SYSNAME = re.compile(r"^\s*System name\s*:\s*(.+)$", re.M)
_HW_SYSDESC = re.compile(r"^\s*System description\s*:\s*(.+)$", re.M)
_HW_PORTID = re.compile(r"^\s*Port ID\s*:\s*(.+)$", re.M)
_HW_MGMT = re.compile(r"^\s*Management address\s*:\s*(.+)$", re.M)
_HW_IP = re.compile(r"([0-9]{1,3}(?:\.[0-9]{1,3}){3}|[0-9a-fA-F:]{6,})")
_HW_MODEL = re.compile(r"(Huawei\s+\S+|CloudEngine\s+\S+|\bVRP\b.*)", re.I)
_HW_VERSION = re.compile(r"Version\s+([^\s,()]+)")


def parse_huawei_brief(text: str) -> list[dict[str, Any]]:
    """解析华为 `display lldp neighbor brief` 表格输出。

    简要格式只有本端接口、邻居设备名、对端接口、过期时间，
    没有 IP 与型号，这些字段留空。
    """
    if not text:
        return []
    lines = text.splitlines()
    start = None
    for idx, line in enumerate(lines):
        if re.search(r"Local\s+Intf", line, re.I) and re.search(r"Neighbor\s+Dev", line, re.I):
            start = idx + 1
            break
    if start is None:
        return []

    neighbors: list[dict[str, Any]] = []
    for line in lines[start:]:
        if not line.strip():
            # 空行后若还有分隔线则结束表格
            continue
        if re.match(r"^-{3,}", line.strip()):
            continue
        if re.match(r"^(Local\s+Intf|<)", line.strip(), re.I):
            break
        m = _HW_BRIEF_ROW.match(line)
        if not m:
            # 表头换行（Neighbor Intf / Exptime 分两行）跳过
            if re.search(r"Neighbor|Exptime|Local", line, re.I):
                continue
            if not line.strip():
                continue
            continue
        neighbors.append(
            {
                "device_id": m.group("dev").strip(),
                "ip": "",
                "platform": "",
                "capabilities": "",
                "local_interface": m.group("local").strip(),
                "remote_interface": m.group("remote").strip(),
                "os_version": "",
            }
        )
    return neighbors


def parse_huawei_detailed(text: str) -> list[dict[str, Any]]:
    """解析华为 `display lldp neighbor interface ...` 详细输出。"""
    if not text or "has" not in text:
        return []
    neighbors: list[dict[str, Any]] = []
    for match in _HW_NEIGHBOR_BLOCK.finditer(text):
        body = match.group("body")
        local_if = match.group("local_if").strip()
        sysname = _HW_SYSNAME.search(body)
        sysdesc = _HW_SYSDESC.search(body)
        portid = _HW_PORTID.search(body)
        mgmt = _HW_MGMT.search(body)

        ip = ""
        if mgmt:
            found = _HW_IP.search(mgmt.group(1))
            if found:
                ip = found.group(1)

        platform = ""
        version = ""
        if sysdesc:
            desc = sysdesc.group(1).strip()
            vm = _HW_VERSION.search(desc)
            if vm:
                version = vm.group(1)
            mm = _HW_MODEL.search(desc)
            platform = mm.group(1).strip() if mm else desc[:60]

        neighbors.append(
            {
                "device_id": sysname.group(1).strip() if sysname else "",
                "ip": ip,
                "platform": platform,
                "capabilities": "",
                "local_interface": local_if,
                "remote_interface": portid.group(1).strip() if portid else "",
                "os_version": version,
            }
        )
    return neighbors


def parse_huawei_neighbors(text: str) -> list[dict[str, Any]]:
    """华为邻居解析：优先详细格式，回退简要格式。"""
    detailed = parse_huawei_detailed(text)
    if detailed:
        return detailed
    return parse_huawei_brief(text)


# -------------------------------------------------------------------- 锐捷
# show lldp neighbors 表格格式：
#   Interface          Chassis ID          Port ID                System Name
#   GigabitEthernet0/1  4c1f-cc12-3456GigabitEthernet0/24SW-CORE-02
# 部分版本的列顺序可能不同，这里用宽松匹配逐列抓取
_RG_CHASSIS = re.compile(r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}")
_RG_DETAIL = re.compile(
    r"^(?:Local\s+)?Interface\s*:\s*(?P<local>\S+).*?"
    r"Device\s+(?:ID|Name)\s*:\s*(?P<dev>\S+)",
    re.M | re.S,
)


def parse_ruijie_neighbors(text: str) -> list[dict[str, Any]]:
    """解析锐捷 `show lldp neighbors` 输出。

    表格列顺序在不同版本间有差异，这里不依赖固定列序：
    识别出 Chassis ID 后，把它前后的内容分别当作本端/对端接口，
    最后一段当作系统名。
    """
    if not text:
        return []
    result: list[dict[str, Any]] = []

    # 优先详细格式
    for m in _RG_DETAIL.finditer(text):
        result.append(
            {
                "device_id": m.group("dev").strip(),
                "ip": "",
                "platform": "",
                "capabilities": "",
                "local_interface": m.group("local").strip(),
                "remote_interface": "",
                "os_version": "",
            }
        )
    if result:
        return result

    # 表格格式：逐行找 Chassis ID 定位
    for line in text.splitlines():
        if not line.strip():
            continue
        chassis = _RG_CHASSIS.search(line)
        if not chassis:
            # 表头/分隔行没有 Chassis ID，跳过
            continue
        before = line[: chassis.start()].strip().split()
        after = line[chassis.end() :].strip()
        local_if = before[-1] if before else ""
        rest = after.split()
        remote_if = ""
        dev = ""
        for token in rest:
            # 接口名通常以字母开头且含数字或斜杠
            if not remote_if and re.match(r"^[A-Za-z][A-Za-z\-]*\d", token):
                remote_if = token
            elif remote_if and not dev:
                dev = token
        if dev and not local_if and rest:
            # 兜底：第一个 token 当系统名，第二个当接口
            dev = rest[-1]
            remote_if = rest[0] if len(rest) > 1 else ""
        if dev:
            result.append(
                {
                    "device_id": dev,
                    "ip": "",
                    "platform": "",
                    "capabilities": "",
                    "local_interface": local_if,
                    "remote_interface": remote_if,
                    "os_version": "",
                }
            )
    return result


# ------------------------------------------------------------------- 分发
PARSERS = {
    "cisco": parse_cdp_neighbors,
    "h3c": parse_h3c_neighbors,
    "huawei": parse_huawei_neighbors,
    "ruijie": parse_ruijie_neighbors,
}


def parse_neighbors(text: str, parser_name: str = "cisco") -> list[dict[str, Any]]:
    """按厂商解析邻居信息。未知厂商回退 Cisco 解析。"""
    fn = PARSERS.get(parser_name, parse_cdp_neighbors)
    try:
        rows = fn(text or "")
    except Exception:  # noqa: BLE001
        return []
    # 统一字段，补齐缺失键
    normalized = []
    for row in rows:
        normalized.append(
            {
                "device_id": row.get("device_id", ""),
                "ip": row.get("ip", ""),
                "platform": row.get("platform", ""),
                "capabilities": row.get("capabilities", ""),
                "local_interface": row.get("local_interface", ""),
                "remote_interface": row.get("remote_interface", ""),
                "os_version": row.get("os_version", ""),
            }
        )
    # 过滤掉完全空的行
    return [r for r in normalized if r["device_id"] or r["local_interface"]]