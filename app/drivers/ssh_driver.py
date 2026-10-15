"""基于 paramiko 的 SSH 采集驱动（Cisco IOS / NX-OS / IOS-XE 通用）。"""

from __future__ import annotations

import re
import time

import paramiko

from .base import DeviceError

PROMPT_RE = re.compile(r"[A-Za-z0-9_.\-()]+(?:\([^\)\r\n]+\))?[>#][ \t]*$")
MORE_RE = re.compile(r"--More--", re.IGNORECASE)


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
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout
        self.paging_fix = paging_fix
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
            if self.paging_fix:
                self.send("terminal length 0")
                self.prompt = self._wait_for_prompt(self.command_timeout)
                self.send("terminal width 511")
                self._wait_for_prompt(self.command_timeout)
        except DeviceError:
            self.close()
            raise
        return self.prompt

    def enable(self, enable_password: str = "") -> None:
        """进入 enable 特权模式（若已处于特权模式则跳过）。"""
        if self.prompt.endswith("#"):
            return
        self.send("enable")
        # 设备可能直接放行（本来就在 enable 模式），也可能索要密码
        out = self.read_until([r"[Pp]assword\s*:", PROMPT_RE.pattern], timeout=8)
        m = PROMPT_RE.search(out)
        if m:
            # 设备直接给了特权提示符，无需再送 enable 密码
            self.prompt = m.group(0).strip()
            if self.prompt.endswith("#"):
                return
        if not enable_password:
            raise DeviceError("设备要求 enable 密码，但未配置 enable password")
        self.send(enable_password)
        self.prompt = self._wait_for_prompt(self.command_timeout)
        if not self.prompt.endswith("#"):
            raise DeviceError(f"进入 enable 特权模式失败，当前提示符: {self.prompt!r}")

    # ------------------------------------------------------------------- io
    def send(self, line: str) -> None:
        if self.shell is None:
            raise DeviceError("SSH 会话未建立")
        self.shell.send(line + "\n")

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
            # 终端宽度不足时设备会插入 --More-- 分页，发送空格翻页
            while MORE_RE.search(text):
                self.shell.send(" ")
                time.sleep(0.15)
                try:
                    text += self.shell.recv(65535).decode("utf-8", errors="replace")
                except TimeoutError:
                    break
                except Exception:
                    break
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

    def _wait_for_prompt(self, timeout: float) -> str:
        """等待提示符出现，返回识别到的提示符（超时则返回最后一行）。"""
        deadline = time.time() + timeout
        while True:
            match = PROMPT_RE.search(self._strip_ansi(self._buffer))
            if match:
                self._buffer = ""
                return match.group(0).strip()
            if time.time() >= deadline:
                cleaned = self._strip_ansi(self._buffer).strip()
                self._buffer = ""
                return cleaned.splitlines()[-1] if cleaned else ">"
            self._pump(min(deadline, time.time() + 1.0))

    @staticmethod
    def _strip_ansi(text: str) -> str:
        return re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", text).replace(
            "\x1b[?25l", ""
        ).replace("\x1b[?25h", "")

    def run(self, command: str) -> str:
        """发送命令并返回该命令的输出（不含命令回显与提示符）。"""
        self._buffer = ""  # 丢弃发送前的残留，确保输出归属于本条命令
        self.send(command)
        deadline = time.time() + self.command_timeout
        while True:
            match = PROMPT_RE.search(self._strip_ansi(self._buffer))
            if match:
                raw = self._buffer[: match.start()]
                self._buffer = ""
                return raw
            if time.time() >= deadline:
                raw, self._buffer = self._buffer, ""
                return raw
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