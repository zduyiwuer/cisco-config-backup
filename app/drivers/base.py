"""采集层公共接口与工具函数。"""

from __future__ import annotations

import hashlib
import re
from typing import Protocol

DEFAULT_COMMANDS = [
    "show running-config",
    "show cdp neighbors detail",
]


class DeviceError(Exception):
    """登录/采集过程中的可预期错误，消息直接展示给运维。"""


class Session(Protocol):
    def send(self, line: str) -> None: ...
    def read_until(self, patterns: list[str], timeout: float) -> str: ...
    def close(self) -> None: ...


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def slugify(command: str) -> str:
    """把命令转成安全的文件名，例如 show cdp neighbors detail -> show_cdp_neighbors_detail。"""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", command.strip().lower()).strip("_")
    return slug[:60] or "cmd"


_ECHO_RE = re.compile(r"^\s*\S+\([^\)]+\)#[ ]?")


def _strip_prompt_prefix(line: str, prompt: str) -> str:
    """真机回显形如 `SW#show running-config`，提示符和命令在同一行。"""
    if prompt and line.startswith(prompt):
        return line[len(prompt) :].lstrip()
    return line


def clean_output(raw: str, command: str, prompt: str = "") -> str:
    """去掉设备回显的命令行与提示符，压缩多余空行。"""
    lines: list[str] = []
    # 设备把命令原样回显一次，去掉第一处出现的命令
    removed_echo = False
    for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.rstrip()
        candidate = _strip_prompt_prefix(stripped, prompt)
        # 通用情况：提示符后紧跟命令的提示符样式
        if candidate != stripped:
            m = _ECHO_RE.match(candidate)
            if m:
                candidate = candidate[m.end() :]
        if not removed_echo and candidate.strip().lower() == command.strip().lower():
            removed_echo = True
            continue
        if prompt and stripped.strip() == prompt.strip():
            continue
        if _ECHO_RE.match(stripped) and not stripped.strip().startswith(command.strip()):
            continue
        lines.append(stripped)
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text) + "\n"


VERSION_RE = re.compile(r"Cisco IOS.*?Version\s+([^\s,]+)", re.IGNORECASE)


def parse_os_version(text: str) -> str | None:
    m = VERSION_RE.search(text or "")
    return m.group(1) if m else None