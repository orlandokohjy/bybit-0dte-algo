"""
APScheduler-based job scheduler for the three daily sessions.

Registers entry and close jobs based on SessionConfig times.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import structlog

import config
from config import SessionConfig, SESSIONS
from utils.time_utils import UTC

log = structlog.get_logger(__name__)


class Scheduler:
    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler(timezone=UTC)
        self._entry_callbacks: dict[int, callable] = {}
        self._close_callbacks: dict[int, callable] = {}

    def register_session(
        self,
        session_cfg: SessionConfig,
        on_entry: callable,
        on_close: callable,
    ) -> None:
        sid = session_cfg.session_id.value
        self._entry_callbacks[sid] = on_entry
        self._close_callbacks[sid] = on_close

        weekdays = ",".join(
            ["mon", "tue", "wed", "thu", "fri"][d] for d in sorted(config.ALLOWED_WEEKDAYS)
        )

        entry_t = session_cfg.entry_time_utc
        self._scheduler.add_job(
            on_entry,
            CronTrigger(
                hour=entry_t.hour,
                minute=entry_t.minute,
                day_of_week=weekdays,
                timezone=UTC,
            ),
            id=f"entry_s{sid}",
            name=f"Session {sid} Entry",
            replace_existing=True,
        )

        close_t = session_cfg.close_time_utc
        self._scheduler.add_job(
            on_close,
            CronTrigger(
                hour=close_t.hour,
                minute=close_t.minute,
                day_of_week=weekdays,
                timezone=UTC,
            ),
            id=f"close_s{sid}",
            name=f"Session {sid} Close",
            replace_existing=True,
        )

        log.info(
            "session_scheduled",
            session=sid,
            entry=f"{entry_t.hour:02d}:{entry_t.minute:02d} UTC",
            close=f"{close_t.hour:02d}:{close_t.minute:02d} UTC",
            days=weekdays,
        )

    def register_daily_reset(self, callback: callable) -> None:
        """Schedule daily reset at 08:00 UTC (settlement time)."""
        weekdays = ",".join(
            ["mon", "tue", "wed", "thu", "fri"][d] for d in sorted(config.ALLOWED_WEEKDAYS)
        )
        self._scheduler.add_job(
            callback,
            CronTrigger(hour=8, minute=0, day_of_week=weekdays, timezone=UTC),
            id="daily_reset",
            name="Daily Reset (08:00 UTC)",
            replace_existing=True,
        )
        log.info("daily_reset_scheduled", time="08:00 UTC")

    def start(self) -> None:
        self._scheduler.start()
        log.info("scheduler_started", jobs=len(self._scheduler.get_jobs()))

    def stop(self) -> None:
        self._scheduler.shutdown(wait=False)
        log.info("scheduler_stopped")

    def get_next_fire_times(self) -> dict[str, datetime | None]:
        return {
            job.id: job.next_run_time
            for job in self._scheduler.get_jobs()
        }
