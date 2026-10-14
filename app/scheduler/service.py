"""APScheduler as the engine; the metadata database as the source of truth.

Jobs are not kept in APScheduler's memory. A single interval job polls the database for due
slots, so a restarted (or second) worker rehydrates everything from the table: nothing is lost
when a process dies, and slot leases decide which worker runs what. Each claimed slot is handed
to APScheduler's thread pool as a one-off job.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

from app.ports.scheduler import Claim
from app.scheduler.runner import SlotRunner

logger = logging.getLogger(__name__)


class SchedulerService:
    def __init__(
        self, runner: SlotRunner, *, poll_seconds: float = 15.0, max_concurrent_jobs: int = 4
    ) -> None:
        self._runner = runner
        self._poll_seconds = poll_seconds
        self._scheduler = BackgroundScheduler(
            executors={"default": ThreadPoolExecutor(max_concurrent_jobs)},
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": None},
            timezone=UTC,
        )

    def start(self) -> None:
        self._scheduler.add_job(
            self.poll_once,
            "interval",
            seconds=self._poll_seconds,
            id="poll-due-slots",
            next_run_time=datetime.now(UTC),  # rehydrate immediately after a restart
            replace_existing=True,
        )
        self._scheduler.start()
        logger.info("Scheduler started", extra={"worker_id": self._runner.worker_id})

    def poll_once(self) -> int:
        try:
            return self._runner.poll(self._submit)
        except Exception:
            logger.exception("Scheduler poll failed")
            return 0

    def _submit(self, claim: Claim) -> None:
        self._scheduler.add_job(
            self._runner.execute,
            args=[claim],
            id=f"run:{claim.job.id}:{claim.slot.isoformat()}:{claim.attempt}",
            misfire_grace_time=None,
        )

    def stop(self, *, wait: bool = True) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=wait)
