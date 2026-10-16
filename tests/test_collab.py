"""Dashboards, saved questions, shares and scheduled questions (Day 7, Track B).

The first test is the one the plan asks for before anything else: two viewers with different row
policies open the same shared tile and each gets only their own rows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.adapters.metadata.collab import InMemoryCollabStore
from app.auth.models import AuthContext
from app.collab.jobs import SavedQuestionJobHandler
from app.collab.service import CollabService, Invalid, NotFound, ProjectRuntime, QueryFailed
from app.core.types import QueryResult, ValidatedQuery
from app.ports.scheduler import JOB_KIND_SAVED_QUESTION
from app.scheduler.runner import SlotRunner
from tests.support.audit import RecordingAudit
from tests.support.ecommerce_mdl import build_mdl, build_policies
from tests.support.fake_scheduler import FakeJobStore, FakeOwners

PROJECT = "p1"
MDL = build_mdl()
POLICIES = build_policies()
LOGICAL = "SELECT order_id FROM orders"
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


class RegionWarehouse:
    """Returns rows that depend on the policy parameter, so a leak between viewers is visible."""

    def __init__(self) -> None:
        self.executed: list[ValidatedQuery] = []

    def explain(self, query: ValidatedQuery) -> None:
        return None

    def execute_read_only(self, query: ValidatedQuery, *, max_rows: int) -> QueryResult:
        self.executed.append(query)
        region = query.parameters.get("p_region", "ALL")
        return QueryResult(("order_id",), tuple((f"{region}-{n}",) for n in range(3)))


def context(
    user_id: str, region: str | None = None, *, role: str = "Analyst",
    capabilities: frozenset[str] = frozenset(
        {"view_shared", "create_dashboard", "schedule_question", "share", "ask_question"}
    ),
    projects: frozenset[str] = frozenset({PROJECT}),
) -> AuthContext:
    return AuthContext(
        user_id, user_id, role, capabilities, projects, False,
        attributes={"region": region} if region else {},
    )


class Fixture:
    def __init__(self) -> None:
        self.store = InMemoryCollabStore()
        self.jobs = FakeJobStore()
        self.jobs.clock[0] = NOW
        self.audit = RecordingAudit()
        self.warehouse = RegionWarehouse()
        self.service = CollabService(
            self.store, self.jobs,
            lambda _project: ProjectRuntime(1, MDL, list(POLICIES), self.warehouse, 1000),
            self.audit, clock=lambda: self.jobs.now,
        )

    def dashboard_with_tile(self, owner: AuthContext) -> tuple[str, str, str]:
        question = self.service.save_question(owner, PROJECT, "orders?", LOGICAL, None)
        dashboard = self.service.create_dashboard(owner, PROJECT, "Sales", {})
        (tile,) = self.service.put_tiles(
            owner, dashboard["id"], [{"saved_question_id": question["id"]}]
        )
        return dashboard["id"], tile["id"], question["id"]


@pytest.fixture
def fx() -> Fixture:
    return Fixture()


# ---------------------------------------------------------------- the two-viewer test


def test_two_viewers_with_different_policies_each_get_only_their_own_rows(fx: Fixture) -> None:
    owner = context("owner", "West")
    west, east = context("v-west", "West"), context("v-east", "EU")
    dashboard_id, tile_id, _ = fx.dashboard_with_tile(owner)
    fx.service.create_share(owner, "dashboard", dashboard_id, "user", "v-west")
    fx.service.create_share(owner, "dashboard", dashboard_id, "user", "v-east")

    first = fx.service.tile_result(west, dashboard_id, tile_id, 100)
    other = fx.service.tile_result(east, dashboard_id, tile_id, 100)

    assert [r[0] for r in first["rows"]] == ["West-0", "West-1", "West-2"]
    assert [r[0] for r in other["rows"]] == ["EU-0", "EU-1", "EU-2"]
    assert other["cached"] is False  # West's cached rows were not reused for EU
    assert first["policy_context"] != other["policy_context"]


def test_a_cached_result_is_reused_only_for_the_same_policy_context(fx: Fixture) -> None:
    owner = context("owner", "West")
    same = context("same", "West")
    dashboard_id, tile_id, _ = fx.dashboard_with_tile(owner)
    fx.service.create_share(owner, "dashboard", dashboard_id, "user", "same")

    fx.service.tile_result(owner, dashboard_id, tile_id, 100)
    reused = fx.service.tile_result(same, dashboard_id, tile_id, 100)

    assert reused["cached"] is True and len(fx.warehouse.executed) == 1


def test_refresh_drops_every_viewers_cache(fx: Fixture) -> None:
    owner = context("owner", "West")
    dashboard_id, tile_id, _ = fx.dashboard_with_tile(owner)
    fx.service.tile_result(owner, dashboard_id, tile_id, 100)
    assert fx.service.refresh_dashboard(owner, dashboard_id) == 1
    assert fx.service.tile_result(owner, dashboard_id, tile_id, 100)["cached"] is False


def test_the_cache_expires(fx: Fixture) -> None:
    owner = context("owner", "West")
    dashboard_id, tile_id, _ = fx.dashboard_with_tile(owner)
    fx.service.tile_result(owner, dashboard_id, tile_id, 100)
    fx.jobs.advance(seconds=301)
    assert fx.service.tile_result(owner, dashboard_id, tile_id, 100)["cached"] is False


# ---------------------------------------------------------------- hostile requests


def test_a_stranger_cannot_open_or_read_a_dashboard(fx: Fixture) -> None:
    owner = context("owner", "West")
    dashboard_id, tile_id, question_id = fx.dashboard_with_tile(owner)
    stranger = context("stranger", "West")
    with pytest.raises(NotFound):
        fx.service.get_dashboard(stranger, dashboard_id)
    with pytest.raises(NotFound):
        fx.service.tile_result(stranger, dashboard_id, tile_id, 10)
    with pytest.raises(NotFound):
        fx.service.saved_question_result(stranger, question_id, 10)


def test_another_projects_dashboard_is_not_found_even_when_shared(fx: Fixture) -> None:
    owner = context("owner", "West")
    dashboard_id, tile_id, _ = fx.dashboard_with_tile(owner)
    fx.service.create_share(owner, "dashboard", dashboard_id, "user", "outsider")
    outsider = context("outsider", "West", projects=frozenset({"other-project"}))
    with pytest.raises(NotFound):
        fx.service.tile_result(outsider, dashboard_id, tile_id, 10)


def test_a_share_does_nothing_without_view_shared(fx: Fixture) -> None:
    owner = context("owner", "West")
    dashboard_id, _, _ = fx.dashboard_with_tile(owner)
    fx.service.create_share(owner, "dashboard", dashboard_id, "user", "v")
    with pytest.raises(NotFound):
        fx.service.get_dashboard(context("v", "West", capabilities=frozenset()), dashboard_id)


def test_a_revoked_share_stops_working_at_once(fx: Fixture) -> None:
    owner = context("owner", "West")
    viewer = context("v", "West")
    dashboard_id, tile_id, _ = fx.dashboard_with_tile(owner)
    share = fx.service.create_share(owner, "dashboard", dashboard_id, "user", "v")
    fx.service.tile_result(viewer, dashboard_id, tile_id, 10)
    fx.service.revoke_share(owner, share["id"])
    with pytest.raises(NotFound):
        fx.service.tile_result(viewer, dashboard_id, tile_id, 10)
    assert "share_revoked" in fx.audit.actions()


def test_only_the_owner_can_share_revoke_or_list_shares(fx: Fixture) -> None:
    owner, other = context("owner", "West"), context("other", "West")
    dashboard_id, _, _ = fx.dashboard_with_tile(owner)
    share = fx.service.create_share(owner, "dashboard", dashboard_id, "user", "v")
    with pytest.raises(NotFound):
        fx.service.create_share(other, "dashboard", dashboard_id, "user", "v")
    with pytest.raises(NotFound):
        fx.service.revoke_share(other, share["id"])
    with pytest.raises(NotFound):
        fx.service.list_shares(other, "dashboard", dashboard_id)


def test_role_shares_reach_everyone_in_the_role(fx: Fixture) -> None:
    owner = context("owner", "West")
    dashboard_id, tile_id, _ = fx.dashboard_with_tile(owner)
    fx.service.create_share(owner, "dashboard", dashboard_id, "role", "Viewer")
    viewer = context("v", "EU", role="Viewer")
    assert fx.service.tile_result(viewer, dashboard_id, tile_id, 10)["rows"][0][0] == "EU-0"
    assert [i["resource_id"] for i in fx.service.shared_with_me(viewer)] == [dashboard_id]


def test_sharing_with_yourself_or_an_unknown_grantee_is_refused() -> None:
    fx = Fixture()
    fx.service._user_exists = lambda user_id: user_id == "real"
    fx.service._role_exists = lambda name: name == "Viewer"
    owner = context("owner", "West")
    dashboard_id, _, _ = fx.dashboard_with_tile(owner)
    with pytest.raises(Invalid):
        fx.service.create_share(owner, "dashboard", dashboard_id, "user", "owner")
    with pytest.raises(Invalid):
        fx.service.create_share(owner, "dashboard", dashboard_id, "user", "ghost")
    with pytest.raises(Invalid):
        fx.service.create_share(owner, "dashboard", dashboard_id, "role", "Nope")


def test_a_huge_max_rows_is_clamped_to_the_runtime_limit(fx: Fixture) -> None:
    owner = context("owner", "West")
    _, _, question_id = fx.dashboard_with_tile(owner)
    result = fx.service.saved_question_result(owner, question_id, 10**9)
    assert len(result["rows"]) == 3  # the warehouse got max_rows 1000, not 10**9


def test_tiles_can_only_pin_your_own_saved_questions(fx: Fixture) -> None:
    owner, other = context("owner", "West"), context("other", "West")
    _, _, question_id = fx.dashboard_with_tile(owner)
    theirs = fx.service.create_dashboard(other, PROJECT, "Mine", {})
    with pytest.raises(Invalid):
        fx.service.put_tiles(other, theirs["id"], [{"saved_question_id": question_id}])


def test_a_saved_question_must_compile_for_its_owner(fx: Fixture) -> None:
    with pytest.raises(Invalid):
        fx.service.save_question(context("owner", "West"), PROJECT, "x", "DROP TABLE orders", None)


def test_shares_and_dashboard_changes_are_audited(fx: Fixture) -> None:
    owner = context("owner", "West")
    dashboard_id, _, _ = fx.dashboard_with_tile(owner)
    fx.service.create_share(owner, "dashboard", dashboard_id, "user", "v")
    fx.service.refresh_dashboard(owner, dashboard_id)
    assert {"saved_question_created", "dashboard_created", "dashboard_tiles_changed",
            "share_created", "dashboard_refreshed"} <= set(fx.audit.actions())


# ---------------------------------------------------------------- scheduled questions


def scheduled(fx: Fixture, owner: AuthContext) -> tuple[str, str]:
    question = fx.service.save_question(owner, PROJECT, "orders?", LOGICAL, None)
    job = fx.service.schedule_question(owner, question["id"], "hourly", "UTC")
    return job.id, question["id"]


def test_presets_and_cron_are_accepted_and_too_frequent_schedules_refused(fx: Fixture) -> None:
    owner = context("owner", "West")
    question = fx.service.save_question(owner, PROJECT, "orders?", LOGICAL, None)
    assert fx.service.schedule_question(owner, question["id"], "daily", "UTC").schedule == "0 6 * * *"
    assert fx.service.schedule_question(owner, question["id"], "*/30 * * * *", "UTC")
    with pytest.raises(Invalid):
        fx.service.schedule_question(owner, question["id"], "* * * * *", "UTC")
    with pytest.raises(Invalid):
        fx.service.schedule_question(owner, question["id"], "not cron", "UTC")


def test_run_now_runs_as_the_owner_and_keeps_history_with_an_idempotency_key(fx: Fixture) -> None:
    owner = context("owner", "West")
    job_id, _ = scheduled(fx, owner)
    first = fx.service.run_now(owner, job_id, "key-1")
    again = fx.service.run_now(owner, job_id, "key-1")
    assert first["status"] == "succeeded" and again["id"] == first["id"]
    assert len(fx.warehouse.executed) == 1
    assert first["policy_context"]


def test_only_the_owner_can_touch_a_schedule(fx: Fixture) -> None:
    owner, other = context("owner", "West"), context("other", "West")
    job_id, _ = scheduled(fx, owner)
    for call in (fx.service.get_scheduled, fx.service.pause, fx.service.resume,
                 fx.service.delete_scheduled, fx.service.list_runs):
        with pytest.raises(NotFound):
            call(other, job_id)
    with pytest.raises(NotFound):
        fx.service.run_now(other, job_id, None)


def test_only_the_last_20_runs_are_kept(fx: Fixture) -> None:
    owner = context("owner", "West")
    job_id, _ = scheduled(fx, owner)
    for _ in range(25):
        fx.jobs.advance(seconds=1)
        fx.service.run_now(owner, job_id, None)
    assert len(fx.service.list_runs(owner, job_id)) == 20


def test_pause_and_resume(fx: Fixture) -> None:
    owner = context("owner", "West")
    job_id, _ = scheduled(fx, owner)
    fx.service.pause(owner, job_id)
    assert fx.service.get_scheduled(owner, job_id).status == "paused"
    assert fx.service.resume(owner, job_id).status == "active"


def test_a_failed_run_is_recorded_and_notifies_the_owner(fx: Fixture) -> None:
    owner = context("owner", "West")
    job_id, _ = scheduled(fx, owner)
    fx.warehouse.execute_read_only = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("down"))  # type: ignore[method-assign]
    with pytest.raises(QueryFailed):
        fx.service.run_now(owner, job_id, None)
    (run,) = fx.service.list_runs(owner, job_id)
    assert run["status"] == "failed" and run["error"]
    (note,) = fx.store.list_notifications("owner", 10)
    assert note["kind"] == "scheduled_run_failed"


# ---- the real worker path: slots, two workers, disabled owner


def worker(fx: Fixture, owners: FakeOwners, worker_id: str) -> SlotRunner:
    return SlotRunner(
        fx.jobs, owners,
        {JOB_KIND_SAVED_QUESTION: SavedQuestionJobHandler(fx.service)},
        worker_id=worker_id, audit=fx.audit, clock=lambda: fx.jobs.now,
    )


def _owners(owner: AuthContext) -> FakeOwners:
    owners = FakeOwners()
    owners.contexts[owner.user_id] = owner
    return owners


def test_a_slot_runs_once_with_two_workers(fx: Fixture) -> None:
    owner = context("owner", "West")
    job_id, _ = scheduled(fx, owner)
    fx.jobs.advance(hours=2)
    owners = _owners(owner)
    claimed: list[Any] = []
    for name in ("w1", "w2"):
        worker(fx, owners, name).poll(claimed.append)
    assert len(claimed) == 1
    worker(fx, owners, "w1").execute(claimed[0])
    assert len(fx.service.list_runs(owner, job_id)) == 1
    assert len(fx.warehouse.executed) == 1


def test_a_job_whose_owner_is_disabled_pauses_itself_and_never_runs(fx: Fixture) -> None:
    owner = context("owner", "West")
    job_id, _ = scheduled(fx, owner)
    fx.jobs.advance(hours=2)
    runner = worker(fx, FakeOwners(), "w1")  # the owner is unknown: disabled / locked / deleted
    claimed: list[Any] = []
    runner.poll(claimed.append)
    assert runner.execute(claimed[0]) == "skipped"
    assert fx.jobs.get(job_id).status == "paused"  # type: ignore[union-attr]
    assert fx.jobs.get(job_id).paused_reason == "owner_unavailable"  # type: ignore[union-attr]
    assert fx.warehouse.executed == []


def test_an_owner_without_schedule_question_pauses_the_job(fx: Fixture) -> None:
    owner = context("owner", "West")
    job_id, _ = scheduled(fx, owner)
    fx.jobs.advance(hours=2)
    demoted = context("owner", "West", capabilities=frozenset({"view_shared"}))
    runner = worker(fx, _owners(demoted), "w1")
    claimed: list[Any] = []
    runner.poll(claimed.append)
    runner.execute(claimed[0])
    assert fx.jobs.get(job_id).paused_reason == "owner_lacks_access"  # type: ignore[union-attr]


def test_a_scheduled_result_is_stored_under_the_owners_policy_context(fx: Fixture) -> None:
    owner = context("owner", "West")
    _job_id, question_id = scheduled(fx, owner)
    fx.jobs.advance(hours=2)
    runner = worker(fx, _owners(owner), "w1")
    claimed: list[Any] = []
    runner.poll(claimed.append)
    runner.execute(claimed[0])
    east = context("east", "EU")
    fx.service.create_share(owner, "saved_question", question_id, "user", "east")
    result = fx.service.saved_question_result(east, question_id, 10)
    assert result["cached"] is False and result["rows"][0][0] == "EU-0"


def test_a_failed_run_with_a_key_is_not_replayed_as_a_success(fx: Fixture) -> None:
    owner = context("owner", "West")
    job_id, _ = scheduled(fx, owner)

    def boom(query: ValidatedQuery, *, max_rows: int) -> QueryResult:
        raise RuntimeError("warehouse down")

    fx.warehouse.execute_read_only = boom  # type: ignore[method-assign]
    with pytest.raises(QueryFailed):
        fx.service.run_now(owner, job_id, "key-f")
    with pytest.raises(Invalid):
        fx.service.run_now(owner, job_id, "key-f")


def test_shares_of_a_disabled_owner_stop_being_visible() -> None:
    fx = Fixture()
    active = {"owner": True}
    fx.service = CollabService(
        fx.store, fx.jobs,
        lambda _project: ProjectRuntime(1, MDL, list(POLICIES), fx.warehouse, 1000),
        fx.audit, clock=lambda: fx.jobs.now, user_exists=lambda uid: active.get(uid, True),
    )
    owner = context("owner", "West")
    dashboard_id, tile_id, _ = fx.dashboard_with_tile(owner)
    fx.service.create_share(owner, "dashboard", dashboard_id, "role", "Viewer")
    viewer = context("v", "EU", role="Viewer")
    assert fx.service.get_dashboard(viewer, dashboard_id)["id"] == dashboard_id

    active["owner"] = False

    with pytest.raises(NotFound):
        fx.service.get_dashboard(viewer, dashboard_id)
    with pytest.raises(NotFound):
        fx.service.tile_result(viewer, dashboard_id, tile_id, 10)
    assert fx.service.shared_with_me(viewer) == []
