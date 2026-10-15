"""SSH 采集驱动（多厂商通用）。

通过 vendors 模块按设备类型切换提示符正则、分页命令与enable 逻辑，
支持 Cisco IOS / H3C Comware / 华为 VRP / 锐捷 RGOS。
"""

from __future__ import annotations

import re
import time
from typing import Any

import paramiko

from .. import vendors
from .base import DeviceError

MORE_RE = re.compile(r"--More--", re.IGNORECASE)
# 华为/H3C 用不同风格的分页提示
MORE_RE_ALT = re.compile(
    r"(?:----\s*More\s*----|Press\s+Q\s+to\s+break|继续|\[Y/N\]|More\s*\()", re.IGNORECASE
)


class SSHClient:
    def __init__(
        self,
        host: str,
        port: int = 22,
        username: str = "",
        password: str = "",
        connect_timeout: int = 15,
        command_timeout: int = 45,
        paging_fix: bool = True,
        device_type: str = "cisco_ios",
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout
        self.paging_fix = paging_fix
        self.device_type = device_type
        self.vendor = vendors.vendor_config(device_type)
        self.prompt_patterns = vendors.all_prompt_regexes(device_type)
        self.client: paramiko.SSHClient | None = None
        self.shell = None
        self._buffer = ""
        self.prompt = ""

    # ------------------------------------------------------------------ connect
    def connect(self) -> str:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                timeout=self.connect_timeout,
                banner_timeout=self.connect_timeout,
                auth_timeout=self.connect_timeout,
                allow_agent=False,
                look_for_keys=False,
            )
            transport = client.get_transport()
            if transport is None:
                raise DeviceError("SSH 传输通道建立失败")
            transport.set_keepalive(30)
            self.shell = client.invoke_shell(term="vt100", width=511, height=1000)
            self.shell.settimeout(1.0)
            self.client = client
        except paramiko.AuthenticationException as exc:
            raise DeviceError(f"SSH 认证失败: {exc}") from exc
        except paramiko.ssh_exception.BadHostKeyException as exc:
            raise DeviceError(f"主机密钥校验失败: {exc}") from exc
        except Exception as exc:
            raise DeviceError(f"SSH 连接失败: {type(exc).__name__}: {exc}") from exc

        try:
            self.prompt = self._wait_for_prompt(self.connect_timeout)
            if vendors.is_login_prompt(self._buffer):
                raise DeviceError("仍停留在登录界面，密码可能不正确")
            if self.paging_fix:
                for cmd in self.vendor["paging_commands"]:
                    self.send(cmd)
                    self.prompt = self._wait_for_prompt(self.command_timeout)
        except DeviceError:
            self.close()
            raise
        return self.prompt

    def enable(self, enable_password: str = "") -> None:
        """进入特权模式。仅 Cisco 需要（华为/H3C 登录后即特权视图）。"""
        if not self.vendor["needs_enable"]:
            return
        if self.prompt.rstrip().endswith("#"):
            return
        self.send("enable")
        # 设备可能直接放行（本来就在 enable 模式），也可能索要密码
        out = self.read_until([r"[Pp]assword\s*:", self.prompt_patterns[0].pattern], timeout=8)
        m = self.prompt_patterns[0].search(out)
        if m:
            self.prompt = m.group(0).strip()
            if self.prompt.endswith("#"):
                return
        if not enable_password:
            raise DeviceError("设备要求 enable 密码，但未配置 enable password")
        self.send(enable_password)
        self.prompt = self._wait_for_prompt(self.command_timeout)
        if not self.prompt.rstrip().endswith("#"):
            raise DeviceError(f"进入 enable 特权模式失败，当前提示符: {self.prompt!r}")

    # ------------------------------------------------------------------- io
    def send(self, line: str) -> None:
        if self.shell is None:
            raise DeviceError("SSH 会话未建立")
        self.shell.send(line + "\n")

    @staticmethod
    def _strip_ansi(text: str) -> str:
        return re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", text).replace("\x1b[?25l", "").replace(
            "\x1b[?25h", ""
        )

    def _has_more(self, text: str) -> bool:
        return bool(MORE_RE.search(text) or MORE_RE_ALT.search(text))

    def _drain_paging(self, text: str) -> str:
        """处理分页提示，直到不再出现。"""
        guard = 0
        while self._has_more(text) and guard < 2000:
            guard += 1
            text = MORE_RE.sub("", text)
            text = MORE_RE_ALT.sub("", text)
            if self.shell is None:
                break
            try:
                self.shell.send(" ")
                time.sleep(0.05)
                text += self.shell.recv(65535).decode("utf-8", errors="replace")
            except Exception:
                break
        return text

    def _pump(self, deadline: float) -> None:
        assert self.shell is not None
        while time.time() < deadline:
            try:
                data = self.shell.recv(65535)
            except TimeoutError:
                continue
            except Exception as exc:
                raise DeviceError(f"读取设备输出失败: {exc}") from exc
            if not data:
                raise DeviceError("设备主动断开了连接")
            text = data.decode("utf-8", errors="replace")
            text = self._drain_paging(text)
            self._buffer += text

    def read_until(self, patterns: list[str], timeout: float) -> str:
        deadline = time.time() + timeout
        while True:
            for pattern in patterns:
                m = re.search(pattern, self._buffer)
                if m:
                    out, self._buffer = self._buffer[: m.end()], self._buffer[m.end() :]
                    return out
            if time.time() >= deadline:
                out, self._buffer = self._buffer, ""
                return out
            self._pump(min(deadline, time.time() + 1.0))

    def _find_prompt(self, text: str) -> re.Match[str] | None:
        """在文本末尾匹配提示符。逐个厂商正则尝试。"""
        cleaned = self._strip_ansi(text)
        for pattern in self.prompt_patterns:
            m = pattern.search(cleaned)
            if m:
                return m
        return None

    def _wait_for_prompt(self, timeout: float) -> str:
        deadline = time.time() + timeout
        while True:
            match = self._find_prompt(self._buffer)
            if match:
                self._buffer = ""
                return match.group(0).strip()
            if time.time() >= deadline:
                cleaned = self._strip_ansi(self._buffer).strip()
                self._buffer = ""
                return cleaned.splitlines()[-1] if cleaned else ">"
            self._pump(min(deadline, time.time() + 1.0))

    def run(self, command: str) -> str:
        """发送命令并返回该命令的输出（不含命令回显与提示符）。"""
        self._buffer = ""  # 丢弃发送前的残留，确保输出归属于本条命令
        self.send(command)
        deadline = time.time() + self.command_timeout
        while True:
            match = self._find_prompt(self._buffer)
            if match:
                raw = self._buffer[: match.start()]
                self._buffer = ""
                return self._drain_paging(raw)
            if time.time() >= deadline:
                raw, self._buffer = self._buffer, ""
                return self._drain_paging(raw)
            self._pump(min(deadline, time.time() + 1.0))

    def close(self) -> None:
        try:
            if self.shell is not None:
                self.shell.close()
        except Exception:
            pass
        try:
            if self.client is not None:
                self.client.close()
        except Exception:
            pass
        self.shell = None
        self.client = None