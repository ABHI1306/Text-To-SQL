from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

JOB_KIND_PROFILE_DRIFT = "profile_drift"
JOB_KIND_SAVED_QUESTION = "saved_question"


@dataclass(frozen=True)
class ScheduledJob:
    id: str
    project_id: str
    owner_id: str | None  # None once the owning account has been deleted
    kind: str
    schedule: str  # five-field cron expression
    timezone: str
    status: str  # 'active' | 'paused'
    next_run_at: datetime
    paused_reason: str | None = None
    last_run_at: datetime | None = None
    last_status: str | None = None
    saved_question_id: str | None = None  # the target of a 'saved_question' job


@dataclass(frozen=True)
class Claim:
    """The exclusive right to run one slot of one job."""

    job: ScheduledJob
    slot: datetime
    attempt: int


class JobStorePort(Protocol):
    """Jobs and per-slot leases, durable in the metadata database."""

    def list_due(self, now: datetime, limit: int) -> list[ScheduledJob]:
        """Active jobs whose next slot has arrived."""
        ...

    def list_stale(self, now: datetime, limit: int) -> list[tuple[ScheduledJob, datetime]]:
        """(job, slot) whose worker stopped renewing a running lease: crashed mid-run."""
        ...

    def claim(
        self,
        job: ScheduledJob,
        slot: datetime,
        worker_id: str,
        lease_seconds: int,
        advance_to: datetime | None,
    ) -> int | None:
        """Atomically take a slot; the attempt number, or None when someone else holds it.

        A fresh slot (``advance_to`` given) also moves the job's next slot forward in the same
        transaction. An expired 'running' lease can be taken over (``advance_to`` None).
        """
        ...

    def finish(
        self,
        job_id: str,
        slot: datetime,
        worker_id: str,
        status: str,
        result: Mapping[str, object] | None,
        error: str | None,
    ) -> None: ...

    def pause(self, job_id: str, reason: str) -> None: ...

    def get(self, job_id: str) -> ScheduledJob | None: ...

    def get_for_project(self, project_id: str, kind: str) -> ScheduledJob | None: ...

    def upsert_for_project(
        self,
        project_id: str,
        owner_id: str,
        kind: str,
        schedule: str,
        timezone: str,
        next_run_at: datetime,
    ) -> ScheduledJob:
        """Create or replace the project's job of this kind: active, owned by ``owner_id``."""
        ...

    def delete_for_project(self, project_id: str, kind: str) -> bool: ...

    def create_job(
        self,
        project_id: str,
        owner_id: str,
        kind: str,
        schedule: str,
        timezone: str,
        next_run_at: datetime,
        saved_question_id: str | None,
    ) -> ScheduledJob:
        """A new active job (several may share a project and kind, unlike the project upsert)."""
        ...

    def list_jobs(
        self, kind: str, *, owner_id: str | None = None, project_id: str | None = None
    ) -> list[ScheduledJob]: ...

    def update_job(
        self, job_id: str, schedule: str, timezone: str, next_run_at: datetime
    ) -> ScheduledJob | None:
        """New schedule and next slot; the job becomes active again."""
        ...

    def resume(self, job_id: str, next_run_at: datetime) -> ScheduledJob | None: ...

    def delete_job(self, job_id: str) -> bool: ...
