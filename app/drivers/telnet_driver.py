"""极简 Telnet 客户端（多厂商通用）。

Python 3.13 已移除标准库 telnetlib，这里用 socket 手写：
- 完整处理 IAC 协商（DO/DONT/WILL/WONT/SB-SE），拒绝所有请求，避免卡在协商阶段；
- 等待登录提示 -> 发送用户名 -> 等待 Password 提示 -> 发送密码；
- 之后按厂商提示符采集命令输出，对分页自动翻页。

支持 Cisco IOS / H3C Comware / 华为 VRP / 锐捷 RGOS。
"""

from __future__ import annotations

import re
import socket
import time

from .. import vendors
from .base import DeviceError

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
MORE_RE = re.compile(r"--More--", re.IGNORECASE)
MORE_RE_ALT = re.compile(
    r"(?:----\s*More\s*----|Press\s+Q\s+to\s+break|继续|\[Y/N\]|More\s*\()", re.IGNORECASE
)


class TelnetClient:
    def __init__(
        self,
        host: str,
        port: int = 23,
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
        self.sock: socket.socket | None = None
        self._buffer = ""
        self.prompt = ""

    # ------------------------------------------------------------ telnet 协议
    @staticmethod
    def _strip_iac(data: str) -> tuple[str, str]:
        """解析 IAC 序列，返回 (可读数据, 待回复的字节)。"""
        out: list[str] = []
        reply = bytearray()
        i, n = 0, len(data)
        while i < n:
            ch = data[i]
            if ch != IAC:
                out.append(ch)
                i += 1
                continue
            if i + 1 >= n:
                break
            cmd = ord(data[i + 1])
            if cmd == IAC:  # 转义后的 0xFF
                out.append("\xff")
                i += 2
            elif cmd in (DO, DONT, WILL, WONT):
                if i + 2 >= n:
                    break
                option = ord(data[i + 2])
                # 全部拒绝：WONT 对 WILL/DO，DONT 对 DONT/WILL
                if cmd in (DO, WILL):
                    reply += bytes([IAC, WONT if cmd == DO else DONT, option])
                i += 3
            elif cmd == SB:
                j = data.find(f"\xff{chr(SE)}", i)
                i = n if j == -1 else j + 2
            else:
                i += 2
        return "".join(out), reply.decode("latin-1")

    # ----------------------------------------------------------------- connect
    def connect(self) -> str:
        try:
            self.sock = socket.create_connection(
                (self.host, self.port), timeout=self.connect_timeout
            )
            self.sock.settimeout(1.0)
        except Exception as exc:
            raise DeviceError(f"Telnet 连接失败: {type(exc).__name__}: {exc}") from exc

        try:
            banner = self._read_until(
                [r"[Uu]sername\s*:", r"[Ll]ogin\s*:", r"[Pp]assword\s*:"],
                self.connect_timeout,
            )
            if re.search(r"[Pp]assword\s*:", banner) and not self.username:
                raise DeviceError("设备直接要求密码，未配置用户名")

            if re.search(r"[Pp]assword\s*:", banner):
                self._write(self.password)
                after = self._read_until(
                    [r"[Pp]assword\s*:", r"[Ll]ogin incorrect", self._primary_prompt()],
                    self.connect_timeout,
                )
                if re.search(r"[Pp]assword\s*:", after):
                    raise DeviceError("Telnet 登录失败：用户名或密码错误")
                banner += after
            else:
                self._write(self.username)
                after = self._read_until(
                    [r"[Pp]assword\s*:", self._primary_prompt()], self.connect_timeout
                )
                banner += after
                if re.search(r"[Pp]assword\s*:", after):
                    self._write(self.password)
                    banner += self._read_until(
                        [r"[Pp]assword\s*:", r"[Ll]ogin incorrect", self._primary_prompt()],
                        self.connect_timeout,
                    )
                    if "incorrect" in banner.lower():
                        raise DeviceError("Telnet 登录失败：用户名或密码错误")

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

    def _primary_prompt(self) -> str:
        return self.prompt_patterns[0].pattern

    def enable(self, enable_password: str = "") -> None:
        """进入特权模式。仅 Cisco 需要。"""
        if not self.vendor["needs_enable"]:
            return
        if self.prompt.rstrip().endswith("#"):
            return
        self.send("enable")
        time.sleep(0.3)
        self._read_until([r"[Pp]assword\s*:", self._primary_prompt()], 6)
        if not self.prompt.rstrip().endswith("#"):
            if not enable_password:
                raise DeviceError("设备要求 enable 密码，但未配置 enable password")
            self.send(enable_password)
            self.prompt = self._wait_for_prompt(self.command_timeout)
        if not self.prompt.rstrip().endswith("#"):
            raise DeviceError(f"进入 enable 特权模式失败，当前提示符: {self.prompt!r}")

    # ---------------------------------------------------------------------- io
    def _write(self, text: str) -> None:
        if self.sock is None:
            raise DeviceError("Telnet 会话未建立")
        self.sock.sendall((text + "\r\n").encode("utf-8", errors="replace"))

    def send(self, line: str) -> None:
        self._write(line)

    @staticmethod
    def _has_more(text: str) -> bool:
        return bool(MORE_RE.search(text) or MORE_RE_ALT.search(text))

    def _strip_more(self, text: str) -> str:
        text = MORE_RE.sub("", text)
        return MORE_RE_ALT.sub("", text)

    def _recv(self, deadline: float) -> None:
        assert self.sock is not None
        while time.time() < deadline:
            try:
                data = self.sock.recv(65535)
            except socket.timeout:
                continue
            except OSError as exc:
                raise DeviceError(f"读取设备输出失败: {exc}") from exc
            if not data:
                raise DeviceError("设备主动断开了连接")
            text, reply = self._strip_iac(data.decode("latin-1", errors="replace"))
            self._buffer += text
            if reply:
                try:
                    self.sock.sendall(reply.encode("latin-1"))
                except OSError:
                    pass
            # 分页处理：连续发送空格直到不再出现分页提示
            guard = 0
            while self._has_more(self._buffer) and guard < 2000:
                guard += 1
                self._buffer = self._strip_more(self._buffer)
                try:
                    self.sock.sendall(b" ")
                except OSError:
                    return
                time.sleep(0.05)
                self._recv_chunk()

    def _recv_chunk(self) -> None:
        assert self.sock is not None
        try:
            data = self.sock.recv(65535)
        except socket.timeout:
            return
        except OSError:
            return
        if data:
            text, reply = self._strip_iac(data.decode("latin-1", errors="replace"))
            self._buffer += text
            if reply:
                try:
                    self.sock.sendall(reply.encode("latin-1"))
                except OSError:
                    pass

    def _read_until(self, patterns: list[str], timeout: float) -> str:
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
            self._recv(min(deadline, time.time() + 1.0))

    def _find_prompt(self, text: str) -> re.Match[str] | None:
        cleaned = text.replace("\r\n", "\n")
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
                out = self._buffer.replace("\r\n", "\n").strip()
                self._buffer = ""
                return out.splitlines()[-1] if out else ">"
            self._recv(min(deadline, time.time() + 1.0))

    def run(self, command: str) -> str:
        """发送命令并返回该命令的输出（不含命令回显与提示符）。"""
        self._buffer = ""  # 丢弃发送前的残留
        self.send(command)
        deadline = time.time() + self.command_timeout
        while True:
            match = self._find_prompt(self._buffer)
            if match:
                raw = self._buffer[: match.start()]
                self._buffer = ""
                return self._strip_more(raw)
            if time.time() >= deadline:
                raw, self._buffer = self._buffer, ""
                return self._strip_more(raw)
            self._recv(min(deadline, time.time() + 1.0))

    def close(self) -> None:
        try:
            if self.sock is not None:
                self.sock.close()
        except Exception:
            pass
        self.sock = None