"""PostgreSQL store for collaboration records, plus an in-memory twin for tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.ports.collab import MAX_RUNS_KEPT, CollabStorePort, Record


def _clean(row: dict[str, Any] | None) -> Record | None:
    """Convert UUID values in a row to strings (``None`` stays ``None``)."""
    if row is None:
        return None
    return {key: str(value) if isinstance(value, uuid.UUID) else value for key, value in row.items()}


def _clean_all(rows: list[dict[str, Any]]) -> list[Record]:
    """Apply ``_clean`` to every row."""
    return [item for item in (_clean(row) for row in rows) if item is not None]


class PostgresCollabStore:
    """PostgreSQL implementation of the collaboration store (one connection per call)."""
    def __init__(self, dsn: str) -> None:
        """Store the metadata DSN, normalising the SQLAlchemy-style scheme."""
        if not dsn:
            raise ValueError("A metadata database URL is required")
        self._dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)

    def _one(self, sql: str, params: tuple[Any, ...] = ()) -> Record | None:
        """Run a query and return the first row, or ``None``."""
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            return _clean(connection.execute(sql, params).fetchone())

    def _all(self, sql: str, params: tuple[Any, ...] = ()) -> list[Record]:
        """Run a query and return all rows."""
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            return _clean_all(connection.execute(sql, params).fetchall())

    def _run(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        """Run a statement and return the affected row count."""
        with psycopg.connect(self._dsn) as connection:
            return connection.execute(sql, params).rowcount

    # ---------------------------------------------------------------- saved questions
    def create_saved_question(
        self, project_id: str, owner_id: str, question: str, logical_sql: str,
        chart_spec: Record | None,
    ) -> Record:
        """Save a question with its logical SQL and optional chart spec."""
        row = self._one(
            """
            INSERT INTO saved_questions (project_id, owner_id, question, logical_sql, chart_spec)
            VALUES (%s::uuid, %s::uuid, %s, %s, %s) RETURNING *
            """,
            (project_id, owner_id, question, logical_sql,
             Jsonb(chart_spec) if chart_spec is not None else None),
        )
        assert row is not None
        return row

    def get_saved_question(self, question_id: str) -> Record | None:
        """Return one saved question, or ``None``."""
        return self._one("SELECT * FROM saved_questions WHERE id = %s::uuid", (question_id,))

    def list_saved_questions(self, project_id: str, owner_id: str) -> list[Record]:
        """List the owner's saved questions in a project, newest first."""
        return self._all(
            "SELECT * FROM saved_questions WHERE project_id = %s::uuid AND owner_id = %s::uuid"
            " ORDER BY created_at DESC",
            (project_id, owner_id),
        )

    def delete_saved_question(self, question_id: str) -> bool:
        """Delete a saved question and the shares that point at it."""
        with psycopg.connect(self._dsn) as connection:
            connection.execute(
                "DELETE FROM shares WHERE resource_type = 'saved_question' AND resource_id = %s::uuid",
                (question_id,),
            )
            return connection.execute(
                "DELETE FROM saved_questions WHERE id = %s::uuid", (question_id,)
            ).rowcount == 1

    # ---------------------------------------------------------------- dashboards
    def create_dashboard(
        self, project_id: str, owner_id: str, title: str, layout: Record
    ) -> Record:
        """Create a dashboard with its layout."""
        row = self._one(
            "INSERT INTO dashboards (project_id, owner_id, title, layout_json)"
            " VALUES (%s::uuid, %s::uuid, %s, %s) RETURNING *",
            (project_id, owner_id, title, Jsonb(layout)),
        )
        assert row is not None
        return row

    def get_dashboard(self, dashboard_id: str) -> Record | None:
        """Return one dashboard, or ``None``."""
        return self._one("SELECT * FROM dashboards WHERE id = %s::uuid", (dashboard_id,))

    def list_dashboards(self, project_id: str, owner_id: str) -> list[Record]:
        """List the owner's dashboards in a project, newest first."""
        return self._all(
            "SELECT * FROM dashboards WHERE project_id = %s::uuid AND owner_id = %s::uuid"
            " ORDER BY created_at DESC",
            (project_id, owner_id),
        )

    def update_dashboard(
        self, dashboard_id: str, title: str | None, layout: Record | None
    ) -> Record | None:
        """Change the title and/or layout; ``None`` leaves a field as it is."""
        return self._one(
            """
            UPDATE dashboards SET title = COALESCE(%s, title),
                                  layout_json = COALESCE(%s, layout_json)
            WHERE id = %s::uuid RETURNING *
            """,
            (title, Jsonb(layout) if layout is not None else None, dashboard_id),
        )

    def delete_dashboard(self, dashboard_id: str) -> bool:
        """Delete a dashboard, its tiles and the shares that point at it."""
        with psycopg.connect(self._dsn) as connection:
            connection.execute(
                "DELETE FROM shares WHERE resource_type = 'dashboard' AND resource_id = %s::uuid",
                (dashboard_id,),
            )
            return connection.execute(
                "DELETE FROM dashboards WHERE id = %s::uuid", (dashboard_id,)
            ).rowcount == 1

    def replace_tiles(self, dashboard_id: str, tiles: list[Record]) -> list[Record]:
        """Replace all tiles of a dashboard, keeping the given order."""
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            connection.execute(
                "DELETE FROM dashboard_tiles WHERE dashboard_id = %s::uuid", (dashboard_id,)
            )
            saved: list[Record] = []
            for order, tile in enumerate(tiles):
                row = connection.execute(
                    """
                    INSERT INTO dashboard_tiles
                        (dashboard_id, saved_question_id, chart_spec, position_json,
                         refresh_schedule, sort_order)
                    VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s) RETURNING *
                    """,
                    (dashboard_id, tile["saved_question_id"],
                     Jsonb(tile["chart_spec"]) if tile.get("chart_spec") is not None else None,
                     Jsonb(tile.get("position") or {}), tile.get("refresh_schedule"), order),
                ).fetchone()
                cleaned = _clean(row)
                assert cleaned is not None
                saved.append(cleaned)
        return saved

    def list_tiles(self, dashboard_id: str) -> list[Record]:
        """List a dashboard's tiles in display order."""
        return self._all(
            "SELECT * FROM dashboard_tiles WHERE dashboard_id = %s::uuid ORDER BY sort_order",
            (dashboard_id,),
        )

    # ---------------------------------------------------------------- shares
    def create_share(
        self, resource_type: str, resource_id: str, grantee_type: str, grantee_id: str,
        granted_by: str,
    ) -> Record:
        """Grant a user or role view access; granting twice returns the existing share."""
        row = self._one(
            """
            INSERT INTO shares (resource_type, resource_id, grantee_type, grantee_id, granted_by)
            VALUES (%s, %s::uuid, %s, %s, %s::uuid)
            ON CONFLICT (resource_type, resource_id, grantee_type, grantee_id)
                DO UPDATE SET granted_by = EXCLUDED.granted_by
            RETURNING *
            """,
            (resource_type, resource_id, grantee_type, grantee_id, granted_by),
        )
        assert row is not None
        return row

    def get_share(self, share_id: str) -> Record | None:
        """Return one share, or ``None``."""
        return self._one("SELECT * FROM shares WHERE id = %s::uuid", (share_id,))

    def list_shares(self, resource_type: str, resource_id: str) -> list[Record]:
        """List the shares on one resource."""
        return self._all(
            "SELECT * FROM shares WHERE resource_type = %s AND resource_id = %s::uuid"
            " ORDER BY granted_at",
            (resource_type, resource_id),
        )

    def delete_share(self, share_id: str) -> bool:
        """Revoke a share; true if one was removed."""
        return self._run("DELETE FROM shares WHERE id = %s::uuid", (share_id,)) == 1

    def has_share(
        self, resource_type: str, resource_id: str, user_id: str, role_name: str
    ) -> bool:
        """Whether the user, or their role, has been granted the resource."""
        return self._one(
            """
            SELECT 1 AS found FROM shares
            WHERE resource_type = %s AND resource_id = %s::uuid
              AND ((grantee_type = 'user' AND grantee_id = %s)
                   OR (grantee_type = 'role' AND grantee_id = %s))
            LIMIT 1
            """,
            (resource_type, resource_id, user_id, role_name),
        ) is not None

    def list_shared_with(self, user_id: str, role_name: str) -> list[Record]:
        """List resources shared with the user or role, one row per resource."""
        return self._all(
            """
            SELECT DISTINCT ON (resource_type, resource_id) *
            FROM shares
            WHERE (grantee_type = 'user' AND grantee_id = %s)
               OR (grantee_type = 'role' AND grantee_id = %s)
            ORDER BY resource_type, resource_id, granted_at
            """,
            (user_id, role_name),
        )

    # ---------------------------------------------------------------- result cache
    def get_cached(
        self, resource_type: str, resource_id: str, policy_context: str, mdl_version: int,
        now: datetime,
    ) -> Record | None:
        """Return an unexpired cached result for the same policy context and MDL version."""
        return self._one(
            """
            SELECT columns, rows, truncated, computed_at FROM result_cache
            WHERE resource_type = %s AND resource_id = %s::uuid AND policy_context = %s
              AND mdl_version = %s AND expires_at > %s
            """,
            (resource_type, resource_id, policy_context, mdl_version, now),
        )

    def put_cached(
        self, resource_type: str, resource_id: str, policy_context: str, mdl_version: int,
        columns: list[str], rows: list[list[Any]], truncated: bool, computed_at: datetime,
        expires_at: datetime,
    ) -> None:
        """Store a result, replacing any entry for the same context and MDL version."""
        self._run(
            """
            INSERT INTO result_cache (resource_type, resource_id, policy_context, mdl_version,
                                      columns, rows, truncated, computed_at, expires_at)
            VALUES (%s, %s::uuid, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (resource_type, resource_id, policy_context, mdl_version) DO UPDATE
            SET columns = EXCLUDED.columns, rows = EXCLUDED.rows, truncated = EXCLUDED.truncated,
                computed_at = EXCLUDED.computed_at, expires_at = EXCLUDED.expires_at
            """,
            (resource_type, resource_id, policy_context, mdl_version, Jsonb(columns), Jsonb(rows),
             truncated, computed_at, expires_at),
        )

    def invalidate(self, resource_type: str, resource_id: str) -> None:
        """Drop every cached result for a resource."""
        self._run(
            "DELETE FROM result_cache WHERE resource_type = %s AND resource_id = %s::uuid",
            (resource_type, resource_id),
        )

    # ---------------------------------------------------------------- run history
    def record_run(
        self, job_id: str, slot: datetime, status: str, policy_context: str | None,
        row_count: int | None, error: str | None, trigger: str = "schedule",
        idempotency_key: str | None = None,
    ) -> None:
        """Record a run once per slot or idempotency key and keep only the latest runs."""
        with psycopg.connect(self._dsn) as connection:
            connection.execute(
                """
                INSERT INTO question_runs (job_id, slot, finished_at, status, policy_context,
                                           row_count, error, trigger, idempotency_key)
                VALUES (%s::uuid, %s, now(), %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (job_id, slot, status, policy_context, row_count, error, trigger, idempotency_key),
            )
            connection.execute(
                """
                DELETE FROM question_runs WHERE job_id = %s::uuid AND id NOT IN (
                    SELECT id FROM question_runs WHERE job_id = %s::uuid
                    ORDER BY started_at DESC LIMIT %s)
                """,
                (job_id, job_id, MAX_RUNS_KEPT),
            )

    def find_run_by_key(self, job_id: str, idempotency_key: str) -> Record | None:
        """Find a run by its idempotency key."""
        return self._one(
            "SELECT * FROM question_runs WHERE job_id = %s::uuid AND idempotency_key = %s",
            (job_id, idempotency_key),
        )

    def list_runs(self, job_id: str) -> list[Record]:
        """List a job's recent runs, newest first."""
        return self._all(
            "SELECT * FROM question_runs WHERE job_id = %s::uuid ORDER BY started_at DESC LIMIT %s",
            (job_id, MAX_RUNS_KEPT),
        )

    # ---------------------------------------------------------------- notifications
    def add_notification(self, user_id: str, kind: str, payload: Record) -> None:
        """Add an unread notification for a user."""
        self._run(
            "INSERT INTO notifications (user_id, kind, payload) VALUES (%s::uuid, %s, %s)",
            (user_id, kind, Jsonb(payload)),
        )

    def list_notifications(self, user_id: str, limit: int) -> list[Record]:
        """List a user's notifications, newest first."""
        return self._all(
            "SELECT * FROM notifications WHERE user_id = %s::uuid ORDER BY created_at DESC LIMIT %s",
            (user_id, limit),
        )

    def mark_notification_read(self, user_id: str, notification_id: str) -> bool:
        """Mark the user's own unread notification read; true if it changed."""
        return self._run(
            "UPDATE notifications SET read_at = now()"
            " WHERE id = %s::uuid AND user_id = %s::uuid AND read_at IS NULL",
            (notification_id, user_id),
        ) == 1

    # ---------------------------------------------------------------- settings
    def get_setting(self, key: str) -> Any | None:
        """Return an app setting value, or ``None``."""
        row = self._one("SELECT value_json FROM app_settings WHERE key = %s", (key,))
        return row["value_json"] if row else None

    def set_setting(self, key: str, value: Any, updated_by: str) -> None:
        """Create or update an app setting."""
        self._run(
            """
            INSERT INTO app_settings (key, value_json, updated_by) VALUES (%s, %s, %s::uuid)
            ON CONFLICT (key) DO UPDATE
            SET value_json = EXCLUDED.value_json, updated_by = EXCLUDED.updated_by,
                updated_at = now()
            """,
            (key, Jsonb(value), updated_by),
        )

    # ---------------------------------------------------------------- evaluation
    def create_eval_run(
        self, project_id: str, started_by: str, idempotency_key: str
    ) -> tuple[Record, bool]:
        """Start an eval run; a repeated idempotency key returns the existing run and False."""
        row = self._one(
            """
            INSERT INTO eval_runs (project_id, started_by, idempotency_key)
            VALUES (%s::uuid, %s::uuid, %s)
            ON CONFLICT (project_id, idempotency_key) DO NOTHING RETURNING *
            """,
            (project_id, started_by, idempotency_key),
        )
        if row is not None:
            return row, True
        existing = self._one(
            "SELECT * FROM eval_runs WHERE project_id = %s::uuid AND idempotency_key = %s",
            (project_id, idempotency_key),
        )
        assert existing is not None
        return existing, False

    def finish_eval_run(
        self, run_id: str, status: str, summary: Record | None, error: str | None
    ) -> None:
        """Record the final status, summary and error of an eval run."""
        self._run(
            "UPDATE eval_runs SET status = %s, summary = %s, error = %s, finished_at = now()"
            " WHERE id = %s::uuid",
            (status, Jsonb(summary) if summary is not None else None, error, run_id),
        )

    def add_eval_result(self, run_id: str, result: Record) -> None:
        """Add one case result to a run (duplicates are ignored)."""
        self._run(
            """
            INSERT INTO eval_results (run_id, case_id, suite, question, generated_sql, outcome,
                                      stage, exec_match, failure_code, latency_ms, gpu_ms)
            VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id, suite, case_id) DO NOTHING
            """,
            (run_id, result["case_id"], result["suite"], result["question"],
             result.get("generated_sql"), result["outcome"], result.get("stage"),
             result.get("exec_match"), result.get("failure_code"), result.get("latency_ms"),
             result.get("gpu_ms")),
        )

    def get_eval_run(self, run_id: str) -> Record | None:
        """Return one eval run, or ``None``."""
        return self._one("SELECT * FROM eval_runs WHERE id = %s::uuid", (run_id,))

    def list_eval_results(self, run_id: str) -> list[Record]:
        """List a run's case results ordered by suite and case."""
        return self._all(
            "SELECT * FROM eval_results WHERE run_id = %s::uuid ORDER BY suite, case_id", (run_id,)
        )

    def previous_eval_run(self, project_id: str, before_run_id: str) -> Record | None:
        """Return the latest completed run of the project created before the given run."""
        return self._one(
            """
            SELECT * FROM eval_runs
            WHERE project_id = %s::uuid AND status = 'completed' AND id <> %s::uuid
              AND created_at < (SELECT created_at FROM eval_runs WHERE id = %s::uuid)
            ORDER BY created_at DESC LIMIT 1
            """,
            (project_id, before_run_id, before_run_id),
        )


class InMemoryCollabStore:
    """Dict-backed twin with the same observable behaviour, for unit tests."""

    def __init__(self) -> None:
        """Create empty dicts for every record kind."""
        self.saved: dict[str, Record] = {}
        self.dashboards: dict[str, Record] = {}
        self.tiles: dict[str, list[Record]] = {}
        self.shares: dict[str, Record] = {}
        self.cache: dict[tuple[str, str, str, int], Record] = {}
        self.runs: dict[str, list[Record]] = {}
        self.notifications: list[Record] = []
        self.settings: dict[str, Any] = {}
        self.eval_runs: dict[str, Record] = {}
        self.eval_results: dict[str, list[Record]] = {}

    @staticmethod
    def _id() -> str:
        """Return a new UUID string."""
        return str(uuid.uuid4())

    @staticmethod
    def _now() -> datetime:
        """Return the current UTC time."""
        return datetime.now(UTC)

    def create_saved_question(
        self, project_id: str, owner_id: str, question: str, logical_sql: str,
        chart_spec: Record | None,
    ) -> Record:
        """Save a question with its logical SQL and optional chart spec."""
        record: Record = {
            "id": self._id(), "project_id": project_id, "owner_id": owner_id,
            "question": question, "logical_sql": logical_sql, "chart_spec": chart_spec,
            "created_at": self._now(),
        }
        self.saved[record["id"]] = record
        return dict(record)

    def get_saved_question(self, question_id: str) -> Record | None:
        """Return one saved question, or ``None``."""
        record = self.saved.get(question_id)
        return dict(record) if record else None

    def list_saved_questions(self, project_id: str, owner_id: str) -> list[Record]:
        """List the owner's saved questions in a project, newest first."""
        return [dict(r) for r in self.saved.values()
                if r["project_id"] == project_id and r["owner_id"] == owner_id]

    def delete_saved_question(self, question_id: str) -> bool:
        """Delete a saved question and the shares that point at it."""
        self.shares = {k: v for k, v in self.shares.items()
                       if not (v["resource_type"] == "saved_question"
                               and v["resource_id"] == question_id)}
        return self.saved.pop(question_id, None) is not None

    def create_dashboard(
        self, project_id: str, owner_id: str, title: str, layout: Record
    ) -> Record:
        """Create a dashboard with its layout."""
        record: Record = {
            "id": self._id(), "project_id": project_id, "owner_id": owner_id, "title": title,
            "layout_json": layout, "created_at": self._now(),
        }
        self.dashboards[record["id"]] = record
        self.tiles[record["id"]] = []
        return dict(record)

    def get_dashboard(self, dashboard_id: str) -> Record | None:
        """Return one dashboard, or ``None``."""
        record = self.dashboards.get(dashboard_id)
        return dict(record) if record else None

    def list_dashboards(self, project_id: str, owner_id: str) -> list[Record]:
        """List the owner's dashboards in a project, newest first."""
        return [dict(r) for r in self.dashboards.values()
                if r["project_id"] == project_id and r["owner_id"] == owner_id]

    def update_dashboard(
        self, dashboard_id: str, title: str | None, layout: Record | None
    ) -> Record | None:
        """Change the title and/or layout; ``None`` leaves a field as it is."""
        record = self.dashboards.get(dashboard_id)
        if record is None:
            return None
        if title is not None:
            record["title"] = title
        if layout is not None:
            record["layout_json"] = layout
        return dict(record)

    def delete_dashboard(self, dashboard_id: str) -> bool:
        """Delete a dashboard, its tiles and the shares that point at it."""
        self.tiles.pop(dashboard_id, None)
        self.shares = {k: v for k, v in self.shares.items()
                       if not (v["resource_type"] == "dashboard" and v["resource_id"] == dashboard_id)}
        return self.dashboards.pop(dashboard_id, None) is not None

    def replace_tiles(self, dashboard_id: str, tiles: list[Record]) -> list[Record]:
        """Replace all tiles of a dashboard, keeping the given order."""
        self.tiles[dashboard_id] = [
            {
                "id": self._id(), "dashboard_id": dashboard_id,
                "saved_question_id": tile["saved_question_id"],
                "chart_spec": tile.get("chart_spec"), "position_json": tile.get("position") or {},
                "refresh_schedule": tile.get("refresh_schedule"), "sort_order": order,
            }
            for order, tile in enumerate(tiles)
        ]
        return [dict(t) for t in self.tiles[dashboard_id]]

    def list_tiles(self, dashboard_id: str) -> list[Record]:
        """List a dashboard's tiles in display order."""
        return [dict(t) for t in self.tiles.get(dashboard_id, [])]

    def create_share(
        self, resource_type: str, resource_id: str, grantee_type: str, grantee_id: str,
        granted_by: str,
    ) -> Record:
        """Grant a user or role view access; granting twice returns the existing share."""
        for share in self.shares.values():
            if (share["resource_type"], share["resource_id"], share["grantee_type"],
                    share["grantee_id"]) == (resource_type, resource_id, grantee_type, grantee_id):
                return dict(share)
        record: Record = {
            "id": self._id(), "resource_type": resource_type, "resource_id": resource_id,
            "grantee_type": grantee_type, "grantee_id": grantee_id, "permission": "view",
            "granted_by": granted_by, "granted_at": self._now(),
        }
        self.shares[record["id"]] = record
        return dict(record)

    def get_share(self, share_id: str) -> Record | None:
        """Return one share, or ``None``."""
        share = self.shares.get(share_id)
        return dict(share) if share else None

    def list_shares(self, resource_type: str, resource_id: str) -> list[Record]:
        """List the shares on one resource."""
        return [dict(s) for s in self.shares.values()
                if s["resource_type"] == resource_type and s["resource_id"] == resource_id]

    def delete_share(self, share_id: str) -> bool:
        """Revoke a share; true if one was removed."""
        return self.shares.pop(share_id, None) is not None

    def has_share(
        self, resource_type: str, resource_id: str, user_id: str, role_name: str
    ) -> bool:
        """Whether the user, or their role, has been granted the resource."""
        return any(
            s["resource_type"] == resource_type and s["resource_id"] == resource_id
            and ((s["grantee_type"] == "user" and s["grantee_id"] == user_id)
                 or (s["grantee_type"] == "role" and s["grantee_id"] == role_name))
            for s in self.shares.values()
        )

    def list_shared_with(self, user_id: str, role_name: str) -> list[Record]:
        """List resources shared with the user or role, one row per resource."""
        seen: set[tuple[str, str]] = set()
        out: list[Record] = []
        for s in self.shares.values():
            key = (s["resource_type"], s["resource_id"])
            if key in seen:
                continue
            if self.has_share(s["resource_type"], s["resource_id"], user_id, role_name):
                seen.add(key)
                out.append(dict(s))
        return out

    def get_cached(
        self, resource_type: str, resource_id: str, policy_context: str, mdl_version: int,
        now: datetime,
    ) -> Record | None:
        """Return an unexpired cached result for the same policy context and MDL version."""
        hit = self.cache.get((resource_type, resource_id, policy_context, mdl_version))
        if hit is None or hit["expires_at"] <= now:
            return None
        return {k: hit[k] for k in ("columns", "rows", "truncated", "computed_at")}

    def put_cached(
        self, resource_type: str, resource_id: str, policy_context: str, mdl_version: int,
        columns: list[str], rows: list[list[Any]], truncated: bool, computed_at: datetime,
        expires_at: datetime,
    ) -> None:
        """Store a result, replacing any entry for the same context and MDL version."""
        self.cache[(resource_type, resource_id, policy_context, mdl_version)] = {
            "columns": columns, "rows": rows, "truncated": truncated,
            "computed_at": computed_at, "expires_at": expires_at,
        }

    def invalidate(self, resource_type: str, resource_id: str) -> None:
        """Drop every cached result for a resource."""
        self.cache = {k: v for k, v in self.cache.items()
                      if not (k[0] == resource_type and k[1] == resource_id)}

    def record_run(
        self, job_id: str, slot: datetime, status: str, policy_context: str | None,
        row_count: int | None, error: str | None, trigger: str = "schedule",
        idempotency_key: str | None = None,
    ) -> None:
        """Record a run once per slot or idempotency key and keep only the latest runs."""
        runs = self.runs.setdefault(job_id, [])
        if any(r["slot"] == slot for r in runs):
            return
        if idempotency_key and any(r["idempotency_key"] == idempotency_key for r in runs):
            return
        runs.insert(0, {
            "id": self._id(), "job_id": job_id, "slot": slot, "started_at": self._now(),
            "status": status, "policy_context": policy_context, "row_count": row_count,
            "error": error, "trigger": trigger, "idempotency_key": idempotency_key,
        })
        del runs[MAX_RUNS_KEPT:]

    def find_run_by_key(self, job_id: str, idempotency_key: str) -> Record | None:
        """Find a run by its idempotency key."""
        return next(
            (dict(r) for r in self.runs.get(job_id, []) if r["idempotency_key"] == idempotency_key),
            None,
        )

    def list_runs(self, job_id: str) -> list[Record]:
        """List a job's recent runs, newest first."""
        return [dict(r) for r in self.runs.get(job_id, [])]

    def add_notification(self, user_id: str, kind: str, payload: Record) -> None:
        """Add an unread notification for a user."""
        self.notifications.insert(0, {
            "id": self._id(), "user_id": user_id, "kind": kind, "payload": payload,
            "created_at": self._now(), "read_at": None,
        })

    def list_notifications(self, user_id: str, limit: int) -> list[Record]:
        """List a user's notifications, newest first."""
        return [dict(n) for n in self.notifications if n["user_id"] == user_id][:limit]

    def mark_notification_read(self, user_id: str, notification_id: str) -> bool:
        """Mark the user's own unread notification read; true if it changed."""
        for n in self.notifications:
            if n["id"] == notification_id and n["user_id"] == user_id and n["read_at"] is None:
                n["read_at"] = self._now()
                return True
        return False

    def get_setting(self, key: str) -> Any | None:
        """Return an app setting value, or ``None``."""
        return self.settings.get(key)

    def set_setting(self, key: str, value: Any, updated_by: str) -> None:
        """Create or update an app setting."""
        self.settings[key] = value

    def create_eval_run(
        self, project_id: str, started_by: str, idempotency_key: str
    ) -> tuple[Record, bool]:
        """Start an eval run; a repeated idempotency key returns the existing run and False."""
        for run in self.eval_runs.values():
            if run["project_id"] == project_id and run["idempotency_key"] == idempotency_key:
                return dict(run), False
        run = {
            "id": self._id(), "project_id": project_id, "started_by": started_by,
            "idempotency_key": idempotency_key, "status": "running", "summary": None,
            "error": None, "created_at": self._now(), "finished_at": None,
        }
        self.eval_runs[run["id"]] = run
        self.eval_results[run["id"]] = []
        return dict(run), True

    def finish_eval_run(
        self, run_id: str, status: str, summary: Record | None, error: str | None
    ) -> None:
        """Record the final status, summary and error of an eval run."""
        self.eval_runs[run_id].update(
            status=status, summary=summary, error=error, finished_at=self._now()
        )

    def add_eval_result(self, run_id: str, result: Record) -> None:
        """Add one case result to a run (duplicates are ignored)."""
        self.eval_results[run_id].append(dict(result))

    def get_eval_run(self, run_id: str) -> Record | None:
        """Return one eval run, or ``None``."""
        run = self.eval_runs.get(run_id)
        return dict(run) if run else None

    def list_eval_results(self, run_id: str) -> list[Record]:
        """List a run's case results ordered by suite and case."""
        return [dict(r) for r in self.eval_results.get(run_id, [])]

    def previous_eval_run(self, project_id: str, before_run_id: str) -> Record | None:
        """Return the latest completed run of the project created before the given run."""
        current = self.eval_runs[before_run_id]
        earlier = [
            r for r in self.eval_runs.values()
            if r["project_id"] == project_id and r["status"] == "completed"
            and r["created_at"] < current["created_at"]
        ]
        return dict(max(earlier, key=lambda r: r["created_at"])) if earlier else None


_check: type[CollabStorePort] = InMemoryCollabStore
_check_pg: type[CollabStorePort] = PostgresCollabStore
