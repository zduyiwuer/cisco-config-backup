"""多厂商设备命令集与提示符适配。

各厂商的命令语法、提示符风格、邻居输出格式都不同，这里集中管理：

- VENDORS 定义每个厂商的默认采集命令与命令模板
- PROMPT_RE 识别各厂商的提示符
- LLDP/CDP 邻居解析器按厂商格式分别实现

新增厂商只需在此登记命令与提示符；若邻居输出格式是新的，
再在 app/neighbors.py 里补一个解析函数。
"""

from __future__ import annotations

import re
from typing import Any

# --------------------------------------------------------------------- 厂商定义
VENDOR_CISCO = "cisco"
VENDOR_HUAWEI = "huawei"
VENDOR_H3C = "h3c"
VENDOR_RUIJIE = "ruijie"  # 锐捷，命令接近华为，附带支持

VENDORS: dict[str, dict[str, Any]] = {
    VENDOR_CISCO: {
        "label": "Cisco IOS",
        "default_port": 22,
        "protocols": ("ssh", "telnet"),
        # 进入特权模式：思科需要 enable
        "needs_enable": True,
        "enable_user": "",
        "default_commands": [
            "show running-config",
            "show cdp neighbors detail",
        ],
        # 关闭分页的命令（各家不同）
        "paging_commands": ["terminal length 0", "terminal width 511"],
        "prompt_patterns": [
            # SW>  SW#  SW(config)#  switch(config-if)#
            r"[A-Za-z0-9_.\-()]+(?:\([^\)\r\n]+\))?\s*[>#]\s*$",
            # 精简提示符：SW>
            r"[A-Za-z0-9_.\-]+\s*>\s*$",
        ],
        "version_command": "show version",
        "version_pattern": r"(?:Cisco IOS|Cisco NX-OS|IOS-XE).*?Version\s+([^\s,]+)",
        "neighbor_command": "show cdp neighbors detail",
        "neighbor_parser": "cisco",
    },
    VENDOR_H3C: {
        "label": "H3C Comware",
        "default_port": 22,
        "protocols": ("ssh", "telnet"),
        # H3C 直接在用户视图即特权视图，无需 enable
        "needs_enable": False,
        "default_commands": [
            "display current-configuration",
            "display lldp neighbor-information",
        ],
        "paging_commands": ["screen-length disable", "screen-width 511"],
        "prompt_patterns": [
            # <Sysname>  [Sysname]  [Sysname-GigabitEthernet1/0/1]
            r"<[A-Za-z0-9_\-./ ]+>\s*$",
            r"\[[A-Za-z0-9_\-./ ]+\]\s*$",
        ],
        "version_command": "display version",
        "version_pattern": r"(?:H3C|HPE).*?Version\s+([^\s,]+)|Version\s+([^\s,]+)",
        "neighbor_command": "display lldp neighbor-information",
        "neighbor_parser": "h3c",
    },
    VENDOR_HUAWEI: {
        "label": "华为 VRP",
        "default_port": 22,
        "protocols": ("ssh", "telnet"),
        # 华为登录后即在用户视图，无enable 概念
        "needs_enable": False,
        "default_commands": [
            "display current-configuration",
            "display lldp neighbor brief",
        ],
        "paging_commands": ["screen-length 0 temporary"],
        "prompt_patterns": [
            # <Sysname>  [Sysname]  [~HUAWEI]  [Huawei-GigabitEthernet0/0/1]
            r"<[A-Za-z0-9_\-./ ]+>\s*$",
            r"\[[A-Za-z0-9_\-./~\u4e00-\u9fff ]+\]\s*$",
        ],
        "version_command": "display version",
        "version_pattern": r"VRP\s+\(R\)\s+software,\s*Version\s+([^\s,]+)|Version\s+([^\s,]+)",
        "neighbor_command": "display lldp neighbor brief",
        "neighbor_parser": "huawei",
    },
    VENDOR_RUIJIE: {
        "label": "锐捷 RGOS",
        "default_port": 22,
        "protocols": ("ssh", "telnet"),
        "needs_enable": False,
        "default_commands": [
            "show running-config",
            "show lldp neighbors",
        ],
        "paging_commands": ["terminal length 0"],
        "prompt_patterns": [
            r"[A-Za-z0-9_.\-]+\s*[>#]\s*$",
        ],
        "version_command": "show version",
        "version_pattern": r"Version\s+([^\s,]+)",
        "neighbor_command": "show lldp neighbors",
        "neighbor_parser": "ruijie",
    },
}

# 老代码里的 cisco_ios / h3c_comware 之类类型名归一
VENDOR_ALIASES = {
    "cisco_ios": VENDOR_CISCO,
    "cisco_nxos": VENDOR_CISCO,
    "cisco_ios_xe": VENDOR_CISCO,
    "ios": VENDOR_CISCO,
    "h3c_comware": VENDOR_H3C,
    "comware": VENDOR_H3C,
    "h3c": VENDOR_H3C,
    "huawei_vrp": VENDOR_HUAWEI,
    "huawei": VENDOR_HUAWEI,
    "vrp": VENDOR_HUAWEI,
    "ruijie_rgos": VENDOR_RUIJIE,
    "ruijie": VENDOR_RUIJIE,
    "rgos": VENDOR_RUIJIE,
}


def normalize_vendor(device_type: str | None) -> str:
    """把设备类型归一为厂商标识，未知一律回退 cisco（保持向后兼容）。"""
    key = (device_type or "").strip().lower().replace(" ", "_")
    if key in VENDORS:
        return key
    return VENDOR_ALIASES.get(key, VENDOR_CISCO)


def vendor_config(device_type: str | None) -> dict[str, Any]:
    return VENDORS[normalize_vendor(device_type)]


def vendor_label(device_type: str | None) -> str:
    return vendor_config(device_type)["label"]


def default_commands_for(device_type: str | None) -> list[str]:
    return list(vendor_config(device_type)["default_commands"])


def default_port_for(device_type: str | None) -> int:
    return int(vendor_config(device_type)["default_port"])


def prompt_regex(device_type: str | None) -> re.Pattern[str]:
    """编译该厂商的提示符正则（取第一个作为主用）。"""
    patterns = vendor_config(device_type)["prompt_patterns"]
    return re.compile(patterns[0])


def all_prompt_regexes(device_type: str | None) -> list[re.Pattern[str]]:
    """全部提示符形态。有些设备输出里会混入其他风格的提示符。"""
    return [re.compile(p) for p in vendor_config(device_type)["prompt_patterns"]]


def is_login_prompt(text: str) -> bool:
    """判断文本是否停在登录交互界面（还没进设备）。"""
    patterns = (
        r"[Uu]sername\s*:",
        r"[Ll]ogin\s*:",
        r"[Pp]assword\s*:",
        r"^\s*[Pp]lease (?:input|enter)",
    )
    return any(re.search(p, text) for p in patterns)


def is_privilege_prompt(prompt: str) -> bool:
    """是否已在特权/用户视图（H3C/华为登录后即为特权视图）。"""
    cfg = vendor_config(None)
    if not cfg["needs_enable"]:
        return True
    return prompt.rstrip().endswith("#")