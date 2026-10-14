"""Scheduler: slot leases, run-as-owner fail-closed, restart resume, crash takeover, cron rules."""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest

from app.audit.service import AuditService
from app.core.types import AuditEvent
from app.ports.scheduler import Claim, ScheduledJob
from app.scheduler.cron import MIN_INTERVAL, next_run, validate_schedule
from app.scheduler.runner import JobContext, JobSkipped, SlotRunner
from app.scheduler.service import SchedulerService
from tests.support.fake_scheduler import FakeJobStore, FakeOwners

EVERY_30 = "*/30 * * * *"


class Audit:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def append(self, event: AuditEvent) -> None:
        self.events.append(event)


class Handler:
    def __init__(self) -> None:
        self.runs: list[JobContext] = []
        self.error: Exception | None = None

    def __call__(self, context: JobContext) -> Mapping[str, object]:
        self.runs.append(context)
        if self.error:
            raise self.error
        return {"ok": True}


def world(due: bool = True) -> tuple[FakeJobStore, FakeOwners, Handler, Audit]:
    store = FakeJobStore()
    owners = FakeOwners()
    owners.add("owner")
    store.add(ScheduledJob(
        "j1", "p1", "owner", "profile_drift", EVERY_30, "UTC", "active",
        store.now - timedelta(minutes=1) if due else store.now + timedelta(hours=1),
    ))
    return store, owners, Handler(), Audit()


def runner(store: FakeJobStore, owners: FakeOwners, handler: Handler, audit: Audit,
           worker: str = "w1", lease: int = 300) -> SlotRunner:
    return SlotRunner(
        store, owners, {"profile_drift": handler}, worker_id=worker, lease_seconds=lease,
        audit=AuditService(audit), clock=lambda: store.now,  # type: ignore[arg-type]
    )


def run_all(r: SlotRunner) -> list[str]:
    claims: list[Claim] = []
    r.poll(claims.append)
    return [r.execute(c) for c in claims]


def test_a_due_slot_runs_once_as_the_owner_and_the_job_moves_to_its_next_slot() -> None:
    store, owners, handler, audit = world()
    slot = store.jobs["j1"].next_run_at
    assert run_all(runner(store, owners, handler, audit)) == ["succeeded"]
    assert [(c.slot, c.owner.user_id) for c in handler.runs] == [(slot, "owner")]
    assert store.jobs["j1"].next_run_at > store.now and store.jobs["j1"].last_status == "succeeded"
    assert run_all(runner(store, owners, handler, audit)) == []  # nothing due any more
    run = next(e for e in audit.events if e.action == "scheduled_job_run")
    assert run.actor_id == "owner" and run.details["status"] == "succeeded"


def test_two_workers_polling_the_same_slot_run_it_exactly_once() -> None:
    store, owners, handler, audit = world()
    a, b = runner(store, owners, handler, audit, "w1"), runner(store, owners, handler, audit, "w2")
    claims: list[Claim] = []
    barrier = threading.Barrier(2)

    def poll(r: SlotRunner) -> None:
        barrier.wait()
        r.poll(claims.append)

    threads = [threading.Thread(target=poll, args=(r,)) for r in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(claims) == 1
    a.execute(claims[0])
    assert len(handler.runs) == 1


def test_a_second_claim_on_a_leased_slot_is_refused_even_after_the_job_is_advanced() -> None:
    store, *_ = world()
    job = store.jobs["j1"]
    assert store.claim(job, job.next_run_at, "w1", 300, next_run(EVERY_30, "UTC", store.now)) == 1
    assert store.claim(job, job.next_run_at, "w2", 300, next_run(EVERY_30, "UTC", store.now)) is None
    assert store.claim(job, job.next_run_at, "w2", 300, None) is None  # lease still live


def test_a_crashed_workers_expired_lease_is_taken_over_once() -> None:
    store, owners, handler, audit = world()
    first = runner(store, owners, handler, audit, "dead", lease=60)
    claims: list[Claim] = []
    first.poll(claims.append)  # claimed, then the process dies before execute()
    other = runner(store, owners, handler, audit, "w2")
    assert run_all(other) == []  # lease still valid: not stolen early
    store.advance(seconds=61)
    assert run_all(other) == ["succeeded"]
    assert handler.runs[0].attempt == 2
    assert run_all(runner(store, owners, handler, audit, "w3")) == []  # finished: not again
    store.advance(seconds=61)
    assert run_all(runner(store, owners, handler, audit, "w3")) == []


def test_a_restarted_worker_resumes_from_the_database_and_coalesces_missed_slots() -> None:
    store, owners, handler, audit = world()
    store.advance(hours=5)  # the worker was down for ~10 slots
    restarted = runner(store, owners, handler, audit, "w-new")
    assert run_all(restarted) == ["succeeded"]
    assert len(handler.runs) == 1  # one catch-up run, not ten
    assert store.jobs["j1"].next_run_at > store.now


def test_a_job_that_is_not_due_does_not_run() -> None:
    store, owners, handler, audit = world(due=False)
    assert run_all(runner(store, owners, handler, audit)) == []


@pytest.mark.parametrize("mutation", ["disabled", "deleted_owner_id", "no_capability", "no_project"])
def test_an_owner_who_can_no_longer_run_it_pauses_the_job_and_nothing_runs(mutation: str) -> None:
    store, owners, handler, audit = world()
    if mutation == "disabled":
        del owners.contexts["owner"]  # get_auth_context returns None for disabled/locked users
    elif mutation == "deleted_owner_id":
        store.jobs["j1"] = ScheduledJob(**{**store.jobs["j1"].__dict__, "owner_id": None})
    elif mutation == "no_capability":
        owners.add("owner", capabilities=frozenset({"ask_question"}))
    else:
        owners.add("owner", projects=frozenset({"other"}))
    assert run_all(runner(store, owners, handler, audit)) == ["skipped"]
    assert handler.runs == []
    assert store.jobs["j1"].status == "paused"
    assert store.jobs["j1"].paused_reason in {"owner_unavailable", "owner_lacks_access"}
    assert any(e.action == "scheduled_job_paused" for e in audit.events)
    store.advance(days=1)
    assert run_all(runner(store, owners, handler, audit)) == []  # stays paused


def test_the_owner_is_reloaded_at_run_time_not_cached_from_creation() -> None:
    store, owners, handler, audit = world()
    r = runner(store, owners, handler, audit)
    run_all(r)
    store.advance(hours=1)
    del owners.contexts["owner"]  # disabled between two slots
    assert run_all(r) == ["skipped"]
    assert owners.lookups == ["owner", "owner"]


def test_a_handler_failure_fails_the_slot_without_stopping_the_worker() -> None:
    store, owners, handler, audit = world()
    handler.error = RuntimeError("warehouse down")
    r = runner(store, owners, handler, audit)
    assert run_all(r) == ["failed"]
    lease = next(iter(store.leases.values()))
    assert lease.status == "failed" and "warehouse down" in (lease.error or "")
    assert store.jobs["j1"].status == "active"
    handler.error = None
    store.advance(hours=1)
    assert run_all(r) == ["succeeded"]  # the next slot still runs


def test_a_handler_may_skip_a_slot() -> None:
    store, owners, handler, audit = world()
    handler.error = JobSkipped("no deployed MDL")
    assert run_all(runner(store, owners, handler, audit)) == ["skipped"]
    assert store.jobs["j1"].status == "active"


def test_an_unknown_job_kind_is_paused_not_retried_forever() -> None:
    store, owners, handler, audit = world()
    store.jobs["j1"] = ScheduledJob(**{**store.jobs["j1"].__dict__, "kind": "mystery"})
    assert run_all(runner(store, owners, handler, audit)) == ["failed"]
    assert store.jobs["j1"].status == "paused"


def test_an_invalid_stored_schedule_pauses_the_job() -> None:
    store, owners, handler, audit = world()
    store.jobs["j1"] = ScheduledJob(**{**store.jobs["j1"].__dict__, "schedule": "nonsense"})
    assert run_all(runner(store, owners, handler, audit)) == []
    assert store.jobs["j1"].paused_reason == "invalid_schedule"


def test_a_job_paused_between_claim_and_run_is_skipped() -> None:
    store, owners, handler, audit = world()
    r = runner(store, owners, handler, audit)
    claims: list[Claim] = []
    r.poll(claims.append)
    store.pause("j1", "someone paused it")
    assert r.execute(claims[0]) == "skipped" and handler.runs == []


# ---- cron ---------------------------------------------------------------------------------


def test_cron_next_run_and_timezones() -> None:
    after = datetime(2026, 1, 1, 0, 10, tzinfo=UTC)
    assert next_run("0 9 * * *", "UTC", after) == datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    assert next_run("0 9 * * *", "Asia/Kolkata", after) == datetime(2026, 1, 1, 3, 30, tzinfo=UTC)
    assert next_run("*/30 * * * *", "UTC", after) == datetime(2026, 1, 1, 0, 30, tzinfo=UTC)


@pytest.mark.parametrize(("schedule", "zone"), [
    ("* * * *", "UTC"), ("61 * * * *", "UTC"), ("0 0 * * * *", "UTC"), ("", "UTC"),
    ("0 9 * * *", "Mars/Olympus"),
    ("* * * * *", "UTC"), ("*/5 * * * *", "UTC"),  # too frequent
])
def test_bad_or_too_frequent_schedules_are_rejected(schedule: str, zone: str) -> None:
    with pytest.raises(ValueError):
        validate_schedule(schedule, zone, datetime.now(UTC))


def test_a_sensible_schedule_is_accepted_and_returns_its_first_slot() -> None:
    now = datetime(2026, 1, 1, 0, 10, tzinfo=UTC)
    assert validate_schedule("0 */1 * * *", "UTC", now) == datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
    assert MIN_INTERVAL == timedelta(minutes=15)


# ---- the real APScheduler engine ------------------------------------------------------------


def test_the_apscheduler_service_polls_and_runs_due_slots_then_stops_cleanly() -> None:
    store, owners, handler, audit = world()
    done = threading.Event()
    original = handler.__call__

    class Signalling(Handler):
        def __call__(self, context: JobContext) -> Mapping[str, object]:
            result = original(context)
            done.set()
            return result

    signalling = Signalling()
    signalling.runs = handler.runs
    service = SchedulerService(runner(store, owners, signalling, audit), poll_seconds=0.2)
    service.start()
    try:
        assert done.wait(5), "the due slot was not run by the scheduler"
        time.sleep(0.6)  # further polls must not re-run the slot
    finally:
        service.stop()
    assert len(handler.runs) == 1
    assert store.jobs["j1"].last_status == "succeeded"


def test_a_failing_poll_does_not_stop_the_scheduler() -> None:
    _, owners, handler, audit = world()

    class Broken(FakeJobStore):
        calls = 0

        def list_due(self, now: datetime, limit: int) -> list[ScheduledJob]:
            Broken.calls += 1
            raise ConnectionError("database restarting")

    broken = Broken()
    service = SchedulerService(runner(broken, owners, handler, audit), poll_seconds=0.1)  # type: ignore[arg-type]
    service.start()
    time.sleep(0.5)
    service.stop()
    assert Broken.calls >= 2
