"""CDP 邻居详情解析。

`show cdp neighbors detail` 输出形如：

-------------------------
Device ID: SW-CORE-01.corp.local
Entry address(es):
  IP address: 10.1.1.2
Platform: cisco WS-C3750-48P,  Capabilities: Switch IGMP
Interface: GigabitEthernet1/0/24,  Port ID (outgoing port): GigabitEthernet0/24
-------------------------

解析结果用于前端"邻居拓扑"视图。
"""

from __future__ import annotations

import re
from typing import Any

_SPLIT = re.compile(r"-{5,}")
_DEVID = re.compile(r"Device ID:\s*(.+)")
_IPADDR = re.compile(r"IP(?:v4)? address:\s*([0-9a-fA-F.:]+)")
_IPV6 = re.compile(r"IPv6 address:\s*([0-9a-fA-F:]+)")
_PLATFORM = re.compile(r"Platform:\s*([^,]+)")
_CAP = re.compile(r"Capabilities:\s*(.+)")
_LOCAL_IF = re.compile(r"Interface:\s*([^,]+)")
_REMOTE_IF = re.compile(r"Port ID \(outgoing port\):\s*(.+)")


def parse_cdp_neighbors(text: str) -> list[dict[str, Any]]:
    if not text:
        return []
    blocks = [b for b in _SPLIT.split(text) if "Device ID" in b]
    neighbors: list[dict[str, Any]] = []
    for block in blocks:
        dev = _DEVID.search(block)
        ip4 = _IPADDR.search(block)
        ip6 = _IPV6.search(block)
        plat = _PLATFORM.search(block)
        cap = _CAP.search(block)
        lif = _LOCAL_IF.search(block)
        rif = _REMOTE_IF.search(block)
        neighbors.append(
            {
                "device_id": dev.group(1).strip() if dev else "",
                "ip": (ip4.group(1).strip() if ip4 else (ip6.group(1).strip() if ip6 else "")),
                "platform": plat.group(1).strip() if plat else "",
                "capabilities": cap.group(1).strip() if cap else "",
                "local_interface": lif.group(1).strip() if lif else "",
                "remote_interface": rif.group(1).strip() if rif else "",
            }
        )
    return neighbors