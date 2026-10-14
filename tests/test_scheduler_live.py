"""Job leases, drift reports and OIDC state against a real metadata database (needs migration 0008).

Skipped unless METADATA_APP_DSN points at a database that has the scheduler tables.
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from app.adapters.metadata.drift import PostgresDriftRepository
from app.adapters.metadata.oidc_state import PostgresOidcStateStore
from app.adapters.metadata.postgres import PostgresMetadataRepository
from app.adapters.metadata.scheduler import PostgresJobStore
from app.auth.service import hash_password
from app.ports.scheduler import ScheduledJob

DSN = os.getenv("METADATA_APP_DSN", "")


def _ready() -> bool:
    if not DSN:
        return False
    try:
        with psycopg.connect(DSN) as connection:
            row = connection.execute("SELECT to_regclass('public.job_leases')").fetchone()
        return bool(row and row[0])
    except psycopg.Error:
        return False


pytestmark = pytest.mark.skipif(
    not _ready(), reason="METADATA_APP_DSN with the 0008 scheduler tables is required"
)


@pytest.fixture()
def project() -> tuple[str, str]:
    repository = PostgresMetadataRepository(DSN)
    suffix = uuid.uuid4().hex[:10]
    owner = repository.create_local_user(f"sched-{suffix}", "DBA", hash_password("a-long-password-1"))
    project_id = str(uuid.uuid4())
    repository.create_project(
        project_id=project_id, name=f"sched {suffix}", host="demo-db", port=5432,
        database_name="demo", ssl_enabled=False, encrypted_connection=b"x", owner_id=owner,
    )
    return project_id, owner


def make_job(store: PostgresJobStore, project_id: str, owner: str, slot: datetime) -> ScheduledJob:
    return store.upsert_for_project(project_id, owner, "profile_drift", "0 * * * *", "UTC", slot)


def test_a_slot_is_claimed_by_exactly_one_of_many_concurrent_workers(
    project: tuple[str, str],
) -> None:
    project_id, owner = project
    store = PostgresJobStore(DSN)
    slot = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=1)
    job = make_job(store, project_id, owner, slot)
    results: list[int | None] = []
    barrier = threading.Barrier(8)

    def worker(n: int) -> None:
        barrier.wait()
        results.append(store.claim(job, slot, f"w{n}", 300, slot + timedelta(hours=1)))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r for r in results if r is not None) == [1] and results.count(None) == 7
    moved = store.get(job.id)
    assert moved is not None and moved.next_run_at == slot + timedelta(hours=1)


def test_due_listing_lease_takeover_finish_and_pause(project: tuple[str, str]) -> None:
    project_id, owner = project
    store = PostgresJobStore(DSN)
    slot = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=1)
    job = make_job(store, project_id, owner, slot)
    assert job.id in {j.id for j in store.list_due(datetime.now(UTC), 100)}
    assert store.claim(job, slot, "dead", 0, slot + timedelta(hours=1)) == 1  # lease already expired
    assert job.id not in {j.id for j in store.list_due(datetime.now(UTC), 100)}
    stale = [(j.id, s) for j, s in store.list_stale(datetime.now(UTC), 100)]
    assert (job.id, slot) in stale
    assert store.claim(job, slot, "w2", 300, None) == 2  # takeover of the dead worker's slot
    assert store.claim(job, slot, "w3", 300, None) is None  # live lease: no second takeover
    store.finish(job.id, slot, "dead", "succeeded", {"x": 1}, None)  # the dead worker is ignored
    store.finish(job.id, slot, "w2", "succeeded", {"x": 1}, None)
    assert (job.id, slot) not in [(j.id, s) for j, s in store.list_stale(datetime.now(UTC), 100)]
    assert store.claim(job, slot, "w4", 300, None) is None  # finished slots are never retaken
    done = store.get(job.id)
    assert done is not None and done.last_status == "succeeded"
    store.pause(job.id, "owner_unavailable")
    paused = store.get(job.id)
    assert paused is not None and paused.status == "paused" and paused.paused_reason == "owner_unavailable"
    assert job.id not in {j.id for j in store.list_due(datetime.now(UTC) + timedelta(days=2), 100)}
    assert store.claim(job, slot + timedelta(hours=1), "w5", 300, slot + timedelta(hours=2)) is None


def test_replacing_a_schedule_reactivates_it_under_the_new_owner(project: tuple[str, str]) -> None:
    project_id, owner = project
    store = PostgresJobStore(DSN)
    slot = datetime.now(UTC) + timedelta(hours=1)
    job = make_job(store, project_id, owner, slot)
    store.pause(job.id, "owner_unavailable")
    again = store.upsert_for_project(project_id, owner, "profile_drift", "30 2 * * *", "UTC", slot)
    assert again.id == job.id and again.status == "active" and again.paused_reason is None
    assert again.schedule == "30 2 * * *"
    assert store.delete_for_project(project_id, "profile_drift") is True
    assert store.get(job.id) is None


def test_drift_reports_are_idempotent_per_slot_and_a_draft_is_created_once(
    project: tuple[str, str],
) -> None:
    project_id, owner = project
    jobs = PostgresJobStore(DSN)
    reports = PostgresDriftRepository(DSN)
    metadata = PostgresMetadataRepository(DSN)
    slot = datetime.now(UTC).replace(microsecond=0)
    job = make_job(jobs, project_id, owner, slot)
    document = {"models": [], "relationships": [], "metrics": [], "instructions": [], "layout": {}}
    base = metadata.save_mdl(project_id, document)
    kwargs = {
        "project_id": project_id, "mdl_version": base, "job_id": job.id, "slot": slot, "has_drift": True,
        "summary": {"column_added": 1}, "findings": [{"kind": "column_added"}], "created_by": owner,
    }
    first = reports.save_report(**kwargs)  # type: ignore[arg-type]
    assert reports.save_report(**kwargs) == first  # type: ignore[arg-type]
    listed = reports.list_reports(project_id, limit=10)
    assert [r.id for r in listed] == [first] and listed[0].findings == [{"kind": "column_added"}]
    assert reports.get_report(project_id, first) is not None
    assert reports.get_report(str(uuid.uuid4()), first) is None  # another project: not visible

    results: list[int | None] = []
    barrier = threading.Barrier(4)

    def draft() -> None:
        barrier.wait()
        results.append(reports.create_draft(project_id, first, document, created_by=owner))

    threads = [threading.Thread(target=draft) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    created = [r for r in results if r is not None]
    assert created == [base + 1] and results.count(None) == 3
    stored = reports.get_report(project_id, first)
    assert stored is not None and stored.draft_mdl_version == base + 1
    assert metadata.get_mdl(project_id, base + 1) is not None


def test_oidc_state_is_one_time_and_expires() -> None:
    store = PostgresOidcStateStore(DSN)
    now = datetime.now(UTC)
    key = uuid.uuid4().hex
    assert store.put(key, "nonce-1", "verifier-1", now + timedelta(minutes=5))
    assert store.consume(key, now) == ("nonce-1", "verifier-1")
    assert store.consume(key, now) is None
    expired = uuid.uuid4().hex
    store.put(expired, "n", "v", now - timedelta(seconds=1))
    assert store.consume(expired, now) is None
    racing = uuid.uuid4().hex
    store.put(racing, "n", "v", now + timedelta(minutes=5))
    winners: list[tuple[str, str] | None] = []
    barrier = threading.Barrier(6)

    def consume() -> None:
        barrier.wait()
        winners.append(store.consume(racing, now))

    threads = [threading.Thread(target=consume) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len([w for w in winners if w is not None]) == 1
