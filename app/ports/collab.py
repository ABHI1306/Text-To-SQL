"""Port for saved questions, dashboards, shares, cached results, run history and settings."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

Record = dict[str, Any]

RESOURCE_TYPES = ("dashboard", "saved_question", "thread")
MAX_RUNS_KEPT = 20


class CollabStorePort(Protocol):
    """Saved questions, dashboards, shares, cached results, run history and notifications.

    Records are plain dicts with string ids. Authorization is the service's job; this port only
    stores and fetches.
    """

    # saved questions
    def create_saved_question(
        self, project_id: str, owner_id: str, question: str, logical_sql: str,
        chart_spec: Record | None,
    ) -> Record:
        """Store a saved question owned by ``owner_id`` and return the new record."""
        ...
    def get_saved_question(self, question_id: str) -> Record | None:
        """Return the saved question, or None when it does not exist."""
        ...
    def list_saved_questions(self, project_id: str, owner_id: str) -> list[Record]:
        """List the saved questions ``owner_id`` owns in a project."""
        ...
    def delete_saved_question(self, question_id: str) -> bool:
        """Delete a saved question; True when one was removed."""
        ...

    # dashboards and tiles
    def create_dashboard(
        self, project_id: str, owner_id: str, title: str, layout: Record
    ) -> Record:
        """Store a dashboard with its ``layout`` and return the new record."""
        ...
    def get_dashboard(self, dashboard_id: str) -> Record | None:
        """Return the dashboard, or None when it does not exist."""
        ...
    def list_dashboards(self, project_id: str, owner_id: str) -> list[Record]:
        """List the dashboards ``owner_id`` owns in a project."""
        ...
    def update_dashboard(
        self, dashboard_id: str, title: str | None, layout: Record | None
    ) -> Record | None:
        """Change the given title and/or layout; None when the dashboard is missing."""
        ...
    def delete_dashboard(self, dashboard_id: str) -> bool:
        """Delete a dashboard; True when one was removed."""
        ...
    def replace_tiles(self, dashboard_id: str, tiles: list[Record]) -> list[Record]:
        """Replace all tiles of a dashboard with ``tiles`` and return the stored tiles."""
        ...
    def list_tiles(self, dashboard_id: str) -> list[Record]:
        """Return a dashboard's tiles."""
        ...

    # shares (read-only grants; role grantees are identified by role name)
    def create_share(
        self, resource_type: str, resource_id: str, grantee_type: str, grantee_id: str,
        granted_by: str,
    ) -> Record:
        """Grant a read-only share of a resource to a user or role and return it."""
        ...
    def get_share(self, share_id: str) -> Record | None:
        """Return the share, or None when it does not exist."""
        ...
    def list_shares(self, resource_type: str, resource_id: str) -> list[Record]:
        """List the shares granted on one resource."""
        ...
    def delete_share(self, share_id: str) -> bool:
        """Revoke a share; True when one was removed."""
        ...
    def has_share(
        self, resource_type: str, resource_id: str, user_id: str, role_name: str
    ) -> bool:
        """Whether the resource is shared with the user directly or through their role."""
        ...
    def list_shared_with(self, user_id: str, role_name: str) -> list[Record]:
        """List the shares that reach the user directly or through their role."""
        ...

    # result cache, keyed by the policy context the result was computed under
    def get_cached(
        self, resource_type: str, resource_id: str, policy_context: str, mdl_version: int,
        now: datetime,
    ) -> Record | None:
        """Return the cached result for this policy context and MDL version, or None."""
        ...
    def put_cached(
        self, resource_type: str, resource_id: str, policy_context: str, mdl_version: int,
        columns: list[str], rows: list[list[Any]], truncated: bool, computed_at: datetime,
        expires_at: datetime,
    ) -> None:
        """Store a computed result for this policy context and MDL version until ``expires_at``."""
        ...
    def invalidate(self, resource_type: str, resource_id: str) -> None:
        """Drop every cached result of one resource."""
        ...

    # scheduled question run history (the last MAX_RUNS_KEPT per job)
    def record_run(
        self, job_id: str, slot: datetime, status: str, policy_context: str | None,
        row_count: int | None, error: str | None, trigger: str = "schedule",
        idempotency_key: str | None = None,
    ) -> None:
        """Record one run of a scheduled job slot, keeping the last ``MAX_RUNS_KEPT``."""
        ...
    def list_runs(self, job_id: str) -> list[Record]:
        """Return a job's recorded runs."""
        ...
    def find_run_by_key(self, job_id: str, idempotency_key: str) -> Record | None:
        """Return the run recorded with this idempotency key, or None."""
        ...

    # notifications
    def add_notification(self, user_id: str, kind: str, payload: Record) -> None:
        """Add an unread notification for a user."""
        ...
    def list_notifications(self, user_id: str, limit: int) -> list[Record]:
        """Return up to ``limit`` of the user's notifications."""
        ...
    def mark_notification_read(self, user_id: str, notification_id: str) -> bool:
        """Mark the user's notification read; False when it is not theirs or missing."""
        ...

    # installation settings
    def get_setting(self, key: str) -> Any | None:
        """Return an installation setting's value, or None when unset."""
        ...
    def set_setting(self, key: str, value: Any, updated_by: str) -> None:
        """Create or replace an installation setting, recording who changed it."""
        ...

    # evaluation runs
    def create_eval_run(
        self, project_id: str, started_by: str, idempotency_key: str
    ) -> tuple[Record, bool]:
        """(run, created); an existing run with the same key is returned with created=False."""
        ...
    def finish_eval_run(
        self, run_id: str, status: str, summary: Record | None, error: str | None
    ) -> None:
        """Close an evaluation run with its final status, summary or error."""
        ...
    def add_eval_result(self, run_id: str, result: Record) -> None:
        """Attach one per-question result to an evaluation run."""
        ...
    def get_eval_run(self, run_id: str) -> Record | None:
        """Return the evaluation run, or None when it does not exist."""
        ...
    def list_eval_results(self, run_id: str) -> list[Record]:
        """Return the per-question results of an evaluation run."""
        ...
    def previous_eval_run(self, project_id: str, before_run_id: str) -> Record | None:
        """Return the project's evaluation run before ``before_run_id``, or None."""
        ...
