"""Claim due slots and run them as the job's owner.

Safety rules, all fail-closed:
- a slot is run by at most one worker (the store's lease; see JobStorePort.claim);
- the owner is re-loaded from the database at run time, never trusted from when the job was
  created; a disabled, locked, deleted or no-longer-authorised owner pauses the job instead
  of running it with leftover authority;
- a handler error ends that slot as 'failed' and never takes the worker down.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from app.audit.service import AuditService
from app.auth.models import AuthContext
from app.ports.scheduler import Claim, JobStorePort, ScheduledJob
from app.scheduler.cron import next_run

logger = logging.getLogger(__name__)

REQUIRED_CAPABILITY = "manage_profiling"  # the default, and what profile_drift needs
# A job runs with its owner's authority, so each kind names the capability the owner must still hold.
KIND_CAPABILITY = {"profile_drift": "manage_profiling", "saved_question": "schedule_question"}
BATCH_SIZE = 20


class OwnerLookup(Protocol):
    """Looks up the live authorisation context of a job's owner."""
    def get_auth_context(self, user_id: str) -> AuthContext | None:
        """The live context of an active, unlocked account; None otherwise."""
        ...


@dataclass(frozen=True)
class JobContext:
    """Everything a handler needs: the job, the slot, the attempt number and the owner."""
    job: ScheduledJob
    slot: datetime
    attempt: int
    owner: AuthContext


class JobSkipped(Exception):
    """Raised by a handler when there is legitimately nothing to do (recorded as 'skipped')."""


Handler = Callable[[JobContext], Mapping[str, object]]
Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    """The current time as an aware UTC datetime (the default clock)."""
    return datetime.now(UTC)


class SlotRunner:
    """Claims due slots from the job store and runs them as the job's owner."""
    def __init__(
        self,
        store: JobStorePort,
        owners: OwnerLookup,
        handlers: Mapping[str, Handler],
        *,
        worker_id: str,
        lease_seconds: int = 300,
        audit: AuditService | None = None,
        clock: Clock = _utc_now,
    ) -> None:
        """Store the job store, owner lookup, per-kind handlers, lease, audit and clock."""
        self._store = store
        self._owners = owners
        self._handlers = dict(handlers)
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._audit = audit
        self._clock = clock

    @property
    def worker_id(self) -> str:
        """The id this runner uses when claiming slot leases."""
        return self._worker_id

    def poll(self, submit: Callable[[Claim], None]) -> int:
        """Claim every slot that is due (or abandoned by a crashed worker); hand each to submit."""
        now = self._clock()
        claimed = 0
        for job in self._store.list_due(now, BATCH_SIZE):
            slot = job.next_run_at
            try:
                # Coalesce: several missed slots (worker down) become a single run now.
                advance_to = next_run(job.schedule, job.timezone, now)
            except ValueError:
                self._store.pause(job.id, "invalid_schedule")
                continue
            attempt = self._store.claim(job, slot, self._worker_id, self._lease_seconds, advance_to)
            if attempt is not None:
                claimed += 1
                submit(Claim(job, slot, attempt))
        for job, slot in self._store.list_stale(now, BATCH_SIZE):
            attempt = self._store.claim(job, slot, self._worker_id, self._lease_seconds, None)
            if attempt is not None:
                claimed += 1
                submit(Claim(job, slot, attempt))
        return claimed

    def execute(self, claim: Claim) -> str:
        """Run one claimed slot and record its outcome; returns the final status."""
        job, slot = claim.job, claim.slot
        try:
            status, result, error = self._run(claim)
        except Exception as failure:
            logger.exception("Scheduled job crashed", extra={"job_id": job.id})
            status, result, error = "failed", None, _describe(failure)
        try:
            self._store.finish(job.id, slot, self._worker_id, status, result, error)
        except Exception:
            logger.exception("Could not record a job outcome", extra={"job_id": job.id})
        self._record(job, "scheduled_job_run", job.owner_id, {
            "kind": job.kind, "slot": slot.isoformat(), "status": status,
            "attempt": claim.attempt, **({"error": error} if error else {}),
            **({"result": dict(result)} if result else {}),
        })
        return status

    def _run(self, claim: Claim) -> tuple[str, Mapping[str, object] | None, str | None]:
        """Re-check the job and its owner, then call the handler; returns status, result, error."""
        job = claim.job
        # Re-read the job: it may have been paused or re-assigned since the slot was claimed.
        current = self._store.get(job.id)
        if current is None or current.status != "active":
            return "skipped", None, "job is no longer active"
        owner = self._owners.get_auth_context(current.owner_id) if current.owner_id else None
        if owner is None:
            self._pause(current, "owner_unavailable")
            return "skipped", None, "owner is disabled, locked or deleted"
        if (
            KIND_CAPABILITY.get(current.kind, REQUIRED_CAPABILITY) not in owner.capabilities
            or current.project_id not in owner.project_ids
        ):
            self._pause(current, "owner_lacks_access")
            return "skipped", None, "owner no longer has access to run this job"
        handler = self._handlers.get(current.kind)
        if handler is None:
            self._pause(current, "unknown_job_kind")
            return "failed", None, f"no handler for job kind {current.kind!r}"
        try:
            result = handler(JobContext(current, claim.slot, claim.attempt, owner))
        except JobSkipped as skipped:
            return "skipped", None, str(skipped)
        return "succeeded", result, None

    def _pause(self, job: ScheduledJob, reason: str) -> None:
        """Pause the job with a reason and write an audit event for it."""
        self._store.pause(job.id, reason)
        self._record(job, "scheduled_job_paused", None, {"kind": job.kind, "reason": reason})

    def _record(
        self, job: ScheduledJob, action: str, actor: str | None, details: Mapping[str, object]
    ) -> None:
        """Write a scheduler audit event, logging instead of raising if that fails."""
        if self._audit is None:
            return
        try:
            self._audit.record(actor, action, "project", job.project_id, details)
        except Exception:
            logger.exception("Could not write a scheduler audit event", extra={"job_id": job.id})


def _describe(error: BaseException) -> str:
    """Format an exception as ``Type: message`` with the message cut to 300 characters."""
    return f"{type(error).__name__}: {str(error)[:300]}"
