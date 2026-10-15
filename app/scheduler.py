"""定时调度：按设备的 crontab 表达式自动备份。"""

from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import collector, db, probe

log = logging.getLogger("cisco-backup.scheduler")
JOB_PREFIX = "device-backup:"
PROBE_JOB_ID = "device-probe:all"

scheduler = BackgroundScheduler(timezone="Asia/Shanghai")


def _job(device_id: int) -> None:
    try:
        result = collector.backup_device(device_id, trigger="schedule")
        log.info("定时备份 device=%s -> %s", device_id, result.get("status"))
    except Exception:  # noqa: BLE001
        log.exception("定时备份失败 device=%s", device_id)


def _probe_job() -> None:
    try:
        summary = probe.probe_all()
        log.info(
            "定时探测: %s/%s 在线", summary["online"], summary["total"]
        )
    except Exception:  # noqa: BLE001
        log.exception("在线探测任务失败")


def sync_schedule() -> None:
    """根据设备表的 schedule_enabled / cron 重建备份任务，并同步在线探测任务。"""
    for job in scheduler.get_jobs():
        if job.id.startswith(JOB_PREFIX):
            scheduler.remove_job(job.id)

    devices = db.list_devices()
    for device in devices:
        if not device["enabled"] or not device["schedule_enabled"]:
            continue
        try:
            trigger = CronTrigger.from_crontab(device["cron"])
        except ValueError:
            log.warning("设备 %s 的 cron 表达式无效: %s", device["name"], device["cron"])
            continue
        scheduler.add_job(
            _job,
            trigger=trigger,
            args=[device["id"]],
            id=f"{JOB_PREFIX}{device['id']}",
            name=f"备份 {device['name']}",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=600,
            replace_existing=True,
        )
    sync_probe_job()


def sync_probe_job() -> None:
    """按设置重建在线探测任务（默认每 5 分钟一轮）。"""
    if scheduler.get_job(PROBE_JOB_ID):
        scheduler.remove_job(PROBE_JOB_ID)
    settings = db.get_settings()
    if not settings.get("probe_enabled", True):
        log.info("在线探测已关闭")
        return
    minutes = max(1, int(settings.get("probe_interval_minutes", 5)))
    if not scheduler.running:
        log.info("调度器未运行，跳过探测任务注册")
        return
    scheduler.add_job(
        _probe_job,
        trigger=IntervalTrigger(minutes=minutes),
        id=PROBE_JOB_ID,
        name="设备在线探测",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
        replace_existing=True,
    )
    log.info("在线探测任务已就绪，间隔 %s 分钟", minutes)


def start() -> None:
    if scheduler.running:
        return
    # 先启动调度器，再注册任务：add_job 要求调度器处于运行状态
    scheduler.start()
    sync_schedule()
    log.info("调度器已启动，共 %d 个任务", len(scheduler.get_jobs()))
    # 启动后立刻探测一轮，避免界面上一段时间显示"未知"
    if db.get_settings().get("probe_enabled", True):
        scheduler.add_job(
            _probe_job,
            trigger="date",
            run_date=datetime.now(),
            id=PROBE_JOB_ID + ":initial",
            name="首次在线探测",
            misfire_grace_time=30,
            replace_existing=True,
        )


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


def job_overview() -> list[dict]:
    jobs = []
    for job in scheduler.get_jobs():
        if job.id.startswith(PROBE_JOB_ID):
            jobs.append(
                {
                    "type": "probe",
                    "id": job.id,
                    "name": job.name,
                    "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
                }
            )
        elif job.id.startswith(JOB_PREFIX):
            device_id = int(job.id.split(":")[1])
            jobs.append(
                {
                    "type": "backup",
                    "device_id": device_id,
                    "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
                }
            )
    return jobs