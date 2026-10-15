"""用户、会话与登录审计的数据层。

要点：
- 密码只存 PBKDF2-HMAC-SHA256 派生值，绝不存明文。
- 会话 token 用 secrets.token_urls 生成，数据库只存哈希后的 token，
  即使数据库泄露也无法直接冒用会话。
- AD 用户首次登录自动建档，可配置哪些域用户自动成为管理员。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from . import db

PBKDF2_ROUNDS = 260_000
SESSION_HOURS = 12
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15

ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLE_VIEWER = "viewer"
ROLES = (ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER)

STATUS_ACTIVE = "active"
STATUS_LOCKED = "locked"
STATUS_DISABLED = "disabled"


# --------------------------------------------------------------------- 密码
def hash_password(password: str) -> str:
    """派生密码：pbkdf2_sha256$rounds$salt$hash，全部十六进制存储。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码。使用 hmac.compare_digest 防时序攻击。"""
    if not stored or not password:
        return False
    try:
        algo, rounds, salt_hex, digest_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds)
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def password_strength(password: str) -> tuple[bool, str]:
    """密码强度检查。返回 (是否通过, 说明)。"""
    if not password or len(password) < 8:
        return False, "密码至少 8 位"
    if not any(c.isalpha() for c in password):
        return False, "密码需包含字母"
    if not any(c.isdigit() for c in password):
        return False, "密码需包含数字"
    return True, "强度合格"


# --------------------------------------------------------------------- token
def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """会话 token 落库前先哈希，数据库泄露也无法冒用。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------- 用户
def create_user(
    username: str,
    password: str | None,
    role: str = ROLE_OPERATOR,
    source: str = "local",
    display_name: str = "",
    email: str = "",
    ad_dn: str = "",
) -> int:
    ts = db.now_iso()
    with db.tx() as conn:
        cur = conn.execute(
            """
            INSERT INTO users (username, password_hash, role, source, display_name,
                               email, ad_dn, status, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                username,
                hash_password(password) if password else None,
                role if role in ROLES else ROLE_VIEWER,
                source,
                display_name or username,
                email,
                ad_dn,
                STATUS_ACTIVE,
                ts,
                ts,
            ),
        )
        return int(cur.lastrowid)


def get_user(user_id: int) -> dict[str, Any] | None:
    row = db.get_conn().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _row_to_user(row) if row else None


def get_user_by_username(username: str) -> dict[str, Any] | None:
    row = db.get_conn().execute(
        "SELECT * FROM users WHERE username = ?", (username.strip(),)
    ).fetchone()
    return _row_to_user(row) if row else None


def _row_to_user(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "source": row["source"],
        "display_name": row["display_name"] or row["username"],
        "email": row["email"] or "",
        "ad_dn": row["ad_dn"] or "",
        "status": row["status"],
        "must_change_password": bool(row["must_change_password"]),
        "failed_attempts": row["failed_attempts"],
        "locked_until": row["locked_until"],
        "last_login_at": row["last_login_at"],
        "created_at": row["created_at"],
    }


def list_users() -> list[dict[str, Any]]:
    rows = db.get_conn().execute("SELECT * FROM users ORDER BY id").fetchall()
    return [_row_to_user(r) for r in rows]


def set_password(user_id: int, password: str) -> None:
    with db.tx() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ?, must_change_password = 0, "
            "failed_attempts = 0, locked_until = NULL, updated_at = ? WHERE id = ?",
            (hash_password(password), db.now_iso(), user_id),
        )


def update_user(user_id: int, patch: dict[str, Any]) -> dict[str, Any] | None:
    if not get_user(user_id):
        return None
    fields, values = [], []
    if "role" in patch and patch["role"] in ROLES:
        fields.append("role = ?")
        values.append(patch["role"])
    if "status" in patch and patch["status"] in (STATUS_ACTIVE, STATUS_DISABLED):
        fields.append("status = ?")
        values.append(patch["status"])
    for key in ("display_name", "email"):
        if key in patch:
            fields.append(f"{key} = ?")
            values.append(patch[key])
    if fields:
        fields.append("updated_at = ?")
        values.extend([db.now_iso(), user_id])
        with db.tx() as conn:
            conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", values)
    return get_user(user_id)


def delete_user(user_id: int) -> None:
    with db.tx() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def register_failed_login(username: str) -> tuple[bool, str]:
    """记录一次登录失败。返回 (是否已锁定, 说明)。"""
    user = get_user_by_username(username)
    if not user:
        # 不区分"用户不存在"与"密码错误"，避免用户名枚举
        return False, "用户名或密码错误"
    attempts = user["failed_attempts"] + 1
    locked_until = None
    message = "用户名或密码错误"
    if attempts >= MAX_FAILED_ATTEMPTS:
        until = (datetime.now(timezone.utc) + timedelta(minutes=LOCKOUT_MINUTES)).isoformat(
            timespec="seconds"
        )
        locked_until = until
        attempts = 0
        message = f"连续失败 {MAX_FAILED_ATTEMPTS} 次，账号已锁定 {LOCKOUT_MINUTES} 分钟"
    with db.tx() as conn:
        conn.execute(
            "UPDATE users SET failed_attempts = ?, locked_until = ? WHERE id = ?",
            (attempts, locked_until, user["id"]),
        )
    return bool(locked_until), message


def register_successful_login(user_id: int) -> None:
    with db.tx() as conn:
        conn.execute(
            "UPDATE users SET failed_attempts = 0, locked_until = NULL, last_login_at = ? "
            "WHERE id = ?",
            (db.now_iso(), user_id),
        )


def is_locked(user: dict[str, Any]) -> bool:
    if not user.get("locked_until"):
        return False
    try:
        until = datetime.fromisoformat(user["locked_until"])
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) < until
    except ValueError:
        return False


# ------------------------------------------------------------------- 会话
def create_session(user_id: int, hours: int = SESSION_HOURS) -> tuple[str, dict[str, Any]]:
    token = new_token()
    expires = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds")
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) "
            "VALUES (?,?,?,?)",
            (hash_token(token), user_id, db.now_iso(), expires),
        )
    return token, {"expires_at": expires, "expires_in": hours * 3600}


def get_session_user(token: str) -> dict[str, Any] | None:
    """凭 token 取用户，过期会话视为无效并顺手清理。"""
    if not token:
        return None
    row = db.get_conn().execute(
        "SELECT * FROM sessions WHERE token_hash = ?", (hash_token(token),)
    ).fetchone()
    if not row:
        return None
    try:
        expires = datetime.fromisoformat(row["expires_at"])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > expires:
            with db.tx() as conn:
                conn.execute("DELETE FROM sessions WHERE id = ?", (row["id"],))
            return None
    except ValueError:
        return None
    user = get_user(row["user_id"])
    if not user or user["status"] != STATUS_ACTIVE:
        return None
    return user


def destroy_session(token: str) -> None:
    if not token:
        return
    with db.tx() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_token(token),))


def destroy_user_sessions(user_id: int) -> None:
    with db.tx() as conn:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def cleanup_sessions() -> int:
    """清理过期会话，返回清理条数。"""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db.tx() as conn:
        return conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,)).rowcount


def list_active_sessions() -> list[dict[str, Any]]:
    rows = db.get_conn().execute(
        "SELECT s.id, s.created_at, s.expires_at, u.username, u.display_name "
        "FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.expires_at > ? ORDER BY s.id DESC",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"),),
    ).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------- 审计
def log_login(username: str, success: bool, source: str, ip: str = "", message: str = "") -> None:
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO login_log (username, success, source, ip, message, at) "
            "VALUES (?,?,?,?,?,?)",
            (username, 1 if success else 0, source, ip, message[:200], db.now_iso()),
        )
    # 只保留最近 500 条
    with db.tx() as conn:
        conn.execute(
            "DELETE FROM login_log WHERE id NOT IN "
            "(SELECT id FROM login_log ORDER BY id DESC LIMIT 500)"
        )


def recent_login_log(limit: int = 50) -> list[dict[str, Any]]:
    rows = db.get_conn().execute(
        "SELECT * FROM login_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def count_admins() -> int:
    row = db.get_conn().execute(
        "SELECT COUNT(*) AS c FROM users WHERE role = ? AND status = ?",
        (ROLE_ADMIN, STATUS_ACTIVE),
    ).fetchone()
    return row["c"]


def ensure_default_admin() -> dict[str, Any] | None:
    """首次运行时创建默认管理员。

    密码取自环境变量 CBS_ADMIN_PASSWORD；未设置则随机生成并打印一次，
    避免出现弱口令默认账号。
    """
    import os

    existing = get_user_by_username("admin")
    if existing:
        return None

    password = os.environ.get("CBS_ADMIN_PASSWORD")
    generated = False
    if not password:
        password = secrets.token_urlsafe(12)
        generated = True
    ok, msg = password_strength(password)
    if not ok:
        # 随机生成的密码一定满足强度要求，这里只防御人为设置过弱的环境变量
        password = password + "1A"
    create_user("admin", password, role=ROLE_ADMIN, source="local", display_name="系统管理员")
    result = {"username": "admin", "password": password, "generated": generated}
    if generated:
        print(
            "\n" + "=" * 62 + "\n"
            "  已创建默认管理员账号：\n"
            f"      用户名: admin\n"
            f"      密码  : {password}\n"
            "  该密码仅显示这一次，请立即登录并修改。\n"
            "  也可通过环境变量 CBS_ADMIN_PASSWORD 预设密码。\n"
            + "=" * 62 + "\n"
        )
    return result