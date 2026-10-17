"""Scheduled jobs and per-slot leases in the metadata database."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import TypeAlias, cast

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.ports.scheduler import ScheduledJob

Row: TypeAlias = dict[str, object]

_COLUMNS = (
    "id, project_id, owner_id, kind, schedule, timezone, status, paused_reason, "
    "next_run_at, last_run_at, last_status, saved_question_id"
)


class PostgresJobStore:
    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ValueError("A metadata database URL is required")
        self._dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)

    def _connect(self) -> psycopg.Connection[Row]:
        return psycopg.connect(self._dsn, row_factory=dict_row)

    def list_due(self, now: datetime, limit: int) -> list[ScheduledJob]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT {_COLUMNS} FROM scheduled_jobs
                WHERE status = 'active' AND next_run_at <= %s
                ORDER BY next_run_at LIMIT %s
                """,
                (now, limit),
            )
            return [_job(row) for row in cursor.fetchall()]

    def list_stale(self, now: datetime, limit: int) -> list[tuple[ScheduledJob, datetime]]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT j.id, j.project_id, j.owner_id, j.kind, j.schedule, j.timezone, j.status,
                       j.paused_reason, j.next_run_at, j.last_run_at, j.last_status, j.saved_question_id,
                       l.slot
                FROM job_leases l JOIN scheduled_jobs j ON j.id = l.job_id
                WHERE l.status = 'running' AND l.leased_until < %s AND j.status = 'active'
                ORDER BY l.leased_until LIMIT %s
                """,
                (now, limit),
            )
            return [(_job(row), cast(datetime, row["slot"])) for row in cursor.fetchall()]

    def claim(
        self,
        job: ScheduledJob,
        slot: datetime,
        worker_id: str,
        lease_seconds: int,
        advance_to: datetime | None,
    ) -> int | None:
        with self._connect() as connection, connection.cursor() as cursor:
            if advance_to is not None:
                # The row lock serialises racing workers: the loser finds next_run_at already
                # moved (or the job paused) and claims nothing.
                cursor.execute(
                    """
                    UPDATE scheduled_jobs SET next_run_at = %s, updated_at = now()
                    WHERE id = %s::uuid AND status = 'active' AND next_run_at = %s
                    """,
                    (advance_to, job.id, slot),
                )
                if cursor.rowcount != 1:
                    return None
            # The (job_id, slot) primary key is the second, independent guard. An expired
            # 'running' lease (its worker died) may be taken over; nothing else can.
            cursor.execute(
                """
                INSERT INTO job_leases (job_id, slot, worker_id, leased_until)
                VALUES (%s::uuid, %s, %s, now() + make_interval(secs => %s))
                ON CONFLICT (job_id, slot) DO UPDATE
                SET worker_id = EXCLUDED.worker_id,
                    leased_until = EXCLUDED.leased_until,
                    attempts = job_leases.attempts + 1,
                    started_at = now()
                WHERE job_leases.status = 'running' AND job_leases.leased_until < now()
                  AND %s::boolean
                RETURNING attempts
                """,
                (job.id, slot, worker_id, lease_seconds, advance_to is None),
            )
            row = cursor.fetchone()
            if row is None:
                connection.rollback()  # also undoes the next_run_at move
                return None
            return int(cast(int, row["attempts"]))

    def finish(
        self,
        job_id: str,
        slot: datetime,
        worker_id: str,
        status: str,
        result: Mapping[str, object] | None,
        error: str | None,
    ) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE job_leases
                SET status = %s, result = %s, error = %s, finished_at = now()
                WHERE job_id = %s::uuid AND slot = %s AND worker_id = %s AND status = 'running'
                """,
                (status, Jsonb(dict(result)) if result is not None else None, error,
                 job_id, slot, worker_id),
            )
            if cursor.rowcount == 1:
                cursor.execute(
                    """
                    UPDATE scheduled_jobs SET last_run_at = now(), last_status = %s
                    WHERE id = %s::uuid
                    """,
                    (status, job_id),
                )

    def pause(self, job_id: str, reason: str) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE scheduled_jobs
                SET status = 'paused', paused_reason = %s, updated_at = now()
                WHERE id = %s::uuid
                """,
                (reason, job_id),
            )

    def get(self, job_id: str) -> ScheduledJob | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {_COLUMNS} FROM scheduled_jobs WHERE id = %s::uuid", (job_id,)
            )
            row = cursor.fetchone()
        return _job(row) if row else None

    def get_for_project(self, project_id: str, kind: str) -> ScheduledJob | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {_COLUMNS} FROM scheduled_jobs WHERE project_id = %s::uuid AND kind = %s",
                (project_id, kind),
            )
            row = cursor.fetchone()
        return _job(row) if row else None

    def upsert_for_project(
        self,
        project_id: str,
        owner_id: str,
        kind: str,
        schedule: str,
        timezone: str,
        next_run_at: datetime,
    ) -> ScheduledJob:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO scheduled_jobs
                    (project_id, owner_id, kind, schedule, timezone, next_run_at)
                VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s)
                ON CONFLICT (project_id, kind) WHERE kind = 'profile_drift' DO UPDATE
                SET owner_id = EXCLUDED.owner_id, schedule = EXCLUDED.schedule,
                    timezone = EXCLUDED.timezone, next_run_at = EXCLUDED.next_run_at,
                    status = 'active', paused_reason = NULL, updated_at = now()
                RETURNING {_COLUMNS}
                """,
                (project_id, owner_id, kind, schedule, timezone, next_run_at),
            )
            row = cursor.fetchone()
        assert row is not None
        return _job(row)

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
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO scheduled_jobs
                    (project_id, owner_id, kind, schedule, timezone, next_run_at, saved_question_id)
                VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s, %s::uuid)
                RETURNING {_COLUMNS}
                """,
                (project_id, owner_id, kind, schedule, timezone, next_run_at, saved_question_id),
            )
            row = cursor.fetchone()
        assert row is not None
        return _job(row)

    def list_jobs(
        self, kind: str, *, owner_id: str | None = None, project_id: str | None = None
    ) -> list[ScheduledJob]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT {_COLUMNS} FROM scheduled_jobs
                WHERE kind = %s AND (%s::uuid IS NULL OR owner_id = %s::uuid)
                  AND (%s::uuid IS NULL OR project_id = %s::uuid)
                ORDER BY created_at
                """,
                (kind, owner_id, owner_id, project_id, project_id),
            )
            return [_job(row) for row in cursor.fetchall()]

    def update_job(
        self, job_id: str, schedule: str, timezone: str, next_run_at: datetime
    ) -> ScheduledJob | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE scheduled_jobs
                SET schedule = %s, timezone = %s, next_run_at = %s, status = 'active',
                    paused_reason = NULL, updated_at = now()
                WHERE id = %s::uuid RETURNING {_COLUMNS}
                """,
                (schedule, timezone, next_run_at, job_id),
            )
            row = cursor.fetchone()
        return _job(row) if row else None

    def resume(self, job_id: str, next_run_at: datetime) -> ScheduledJob | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE scheduled_jobs
                SET status = 'active', paused_reason = NULL, next_run_at = %s, updated_at = now()
                WHERE id = %s::uuid RETURNING {_COLUMNS}
                """,
                (next_run_at, job_id),
            )
            row = cursor.fetchone()
        return _job(row) if row else None

    def delete_job(self, job_id: str) -> bool:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM scheduled_jobs WHERE id = %s::uuid", (job_id,))
            return cursor.rowcount == 1

    def delete_for_project(self, project_id: str, kind: str) -> bool:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM scheduled_jobs WHERE project_id = %s::uuid AND kind = %s",
                (project_id, kind),
            )
            return cursor.rowcount == 1

    def health(self, now: datetime, *, grace_seconds: int) -> Mapping[str, object]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                  count(*) FILTER (WHERE status = 'active') AS active,
                  count(*) FILTER (WHERE status = 'paused') AS paused,
                  count(*) FILTER (WHERE status = 'active'
                                   AND next_run_at < %s - make_interval(secs => %s)) AS overdue,
                  count(*) FILTER (WHERE last_status = 'failed') AS last_failed,
                  min(next_run_at) FILTER (WHERE status = 'active') AS next_slot,
                  max(last_run_at) AS last_run_at
                FROM scheduled_jobs
                """,
                (now, grace_seconds),
            )
            jobs = cursor.fetchone() or {}
            cursor.execute(
                """
                SELECT count(*) FILTER (WHERE status = 'running' AND leased_until >= %s) AS running,
                       count(*) FILTER (WHERE status = 'running' AND leased_until < %s) AS stale,
                       count(*) FILTER (WHERE status = 'failed'
                                        AND started_at > %s - interval '24 hours') AS failed_24h,
                       count(*) FILTER (WHERE status = 'succeeded'
                                        AND started_at > %s - interval '24 hours') AS succeeded_24h
                FROM job_leases
                """,
                (now, now, now, now),
            )
            leases = cursor.fetchone() or {}
        return {**jobs, **leases}


def _job(row: Row) -> ScheduledJob:
    return ScheduledJob(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        owner_id=str(row["owner_id"]) if row["owner_id"] is not None else None,
        kind=str(row["kind"]),
        schedule=str(row["schedule"]),
        timezone=str(row["timezone"]),
        status=str(row["status"]),
        next_run_at=cast(datetime, row["next_run_at"]),
        paused_reason=cast("str | None", row["paused_reason"]),
        last_run_at=cast("datetime | None", row["last_run_at"]),
        last_status=cast("str | None", row["last_status"]),
        saved_question_id=(
            str(row["saved_question_id"]) if row["saved_question_id"] is not None else None
        ),
    )
