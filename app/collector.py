"""备份采集编排：登录设备 -> 逐条执行命令 -> 清洗 -> 入库 -> 生成 diff。"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from . import db, neighbors, vendors
from .drivers.base import (
    DeviceError,
    clean_output,
    parse_os_version,
    sha256_text,
    slugify,
)
from .drivers.ssh_driver import SSHClient
from .drivers.telnet_driver import TelnetClient

log = logging.getLogger("cisco-backup.collector")

# 进行中的任务：device_id -> 状态字典
RUNNING: dict[int, dict[str, Any]] = {}
# 最近完成的任务（环形裁剪）
HISTORY: list[dict[str, Any]] = []
MAX_HISTORY = 200


def _build_client(device: dict[str, Any], settings: dict[str, Any]):
    kwargs = dict(
        host=device["host"],
        port=device["port"],
        username=device.get("username", ""),
        password=device.get("password", ""),
        connect_timeout=int(settings.get("connect_timeout", 15)),
        command_timeout=int(settings.get("command_timeout", 45)),
        paging_fix=bool(settings.get("paging_fix", True)),
        device_type=device.get("device_type", "cisco_ios"),
    )
    if device["protocol"] == "telnet":
        return TelnetClient(**kwargs)
    return SSHClient(**kwargs)


def backup_device(device_id: int, trigger: str = "manual") -> dict[str, Any]:
    """执行一次备份，返回结果摘要。同步函数，供 API 与调度器共用。"""
    device = db.get_device(device_id, reveal=True)
    if not device:
        raise DeviceError(f"设备 id={device_id} 不存在")
    settings = db.get_settings()
    vendor = vendors.normalize_vendor(device.get("device_type"))
    # 命令优先级：设备自定义 > 厂商默认 > 全局默认
    commands = device.get("commands") or vendors.default_commands_for(vendor)
    if not commands:
        commands = settings["default_commands"]

    RUNNING[device_id] = {
        "device_id": device_id,
        "name": device["name"],
        "trigger": trigger,
        "started_at": time.time(),
        "done": False,
    }

    backup_id = db.create_backup(device_id, trigger, device["host"])
    started = time.time()
    client = None
    changed = False
    try:
        client = _build_client(device, settings)
        client.connect()
        client.enable(device.get("enable_password") or "")
        RUNNING[device_id]["prompt"] = client.prompt

        os_version = None
        # 优先跑厂商的 version 命令拿准确版本号；命令列表里已有它就复用，
        # 没有则额外跑一次（该输出不计入备份文件，仅用于展示版本）
        version_cmd = vendors.vendor_config(device["device_type"])["version_command"]
        version_output = ""
        config_output = ""

        for command in commands:
            raw = client.run(command)
            content = clean_output(raw, command, client.prompt)
            sha = sha256_text(content)
            file_meta, diff_meta = db.add_file(
                backup_id, device_id, command, slugify(command), content, sha
            )
            RUNNING[device_id]["last_command"] = command
            if file_meta["is_new"] and diff_meta:
                changed = True
            log.info("[%s] %s %s", device["name"], command, "CHANGED" if changed else "OK")

            low = command.strip().lower()
            if low == version_cmd:
                version_output = content
            if "current-configuration" in low or "running-config" in low:
                config_output = content

        # 版本号：version 命令输出最准确；没有则从配置文件里兜底提取
        if not version_output and version_cmd not in [
            c.strip().lower() for c in commands
        ]:
            try:
                version_output = clean_output(
                    client.run(version_cmd), version_cmd, client.prompt
                )
            except DeviceError:
                log.debug("[%s] 获取版本号失败，忽略", device["name"])
        os_version = (
            parse_os_version(version_output, vendor)
            or parse_os_version(config_output, vendor)
        )

        duration = int((time.time() - started) * 1000)
        db.finish_backup(backup_id, "success", changed=changed, duration_ms=duration, os_version=os_version)
        db.set_device_result(device_id, "success", f"{len(commands)} 条命令采集完成")
        db.prune_versions(device_id, int(settings.get("keep_versions", 50)))
        return {
            "backup_id": backup_id,
            "device_id": device_id,
            "status": "success",
            "changed": changed,
            "duration_ms": duration,
            "files": len(commands),
        }
    except DeviceError as exc:
        duration = int((time.time() - started) * 1000)
        db.finish_backup(backup_id, "failed", error=str(exc), duration_ms=duration)
        db.set_device_result(device_id, "failed", str(exc))
        log.warning("[%s] 备份失败: %s", device["name"], exc)
        return {"backup_id": backup_id, "device_id": device_id, "status": "failed", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        duration = int((time.time() - started) * 1000)
        msg = f"未预期错误: {type(exc).__name__}: {exc}"
        db.finish_backup(backup_id, "failed", error=msg, duration_ms=duration)
        db.set_device_result(device_id, "failed", msg)
        log.exception("[%s] 备份异常", device["name"])
        return {"backup_id": backup_id, "device_id": device_id, "status": "failed", "error": msg}
    finally:
        if client is not None:
            client.close()
        state = RUNNING.get(device_id)
        if state is not None:
            state["done"] = True
            state["finished_at"] = time.time()
            # 清理已结束的条目，避免无界增长
            RUNNING.pop(device_id, None)
            HISTORY.append(
                {
                    "device_id": device_id,
                    "name": device["name"],
                    "status": "done",
                    "finished_at": state["finished_at"],
                }
            )
            del HISTORY[:-MAX_HISTORY]


def backup_devices(device_ids: list[int], trigger: str = "manual") -> list[dict[str, Any]]:
    if not device_ids:
        return []
    settings = db.get_settings()
    workers = max(1, min(int(settings.get("concurrency", 4)), len(device_ids)))
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(backup_device, did, trigger): did for did in device_ids}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001
                results.append({"device_id": futures[future], "status": "failed", "error": str(exc)})
    return results


def test_connection(device_id: int) -> dict[str, Any]:
    """只登录不采集，用于前端"测试连接"。"""
    device = db.get_device(device_id, reveal=True)
    if not device:
        raise DeviceError("设备不存在")
    settings = db.get_settings()
    client = _build_client(device, settings)
    started = time.time()
    try:
        client.connect()
        client.enable(device.get("enable_password") or "")
        return {
            "ok": True,
            "prompt": client.prompt,
            "elapsed_ms": int((time.time() - started) * 1000),
            "message": "连接成功",
        }
    except DeviceError as exc:
        return {"ok": False, "message": str(exc), "elapsed_ms": int((time.time() - started) * 1000)}
    finally:
        client.close()


def neighbors_of(device_id: int) -> list[dict[str, Any]]:
    """取该设备最近一次成功备份中的邻居信息（按厂商解析）。"""
    device = db.get_device(device_id)
    if not device:
        return []
    vendor = vendors.normalize_vendor(device.get("device_type"))
    parser_name = vendors.vendor_config(device["device_type"])["neighbor_parser"]
    neighbor_cmd = vendors.vendor_config(device["device_type"])["neighbor_command"]

    # 优先按厂商的邻居命令找；找不到再退回历史里的 lldp/cdp 输出
    candidates = [neighbor_cmd]
    candidates += [c for c in device.get("commands", []) if c != neighbor_cmd]
    for cmd in candidates:
        row = db.latest_config(device_id, cmd)
        if not row:
            continue
        # 命令名与解析器不匹配时（如用户自定义了邻居命令），
        # 退回 cisco 解析器试一次
        rows = neighbors.parse_neighbors(row["content"], parser_name)
        if rows:
            return rows
    return []