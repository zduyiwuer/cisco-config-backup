"""FastAPI 后端：设备管理、备份触发、版本与差异查询。"""

from __future__ import annotations

import io
import logging
import zipfile
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import ad, auth, collector, db, exporter, importer, probe, scheduler
from .cdp import parse_cdp_neighbors
from .drivers.base import DEFAULT_COMMANDS, DeviceError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("cisco-backup")

STATIC_DIR = __import__("pathlib").Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    first_admin = auth.ensure_default_admin()
    if first_admin:
        app.state.first_admin_password = first_admin
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="Cisco 配置备份中心", version="1.0.0", lifespan=lifespan)

SESSION_COOKIE = "cbs_session"
PUBLIC_PATHS = {
    "/api/health",
    "/api/auth/login",
    "/api/auth/ad-status",
    "/api/auth/force-change-password",
}

# 角色等级，用于粗粒度授权
ROLE_LEVEL = {"viewer": 1, "operator": 2, "admin": 3}


def current_user(request: Request) -> dict[str, Any] | None:
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    if not token:
        return None
    return auth.get_session_user(token)


def auth_required(request: Request) -> dict[str, Any]:
    """强制登录校验。未开启认证时返回一个虚拟管理员。"""
    if not db.get_settings().get("auth_enabled", True):
        return {
            "id": 0, "username": "anonymous", "role": "admin",
            "display_name": "未启用认证", "source": "none",
        }
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def require_role(request: Request, minimum: str) -> dict[str, Any]:
    user = auth_required(request)
    need = ROLE_LEVEL.get(minimum, 3)
    have = ROLE_LEVEL.get(user.get("role", "viewer"), 1)
    if have < need:
        raise HTTPException(status_code=403, detail="权限不足，该操作需要更高角色")
    return user


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    """未登录拦截。登录页与静态资源放行，其余 API 要求有效会话。"""
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/static"):
        return await call_next(request)
    if path.startswith("/api/") and db.get_settings().get("auth_enabled", True):
        user = current_user(request)
        if not user:
            return JSONResponse(
                status_code=401,
                content={"detail": "请先登录", "login_url": "/login"},
                headers={"Location": "/login"} if path == "/" else None,
            )
    return await call_next(request)


# --------------------------------------------------------------------- models
class DeviceIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    host: str = Field(min_length=1)
    protocol: str = "ssh"
    port: int | None = None
    username: str = ""
    password: str = ""
    enable_password: str = ""
    device_type: str = "cisco_ios"
    commands: list[str] = Field(default_factory=lambda: list(DEFAULT_COMMANDS))
    enabled: bool = True
    schedule_enabled: bool = False
    cron: str = "0 2 * * *"
    site: str = ""
    site_id: int | None = None
    note: str = ""


class DevicePatch(BaseModel):
    name: str | None = None
    host: str | None = None
    protocol: str | None = None
    port: int | None = None
    username: str | None = None
    password: str | None = None
    enable_password: str | None = None
    commands: list[str] | None = None
    enabled: bool | None = None
    schedule_enabled: bool | None = None
    cron: str | None = None
    site: str | None = None
    site_id: int | None = None
    note: str | None = None


class SiteIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    code: str = ""
    region: str = ""
    address: str = ""
    note: str = ""
    sort_order: int = 100


class SitePatch(BaseModel):
    name: str | None = None
    code: str | None = None
    region: str | None = None
    address: str | None = None
    note: str | None = None
    sort_order: int | None = None


class ExportIn(BaseModel):
    scope: str = "all"
    site_id: int | None = None
    device_ids: list[int] | None = None
    limit_per_device: int | None = None
    since: str | None = None
    until: str | None = None
    commands: list[str] | None = None
    include_diff: bool = True


class ImportIn(BaseModel):
    mode: str = "create"
    default_site: str = ""


class ProbeAllIn(BaseModel):
    device_ids: list[int] | None = None


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=256)
    mode: str = "local"  # local | ad


class PasswordChangeIn(BaseModel):
    old_password: str = ""
    new_password: str = Field(min_length=1, max_length=256)


class UserCreateIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    role: str = "operator"
    display_name: str = ""


class UserPatchIn(BaseModel):
    role: str | None = None
    status: str | None = None
    display_name: str | None = None
    email: str | None = None


class ADIn(BaseModel):
    ad_enabled: bool | None = None
    ad_host: str | None = None
    ad_port: int | None = None
    ad_use_ssl: bool | None = None
    ad_domain: str | None = None
    ad_base_dn: str | None = None
    ad_bind_dn: str | None = None
    ad_bind_password: str | None = None
    ad_auth_strategy: str | None = None
    ad_use_ntlm: bool | None = None
    ad_admin_group: str | None = None
    ad_default_role: str | None = None
    ad_allow_plaintext: bool | None = None
    ad_ca_cert: str | None = None
    auth_enabled: bool | None = None
    session_hours: int | None = None
    allow_password_login: bool | None = None


class SettingsIn(BaseModel):
    default_commands: list[str] | None = None
    concurrency: int | None = None
    connect_timeout: int | None = None
    command_timeout: int | None = None
    keep_versions: int | None = None
    paging_fix: bool | None = None
    probe_enabled: bool | None = None
    probe_interval_minutes: int | None = None
    probe_timeout: int | None = None
    probe_workers: int | None = None
    session_hours: int | None = None


class BackupAllIn(BaseModel):
    device_ids: list[int] | None = None


# --------------------------------------------------------------------- sites
@app.get("/api/sites")
def api_list_sites() -> list[dict[str, Any]]:
    """门店列表，含设备数量与最近备份时间。"""
    return db.list_sites()


@app.post("/api/sites", status_code=201)
def api_create_site(payload: SiteIn, request: Request) -> dict[str, Any]:
    require_role(request, "operator")
    try:
        return db.create_site(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/sites/{site_id}")
def api_get_site(site_id: int) -> dict[str, Any]:
    site = db.get_site(site_id)
    if not site:
        raise HTTPException(status_code=404, detail="门店不存在")
    devices = db.list_devices(site_id=site_id)
    for device in devices:
        device["summary"] = db.device_backup_summary(device["id"])
    site["devices"] = devices
    return site


@app.patch("/api/sites/{site_id}")
def api_update_site(site_id: int, payload: SitePatch, request: Request) -> dict[str, Any]:
    require_role(request, "operator")
    site = db.update_site(site_id, payload.model_dump(exclude_unset=True))
    if not site:
        raise HTTPException(status_code=404, detail="门店不存在")
    return site


@app.delete("/api/sites/{site_id}")
def api_delete_site(site_id: int, request: Request, reassign_to: int | None = None) -> dict[str, Any]:
    require_role(request, "admin")
    if not db.get_site(site_id):
        raise HTTPException(status_code=404, detail="门店不存在")
    if reassign_to is not None and not db.get_site(reassign_to):
        raise HTTPException(status_code=404, detail="目标门店不存在")
    moved = db.delete_site(site_id, reassign_to)
    return {"ok": True, "moved_devices": moved}


@app.post("/api/sites/{site_id}/backup")
def api_backup_site(site_id: int, request: Request, background: BackgroundTasks) -> dict[str, Any]:
    """按门店批量备份其下所有启用设备。"""
    require_role(request, "operator")
    if not db.get_site(site_id):
        raise HTTPException(status_code=404, detail="门店不存在")
    targets = db.site_device_ids(site_id, only_enabled=True)
    if not targets:
        raise HTTPException(status_code=400, detail="该门店下没有启用的设备")
    background.add_task(collector.backup_devices, targets, "manual")
    return {"queued": len(targets), "device_ids": targets}


# --------------------------------------------------------------------- auth
@app.get("/api/auth/ad-status")
def api_ad_status() -> dict[str, Any]:
    """登录页用：AD 是否可用、是否允许本地登录。"""
    cfg = db.get_settings()
    return {
        "auth_enabled": bool(cfg.get("auth_enabled", True)),
        "ad_enabled": bool(cfg.get("ad_enabled")),
        "ad_domain": cfg.get("ad_domain", ""),
        "ad_host": cfg.get("ad_host", ""),
        "allow_password_login": bool(cfg.get("allow_password_login", True)),
        "first_run": bool(getattr(app.state, "first_admin_password", None)),
    }


def _login_response(request: Request, user: dict[str, Any], source: str) -> Response:
    hours = int(db.get_settings().get("session_hours", 12))
    token, meta = auth.create_session(user["id"], hours)
    auth.register_successful_login(user["id"])
    auth.log_login(user["username"], True, source, request.client.host if request.client else "")
    secure = request.url.scheme == "https"
    response = JSONResponse(
        {
            "ok": True,
            "user": {
                "id": user["id"],
                "username": user["username"],
                "display_name": user.get("display_name") or user["username"],
                "role": user["role"],
                "source": user.get("source", source),
            },
            "must_change_password": user.get("must_change_password", False),
            "expires_at": meta["expires_at"],
        }
    )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=hours * 3600,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )
    return response


def _fail(request: Request, message: str, source: str, username: str) -> JSONResponse:
    auth.log_login(username, False, source, request.client.host if request.client else "", message)
    return JSONResponse(status_code=401, content={"detail": message})


@app.post("/api/auth/login")
def api_login(payload: LoginIn, request: Request) -> Response:
    """登录入口。mode=local 走本地账号，mode=ad 走域账号。"""
    cfg = db.get_settings()
    username = payload.username.strip()

    if payload.mode == "ad":
        if not cfg.get("ad_enabled"):
            return _fail(request, "未启用 AD 登录", "ad", username)
        try:
            identity = ad.authenticate(username, payload.password)
        except ad.ADAuthError as exc:
            return _fail(request, str(exc), "ad", username)

        # AD 验证通过：本地已有账号则更新资料，否则按策略自动建档
        user = auth.get_user_by_username(identity["username"])
        if user:
            if user["status"] != auth.STATUS_ACTIVE:
                return _fail(request, "账号已被禁用", "ad", username)
            user = auth.update_user(
                user["id"],
                {
                    "role": identity["role"],
                    "display_name": identity["display_name"],
                    "email": identity["email"],
                },
            ) or user
        else:
            user_id = auth.create_user(
                username=identity["username"],
                password=None,
                role=identity["role"],
                source="ad",
                display_name=identity["display_name"],
                email=identity["email"],
                ad_dn=identity["ad_dn"],
            )
            user = auth.get_user(user_id)
        log.info("AD 登录成功: %s (角色 %s)", user["username"], user["role"])
        return _login_response(request, user, "ad")

    # 本地登录
    if not cfg.get("allow_password_login", True):
        return _fail(request, "已禁用本地密码登录，请使用域账号", "local", username)

    user = auth.get_user_by_username(username)
    if not user:
        return _fail(request, "用户名或密码错误", "local", username)
    if user["source"] != "local" or user["id"] == 0:
        # AD 账号请走AD 登录入口，AD 不可用时给出明确指引
        return _fail(request, "该账号为域账号，请选择「域账号登录」", "local", username)
    if user["status"] != auth.STATUS_ACTIVE:
        return _fail(request, "账号已被禁用", "local", username)
    if auth.is_locked(user):
        return _fail(request, f"账号已锁定，请 {auth.LOCKOUT_MINUTES} 分钟后重试", "local", username)

    stored = db.get_conn().execute(
        "SELECT password_hash FROM users WHERE id = ?", (user["id"],)
    ).fetchone()
    if not stored or not auth.verify_password(payload.password, stored["password_hash"]):
        locked, message = auth.register_failed_login(username)
        return _fail(request, "账号已锁定，请稍后重试" if locked else message, "local", username)

    fresh = auth.get_user_by_username(username)
    return _login_response(request, fresh, "local")


@app.post("/api/auth/logout")
def api_logout(request: Request) -> Response:
    auth.destroy_session(request.cookies.get(SESSION_COOKIE, ""))
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/auth/me")
def api_me(request: Request) -> dict[str, Any]:
    user = auth_required(request)
    return {
        "user": {
            "id": user["id"],
            "username": user["username"],
            "display_name": user.get("display_name") or user["username"],
            "role": user["role"],
            "source": user.get("source", "local"),
            "must_change_password": user.get("must_change_password", False),
        }
    }


@app.post("/api/auth/change-password")
def api_change_password(payload: PasswordChangeIn, request: Request) -> dict[str, Any]:
    """修改自己的密码；其他用户的密码由管理员在用户管理里重置。"""
    user = auth_required(request)
    if user["id"] == 0:
        raise HTTPException(status_code=400, detail="未启用认证，无需修改密码")
    if payload.new_password == payload.old_password:
        raise HTTPException(status_code=400, detail="新密码不能与旧密码相同")
    ok, msg = auth.password_strength(payload.new_password)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)

    row = db.get_conn().execute(
        "SELECT password_hash FROM users WHERE id = ?", (user["id"],)
    ).fetchone()
    if row and row["password_hash"]:
        if not auth.verify_password(payload.old_password, row["password_hash"]):
            raise HTTPException(status_code=400, detail="原密码不正确")
    auth.set_password(user["id"], payload.new_password)
    auth.log_login(
        user["username"], True, "password_change",
        request.client.host if request.client else "", "修改密码",
    )
    return {"ok": True, "message": "密码已更新"}


@app.get("/api/auth/users")
def api_list_users(request: Request) -> list[dict[str, Any]]:
    require_role(request, "admin")
    return auth.list_users()


@app.post("/api/auth/users", status_code=201)
def api_create_user(payload: UserCreateIn, request: Request) -> dict[str, Any]:
    require_role(request, "admin")
    ok, msg = auth.password_strength(payload.password)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    if auth.get_user_by_username(payload.username):
        raise HTTPException(status_code=400, detail="用户名已存在")
    if payload.role not in auth.ROLES:
        raise HTTPException(status_code=400, detail=f"角色必须是 {', '.join(auth.ROLES)}")
    user_id = auth.create_user(
        payload.username, payload.password, payload.role, "local",
        payload.display_name or payload.username,
    )
    return auth.get_user(user_id)  # type: ignore[return-value]


@app.patch("/api/auth/users/{user_id}")
def api_update_user(user_id: int, payload: UserPatchIn, request: Request) -> dict[str, Any]:
    me = require_role(request, "admin")
    target = auth.get_user(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    patch = payload.model_dump(exclude_unset=True)
    if target["id"] == me["id"] and patch.get("role") not in (None, "admin"):
        raise HTTPException(status_code=400, detail="不能降低自己的管理员权限")
    updated = auth.update_user(user_id, patch)
    return updated  # type: ignore[return-value]


@app.delete("/api/auth/users/{user_id}")
def api_delete_user(user_id: int, request: Request) -> dict[str, Any]:
    me = require_role(request, "admin")
    if user_id == me["id"]:
        raise HTTPException(status_code=400, detail="不能删除自己的账号")
    target = auth.get_user(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    if target["role"] == "admin" and auth.count_admins() <= 1:
        raise HTTPException(status_code=400, detail="至少保留一个管理员账号")
    auth.delete_user(user_id)
    return {"ok": True}


@app.get("/api/auth/sessions")
def api_list_sessions(request: Request) -> dict[str, Any]:
    require_role(request, "admin")
    return {"sessions": auth.list_active_sessions()}


@app.get("/api/auth/login-log")
def api_login_log(request: Request, limit: int = 50) -> dict[str, Any]:
    require_role(request, "admin")
    return {"entries": auth.recent_login_log(min(limit, 200))}


@app.put("/api/auth/ad")
def api_update_ad(payload: ADIn, request: Request) -> dict[str, Any]:
    """配置 AD / 认证策略（仅管理员）。"""
    require_role(request, "admin")
    patch = payload.model_dump(exclude_unset=True)
    db.update_settings(patch)
    return {"ok": True, "settings": db.get_public_settings()}


@app.post("/api/auth/ad/test")
def api_test_ad(request: Request) -> dict[str, Any]:
    require_role(request, "admin")
    return ad.test_connection()


# ------------------------------------------------------------------- import
@app.get("/api/import/template")
def api_import_template() -> Response:
    """下载 CSV 导入模板（含两行示例）。"""
    return Response(
        content=importer.template_csv().encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": exporter.content_disposition("设备导入模板.csv")
        },
    )


@app.get("/api/import/template.xlsx")
def api_import_template_xlsx() -> Response:
    """下载 Excel 导入模板：带列宽、表头样式、列批注与填写说明页。"""
    return Response(
        content=importer.template_xlsx(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": exporter.content_disposition("设备导入模板.xlsx")
        },
    )


@app.post("/api/import/devices")
async def api_import_devices(
    file: UploadFile, request: Request, params: str = "create", default_site: str = ""
) -> dict[str, Any]:
    require_role(request, "operator")
    """批量导入设备：上传 CSV / Excel 文件。

    params: create（仅新增，重名跳过）/ upsert（按名称更新）
    """
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="上传文件为空")
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="文件过大，请控制在 5MB 以内")

    try:
        rows = importer.read_table(content, file.filename or "devices.csv")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"文件解析失败: {exc}") from exc

    if not rows:
        raise HTTPException(status_code=400, detail="文件里没有数据行")

    result = importer.import_devices(rows, mode=params, default_site=default_site)
    log.info(
        "设备导入(%s): 新增 %s 更新 %s 跳过 %s 失败 %s",
        file.filename, result["summary"]["created"], result["summary"]["updated"],
        result["summary"]["skipped"], result["summary"]["failed"],
    )
    return result


# -------------------------------------------------------------------- probe
@app.get("/api/probe/status")
def api_probe_status() -> dict[str, Any]:
    """全局在线概况 + 离线设备清单。"""
    overview = db.online_overview()
    settings = db.get_settings()
    next_probe = None
    for job in scheduler.job_overview():
        if job.get("type") == "probe":
            next_probe = job["next_run"]
            break
    overview["probe_enabled"] = bool(settings.get("probe_enabled", True))
    overview["probe_interval_minutes"] = int(settings.get("probe_interval_minutes", 5))
    overview["next_probe_at"] = next_probe
    return overview


@app.post("/api/probe/run")
def api_probe_run(payload: ProbeAllIn, request: Request) -> dict[str, Any]:
    require_role(request, "operator")
    """立即执行一轮在线探测。"""
    if payload.device_ids:
        devices = [d for d in db.list_devices() if d["id"] in set(payload.device_ids)]
    else:
        devices = [d for d in db.list_devices() if d["enabled"]]
    settings = db.get_settings()
    results = [
        probe.probe_device(d, int(settings.get("probe_timeout", 5))) for d in devices
    ]
    online = sum(1 for r in results if r["online"])
    return {
        "total": len(results),
        "online": online,
        "offline": len(results) - online,
        "results": sorted(results, key=lambda r: r["name"]),
    }


@app.get("/api/probe/{device_id}/history")
def api_probe_history(device_id: int, limit: int = 50) -> dict[str, Any]:
    """单台设备的探测历史。"""
    if not db.get_device(device_id):
        raise HTTPException(status_code=404, detail="设备不存在")
    return {"device_id": device_id, "history": db.probe_history(device_id, min(limit, 200))}


# --------------------------------------------------------------------- devices
@app.get("/api/devices")
def api_list_devices(
    site_id: int | None = None, keyword: str | None = None
) -> list[dict[str, Any]]:
    """设备列表，可按门店过滤（site_id=0 表示未归属）与关键字搜索。"""
    devices = db.list_devices(site_id=site_id, keyword=keyword)
    for device in devices:
        device["summary"] = db.device_backup_summary(device["id"])
    return devices


@app.post("/api/devices", status_code=201)
def api_create_device(payload: DeviceIn, request: Request) -> dict[str, Any]:
    require_role(request, "operator")
    data = payload.model_dump()
    try:
        device = db.create_device(data)
    except Exception as exc:  # 名称唯一约束等
        raise HTTPException(status_code=400, detail=f"创建设备失败: {exc}") from exc
    scheduler.sync_schedule()
    return device


@app.get("/api/devices/{device_id}")
def api_get_device(device_id: int) -> dict[str, Any]:
    device = db.get_device(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="设备不存在")
    device["summary"] = db.device_backup_summary(device_id)
    device["neighbors"] = collector.neighbors_of(device_id)
    return device


@app.patch("/api/devices/{device_id}")
def api_update_device(device_id: int, payload: DevicePatch, request: Request) -> dict[str, Any]:
    require_role(request, "operator")
    device = db.update_device(device_id, payload.model_dump(exclude_unset=True))
    if not device:
        raise HTTPException(status_code=404, detail="设备不存在")
    scheduler.sync_schedule()
    return device


@app.delete("/api/devices/{device_id}")
def api_delete_device(device_id: int, request: Request) -> dict[str, Any]:
    require_role(request, "admin")
    if not db.get_device(device_id):
        raise HTTPException(status_code=404, detail="设备不存在")
    db.delete_device(device_id)
    scheduler.sync_schedule()
    return {"ok": True}


@app.post("/api/devices/{device_id}/test")
def api_test_device(device_id: int) -> dict[str, Any]:
    if not db.get_device(device_id):
        raise HTTPException(status_code=404, detail="设备不存在")
    return collector.test_connection(device_id)


@app.post("/api/devices/{device_id}/backup")
def api_backup_device(device_id: int, request: Request) -> dict[str, Any]:
    require_role(request, "operator")
    if not db.get_device(device_id):
        raise HTTPException(status_code=404, detail="设备不存在")
    return collector.backup_device(device_id, trigger="manual")


@app.get("/api/devices/{device_id}/neighbors")
def api_neighbors(device_id: int) -> dict[str, Any]:
    device = db.get_device(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="设备不存在")
    return {"device": device["name"], "neighbors": collector.neighbors_of(device_id)}


# ------------------------------------------------------------------- backups
@app.get("/api/backups")
def api_list_backups(
    device_id: int | None = None, limit: int = 100, offset: int = 0
) -> list[dict[str, Any]]:
    return db.list_backups(device_id=device_id, limit=min(limit, 500), offset=offset)


@app.post("/api/backups")
def api_backup_all(payload: BackupAllIn, request: Request, background: BackgroundTasks) -> dict[str, Any]:
    require_role(request, "operator")
    devices = db.list_devices()
    targets = payload.device_ids or [d["id"] for d in devices if d["enabled"]]
    valid = {d["id"] for d in devices}
    targets = [t for t in targets if t in valid]
    if not targets:
        raise HTTPException(status_code=400, detail="没有可备份的设备")

    def run() -> None:
        collector.backup_devices(targets, trigger="manual")

    background.add_task(run)
    return {"queued": len(targets), "device_ids": targets}


@app.get("/api/backups/{backup_id}")
def api_get_backup(backup_id: int) -> dict[str, Any]:
    backup = db.get_backup(backup_id)
    if not backup:
        raise HTTPException(status_code=404, detail="备份记录不存在")
    return backup


@app.get("/api/files/{file_id}")
def api_get_file(file_id: int) -> dict[str, Any]:
    row = db.get_file_content(file_id)
    if not row:
        raise HTTPException(status_code=404, detail="文件不存在")
    return {
        "id": row["id"],
        "device_id": row["device_id"],
        "backup_id": row["backup_id"],
        "command": row["command"],
        "content": row["content"],
        "sha256": row["sha256"],
        "size": row["size"],
        "created_at": row["created_at"],
    }


@app.get("/api/files/{file_id}/diff")
def api_get_diff(file_id: int) -> dict[str, Any]:
    row = db.get_diff(file_id)
    if not row:
        raise HTTPException(status_code=404, detail="该文件没有差异记录（内容与上一版本一致）")
    return {
        "id": row["id"],
        "file_id": row["file_id"],
        "command": row["command"],
        "diff_text": row["diff_text"],
        "added": row["added"],
        "removed": row["removed"],
    }


@app.get("/api/files/{file_id}/raw", response_class=PlainTextResponse)
def api_get_raw(file_id: int) -> str:
    row = db.get_file_content(file_id)
    if not row:
        raise HTTPException(status_code=404, detail="文件不存在")
    return row["content"]


@app.get("/api/backup/{backup_id}/download")
def api_download_backup(backup_id: int) -> Response:
    """下载单次备份为zip（保持原有行为）。"""
    backup = db.get_backup(backup_id)
    if not backup:
        raise HTTPException(status_code=404, detail="备份记录不存在")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in backup["files"]:
            row = db.get_file_content(item["id"])
            if row:
                zf.writestr(f"{row['slug']}.txt", row["content"])
    buffer.seek(0)
    filename = f"{backup['device_name']}_{backup['started_at'][:19].replace(':', '')}.zip"
    return Response(
        content=buffer.read(),
        media_type="application/zip",
        headers={"Content-Disposition": exporter.content_disposition(filename)},
    )


# -------------------------------------------------------------------- export
@app.post("/api/export/package")
def api_export_package(payload: ExportIn, request: Request) -> Response:
    require_role(request, "viewer")
    """按门店/设备/全量打包备份文件下载。

    包内按 <门店>/<设备>/<备份时间>/ 组织，并附带 manifest.csv 清单。
    """
    try:
        content, filename, stats = exporter.export_scope(
            scope=payload.scope,
            site_id=payload.site_id,
            device_ids=payload.device_ids,
            limit_per_device=payload.limit_per_device,
            since=payload.since,
            until=payload.until,
            commands=payload.commands,
            include_diff=payload.include_diff,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    log.info(
        "导出 %s: %s 个文件 / %s 台设备 / %s 个门店",
        filename, stats.get("files"), stats.get("devices"), stats.get("sites"),
    )
    return Response(
        content=content,
        media_type="application/zip",
        headers={
            "Content-Disposition": exporter.content_disposition(filename),
            "X-Export-Files": str(stats.get("files", 0)),
            "X-Export-Devices": str(stats.get("devices", 0)),
        },
    )


@app.get("/api/export/sites/{site_id}/package")
def api_export_site(site_id: int, limit_per_device: int | None = None) -> Response:
    """快捷入口：直接下载某个门店的打包文件。"""
    site = db.get_site(site_id)
    if not site:
        raise HTTPException(status_code=404, detail="门店不存在")
    content, filename, _ = exporter.export_scope(
        scope="site", site_id=site_id, limit_per_device=limit_per_device
    )
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": exporter.content_disposition(filename)},
    )


@app.get("/api/export/preview")
def api_export_preview(
    scope: str = "all",
    site_id: int | None = None,
    device_ids: str | None = None,
    limit_per_device: int | None = None,
) -> dict[str, Any]:
    """导出前的规模预览，让用户在下载前知道包里有多少内容。"""
    ids: list[int] = []
    if scope == "site" and site_id is not None:
        ids = db.site_device_ids(site_id, only_enabled=False)
    elif scope == "device" and device_ids:
        ids = [int(x) for x in device_ids.split(",") if x.strip().isdigit()]
    else:
        ids = [d["id"] for d in db.list_devices()]
    rows = exporter._iter_backup_files(ids, limit_per_device)
    sites = {r["site_name"] or r["site"] or "未归属门店" for r in rows}
    return {
        "devices": len(ids),
        "files": len(rows),
        "sites": len(sites),
        "bytes": sum(r["size"] for r in rows),
        "backups": len({(r["device_id"], r["backup_id"]) for r in rows}),
    }


@app.get("/api/status")
def api_status() -> dict[str, Any]:
    devices = db.list_devices()
    sites = db.list_sites()
    ok = sum(1 for d in devices if d["last_status"] == "success")
    failed = sum(1 for d in devices if d["last_status"] == "failed")
    online_info = db.online_overview()
    return {
        "devices": len(devices),
        "sites": len(sites),
        "unassigned": len(db.unassigned_device_ids()),
        "ok": ok,
        "failed": failed,
        "online": online_info["online"],
        "offline": online_info["offline"],
        "unknown": online_info["unknown"],
        "scheduled": sum(1 for d in devices if d["schedule_enabled"] and d["enabled"]),
        "next_runs": scheduler.job_overview(),
        "running": list(collector.RUNNING.values()),
        "recent": collector.HISTORY[-10:],
    }


# ------------------------------------------------------------------ settings
@app.get("/api/settings")
def api_get_settings(request: Request) -> dict[str, Any]:
    require_role(request, "viewer")
    return db.get_public_settings()


@app.put("/api/settings")
def api_put_settings(payload: SettingsIn, request: Request) -> dict[str, Any]:
    require_role(request, "admin")
    result = db.update_settings(payload.model_dump(exclude_unset=True))
    scheduler.sync_schedule()
    scheduler.sync_probe_job()
    return result


@app.get("/api/health")
def api_health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> Any:
    """未登录跳登录页；已登录进主界面。"""
    if db.get_settings().get("auth_enabled", True) and not current_user(request):
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> Any:
    if db.get_settings().get("auth_enabled", True) and current_user(request):
        return RedirectResponse("/", status_code=302)
    return FileResponse(STATIC_DIR / "login.html")


class NoCacheStaticFiles(StaticFiles):
    """禁用静态资源缓存。

    这是个运维工具，改完前端代码刷新页面就该看到新版本，
    而不是被浏览器缓存的旧 JS 坑到「点击没反应」。
    """

    def file_response(self, *args, **kwargs):  # type: ignore[override]
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response


app.mount("/static", NoCacheStaticFiles(directory=STATIC_DIR), name="static")