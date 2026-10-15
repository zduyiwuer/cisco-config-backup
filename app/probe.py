"""设备在线探测。

与备份采集分离的轻量探活：
- SSH：只做TCP 连接 + 协议 banner 读取，不做完整认证，失败时降级为端口探测。
- Telnet：只做 TCP 连接并读取首段数据。
- 记录延迟、连续失败次数，历史保留在 probe_log 表。

这样每 5 分钟跑一轮的成本极低，且不会因为设备 enable 密码错误之类的问题
把"在线"误判为"离线"。
"""

from __future__ import annotations

import logging
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from . import db

log = logging.getLogger("cisco-backup.probe")

DEFAULT_PROBE_TIMEOUT = 5


def _probe_tcp(host: str, port: int, timeout: int) -> tuple[bool, int | None, str | None]:
    """最轻量的探活：能否建立 TCP 连接。"""
    start = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, int((time.time() - start) * 1000), None
    except socket.timeout:
        return False, None, "连接超时"
    except OSError as exc:
        return False, None, f"{type(exc).__name__}: {exc}"


def _probe_ssh_banner(host: str, port: int, timeout: int) -> tuple[bool, int | None, str | None]:
    """读 SSH banner，进一步确认对端确实是 SSH 服务而非普通 TCP 端口。"""
    start = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            try:
                banner = sock.recv(255).decode("utf-8", errors="replace")
            except (socket.timeout, OSError):
                # 连上了但没 banner：可能不是 SSH 服务，降级按端口通判在线
                return True, int((time.time() - start) * 1000), None
            if banner.startswith("SSH-"):
                return True, int((time.time() - start) * 1000), None
            return (
                False,
                int((time.time() - start) * 1000),
                f"端口开放但非 SSH 服务(返回: {banner.strip()[:40]!r})",
            )
    except socket.timeout:
        return False, None, "连接超时"
    except OSError as exc:
        return False, None, f"{type(exc).__name__}: {exc}"


def probe_device(device: dict[str, Any], timeout: int = DEFAULT_PROBE_TIMEOUT) -> dict[str, Any]:
    """探测单台设备，返回并落库探测结果。"""
    host, port, protocol = device["host"], device["port"], device["protocol"]
    if protocol == "ssh":
        online, latency, error = _probe_ssh_banner(host, port, timeout)
        if not online and error and "非 SSH" in error:
            # SSH 服务未就绪但端口通，判定为"通但不健康"，仍记为离线并保留原因
            pass
    else:
        online, latency, error = _probe_tcp(host, port, timeout)

    db.record_probe(device["id"], online, latency, error)
    return {
        "device_id": device["id"],
        "name": device["name"],
        "host": host,
        "online": online,
        "latency_ms": latency,
        "error": error,
    }


def probe_all(timeout: int | None = None, workers: int = 16) -> dict[str, Any]:
    """并发探测所有启用设备。"""
    settings = db.get_settings()
    probe_timeout = timeout or int(settings.get("probe_timeout", DEFAULT_PROBE_TIMEOUT))
    devices = [d for d in db.list_devices() if d["enabled"]]
    results: list[dict[str, Any]] = []
    started = time.time()

    pool_size = max(1, min(workers, len(devices) or 1))
    with ThreadPoolExecutor(max_workers=pool_size) as pool:
        futures = {pool.submit(probe_device, d, probe_timeout): d for d in devices}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001
                results.append(
                    {
                        "device_id": futures[future]["id"],
                        "name": futures[future]["name"],
                        "online": False,
                        "latency_ms": None,
                        "error": f"探测异常: {exc}",
                    }
                )

    online = sum(1 for r in results if r["online"])
    summary = {
        "total": len(results),
        "online": online,
        "offline": len(results) - online,
        "duration_ms": int((time.time() - started) * 1000),
        "results": sorted(results, key=lambda r: r["name"]),
    }
    log.info(
        "在线探测完成: %s/%s 在线，耗时 %sms",
        online, len(results), summary["duration_ms"],
    )
    return summary