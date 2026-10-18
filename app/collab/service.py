"""Saved questions, dashboards, shares and scheduled questions.

Two rules carry the safety of this module:

* Nobody ever receives rows computed for someone else. A tile or saved question is stored as
  logical SQL; every viewer's rows are compiled and run under that viewer's own principal, and a
  cached result is reused only when the compiled ``policy_context`` and the model version match.
* Every read goes through ``_visible``: the owner, or a holder of ``view_shared`` the resource was
  shared with (directly or through their role), and in both cases a member of the project. Anything
  else looks like "not found", so ids cannot be probed.
"""

from __future__ import annotations

import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any

from app.agent.state import StructuredFailure
from app.audit.service import AuditService
from app.auth.models import AuthContext, to_principal
from app.core.jsonable import json_value
from app.domain.mdl import Mdl
from app.domain.policy import RowPolicy
from app.ports.collab import CollabStorePort, Record
from app.ports.scheduler import JOB_KIND_SAVED_QUESTION, JobStorePort, ScheduledJob
from app.ports.warehouse import WarehousePort
from app.scheduler.cron import validate_schedule
from app.semantic.runner import compile_for, execute_compiled, failure_from

PRESETS = {"hourly": "0 * * * *", "daily": "0 6 * * *", "weekly": "0 6 * * 1"}
DEFAULT_CACHE_SECONDS = 300
MAX_TILES = 24


# Striped locks: one per bucket of idempotency keys, so nothing needs cleaning up afterwards.
_KEY_LOCKS = tuple(Lock() for _ in range(64))


def _key_lock(key: str) -> Lock:
    """Pick the striped lock for an idempotency key (same key, same lock)."""
    return _KEY_LOCKS[zlib.crc32(key.encode()) % len(_KEY_LOCKS)]


class NotFound(LookupError):
    """Missing, or not visible to the caller: the two are indistinguishable by design."""


class Invalid(ValueError):
    """The request is well-formed but not acceptable (bad SQL, bad schedule, bad grantee)."""


class QueryFailed(Exception):
    """A saved question's query failed; carries the ``StructuredFailure``."""
    def __init__(self, failure: StructuredFailure) -> None:
        """Keep the structured failure and use its message as the exception text."""
        super().__init__(failure.message)
        self.failure = failure


@dataclass
class ProjectRuntime:
    """What it takes to run logical SQL in one project; ``close`` releases the connections."""

    version: int
    mdl: Mdl
    policies: list[RowPolicy]
    warehouse: WarehousePort
    max_rows: int
    close: Callable[[], None] = lambda: None


RuntimeFactory = Callable[[str], ProjectRuntime]


def _utc_now() -> datetime:
    """The current time as an aware UTC datetime (the default clock)."""
    return datetime.now(UTC)


def normalize_schedule(schedule: str) -> str:
    """Expand a preset name (hourly, daily, weekly) to cron; otherwise return it stripped."""
    return PRESETS.get(schedule.strip().lower(), schedule.strip())


class CollabService:
    """Saved questions, dashboards, shares and scheduled questions, with per-viewer access."""
    def __init__(
        self,
        store: CollabStorePort,
        jobs: JobStorePort,
        runtime_for: RuntimeFactory,
        audit: AuditService | Any,
        *,
        user_exists: Callable[[str], bool] = lambda _id: True,
        role_exists: Callable[[str], bool] = lambda _name: True,
        clock: Callable[[], datetime] = _utc_now,
        cache_seconds: int = DEFAULT_CACHE_SECONDS,
    ) -> None:
        """Store the collaborators; existence checks, clock and cache lifetime are injectable."""
        self._store = store
        self._jobs = jobs
        self._runtime_for = runtime_for
        self._audit = audit
        self._user_exists = user_exists
        self._role_exists = role_exists
        self._clock = clock
        self._cache_seconds = cache_seconds

    # ------------------------------------------------------------------ access
    def _in_project(self, context: AuthContext, project_id: str) -> bool:
        """Whether the caller is a member of ``project_id``."""
        return project_id in context.project_ids

    def _visible(self, context: AuthContext, resource_type: str, record: Record) -> bool:
        """Whether the caller owns the record or it was shared with them, within their projects."""
        if not self._in_project(context, str(record["project_id"])):
            return False
        if record["owner_id"] == context.user_id:
            return True
        # What a disabled or locked account owns stops being visible to everyone it was shared with.
        return (
            "view_shared" in context.capabilities
            and self._user_exists(str(record["owner_id"]))
            and self._store.has_share(
                resource_type, str(record["id"]), context.user_id, context.role_name
            )
        )

    def _own_saved_question(self, context: AuthContext, question_id: str) -> Record:
        """Load a saved question the caller owns, or raise ``NotFound``."""
        record = self._store.get_saved_question(question_id)
        if (
            record is None
            or record["owner_id"] != context.user_id
            or not self._in_project(context, str(record["project_id"]))
        ):
            raise NotFound("Saved question not found")
        return record

    def _own_dashboard(self, context: AuthContext, dashboard_id: str) -> Record:
        """Load a dashboard the caller owns, or raise ``NotFound``."""
        record = self._store.get_dashboard(dashboard_id)
        if (
            record is None
            or record["owner_id"] != context.user_id
            or not self._in_project(context, str(record["project_id"]))
        ):
            raise NotFound("Dashboard not found")
        return record

    def _record(self, context: AuthContext, action: str, kind: str, target: str,
                details: Mapping[str, object] | None = None) -> None:
        """Write an audit event attributed to the caller."""
        self._audit.record(context.user_id, action, kind, target, details or {})

    # ------------------------------------------------------------------ saved questions
    def save_question(
        self, context: AuthContext, project_id: str, question: str, logical_sql: str,
        chart_spec: Record | None,
    ) -> Record:
        """Validate the logical SQL compiles for the caller, then save it as a question."""
        if not self._in_project(context, project_id):
            raise NotFound("Project not found")
        runtime = self._runtime_for(project_id)
        try:
            compiled = compile_for(logical_sql, runtime.mdl, to_principal(context), runtime.policies)
        finally:
            runtime.close()
        if isinstance(compiled, tuple):
            raise Invalid("; ".join(f"{e.code.value}: {e.message}" for e in compiled))
        record = self._store.create_saved_question(
            project_id, context.user_id, question, logical_sql, chart_spec
        )
        self._record(context, "saved_question_created", "saved_question", record["id"],
                     {"project_id": project_id})
        return record

    def list_saved_questions(self, context: AuthContext, project_id: str) -> list[Record]:
        """List the caller's saved questions in a project they belong to."""
        if not self._in_project(context, project_id):
            raise NotFound("Project not found")
        return self._store.list_saved_questions(project_id, context.user_id)

    def get_saved_question(self, context: AuthContext, question_id: str) -> Record:
        """Return a saved question the caller owns or can see through a share."""
        record = self._store.get_saved_question(question_id)
        if record is None or not self._visible(context, "saved_question", record):
            raise NotFound("Saved question not found")
        return record

    def delete_saved_question(self, context: AuthContext, question_id: str) -> None:
        """Delete an owned question along with its schedules and cached results."""
        record = self._own_saved_question(context, question_id)
        for job in self._jobs.list_jobs(JOB_KIND_SAVED_QUESTION, owner_id=context.user_id):
            if job.saved_question_id == question_id:
                self._jobs.delete_job(job.id)
        self._store.invalidate("saved_question", question_id)
        self._store.delete_saved_question(question_id)
        self._record(context, "saved_question_deleted", "saved_question", question_id,
                     {"project_id": record["project_id"]})

    # ------------------------------------------------------------------ result computing
    def run_for(
        self, context: AuthContext, project_id: str, saved_question: Record, *,
        max_rows: int | None = None, use_cache: bool = True,
    ) -> Record:
        """The saved question's rows under ``context``'s own policies (see module docstring)."""
        runtime = self._runtime_for(project_id)
        try:
            principal = to_principal(context)
            compiled = compile_for(
                str(saved_question["logical_sql"]), runtime.mdl, principal, runtime.policies
            )
            if isinstance(compiled, tuple):
                raise QueryFailed(failure_from(compiled))
            policy_context = compiled.policy_context or ""
            resource_id = str(saved_question["id"])
            limit = runtime.max_rows if max_rows is None else max(1, min(max_rows, runtime.max_rows))
            now = self._clock()
            if use_cache:
                cached = self._store.get_cached(
                    "saved_question", resource_id, policy_context, runtime.version, now
                )
                # A result computed with a smaller row limit must not satisfy a larger request.
                if cached is not None and len(cached["rows"]) <= limit:
                    return {
                        "columns": cached["columns"], "rows": cached["rows"],
                        "truncated": cached["truncated"], "cached": True,
                        "computed_at": cached["computed_at"], "policy_context": policy_context,
                    }
            outcome = execute_compiled(compiled, runtime.mdl, runtime.warehouse, limit)
            if outcome.failure is not None:
                raise QueryFailed(outcome.failure)
            rows = [[json_value(cell) for cell in row] for row in outcome.rows]
            self._store.put_cached(
                "saved_question", resource_id, policy_context, runtime.version, outcome.columns,
                rows, outcome.truncated, now, now + timedelta(seconds=self._cache_seconds),
            )
            return {
                "columns": outcome.columns, "rows": rows, "truncated": outcome.truncated,
                "cached": False, "computed_at": now, "policy_context": policy_context,
            }
        finally:
            runtime.close()

    def saved_question_result(
        self, context: AuthContext, question_id: str, max_rows: int | None
    ) -> Record:
        """Return the rows of a visible saved question under the caller's own policies."""
        record = self.get_saved_question(context, question_id)
        return self.run_for(context, str(record["project_id"]), record, max_rows=max_rows)

    # ------------------------------------------------------------------ dashboards
    def create_dashboard(
        self, context: AuthContext, project_id: str, title: str, layout: Record
    ) -> Record:
        """Create a dashboard in a project the caller belongs to."""
        if not self._in_project(context, project_id):
            raise NotFound("Project not found")
        record = self._store.create_dashboard(project_id, context.user_id, title, layout)
        self._record(context, "dashboard_created", "dashboard", record["id"],
                     {"project_id": project_id})
        return record

    def list_dashboards(self, context: AuthContext, project_id: str) -> list[Record]:
        """List the caller's dashboards in a project they belong to."""
        if not self._in_project(context, project_id):
            raise NotFound("Project not found")
        return self._store.list_dashboards(project_id, context.user_id)

    def get_dashboard(self, context: AuthContext, dashboard_id: str) -> Record:
        """Return a visible dashboard with its tiles and whether the caller owns it."""
        record = self._store.get_dashboard(dashboard_id)
        if record is None or not self._visible(context, "dashboard", record):
            raise NotFound("Dashboard not found")
        tiles = self._store.list_tiles(dashboard_id)
        return {**record, "tiles": tiles, "is_owner": record["owner_id"] == context.user_id}

    def update_dashboard(
        self, context: AuthContext, dashboard_id: str, title: str | None, layout: Record | None
    ) -> Record:
        """Change an owned dashboard's title and/or layout."""
        self._own_dashboard(context, dashboard_id)
        record = self._store.update_dashboard(dashboard_id, title, layout)
        if record is None:
            raise NotFound("Dashboard not found")
        self._record(context, "dashboard_updated", "dashboard", dashboard_id, {})
        return record

    def delete_dashboard(self, context: AuthContext, dashboard_id: str) -> None:
        """Delete an owned dashboard."""
        self._own_dashboard(context, dashboard_id)
        self._store.delete_dashboard(dashboard_id)
        self._record(context, "dashboard_deleted", "dashboard", dashboard_id, {})

    def put_tiles(
        self, context: AuthContext, dashboard_id: str, tiles: list[Record]
    ) -> list[Record]:
        """Replace an owned dashboard's tiles (own questions from this project only)."""
        dashboard = self._own_dashboard(context, dashboard_id)
        if len(tiles) > MAX_TILES:
            raise Invalid(f"A dashboard holds at most {MAX_TILES} tiles")
        for tile in tiles:
            question = self._store.get_saved_question(str(tile["saved_question_id"]))
            if (
                question is None
                or question["owner_id"] != context.user_id
                or question["project_id"] != dashboard["project_id"]
            ):
                raise Invalid("Tiles can only pin your own saved questions from this project")
        saved = self._store.replace_tiles(dashboard_id, tiles)
        self._record(context, "dashboard_tiles_changed", "dashboard", dashboard_id,
                     {"tiles": len(saved)})
        return saved

    def refresh_dashboard(self, context: AuthContext, dashboard_id: str) -> int:
        """Drop every cached result of the dashboard's tiles (all viewers' policy contexts)."""
        self._own_dashboard(context, dashboard_id)
        tiles = self._store.list_tiles(dashboard_id)
        for tile in tiles:
            self._store.invalidate("saved_question", str(tile["saved_question_id"]))
        self._record(context, "dashboard_refreshed", "dashboard", dashboard_id,
                     {"tiles": len(tiles)})
        return len(tiles)

    def tile_result(
        self, context: AuthContext, dashboard_id: str, tile_id: str, max_rows: int | None
    ) -> Record:
        """Return a tile's rows under the caller's own policies, with its chart spec."""
        dashboard = self._store.get_dashboard(dashboard_id)
        if dashboard is None or not self._visible(context, "dashboard", dashboard):
            raise NotFound("Dashboard not found")
        tile = next(
            (t for t in self._store.list_tiles(dashboard_id) if str(t["id"]) == tile_id), None
        )
        if tile is None:
            raise NotFound("Tile not found")
        question = self._store.get_saved_question(str(tile["saved_question_id"]))
        if question is None:
            raise NotFound("Tile not found")
        result = self.run_for(context, str(dashboard["project_id"]), question, max_rows=max_rows)
        return {**result, "chart_spec": tile.get("chart_spec") or question.get("chart_spec"),
                "question": question["question"]}

    # ------------------------------------------------------------------ shares
    def _owned_resource(self, context: AuthContext, resource_type: str, resource_id: str,
                        thread_owner: Callable[[str], tuple[str, str] | None] | None) -> str:
        """The project id of a resource the caller owns; NotFound otherwise."""
        if resource_type == "dashboard":
            return str(self._own_dashboard(context, resource_id)["project_id"])
        if resource_type == "saved_question":
            return str(self._own_saved_question(context, resource_id)["project_id"])
        found = thread_owner(resource_id) if thread_owner else None
        if found is None or found[0] != context.user_id or found[1] not in context.project_ids:
            raise NotFound("Thread not found")
        return found[1]

    def create_share(
        self, context: AuthContext, resource_type: str, resource_id: str, grantee_type: str,
        grantee_id: str, *, thread_owner: Callable[[str], tuple[str, str] | None] | None = None,
    ) -> Record:
        """Share an owned resource with a user or role after checking the grantee exists."""
        project_id = self._owned_resource(context, resource_type, resource_id, thread_owner)
        if grantee_type == "user":
            if grantee_id == context.user_id:
                raise Invalid("You already own this")
            if not self._user_exists(grantee_id):
                raise Invalid("Unknown user")
        elif not self._role_exists(grantee_id):
            raise Invalid("Unknown role")
        share = self._store.create_share(
            resource_type, resource_id, grantee_type, grantee_id, context.user_id
        )
        self._record(context, "share_created", resource_type, resource_id,
                     {"project_id": project_id, "grantee_type": grantee_type,
                      "grantee_id": grantee_id})
        return share

    def list_shares(
        self, context: AuthContext, resource_type: str, resource_id: str, *,
        thread_owner: Callable[[str], tuple[str, str] | None] | None = None,
    ) -> list[Record]:
        """List the shares of a resource the caller owns."""
        self._owned_resource(context, resource_type, resource_id, thread_owner)
        return self._store.list_shares(resource_type, resource_id)

    def revoke_share(
        self, context: AuthContext, share_id: str, *,
        thread_owner: Callable[[str], tuple[str, str] | None] | None = None,
    ) -> None:
        """Delete a share, provided the caller owns the shared resource."""
        share = self._store.get_share(share_id)
        if share is None:
            raise NotFound("Share not found")
        self._owned_resource(context, str(share["resource_type"]), str(share["resource_id"]),
                             thread_owner)
        self._store.delete_share(share_id)
        self._record(context, "share_revoked", str(share["resource_type"]),
                     str(share["resource_id"]),
                     {"grantee_type": share["grantee_type"], "grantee_id": share["grantee_id"]})

    def shared_with_me(self, context: AuthContext) -> list[Record]:
        """List resources shared with the caller (empty without ``view_shared``)."""
        if "view_shared" not in context.capabilities:
            return []
        items: list[Record] = []
        for share in self._store.list_shared_with(context.user_id, context.role_name):
            kind, resource_id = str(share["resource_type"]), str(share["resource_id"])
            if kind == "dashboard":
                record = self._store.get_dashboard(resource_id)
                title = record["title"] if record else None
            elif kind == "saved_question":
                record = self._store.get_saved_question(resource_id)
                title = record["question"] if record else None
            else:
                record, title = None, "Conversation"
            if kind != "thread" and (record is None or not self._visible(context, kind, record)):
                continue
            items.append({
                "share_id": share["id"], "resource_type": kind, "resource_id": resource_id,
                "title": title, "owner_id": record["owner_id"] if record else None,
                "granted_at": share["granted_at"],
            })
        return items

    # ------------------------------------------------------------------ scheduled questions
    def _schedule_slot(self, schedule: str, timezone: str) -> tuple[str, datetime]:
        """Normalise and validate a schedule; return the cron string and first slot."""
        cron = normalize_schedule(schedule)
        try:
            return cron, validate_schedule(cron, timezone, self._clock())
        except ValueError as error:
            raise Invalid(str(error)) from error

    def _own_job(self, context: AuthContext, job_id: str) -> ScheduledJob:
        """Load a saved-question job the caller owns in their projects, or raise ``NotFound``."""
        job = self._jobs.get(job_id)
        if (
            job is None or job.kind != JOB_KIND_SAVED_QUESTION
            or job.owner_id != context.user_id or job.project_id not in context.project_ids
        ):
            raise NotFound("Scheduled question not found")
        return job

    def schedule_question(
        self, context: AuthContext, saved_question_id: str, schedule: str, timezone: str
    ) -> ScheduledJob:
        """Create a schedule for an owned saved question."""
        question = self._own_saved_question(context, saved_question_id)
        cron, first = self._schedule_slot(schedule, timezone)
        job = self._jobs.create_job(
            str(question["project_id"]), context.user_id, JOB_KIND_SAVED_QUESTION, cron, timezone,
            first, saved_question_id,
        )
        self._record(context, "schedule_created", "saved_question", saved_question_id,
                     {"job_id": job.id, "schedule": cron, "timezone": timezone})
        return job

    def list_scheduled(self, context: AuthContext) -> list[ScheduledJob]:
        """List the caller's scheduled questions."""
        return self._jobs.list_jobs(JOB_KIND_SAVED_QUESTION, owner_id=context.user_id)

    def get_scheduled(self, context: AuthContext, job_id: str) -> ScheduledJob:
        """Return a scheduled question the caller owns."""
        return self._own_job(context, job_id)

    def update_schedule(
        self, context: AuthContext, job_id: str, schedule: str | None, timezone: str | None
    ) -> ScheduledJob:
        """Change the cron schedule and/or time zone of an owned job."""
        job = self._own_job(context, job_id)
        cron, first = self._schedule_slot(schedule or job.schedule, timezone or job.timezone)
        updated = self._jobs.update_job(job.id, cron, timezone or job.timezone, first)
        if updated is None:
            raise NotFound("Scheduled question not found")
        self._record(context, "schedule_updated", "saved_question",
                     job.saved_question_id or "", {"job_id": job.id, "schedule": cron})
        return updated

    def pause(self, context: AuthContext, job_id: str) -> None:
        """Pause an owned scheduled question."""
        job = self._own_job(context, job_id)
        self._jobs.pause(job.id, "paused_by_owner")
        self._record(context, "schedule_paused", "saved_question",
                     job.saved_question_id or "", {"job_id": job.id})

    def resume(self, context: AuthContext, job_id: str) -> ScheduledJob:
        """Resume an owned scheduled question from its next valid slot."""
        job = self._own_job(context, job_id)
        _, first = self._schedule_slot(job.schedule, job.timezone)
        resumed = self._jobs.resume(job.id, first)
        if resumed is None:
            raise NotFound("Scheduled question not found")
        self._record(context, "schedule_resumed", "saved_question",
                     job.saved_question_id or "", {"job_id": job.id})
        return resumed

    def delete_scheduled(self, context: AuthContext, job_id: str) -> None:
        """Delete an owned scheduled question."""
        job = self._own_job(context, job_id)
        self._jobs.delete_job(job.id)
        self._record(context, "schedule_deleted", "saved_question",
                     job.saved_question_id or "", {"job_id": job.id})

    def list_runs(self, context: AuthContext, job_id: str) -> list[Record]:
        """List the run history of an owned scheduled question."""
        return self._store.list_runs(self._own_job(context, job_id).id)

    def run_now(
        self, context: AuthContext, job_id: str, idempotency_key: str | None
    ) -> Record:
        """Run immediately as the owner (the caller); the same key never runs twice."""
        job = self._own_job(context, job_id)
        if not idempotency_key:
            return self._run_manual(job, context, None)
        # Two requests with one key at the same moment must not both run the query: the second
        # waits, then finds the first one's stored run.
        with _key_lock(f"{job.id}:{idempotency_key}"):
            existing = self._store.find_run_by_key(job.id, idempotency_key)
            if existing is not None:
                if existing.get("status") == "failed":
                    raise Invalid(
                        "The earlier run with this Idempotency-Key failed; use a new key to retry"
                    )
                return existing
            return self._run_manual(job, context, idempotency_key)

    def _run_manual(
        self, job: ScheduledJob, context: AuthContext, idempotency_key: str | None
    ) -> Record:
        """Execute a manual run for the current instant and return its stored run record."""
        slot = self._clock()
        self.execute_scheduled(job, slot, context, trigger="manual", idempotency_key=idempotency_key)
        runs = self._store.list_runs(job.id)
        return next((r for r in runs if r["slot"] == slot), runs[0] if runs else {})

    def execute_scheduled(
        self, job: ScheduledJob, slot: datetime, owner: AuthContext, *, trigger: str = "schedule",
        idempotency_key: str | None = None,
    ) -> Record:
        """Run one slot as ``owner`` (re-loaded live by the caller), store the result under the
        owner's policy context, record history, and raise after recording when it failed."""
        question = self._store.get_saved_question(job.saved_question_id or "")
        if question is None or question["project_id"] != job.project_id:
            raise NotFound("Saved question not found")
        try:
            result = self.run_for(owner, job.project_id, question, use_cache=False)
        except QueryFailed as failure:
            self._store.record_run(job.id, slot, "failed", None, None, failure.failure.code,
                                   trigger, idempotency_key)
            self._store.add_notification(
                owner.user_id, "scheduled_run_failed",
                {"job_id": job.id, "question": question["question"],
                 "code": failure.failure.code, "message": failure.failure.message},
            )
            raise
        self._store.record_run(job.id, slot, "succeeded", result["policy_context"],
                               len(result["rows"]), None, trigger, idempotency_key)
        return {"policy_context": result["policy_context"], "row_count": len(result["rows"])}

