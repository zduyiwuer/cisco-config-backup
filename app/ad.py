"""Active Directory / LDAP 认证。

支持两种绑定策略：
1. **搜索绑定**（推荐）：先用服务账号连域，在指定 Base DN 下搜索用户，
   拿到完整 DN 后用用户自己的密码 bind 验证。兼容性最好。
2. **直接绑定**：用 DOMAIN\\user 或 user@domain 形式直接 bind。
   配置简单，但要求服务账号无权限时不可用。

安全约束：
- 只允许使用系统上已验证通过的证书，LDAPS 证书自签名时需显式配置 CA 路径，
  绝不提供"跳过证书校验"的开关。
- 连接与绑定均有超时，避免域控不可达时把登录请求挂死。
"""

from __future__ import annotations

import logging
import ssl
from typing import Any

from . import db

log = logging.getLogger("cisco-backup.ad")

DEFAULT_TIMEOUT = 10


class ADAuthError(Exception):
    """认证失败的可预期原因，消息会展示给用户。"""


def _settings() -> dict[str, Any]:
    return db.get_settings()


def ldap_enabled() -> bool:
    return bool(_settings().get("ad_enabled"))


def _escape_filter(value: str) -> str:
    """转义 LDAP 过滤器特殊字符，防注入。"""
    out = []
    for ch in value:
        if ch in ("\\", "*", "(", ")", "\x00"):
            out.append("\\%02x" % ord(ch))
        else:
            out.append(ch)
    return "".join(out)


def _build_server():
    from ldap3 import NONE, Server, Tls

    cfg = _settings()
    host = (cfg.get("ad_host") or "").strip()
    if not host:
        raise ADAuthError("未配置 AD 服务器地址")

    use_ssl = bool(cfg.get("ad_use_ssl"))
    port = int(cfg.get("ad_port") or (636 if use_ssl else 389))
    ca_file = (cfg.get("ad_ca_cert") or "").strip()

    tls = None
    if use_ssl:
        validate = ssl.CERT_REQUIRED
        if ca_file:
            tls = Tls(validate=validate, ca_certs_file=ca_file)
        else:
            # 未提供 CA 时使用系统信任库，仍保持校验开启
            tls = Tls(validate=validate)

    return Server(
        host,
        port=port,
        use_ssl=use_ssl,
        tls=tls,
        get_info=NONE,
        connect_timeout=DEFAULT_TIMEOUT,
    )


def _normalize_domain(username: str) -> str:
    """把多种域账号写法统一成 user@domain。"""
    domain = (_settings().get("ad_domain") or "").strip()
    if username in ("", ".", "anonymous"):
        raise ADAuthError("请输入域账号")
    if "\\" in username:
        user, _, dom = username.partition("\\")
        return f"{user}@{dom}" if dom else username
    if "@" in username:
        return username
    return f"{username}@{domain}" if domain else username


def authenticate(username: str, password: str) -> dict[str, Any]:
    """验证 AD 账号。成功返回用户信息，失败抛 ADAuthError。"""
    if not password:
        raise ADAuthError("请输入密码")
    cfg = _settings()
    if not cfg.get("ad_enabled"):
        raise ADAuthError("未启用 AD 登录")

    try:
        from ldap3 import Connection, SAFE_SYNC, NTLM, SIMPLE
    except ImportError as exc:  # pragma: no cover
        raise ADAuthError("服务端缺少 ldap3 库，无法使用 AD 登录") from exc

    # AD 默认要求加密；未启用 LDAPS 时拒绝明文传输密码
    if not cfg.get("ad_use_ssl") and not cfg.get("ad_allow_plaintext"):
        raise ADAuthError(
            "当前为明文 LDAP（389 端口），为避免密码明文传输请启用 LDAPS，"
            "或在设置中显式允许明文连接"
        )

    login = _normalize_domain(username)
    user_part = login.split("@")[0]
    base_dn = (cfg.get("ad_base_dn") or "").strip()
    bind_dn = (cfg.get("ad_bind_dn") or "").strip()
    bind_pw = (cfg.get("ad_bind_password") or "").strip()
    auth_strategy = (cfg.get("ad_auth_strategy") or "search").strip().lower()

    server = _build_server()
    auth_mechanism = NTLM if cfg.get("ad_use_ntlm") else SIMPLE

    try:
        if auth_strategy == "direct" or not bind_dn:
            # 直接绑定：ldap3 的 SIMPLE 认证需要 user@domain 形式
            conn = Connection(
                server,
                user=login if auth_mechanism == NTLM else login,
                password=password,
                authentication=auth_mechanism,
                auto_bind=True,
                raise_exceptions=True,
                receive_timeout=DEFAULT_TIMEOUT,
            )
            conn.unbind()
            dn, attrs = _lookup_by_domain_login(server, login, auth_mechanism, cfg)
            return _build_identity(username, dn, attrs, "direct")

        # 搜索绑定：服务账号查 DN，再用用户密码验证
        service = Connection(
            server,
            user=bind_dn,
            password=bind_pw,
            authentication=auth_mechanism,
            auto_bind=True,
            raise_exceptions=True,
            receive_timeout=DEFAULT_TIMEOUT,
        )
        try:
            search_filter = (
                f"(&(objectClass=user)(sAMAccountName={_escape_filter(user_part)}))"
            )
            attributes = ["cn", "displayName", "mail", "memberOf", "distinguishedName"]
            service.search(
                search_base=base_dn or None,
                search_filter=search_filter,
                search_scope="SUBTREE",
                attributes=attributes,
            )
            if not service.entries:
                raise ADAuthError("域账号不存在或已禁用")
            entry = service.entries[0]
            dn = str(entry.entry_dn)
            groups = [str(g) for g in (entry.memberOf.values if hasattr(entry, "memberOf") else [])]
            display = ""
            mail = ""
            if hasattr(entry, "displayName") and entry.displayName:
                display = str(entry.displayName)
            if hasattr(entry, "mail") and entry.mail:
                mail = str(entry.mail)
        finally:
            service.unbind()

        # 用用户自己的密码重新绑定验证
        user_conn = Connection(
            server,
            user=dn,
            password=password,
            authentication=auth_mechanism,
            auto_bind=True,
            raise_exceptions=True,
            receive_timeout=DEFAULT_TIMEOUT,
        )
        user_conn.unbind()
        return _build_identity(username, dn, {"displayName": display, "mail": mail, "memberOf": groups}, "search")

    except ADAuthError:
        raise
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        # ldap3 的异常信息可能包含连接细节，过滤后再展示
        if "invalidCredentials" in message or "52e" in message.lower():
            raise ADAuthError("域账号或密码错误") from exc
        if "invalidDNSName" in message or "Name or service not known" in message:
            raise ADAuthError(f"无法解析 AD 服务器地址，请检查设置：{cfg.get('ad_host')}") from exc
        log.warning("AD 认证异常: %s", type(exc).__name__)
        raise ADAuthError("AD 服务器连接失败，请检查网络与配置") from exc


def _lookup_by_domain_login(server, login: str, mechanism, cfg) -> tuple[str, dict]:
    """直接绑定模式下再查一次用户属性，用于取显示名与邮箱。"""
    try:
        from ldap3 import Connection

        user_part = login.split("@")[0]
        cfg_bind_dn = (cfg.get("ad_bind_dn") or "").strip()
        cfg_bind_pw = (cfg.get("ad_bind_password") or "").strip()
        if not cfg_bind_dn:
            return "", {}
        service = Connection(
            server, user=cfg_bind_dn, password=cfg_bind_pw,
            authentication=mechanism, auto_bind=True, raise_exceptions=True,
        )
        try:
            service.search(
                search_base=(cfg.get("ad_base_dn") or None),
                search_filter=f"(&(objectClass=user)(sAMAccountName={_escape_filter(user_part)}))",
                search_scope="SUBTREE",
                attributes=["cn", "displayName", "mail", "memberOf"],
            )
            if not service.entries:
                return "", {}
            entry = service.entries[0]
            attrs = {
                "displayName": str(entry.displayName) if hasattr(entry, "displayName") and entry.displayName else "",
                "mail": str(entry.mail) if hasattr(entry, "mail") and entry.mail else "",
                "memberOf": [str(g) for g in (entry.memberOf.values if hasattr(entry, "memberOf") else [])],
            }
            return str(entry.entry_dn), attrs
        finally:
            service.unbind()
    except Exception:  # noqa: BLE001
        return "", {}


def _dn_parts(dn: str) -> tuple[str, str]:
    """把 DN 拆成 (cn小写, 完整小写)。非 DN 形式返回 (原样小写, 原样小写)。"""
    low = (dn or "").strip().lower()
    if "=" not in low:
        return low, low
    first = low.split(",")[0]
    cn = first.split("=", 1)[1].strip() if "=" in first else first
    return cn, low


def _split_candidates(raw: str) -> list[str]:
    """拆分多个候选。

    先按换行/分号切（不会破坏 DN 内部的逗号），
    再对不含 '=' 的纯文本项按逗号切（兼容 "NetOps,Domain Admins" 这种写法）。
    """
    items: list[str] = []
    for chunk in raw.replace(";", "\n").splitlines():
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" in chunk:  # 完整 DN，整体保留
            items.append(chunk)
            continue
        # 纯组名列表
        items.extend(x.strip() for x in chunk.split(",") if x.strip())
    return items


def _is_admin_by_group(groups: list[str]) -> bool:
    """按 AD 组判定是否管理员。

    支持三种写法（多个候选用换行或分号分隔）：
    - 完整 DN：CN=NetOps,DC=corp,DC=local
    - 仅组名：NetOps
    - 组名列表：NetOps,Domain Admins

    匹配时先按完整 DN 比对，再退化到只比 CN，避免不同 DN 下的同名组混淆。
    """
    cfg = _settings()
    admin_group = (cfg.get("ad_admin_group") or "").strip()
    if not admin_group:
        return False

    group_keys = [_dn_parts(g) for g in groups]
    for name in _split_candidates(admin_group):
        cand_cn, cand_dn = _dn_parts(name)
        for grp_cn, grp_dn in group_keys:
            if "=" in name.lower():
                if grp_dn == cand_dn:
                    return True
            elif grp_cn == cand_cn:
                return True
    return False


def _build_identity(username: str, dn: str, attrs: dict, strategy: str) -> dict[str, Any]:
    groups = attrs.get("memberOf") or []
    cfg = _settings()
    admin_group = (cfg.get("ad_admin_group") or "").strip()

    if admin_group:
        role = "admin" if _is_admin_by_group(groups) else (
            cfg.get("ad_default_role") or "viewer"
        )
    else:
        # 未配置管理员组：所有 AD 用户给 operator，避免默认把人挡在门外
        role = cfg.get("ad_default_role") or "operator"

    return {
        "username": username.strip(),
        "display_name": attrs.get("displayName") or username.strip(),
        "email": attrs.get("mail") or "",
        "ad_dn": dn,
        "role": role,
        "source": "ad",
        "strategy": strategy,
        "groups": groups,
    }


def test_connection() -> dict[str, Any]:
    """测试 AD 连通性，供设置页使用。"""
    cfg = _settings()
    if not cfg.get("ad_host"):
        return {"ok": False, "message": "未配置 AD 服务器地址"}
    try:
        from ldap3 import Connection

        server = _build_server()
        if cfg.get("ad_bind_dn"):
            conn = Connection(
                server,
                user=cfg["ad_bind_dn"],
                password=cfg.get("ad_bind_password") or "",
                auto_bind=True,
                raise_exceptions=True,
                receive_timeout=DEFAULT_TIMEOUT,
            )
            conn.unbind()
            return {"ok": True, "message": "连接成功，服务账号绑定正常"}
        # 无服务账号时只验证能否连上并读到根 DSE
        conn = Connection(server, auto_bind=True, raise_exceptions=True)
        naming = []
        if conn.server.info:
            naming = list(conn.server.info.naming_contexts or [])
        conn.unbind()
        return {"ok": True, "message": "连接成功", "naming_contexts": naming}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"连接失败: {type(exc).__name__}: {exc}"}