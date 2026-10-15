"""管理员密码重置脚本。

忘记密码时运行本脚本：
    ./.venv/Scripts/python scripts/reset_admin_password.py

指定账号与新密码：
    ./.venv/Scripts/python scripts/reset_admin_password.py admin MyNewPass123

只指定账号（交互式输入新密码）：
    ./.venv/Scripts/python scripts/reset_admin_password.py admin
"""

from __future__ import annotations

import getpass
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    from app import auth, db

    db.init_db()

    users = [u for u in auth.list_users() if u["role"] == auth.ROLE_ADMIN]
    if not users:
        print("没有找到管理员账号。可用下面的命令创建一个：")
        print("  python scripts/reset_admin_password.py --create <用户名>")
        return 1

    print("现有管理员账号：")
    for u in users:
        lock = "（已锁定）" if auth.is_locked(u) else ""
        print(f"  - {u['username']}  最后登录: {u['last_login_at'] or '从未'} {lock}")

    # 参数解析：第1 个是账号，第 2 个是密码（可省略则交互式询问）
    username = sys.argv[1] if len(sys.argv) > 1 else None
    if not username:
        try:
            username = input(f"\n要重置哪个账号？[{users[0]['username']}] ").strip() or users[0]["username"]
        except (EOFError, KeyboardInterrupt):
            print("\n已取消")
            return 1

    user = auth.get_user_by_username(username)
    if not user:
        print(f"账号 {username} 不存在")
        return 1

    if len(sys.argv) > 2:
        password = sys.argv[2]
    else:
        while True:
            try:
                password = getpass.getpass(f"为 {username} 设置新密码（直接回车=随机生成）: ")
            except (EOFError, KeyboardInterrupt):
                print("\n已取消")
                return 1
            if not password:
                password = secrets.token_urlsafe(12)
                print(f"已生成随机密码: {password}")
                break
            ok, msg = auth.password_strength(password)
            if ok:
                break
            print(f"  密码不合格：{msg}")

    auth.set_password(user["id"], password)
    # 解锁并清空失败计数
    with db.tx() as conn:
        conn.execute(
            "UPDATE users SET failed_attempts = 0, locked_until = NULL WHERE id = ?",
            (user["id"],),
        )
    # 让旧会话全部失效
    auth.destroy_user_sessions(user["id"])

    print(f"\n账号 {username} 的密码已重置，现有登录会话已失效。")
    print("请重新登录并妥善保管新密码。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ImportError as exc:
        print(f"导入失败：{exc}\n请确认在项目目录下运行，并已安装依赖。")
        sys.exit(1)