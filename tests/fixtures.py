"""测试公共夹具：为 API 测试提供已登录的 TestClient。

认证功能上线后，大部分 API 都需要登录态。集中在这里处理，
避免每个测试文件各写一遍登录逻辑。
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def prepare_env(prefix: str = "cisco_test_") -> Path:
    """把db / crypto 指向临时目录并初始化。返回临时目录路径。"""
    import sys

    sys.path.insert(0, str(ROOT))
    tmp = Path(tempfile.mkdtemp(prefix=prefix))
    from app import crypto, db

    crypto.DATA_DIR = tmp
    crypto.KEY_PATH = tmp / ".secret_key"
    crypto._key()
    crypto._fernet.__init__(crypto.KEY_PATH.read_bytes())
    db.DATA_DIR = tmp
    db.DB_PATH = tmp / "test.db"
    db._local.__dict__.clear()
    db.init_db()
    return tmp


def disable_auth() -> None:
    """关闭强制登录，供只测业务逻辑、不关心认证的测试使用。"""
    from app import db

    db.update_settings({"auth_enabled": False})


def make_admin(username: str = "testadmin", password: str = "admin12345") -> dict[str, Any]:
    """创建管理员并返回账号信息。"""
    from app import auth

    existing = auth.get_user_by_username(username)
    if existing:
        return {"username": username, "password": password}
    user_id = auth.create_user(username, password, auth.ROLE_ADMIN, "local", "测试管理员")
    return {"username": username, "password": password, "id": user_id}


def login(client, username: str = "testadmin", password: str = "admin12345") -> bool:
    """在 TestClient 上完成登录。"""
    res = client.post(
        "/api/auth/login",
        json={"username": username, "password": password, "mode": "local"},
    )
    return res.status_code == 200


def client(tmp: Path, admin: bool = True):
    """构造已登录（或免登录）的 TestClient。"""
    from fastapi.testclient import TestClient

    from app.main import app

    if not admin:
        disable_auth()
    account = make_admin() if admin else None
    with TestClient(app) as c:
        if account and not login(c, account["username"], account["password"]):
            raise RuntimeError("测试夹具登录失败")
        yield c


def cleanup(tmp: Path) -> None:
    shutil.rmtree(tmp, ignore_errors=True)