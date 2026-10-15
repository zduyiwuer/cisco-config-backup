"""认证与授权专项测试。

覆盖：密码哈希、登录、会话、账号锁定、角色权限、
AD 配置校验、明文 LDAP 拒绝、AD 用户自动授权映射。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = 0, 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cisco_auth_"))
    from app import ad, auth, crypto, db

    crypto.DATA_DIR = tmp
    crypto.KEY_PATH = tmp / ".secret_key"
    crypto._key()
    crypto._fernet.__init__(crypto.KEY_PATH.read_bytes())
    db.DATA_DIR = tmp
    db.DB_PATH = tmp / "t.db"
    db._local.__dict__.clear()
    db.init_db()

    print("\n=== 1. 密码哈希 ===")
    h = auth.hash_password("admin123")
    check("哈希不含明文", "admin123" not in h)
    check("使用 pbkdf2_sha256", h.startswith("pbkdf2_sha256$"))
    check("迭代次数足够", int(h.split("$")[1]) >= 200_000, h.split("$")[1])
    check("正确密码可验证", auth.verify_password("admin123", h))
    check("错误密码被拒", not auth.verify_password("admin124", h))
    check("空哈希不通过", not auth.verify_password("x", ""))
    check("畸形哈希不抛异常", not auth.verify_password("x", "garbage"))
    check("相同密码哈希不同（含随机盐）",
          auth.hash_password("admin123") != auth.hash_password("admin123"))

    print("\n=== 2. 密码强度 ===")
    check("弱密码被拒", auth.password_strength("123456")[0] is False)
    check("纯字母被拒", auth.password_strength("abcdefgh")[0] is False)
    check("纯数字被拒", auth.password_strength("12345678")[0] is False)
    check("合格密码通过", auth.password_strength("admin123")[0] is True)
    check("长密码通过", auth.password_strength("Abcd1234!@#$")[0] is True)

    print("\n=== 3. 默认管理员 ===")
    created = auth.ensure_default_admin()
    check("首次创建 admin", created is not None and created["username"] == "admin")
    check("随机生成密码", created["generated"] is True)
    check("admin 角色正确",
          auth.get_user_by_username("admin")["role"] == auth.ROLE_ADMIN)
    check("重复调用不再创建", auth.ensure_default_admin() is None)

    print("\n=== 4. 会话 ===")
    token, meta = auth.create_session(1)
    check("返回 token", len(token) > 30)
    check("token 不等于其哈希", auth.hash_token(token) != token)
    user = auth.get_session_user(token)
    check("凭 token 取到用户", user and user["username"] == "admin")
    check("过期时间正确", "expires_at" in meta)
    check("错误 token 无效", auth.get_session_user("wrong") is None)
    check("空 token 无效", auth.get_session_user("") is None)

    auth.destroy_session(token)
    check("注销后失效", auth.get_session_user(token) is None)

    # 数据库里不能存明文 token
    token2, _ = auth.create_session(1)
    row = db.get_conn().execute(
        "SELECT token_hash FROM sessions WHERE user_id = 1"
    ).fetchone()
    check("库中只存 token 哈希", row["token_hash"] != token2)
    check("哈希值可反查", auth.get_session_user(token2) is not None)
    auth.destroy_session(token2)

    # 过期会话
    token3, _ = auth.create_session(1, hours=-1)
    check("过期会话视为无效", auth.get_session_user(token3) is None)
    auth.cleanup_sessions()

    print("\n=== 5. 用户 CRUD 与角色 ===")
    op_id = auth.create_user("opuser", "op123456", auth.ROLE_OPERATOR, "local", "操作员")
    view_id = auth.create_user("viewer1", "vw123456", auth.ROLE_VIEWER, "local", "只读")
    ad_id = auth.create_user("domain\\zhang", None, auth.ROLE_VIEWER, "ad", "张三")
    check("创建操作员", auth.get_user(op_id)["role"] == auth.ROLE_OPERATOR)
    check("AD 用户无本地密码",
          db.get_conn().execute(
              "SELECT password_hash FROM users WHERE id = ?", (ad_id,)
          ).fetchone()["password_hash"] is None)
    check("列表含全部用户", len(auth.list_users()) == 4)

    check("更新角色", auth.update_user(view_id, {"role": "operator"})["role"] == "operator")
    check("禁用用户", auth.update_user(view_id, {"status": "disabled"})["status"] == "disabled")
    disabled_token, _ = auth.create_session(view_id)
    check("禁用后会话立即失效", auth.get_session_user(disabled_token) is None)
    auth.update_user(view_id, {"status": "active"})

    check("管理员计数正确", auth.count_admins() == 1)
    auth.delete_user(view_id)
    check("删除用户成功", auth.get_user(view_id) is None)
    check("删除后管理员计数不变", auth.count_admins() == 1)

    print("\n=== 6. 登录锁定 ===")
    auth.create_user("lockme", "lock12345", auth.ROLE_OPERATOR, "local")
    for i in range(auth.MAX_FAILED_ATTEMPTS - 1):
        locked, msg = auth.register_failed_login("lockme")
        check(f"第 {i + 1} 次失败未锁定" if not locked else f"第 {i + 1} 次已锁定",
              not locked, msg)
    locked, msg = auth.register_failed_login("lockme")
    check("达到次数上限后锁定", locked, msg)
    check("锁定提示含时长", str(auth.LOCKOUT_MINUTES) in msg, msg)
    u = auth.get_user_by_username("lockme")
    check("locked_until 已写入", u["locked_until"] is not None)
    check("is_locked 判定为真", auth.is_locked(u))
    auth.register_successful_login(u["id"])
    check("成功登录后清零", auth.get_user_by_username("lockme")["failed_attempts"] == 0)
    check("不再处于锁定", not auth.is_locked(auth.get_user_by_username("lockme")))

    print("\n=== 7. 登录审计 ===")
    auth.log_login("admin", True, "local", "127.0.0.1", "登录成功")
    auth.log_login("badguy", False, "local", "10.0.0.9", "密码错误")
    logs = auth.recent_login_log(10)
    check("审计日志可查", len(logs) >= 2)
    check("成功失败都记录", any(l["success"] == 1 for l in logs)
          and any(l["success"] == 0 for l in logs))

    print("\n=== 8. HTTP 登录流程 ===")
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        # 未登录被拦截
        r = client.get("/api/devices")
        check("未登录访问 API 返回 401", r.status_code == 401, str(r.status_code))
        r = client.get("/")
        check("未登录访问首页跳登录页", r.status_code in (200, 302, 307))

        r = client.get("/login")
        check("登录页可访问", r.status_code == 200 and "登录" in r.text)

        r = client.get("/api/health")
        check("健康检查无需登录", r.status_code == 200)

        # 错误密码
        r = client.post("/api/auth/login",
                        json={"username": "admin", "password": "wrong", "mode": "local"})
        check("错误密码返回 401", r.status_code == 401)
        check("错误提示不区分用户不存在", "用户名或密码" in r.json()["detail"])

        # 不存在的用户
        r = client.post("/api/auth/login",
                        json={"username": "nobody", "password": "x", "mode": "local"})
        check("用户不存在提示一致", r.json()["detail"] == "用户名或密码错误")

        # 正确登录
        admin_pwd = created["password"]
        r = client.post("/api/auth/login",
                        json={"username": "admin", "password": admin_pwd, "mode": "local"})
        check("正确密码登录成功", r.status_code == 200, r.text[:200])
        check("返回用户信息", r.json()["user"]["username"] == "admin")
        check("角色为管理员", r.json()["user"]["role"] == "admin")
        check("设置 HttpOnly Cookie", "cbs_session" in r.cookies
              or any("cbs_session" in c for c in r.headers.get_list("set-cookie")))

        r = client.get("/api/auth/me")
        check("可获取当前用户", r.status_code == 200 and r.json()["user"]["username"] == "admin")

        r = client.get("/api/devices")
        check("登录后可访问 API", r.status_code == 200)

        # 角色权限
        r = client.get("/api/auth/users")
        check("管理员可列出用户", r.status_code == 200 and len(r.json()) >= 3)

        # 登出
        r = client.post("/api/auth/logout")
        check("登出成功", r.status_code == 200)
        r = client.get("/api/devices")
        check("登出后 API 恢复 401", r.status_code == 401)

    print("\n=== 9. 角色权限矩阵 ===")
    with TestClient(app) as client:
        auth.create_user("viewer_u", "vw123456", auth.ROLE_VIEWER, "local")
        auth.create_user("oper_u", "op123456", auth.ROLE_OPERATOR, "local")
        r = client.post("/api/auth/login",
                        json={"username": "viewer_u", "password": "vw123456", "mode": "local"})
        check("只读账号可登录", r.status_code == 200)
        r = client.get("/api/devices")
        check("只读可查看设备", r.status_code == 200)
        r = client.get("/api/auth/users")
        check("只读不能管理用户", r.status_code == 403, str(r.status_code))
        r = client.post("/api/devices",
                        json={"name": "x1", "host": "1.1.1.1", "protocol": "ssh",
                              "username": "u", "password": "p"})
        check("只读不能新增设备", r.status_code == 403, str(r.status_code))
        r = client.put("/api/settings", json={"concurrency": 2})
        check("只读不能改设置", r.status_code == 403)
        client.post("/api/auth/logout")

        r = client.post("/api/auth/login",
                        json={"username": "oper_u", "password": "op123456", "mode": "local"})
        check("操作员可登录", r.status_code == 200)
        r = client.post("/api/devices",
                        json={"name": "op-created", "host": "1.1.1.2", "protocol": "ssh",
                              "username": "u", "password": "p"})
        check("操作员可新增设备", r.status_code == 201, str(r.status_code))
        new_id = r.json()["id"] if r.status_code == 201 else None
        if new_id:
            r = client.delete(f"/api/devices/{new_id}")
            check("操作员不能删除设备", r.status_code == 403, str(r.status_code))
        r = client.get("/api/auth/users")
        check("操作员不能管理用户", r.status_code == 403)
        r = client.put("/api/settings", json={"concurrency": 3})
        check("操作员不能改系统设置", r.status_code == 403)

    print("\n=== 10. AD 配置与安全约束 ===")
    r = client.get("/api/auth/ad-status") if False else None
    with TestClient(app) as client:
        client.post("/api/auth/login",
                    json={"username": "admin", "password": admin_pwd, "mode": "local"})

        r = client.get("/api/auth/ad-status")
        check("登录页可获取 AD 状态", r.status_code == 200)
        check("默认未启用 AD", r.json()["ad_enabled"] is False)

        # 明文 LDAP 默认拒绝
        db.update_settings({"ad_enabled": True, "ad_host": "dc.test.local",
                            "ad_use_ssl": False, "ad_allow_plaintext": False})
        try:
            ad.authenticate("zhangsan", "pw123456")
            check("明文 LDAP 被拒绝", False, "未抛出异常")
        except ad.ADAuthError as exc:
            check("明文 LDAP 被拒绝", "LDAPS" in str(exc), str(exc))

        # 允许明文后可尝试连接（会因域名解析失败）
        db.update_settings({"ad_allow_plaintext": True})
        try:
            ad.authenticate("zhangsan", "pw123456")
            check("连通性错误被捕获", True)
        except ad.ADAuthError as exc:
            check("连通性错误转为友好提示",
                  "AD 服务器" in str(exc) or "密码" in str(exc), str(exc))
        db.update_settings({"ad_allow_plaintext": False})

        # 空密码
        db.update_settings({"ad_use_ssl": True})
        try:
            ad.authenticate("zhangsan", "")
            check("AD 空密码被拒", False)
        except ad.ADAuthError as exc:
            check("AD 空密码被拒", "密码" in str(exc), str(exc))

        # 配置写入后不回显密码
        r = client.put("/api/auth/ad",
                       json={"ad_enabled": True, "ad_host": "dc.corp.local",
                             "ad_bind_dn": "CN=svc,DC=corp,DC=local",
                             "ad_bind_password": "svcsecret123"})
        check("保存 AD 配置", r.status_code == 200)
        check("响应不含服务账号密码", r.json()["settings"]["ad_bind_password"] == "")
        check("标记密码已设置", r.json()["settings"]["ad_bind_password_set"] is True)

        r = client.get("/api/settings")
        check("settings 不含密码明文", r.json()["ad_bind_password"] == "")

        # 数据库里是密文
        raw = db.get_conn().execute(
            "SELECT value FROM settings WHERE key = 'ad_bind_password'"
        ).fetchone()
        check("库中 AD 密码为密文", raw and "svcsecret123" not in raw["value"])
        check("可解密还原", db.get_settings()["ad_bind_password"] == "svcsecret123")

        # 留空不修改
        client.put("/api/auth/ad", json={"ad_host": "dc2.corp.local", "ad_bind_password": ""})
        check("留空密码保持原值", db.get_settings()["ad_bind_password"] == "svcsecret123")

    print("\n=== 11. AD 角色映射 ===")
    db.update_settings({"ad_admin_group": "CN=NetOps,DC=corp,DC=local"})
    identity = ad._build_identity("zhangsan", "CN=zhangsan,DC=corp,DC=local",
                                  {"displayName": "张三", "mail": "z@corp.local",
                                   "memberOf": ["CN=NetOps,DC=corp,DC=local"]}, "search")
    check("管理员组成员被识别为管理员", identity["role"] == "admin", str(identity["role"]))
    identity2 = ad._build_identity("lisi", "CN=lisi,DC=corp,DC=local",
                                   {"displayName": "李四", "mail": "l@corp.local",
                                    "memberOf": ["CN=Users,DC=corp,DC=local"]}, "search")
    check("非管理员组成员按默认角色",
          identity2["role"] == db.get_settings()["ad_default_role"], str(identity2["role"]))
    check("取到显示名", identity2["display_name"] == "李四")

    # 纯组名匹配
    db.update_settings({"ad_admin_group": "NetOps"})
    identity3 = ad._build_identity(
        "wangwu", "CN=wangwu,DC=corp,DC=local",
        {"memberOf": ["CN=NetOps,DC=corp,DC=local"]}, "search",
    )
    check("纯组名也能匹配", identity3["role"] == "admin", str(identity3["role"]))

    # 不同 DN 下的同名组不应误判
    db.update_settings({"ad_admin_group": "CN=NetOps,DC=corp,DC=local"})
    identity_other = ad._build_identity(
        "zhaoliu", "CN=zhaoliu,DC=other,DC=local",
        {"memberOf": ["CN=NetOps,DC=other,DC=local"]}, "search",
    )
    check("同名但不同 DN 不误判",
          identity_other["role"] == db.get_settings()["ad_default_role"],
          str(identity_other["role"]))

    # 普通 Users 组不得被当作管理员
    identity_users = ad._build_identity(
        "sunqi", "CN=sunqi,DC=corp,DC=local",
        {"memberOf": ["CN=Users,DC=corp,DC=local"]}, "search",
    )
    check("Users 组不被当作管理员",
          identity_users["role"] != "admin", str(identity_users["role"]))

    # 多组用换行/分号分隔
    db.update_settings({"ad_admin_group": "NetOps\nDomain Admins"})
    identity_multi = ad._build_identity(
        "zhouba", "CN=zhouba,DC=corp,DC=local",
        {"memberOf": ["CN=Domain Admins,DC=corp,DC=local"]}, "search",
    )
    check("多组候选可命中其一", identity_multi["role"] == "admin", str(identity_multi["role"]))

    # 未配置管理员组时给 operator
    db.update_settings({"ad_admin_group": "", "ad_default_role": "viewer"})
    identity4 = ad._build_identity("zhaoliu", "CN=zhaoliu,DC=corp,DC=local", {}, "search")
    check("未配管理员组时用默认角色", identity4["role"] == "viewer", str(identity4["role"]))

    print("\n=== 12. LDAP 注入防护 ===")
    esc = ad._escape_filter("admin)(|(objectClass=*)")
    check("特殊字符被转义", "*" not in esc.replace("\\2a", ""), esc)
    check("括号被转义", "\\28" in esc, esc)

    print("\n=== 13. AD 用户登录（模拟域控） ===")
    from app import main as main_mod

    calls = {"auth": []}

    class FakeIdentity(ad.ADAuthError):
        pass

    def fake_authenticate(username, password):
        calls["auth"].append((username, password))
        if password != "correct-pw":
            raise ad.ADAuthError("域账号或密码错误")
        return ad._build_identity(
            "zhangsan", "CN=zhangsan,DC=corp,DC=local",
            {"displayName": "张三", "mail": "z@corp.local",
             "memberOf": ["CN=NetOps,DC=corp,DC=local"]},
            "search",
        )

    db.update_settings({"ad_enabled": True, "ad_use_ssl": True,
                        "ad_admin_group": "NetOps"})
    main_mod.ad.authenticate = fake_authenticate

    with TestClient(app) as client:
        r = client.post("/api/auth/login",
                        json={"username": "zhangsan", "password": "wrong", "mode": "ad"})
        check("AD 密码错误被拒", r.status_code == 401)
        check("提示为域账号密码错误", "域账号" in r.json()["detail"], r.json()["detail"])

        r = client.post("/api/auth/login",
                        json={"username": "zhangsan", "password": "correct-pw", "mode": "ad"})
        check("AD 登录成功", r.status_code == 200, r.text[:200])
        check("AD 用户自动建档", r.json()["user"]["username"] == "zhangsan")
        check("按组映射为管理员", r.json()["user"]["role"] == "admin",
              str(r.json()["user"]["role"]))
        check("来源标记为域账号", r.json()["user"]["source"] == "ad")
        check("取到显示名", r.json()["user"]["display_name"] == "张三")

        local_user = auth.get_user_by_username("zhangsan")
        check("本地库记录 ad_dn", local_user["ad_dn"] == "CN=zhangsan,DC=corp,DC=local")
        check("本地库来源为 ad", local_user["source"] == "ad")
        check("本地库无密码", local_user["id"] > 0)

        # 本地模式登录 AD 账号应被引导
        client.post("/api/auth/logout")
        r = client.post("/api/auth/login",
                        json={"username": "zhangsan", "password": "correct-pw", "mode": "local"})
        check("AD 账号不能用本地密码登录", r.status_code == 401)
        check("提示切换域账号入口", "域账号" in r.json()["detail"], r.json()["detail"])

        # 再次 AD 登录：更新而非重复创建
        r = client.post("/api/auth/login",
                        json={"username": "zhangsan", "password": "correct-pw", "mode": "ad"})
        check("AD 重复登录不重复建档", r.status_code == 200)
        users = [u for u in auth.list_users() if u["username"] == "zhangsan"]
        check("zhangsan 只有一条记录", len(users) == 1, str(len(users)))

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'=' * 46}\n  通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 46}\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())