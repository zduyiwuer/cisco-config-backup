"""飞塔 FortiGate 配置格式转换。

把 `show full-configuration` 的输出转换成标准 FortiOS `.conf` 备份格式。

为什么要转换
------------
FortiGate 有两种拿配置的方式：

1. `execute backup config <name> <target>` —— 生成真正的备份文件，可直接恢复
2. `show full-configuration` —— 纯文本输出，便于人看、便于脚本处理

官方明确说明：**`show` 系列的输出不能用于恢复配置**，只适合排查时查看。
而 `execute backup` 需要把文件推到 flash / FTP / TFTP / 管理站，
从外部采集时很难拿到文件内容。

本模块做的是：把 `show full-configuration` 的输出整理成 `.conf` 的结构，
补齐备份文件必需的头部元信息，得到一个**规范化、可归档、可对比**的配置文件。

⚠️ 注意：转换后的文件仍不等同于 `execute backup config` 的产物，
不含证书、私钥等 `execute backup` 才有的内容，且 FortiGate 官方不支持
用 show 的输出做整机恢复。它适合作为配置基线与变更对比。

===== 输入样例（show full-configuration）=====
config system global
    set hostname "FGT-01"
    set timezone "04"
end
config system interface
    edit "port1"
        set ip 192.168.1.1 255.255.255.0
        set allowaccess ping https ssh
    next
end

===== 输出样例（标准 .conf）=====
#config-version=FGT60F-7.0.5-FW-build0307-220331:opmode=0:vdom=0
#conf_file_ver=202201,201912
#buildno=0307,220331
#global_vdom=1
config system global
    set hostname "FGT-01"
    set timezone "04"
end
config system interface
    edit "port1"
        set ip 192.168.1.1 255.255.255.0
        set allowaccess ping https ssh
    next
end
"""

from __future__ import annotations

import re
from typing import Any

# 头部字段的默认值。实际值能推出来时会覆盖。
DEFAULT_CONF_FILE_VER = "202201,201912"
DEFAULT_GLOBAL_VDOM = 1

# show full-configuration 输出里的噪音行：
#   == [ N/A ] 之类的段落标题（部分版本会带）
#   纯注释行（不是 # 开头的头信息）
_NOISE_PREFIX = ("== [", "==[", "[N/A]")

# 从 get system status 提取型号与版本
# 典型输出：
#   Version: FortiGate-60F v7.0.5,build0307,220331 (GA)
#   Serial-Number: FGT60FTK20000000
#   Hardware: FortiGate-60F
_STATUS_VERSION = re.compile(
    r"^\s*Version:\s*(?P<model>[A-Za-z0-9_\-]+?)\s*v(?P<ver>[0-9][^\s,]*)"
    r"(?:\s*,\s*build(?P<build>[0-9]+))?"
    r"(?:\s*,\s*(?P<date>[0-9]+))?",
    re.M,
)
_STATUS_SERIAL = re.compile(r"^\s*Serial-Number:\s*(\S+)", re.M)
_STATUS_HARDWARE = re.compile(r"^\s*Hardware:\s*(.+)$", re.M)
# 部分版本版本行形如 "Version: FortiGate-VM64-KVM v7.4.0,build2360,240205 (GA)"
_VERSION_FALLBACK = re.compile(r"v(?P<ver>[0-9]+\.[0-9.]+)(?:[,-]build(?P<build>[0-9]+))?")


def parse_status(status_text: str) -> dict[str, Any]:
    """从 `get system status` 输出里提取型号、版本、build 号。"""
    info: dict[str, Any] = {
        "model": "",
        "version": "",
        "build": "",
        "date": "",
        "serial": "",
        "hardware": "",
    }
    if not status_text:
        return info

    m = _STATUS_VERSION.search(status_text)
    if m:
        info["model"] = m.group("model") or ""
        info["version"] = m.group("ver") or ""
        info["build"] = m.group("build") or ""
        info["date"] = m.group("date") or ""
    else:
        # 版本行格式不同时退回宽松匹配
        fb = _VERSION_FALLBACK.search(status_text)
        if fb:
            info["version"] = fb.group("ver")
            info["build"] = fb.group("build") or ""

    m = _STATUS_SERIAL.search(status_text)
    if m:
        info["serial"] = m.group(1)
    m = _STATUS_HARDWARE.search(status_text)
    if m:
        info["hardware"] = m.group(1).strip()
    return info


def _normalize_model(model: str) -> str:
    """型号归一为 FortiOS 备份文件里的写法。

    官方 .conf 头部用的是产品代号，如 FGT60F、FGT200A、FGVM64，
    而 get system status 里给的是 "FortiGate-60F"、"FortiGate-VM64"。
    这里做一次映射，对不上就保留原样。
    """
    m = (model or "").strip()
    if not m:
        return ""
    # 虚拟机型号要先判：FortiGate-VM64 -> FGVM64，
    # 若先用通用规则会把 VM64 当成型号名的一部分，得到错误的 FGTVM64
    mm = re.match(r"^FortiGate-(VM[0-9]+.*)$", m)
    if mm:
        return f"FG{mm.group(1)}"
    # FortiGate-60F -> FGT60F
    mm = re.match(r"^FortiGate-([A-Za-z0-9]+)$", m)
    if mm:
        return f"FGT{mm.group(1)}"
    return m


def _build_header(info: dict[str, Any]) -> list[str]:
    """生成 .conf 文件的头部元信息行。"""
    model = _normalize_model(info.get("model") or info.get("hardware") or "")
    version = info.get("version") or "0.0.0"
    build = info.get("build") or "0000"
    date = info.get("date") or ""

    # #config-version=FGT60F-7.0.5-FW-build0307-220331:opmode=0:vdom=0
    #   型号-版本-FW-build号-日期
    ver_full = f"{version}-FW-build{build}"
    if date:
        ver_full += f"-{date}"
    header = [
        f"#config-version={model or 'FortiGate'}-{ver_full}:opmode=0:vdom=0",
        f"#conf_file_ver={DEFAULT_CONF_FILE_VER}",
        f"#buildno={build},{date or '000000'}",
        f"#global_vdom={DEFAULT_GLOBAL_VDOM}",
    ]
    if info.get("serial"):
        header.append(f"#serial={info['serial']}")
    return header


def _strip_noise(lines: list[str]) -> list[str]:
    """剔除 show 输出里的噪音行，保留配置正文。"""
    out: list[str] = []
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            out.append("")
            continue
        stripped = line.strip()
        # 段落标题（部分版本 show 输出带）
        if stripped.startswith(_NOISE_PREFIX):
            continue
        # 保留以 # 开头的头部行（我们自己生成的），但 show 输出里不会有
        out.append(line)
    # 压缩连续空行
    result: list[str] = []
    blanks = 0
    for line in out:
        if line == "":
            blanks += 1
            if blanks <= 1:
                result.append(line)
        else:
            blanks = 0
            result.append(line)
    while result and not result[0].strip():
        result.pop(0)
    while result and not result[-1].strip():
        result.pop()
    return result


def to_conf(show_output: str, status_text: str = "") -> str:
    """把 `show full-configuration` 输出转换成标准 FortiOS .conf 文本。

    show_output —— show full-configuration 的原始输出（已去掉命令回显与提示符）
    status_text —— get system status 的输出，用于补全头部元信息，可为空
    """
    if not show_output or not show_output.strip():
        return ""

    info = parse_status(status_text or "")
    body_lines = _strip_noise(show_output.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    if not body_lines:
        return ""

    header = _build_header(info)
    return "\n".join(header + body_lines) + "\n"


def to_conf_filename(device_name: str, version: str = "") -> str:
    """生成 .conf 文件名，如 FGT-01_v7.0.5.conf。"""
    name = re.sub(r"[^\w\-.]+", "_", (device_name or "device").strip()) or "device"
    if version:
        return f"{name}_v{version}.conf"
    return f"{name}.conf"
