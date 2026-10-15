"""模拟 Cisco 设备（SSH + Telnet 服务端），用于端到端验证采集与差异逻辑。

不依赖真实网络设备：
- SSH 走 paramiko 的服务器模式；
- Telnet 自己实现 IAC 应答与登录交互；
- 模拟设备维护一份内存配置，可通过 /mutate 端点改动，从而验证 diff 生成。
"""

from __future__ import annotations

import logging
import re
import socket
import threading
import time
from typing import Callable

import paramiko

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mock_device")

#仅用于本地测试的模拟设备凭据，与任何真实环境无关
USERNAME = "mockuser"
PASSWORD = "mock-password-123"
ENABLE_PASSWORD = "mock-enable-123"

HOST_KEY = paramiko.RSAKey.generate(2048)

CONFIG_V1 = """Building configuration...

Current configuration : 2048 bytes
!
version 15.2
service timestamps debug datetime msec
service timestamps log datetime msec
service password-encryption
!
hostname CORE-SW-01
!
boot-start-marker
boot-end-marker
!
no aaa new-model
clock timezone CST 8 8
!
ip routing
!
interface Loopback0
 description Router-ID
 ip address 10.255.0.1 255.255.255.255
!
interface GigabitEthernet0/1
 description uplink-to-core
 ip address 10.1.1.2 255.255.255.0
 duplex auto
 speed auto
!
interface GigabitEthernet0/2
 description access-port
 switchport mode access
 switchport access vlan 10
 spanning-tree portfast
!
router ospf 1
 router-id 10.255.0.1
 network 10.1.1.0 0.0.0.255 area 0
!
line vty 0 4
 password 7 070C285F4D06
 login local
 transport input ssh
!
end"""

CONFIG_V2 = CONFIG_V1.replace(
    "interface GigabitEthernet0/2\n description access-port",
    "interface GigabitEthernet0/2\n description access-port-printer",
).replace("spanning-tree portfast\n", "spanning-tree portfast\n switchport mode access\n")

CDP_DETAIL = """-------------------------
Device ID: SW-CORE-02.corp.local
Entry address(es):
  IP address: 10.1.1.3
Platform: cisco WS-C3750-48P,  Capabilities: Switch IGMP
Interface: GigabitEthernet0/1,  Port ID (outgoing port): GigabitEthernet0/24
-------------------------
Device ID: AP-FLOOR3-01.corp.local
Entry address(es):
  IP address: 10.1.3.50
Platform: cisco AIR-AP2802I-E,  Capabilities: Bridge AirStation
Interface: GigabitEthernet0/2,  Port ID (outgoing port): GigabitEthernet0
-------------------------"""

RESPONSES = {
    "show running-config": lambda s: CONFIG_V1 if s["v"] == 1 else CONFIG_V2,
    "show run": lambda s: CONFIG_V1 if s["v"] == 1 else CONFIG_V2,
    "show cdp neighbors detail": lambda s: CDP_DETAIL,
    "show cdp neighbor detail": lambda s: CDP_DETAIL,
    "show version": lambda s: (
        "Cisco IOS Software, C3560 Software (C3560-IPSERVICESK9-M), "
        "Version 15.2(4)E7, RELEASE SOFTWARE (fc3)\n"
        "uptime is 12 weeks, 3 days"
    ),
    "show vlan brief": lambda s: (
        " VLAN Name                             Status    Ports\n"
        "---- -------------------------------- --------- -------------------------------\n"
        "1    default                          active    Gi0/3, Gi0/4\n"
        "10   users                            active    Gi0/2\n"
    ),
    # ---------- H3C Comware ----------
    "display current-configuration": lambda s: H3C_CONFIG_V1 if s["v"] == 1 else H3C_CONFIG_V2,
    "display version": lambda s: (
        "H3C Comware Software, Version 7.1.070, Release 3111P05\n"
        "Copyright (c) 2004-2021 New H3C Technologies Co., Ltd.\n"
        "H3C S5560X-54C-EI uptime is 12 weeks"
    ),
    "screen-length disable": lambda s: "",
    "screen-width 511": lambda s: "",
    # ---------- 华为 VRP ----------
    "display lldp neighbor brief": lambda s: HUAWEI_LLDP_BRIEF,
    "display interface brief": lambda s: (
        "Interface                PHY   Protocol InUti OutUti   inErrors  outErrors\n"
        "GigabitEthernet0/0/1     up    up         0%    0%           0          0\n"
    ),
    "screen-length 0 temporary": lambda s: "",
    # ---------- 锐捷 RGOS ----------
    "show lldp neighbors": lambda s: RUIJIE_LLDP,
}

HUAWEI_CONFIG = """#
!Software Version VRP (R) Software, Version 8.180 (CE6865 V200R023C10)
!Copyright (C) 2012-2026 Huawei Technologies Co., Ltd.
sysname HUAWEI-CORE-SW
#
 vlan batch 10 20
#
interface Vlanif10
 ip address 192.168.10.1 255.255.255.0
#
interface GigabitEthernet0/0/1
 description uplink-to-core
 port link-type trunk
 port trunk allow-pass vlan 10 20
#
interface GigabitEthernet0/0/2
 port link-type access
 port default vlan 10
 stp edged-port enable
#
 lldp enable
#
snmp-agent community read cipher %^%#%^%#%
#
user-interface vty 0 4
 authentication-mode aaa
#
return"""

# 华为的 display current-configuration 与 H3C 同名，用状态区分厂商：
# mock 侧按实际测试需要，这里让两端各自服务器复用同一响应即可。
RESPONSES["display current-configuration"] = (
    lambda s: H3C_CONFIG_V1 if s["v"] == 1 else H3C_CONFIG_V2
)

H3C_CONFIG_V1 = """#
 version 7.1.070, Release 3111P05
#
sysname H3C-CORE-SW
#
 irb mac-address timer auto
#
interface LoopBack0
 description Router-ID
 ip address 10.255.0.1 255.255.255.255
#
interface GigabitEthernet1/0/1
 description uplink-to-core
 port link-mode bridge
 speed 1000
#
interface GigabitEthernet1/0/2
 port link-mode bridge
 port access vlan 10
 stp edged-port enable
#
 info-center loghost 10.0.0.100
#
line vty 0 63
 authentication-mode scheme
#
return"""

H3C_CONFIG_V2 = H3C_CONFIG_V1.replace(
    "description uplink-to-core",
    "description uplink-to-core-printer",
).replace("stp edged-port enable", "stp edged-port enable\n loopback-detect enable")

H3C_LLDP = """LLDP neighbor-information of port 1[GigabitEthernet1/0/1]
  Neighbor index :1
  Device type     :LLDP device
  System name     :H3C-ACCESS-SW
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
  System name     :H3C-AP-01
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

HUAWEI_LLDP_BRIEF = """Local Intf            Neighbor Dev       Neighbor Intf      Exptime(s)
-------------------------------------------------------------------------------
10GE1/0/1             HUAWEI-CORE-02     10GE1/0/24         120
10GE1/0/2             HUAWEI-ACCESS-01   10GE1/0/25         98
"""

RUIJIE_LLDP = """Interface          Chassis ID          Port ID                System Name
GigabitEthernet0/1  4c1f-cc12-3456GigabitEthernet0/24       RG-CORE-02
GigabitEthernet0/2  4c1f-cc12-7890  GigabitEthernet0/25       RG-ACCESS-01
"""

# 各厂商登录 banner
VENDOR_BANNER = {
    "cisco": b"\r\nCisco IOS Software, C3560 Software\r\nCopyright (c) 1986-2010\r\n",
    "h3c": b"\r\nH3C Comware Software, Version 7.1.070, Release 3111P05\r\n",
    "huawei": b"\r\nHuawei Versatile Routing Platform Software\r\nVRP (R) Software, Version 8.180\r\n",
    "ruijie": b"\r\nRuijie RGOS\r\n",
}

# 各厂商初始提示符
VENDOR_PROMPT = {
    "cisco": "CORE-SW-01>",
    "h3c": "<H3C-CORE-SW>",
    "huawei": "<HUAWEI-CORE-SW>",
    "ruijie": "RG-SW-01>",
}

STATE = {"v": 1, "vendor": "cisco"}


class MockSession(paramiko.ServerInterface):
    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_auth_password(self, username, password):
        if username == USERNAME and password == PASSWORD:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_shell_request(self, channel):
        return True

    def check_channel_pty_request(self, *args, **kwargs):
        return True


class MockSSHServer(threading.Thread):
    """后台线程里跑一个极简 CLI shell 服务端。

    注意：paramiko 的 Transport 不会自动 accept 监听套接字，
    必须由我们自己 accept 出已连接的 socket 再交给 Transport。

    vendor 参数决定 banner、初始提示符与登录后的行为（华为/H3C 不需 enable）。
    """

    daemon = True

    def __init__(self, host: str = "127.0.0.1", port: int = 0, vendor: str = "cisco") -> None:
        super().__init__(daemon=True)
        self.vendor = vendor
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(16)
        self.sock.settimeout(1.0)
        self.host, self.port = self.sock.getsockname()
        self.running = True
        self.ready = threading.Event()
        self.transports: list[paramiko.Transport] = []

    def run(self) -> None:
        STATE["vendor"] = self.vendor
        self.ready.set()
        log.info("mock SSH 服务端已启动 %s:%s (vendor=%s)", self.host, self.port, self.vendor)
        while self.running:
            try:
                conn, addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve_conn, args=(conn,), daemon=True).start()

    def _serve_conn(self, conn: socket.socket) -> None:
        transport = paramiko.Transport(conn)
        self.transports.append(transport)
        transport.add_server_key(HOST_KEY)
        # event 必传，否则 start_server 会阻塞等待协商，accept 循环无法推进
        transport.start_server(server=MockSession(), event=threading.Event())
        try:
            chan = transport.accept(30)
            if chan is None:
                return
            self._serve(chan)
        except Exception:
            log.exception("mock SSH 会话异常")
        finally:
            try:
                transport.close()
            except Exception:
                pass

    def _serve(self, chan) -> None:
        """在已建立的 channel 上跑 CLI 会话。

        状态机：cli -> enable 时切到 awaiting_enable_password，收到密码后回到 cli。
        华为/H3C/锐捷登录后即为特权视图，不需要 enable。
        """
        vendor = self.vendor
        needs_enable = vendor == "cisco"
        enabled = not needs_enable  # 非 Cisco 视作已在特权视图
        awaiting_enable_pwd = False
        prompt = VENDOR_PROMPT.get(vendor, "SW>")
        # 只有 Cisco 用 # 表示特权视图；华为/H3C 一律是尖括号
        priv_prompt = prompt.replace(">", "#", 1) if vendor == "cisco" else prompt
        chan.settimeout(60)
        chan.send(VENDOR_BANNER.get(vendor, b"\r\n"))
        chan.send(b"\r\nUser Access Verification\r\n\r\n")
        buf = b""
        try:
            while self.running:
                try:
                    data = chan.recv(4096)
                except socket.timeout:
                    break
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    idx = buf.index(b"\n")
                    line = buf[:idx].decode("utf-8", errors="replace").replace("\r", "").strip()
                    buf = buf[idx + 1 :]

                    if awaiting_enable_pwd:
                        awaiting_enable_pwd = False
                        if line == ENABLE_PASSWORD:
                            enabled = True
                            chan.send(f"\r\n{priv_prompt} ".encode())
                        else:
                            chan.send(b"\r\n% Access denied\r\n")
                            chan.send(f"\r\n{prompt} ".encode())
                        continue

                    low = line.lower()
                    if low == "enable" and needs_enable:
                        if not enabled:
                            awaiting_enable_pwd = True
                            chan.send(b"\r\nPassword: ")
                        else:
                            chan.send(f"\r\n{priv_prompt} ".encode())
                        continue

                    resp = _exec(line, enabled)
                    cur = priv_prompt if enabled else prompt
                    if resp == "":
                        chan.send(f"\r\n{cur} ".encode())
                    else:
                        chan.send(
                            ("\r\n" + resp.replace("\n", "\r\n") + f"\r\n{cur} ").encode()
                        )
        except OSError:
            pass
        finally:
            try:
                chan.close()
            except Exception:
                pass

    def stop(self) -> None:
        self.running = False
        for t in self.transports:
            try:
                t.close()
            except Exception:
                pass
        try:
            self.sock.close()
        except Exception:
            pass


def _exec(line: str, enabled: bool) -> str:
    """模拟设备 CLI 的命令执行，返回输出文本（空串表示无输出）。"""
    low = line.lower()
    vendor = STATE.get("vendor", "cisco")
    if not line or low in (
        "terminal length 0", "terminal width 511",
        "screen-length disable", "screen-width 511", "screen-length 0 temporary",
    ):
        return ""
    # 华为与 H3C 命令相同，按厂商返回各自的配置样例
    if low == "display current-configuration":
        if vendor == "huawei":
            return HUAWEI_CONFIG
        return H3C_CONFIG_V1 if STATE["v"] == 1 else H3C_CONFIG_V2
    if low == "display version":
        if vendor == "huawei":
            return (
                "Huawei Versatile Routing Platform Software\n"
                "VRP (R) Software, Version 8.180 (CE6865 V200R023C10)\n"
                "Copyright (C) 2012-2026 Huawei Technologies Co., Ltd."
            )
        return (
            "H3C Comware Software, Version 7.1.070, Release 3111P05\n"
            "Copyright (c) 2004-2021 New H3C Technologies Co., Ltd.\n"
            "H3C S5560X-54C-EI uptime is 12 weeks"
        )
    # H3C 的 LLDP 输出是verbose 分块格式，华为是表格，按厂商区分
    if low == "display lldp neighbor-information":
        if vendor == "huawei":
            return HUAWEI_LLDP_BRIEF
        return H3C_LLDP
    if low.startswith("show ") or low.startswith("display "):
        if not enabled:
            return "% Invalid input detected at '^' marker."
        handler = RESPONSES.get(low)
        return handler(STATE) if handler else "% Invalid input detected at '^' marker."
    return "% Invalid input detected at '^' marker."


# ------------------------------------------------------------------ Telnet 侧
IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240


class MockTelnetServer(threading.Thread):
    """模拟 Telnet 设备：协商 IAC -> Username -> Password -> enable -> CLI。"""

    daemon = True

    def __init__(self, host: str = "127.0.0.1", port: int = 0, vendor: str = "cisco") -> None:
        super().__init__(daemon=True)
        self.vendor = vendor
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(8)
        self.sock.settimeout(1.0)
        self.host, self.port = self.sock.getsockname()
        self.running = True
        self.ready = threading.Event()

    def run(self) -> None:
        STATE["vendor"] = self.vendor
        self.ready.set()
        log.info("mock Telnet 服务端已启动 %s:%s (vendor=%s)", self.host, self.port, self.vendor)
        while self.running:
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn: socket.socket) -> None:
        conn.settimeout(30)
        # 主动发起几个 IAC 协商，验证客户端能否正确应答而不是卡死
        try:
            conn.sendall(bytes([IAC, DO, 1, IAC, WILL, 3, IAC, SB, 24, 1, 0, IAC, SE]))
        except OSError:
            return
        buf = ""
        stage = "user"
        vendor = self.vendor
        needs_enable = vendor == "cisco"
        enabled = not needs_enable  # 非 Cisco 视作已在特权视图
        prompt = VENDOR_PROMPT.get(vendor, "SW>")
        try:
            conn.sendall(VENDOR_BANNER.get(vendor, b"\r\n"))
            conn.sendall(b"\r\nUser Access Verification\r\n\r\nUsername: ")
            while True:
                data = conn.recv(4096).decode("utf-8", errors="replace")
                if not data:
                    return
                buf += data
                while "\r\n" in buf or "\n" in buf:
                    idx = buf.find("\n")
                    line, buf = buf[:idx].replace("\r", ""), buf[idx + 1 :]
                    if stage == "user":
                        if line.strip() != USERNAME:
                            conn.sendall(b"\r\n% Login invalid\r\nUsername: ")
                            continue
                        stage = "pass"
                        conn.sendall(b"\r\nPassword: ")
                    elif stage == "pass":
                        if line.strip() != PASSWORD:
                            conn.sendall(b"\r\n% Login invalid\r\nUsername: ")
                            stage = "user"
                            continue
                        stage = "cli"
                        conn.sendall(f"\r\n\r\n{prompt} ".encode())
                    elif stage == "cli":
                        resp = self._exec_cli(line.strip(), enabled)
                        if resp is None:  # enable 密码校验
                            stage = "enable_pass"
                            conn.sendall(b"\r\nPassword: ")
                            continue
                        if resp == "ENABLE":
                            enabled = True
                            prompt = prompt.replace(">", "#", 1)
                            stage = "cli"
                            conn.sendall(f"\r\n{prompt} ".encode())
                            continue
                        body = resp.replace("\n", "\r\n")
                        conn.sendall(f"\r\n{body}\r\n{prompt} ".encode())
                    elif stage == "enable_pass":
                        if line.strip() != ENABLE_PASSWORD:
                            conn.sendall(b"\r\n% Access denied\r\n")
                            stage = "cli"
                            conn.sendall(f"\r\n{prompt} ".encode())
                            continue
                        enabled = True
                        prompt = prompt.replace(">", "#", 1)
                        stage = "cli"
                        conn.sendall(f"\r\n{prompt} ".encode())
        except (OSError, socket.timeout):
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _exec_cli(self, line: str, enabled: bool) -> str | None:
        """Telnet 侧的命令执行。返回 None 表示设备正在索要 enable 密码。"""
        low = line.lower()
        if low == "enable":
            return None if enabled is False else "ENABLE"
        return _exec(line, enabled)

    def stop(self) -> None:
        self.running = False
        try:
            self.sock.close()
        except Exception:
            pass


def mutate() -> None:
    """切换到配置 V2，用于验证差异生成。"""
    STATE["v"] = 2