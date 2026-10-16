"""In-memory JobStorePort with the same locking and lease rules as the Postgres adapter."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from threading import Lock

from app.auth.models import AuthContext
from app.ports.scheduler import ScheduledJob


@dataclass
class Lease:
    worker_id: str
    leased_until: datetime
    attempts: int = 1
    status: str = "running"
    result: Mapping[str, object] | None = None
    error: str | None = None


@dataclass
class FakeJobStore:
    clock: list[datetime] = field(default_factory=lambda: [datetime.now(UTC)])
    jobs: dict[str, ScheduledJob] = field(default_factory=dict)
    leases: dict[tuple[str, datetime], Lease] = field(default_factory=dict)
    lock: Lock = field(default_factory=Lock)

    @property
    def now(self) -> datetime:
        return self.clock[0]

    def advance(self, **delta: float) -> None:
        self.clock[0] += timedelta(**delta)

    def add(self, job: ScheduledJob) -> None:
        self.jobs[job.id] = job

    def list_due(self, now: datetime, limit: int) -> list[ScheduledJob]:
        with self.lock:
            due = [j for j in self.jobs.values() if j.status == "active" and j.next_run_at <= now]
        return sorted(due, key=lambda j: j.next_run_at)[:limit]

    def list_stale(self, now: datetime, limit: int) -> list[tuple[ScheduledJob, datetime]]:
        with self.lock:
            return [
                (self.jobs[job_id], slot)
                for (job_id, slot), lease in self.leases.items()
                if lease.status == "running" and lease.leased_until < now
                and self.jobs[job_id].status == "active"
            ][:limit]

    def claim(
        self, job: ScheduledJob, slot: datetime, worker_id: str, lease_seconds: int,
        advance_to: datetime | None,
    ) -> int | None:
        with self.lock:
            current = self.jobs.get(job.id)
            key = (job.id, slot)
            if advance_to is not None:
                if current is None or current.status != "active" or current.next_run_at != slot:
                    return None
                if key in self.leases:
                    return None
                self.jobs[job.id] = replace(current, next_run_at=advance_to)
                self.leases[key] = Lease(worker_id, self.now + timedelta(seconds=lease_seconds))
                return 1
            lease = self.leases.get(key)
            if lease is None or lease.status != "running" or lease.leased_until >= self.now:
                return None
            lease.worker_id = worker_id
            lease.leased_until = self.now + timedelta(seconds=lease_seconds)
            lease.attempts += 1
            return lease.attempts

    def finish(
        self, job_id: str, slot: datetime, worker_id: str, status: str,
        result: Mapping[str, object] | None, error: str | None,
    ) -> None:
        with self.lock:
            lease = self.leases.get((job_id, slot))
            if lease is not None and lease.worker_id == worker_id and lease.status == "running":
                lease.status, lease.result, lease.error = status, result, error
                self.jobs[job_id] = replace(self.jobs[job_id], last_status=status,
                                            last_run_at=self.now)

    def pause(self, job_id: str, reason: str) -> None:
        with self.lock:
            self.jobs[job_id] = replace(self.jobs[job_id], status="paused", paused_reason=reason)

    def get(self, job_id: str) -> ScheduledJob | None:
        return self.jobs.get(job_id)

    def get_for_project(self, project_id: str, kind: str) -> ScheduledJob | None:
        return next((j for j in self.jobs.values()
                     if j.project_id == project_id and j.kind == kind), None)

    def upsert_for_project(
        self, project_id: str, owner_id: str, kind: str, schedule: str, timezone: str,
        next_run_at: datetime,
    ) -> ScheduledJob:
        existing = self.get_for_project(project_id, kind)
        job = ScheduledJob(
            existing.id if existing else f"job-{len(self.jobs) + 1}", project_id, owner_id, kind,
            schedule, timezone, "active", next_run_at,
        )
        self.jobs[job.id] = job
        return job

    def create_job(
        self, project_id: str, owner_id: str, kind: str, schedule: str, timezone: str,
        next_run_at: datetime, saved_question_id: str | None,
    ) -> ScheduledJob:
        job = ScheduledJob(
            f"job-{len(self.jobs) + 1}", project_id, owner_id, kind, schedule, timezone, "active",
            next_run_at, saved_question_id=saved_question_id,
        )
        self.jobs[job.id] = job
        return job

    def list_jobs(
        self, kind: str, *, owner_id: str | None = None, project_id: str | None = None
    ) -> list[ScheduledJob]:
        return [
            j for j in self.jobs.values()
            if j.kind == kind and (owner_id is None or j.owner_id == owner_id)
            and (project_id is None or j.project_id == project_id)
        ]

    def update_job(
        self, job_id: str, schedule: str, timezone: str, next_run_at: datetime
    ) -> ScheduledJob | None:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        self.jobs[job_id] = replace(
            job, schedule=schedule, timezone=timezone, next_run_at=next_run_at, status="active",
            paused_reason=None,
        )
        return self.jobs[job_id]

    def resume(self, job_id: str, next_run_at: datetime) -> ScheduledJob | None:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        self.jobs[job_id] = replace(job, status="active", paused_reason=None, next_run_at=next_run_at)
        return self.jobs[job_id]

    def delete_job(self, job_id: str) -> bool:
        return self.jobs.pop(job_id, None) is not None

    def delete_for_project(self, project_id: str, kind: str) -> bool:
        existing = self.get_for_project(project_id, kind)
        if existing is None:
            return False
        del self.jobs[existing.id]
        return True


class FakeOwners:
    def __init__(self) -> None:
        self.contexts: dict[str, AuthContext] = {}
        self.lookups: list[str] = []

    def add(
        self, user_id: str, *, capabilities: frozenset[str] = frozenset({"manage_profiling"}),
        projects: frozenset[str] = frozenset({"p1"}),
    ) -> None:
        self.contexts[user_id] = AuthContext(
            user_id, f"user-{user_id}", "DBA", capabilities, projects, False
        )

    def get_auth_context(self, user_id: str) -> AuthContext | None:
        self.lookups.append(user_id)
        return self.contexts.get(user_id)
