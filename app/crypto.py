"""设备密码加密存储。

使用 cryptography 的 Fernet 对称加密，密钥文件保存在 data/.secret_key，
首次运行自动生成（0600 权限）。密钥缺失时无法解密历史数据，因此不要删除该文件。
"""

from __future__ import annotations

import base64
import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
KEY_PATH = DATA_DIR / ".secret_key"


def _key() -> bytes:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not KEY_PATH.exists():
        KEY_PATH.write_bytes(Fernet.generate_key())
        try:
            os.chmod(KEY_PATH, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    return KEY_PATH.read_bytes()


_fernet = Fernet(_key())


def encrypt(plain: str) -> str:
    if not plain:
        return ""
    return _fernet.encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet.decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""