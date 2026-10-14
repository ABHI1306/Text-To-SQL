"""Drift reports in the metadata database."""

from __future__ import annotations

from datetime import datetime
from typing import TypeAlias, cast

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.ports.drift import StoredDriftReport

Row: TypeAlias = dict[str, object]

_COLUMNS = (
    "id, project_id, mdl_version, job_id, has_drift, summary, findings, "
    "draft_mdl_version, created_by, created_at"
)


class PostgresDriftRepository:
    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ValueError("A metadata database URL is required")
        self._dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)

    def _connect(self) -> psycopg.Connection[Row]:
        return psycopg.connect(self._dsn, row_factory=dict_row)

    def save_report(
        self,
        *,
        project_id: str,
        mdl_version: int,
        job_id: str | None,
        slot: datetime | None,
        has_drift: bool,
        summary: dict[str, int],
        findings: list[dict[str, object]],
        created_by: str | None,
    ) -> str:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO drift_reports
                    (project_id, mdl_version, job_id, slot, has_drift, summary, findings,
                     created_by)
                VALUES (%s::uuid, %s, %s::uuid, %s, %s, %s, %s, %s::uuid)
                ON CONFLICT (job_id, slot) WHERE job_id IS NOT NULL DO NOTHING
                RETURNING id
                """,
                (project_id, mdl_version, job_id, slot, has_drift, Jsonb(summary),
                 Jsonb(findings), created_by),
            )
            row = cursor.fetchone()
            if row is None:  # the slot was already reported (a retaken slot)
                cursor.execute(
                    "SELECT id FROM drift_reports WHERE job_id = %s::uuid AND slot = %s",
                    (job_id, slot),
                )
                row = cursor.fetchone()
        assert row is not None
        return str(row["id"])

    def list_reports(
        self, project_id: str, *, limit: int, before: datetime | None = None
    ) -> list[StoredDriftReport]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT {_COLUMNS} FROM drift_reports
                WHERE project_id = %s::uuid AND (%s::timestamptz IS NULL OR created_at < %s)
                ORDER BY created_at DESC, id DESC LIMIT %s
                """,
                (project_id, before, before, limit),
            )
            return [_report(row) for row in cursor.fetchall()]

    def get_report(self, project_id: str, report_id: str) -> StoredDriftReport | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {_COLUMNS} FROM drift_reports WHERE id = %s::uuid AND project_id = %s::uuid",
                (report_id, project_id),
            )
            row = cursor.fetchone()
        return _report(row) if row else None

    def create_draft(
        self,
        project_id: str,
        report_id: str,
        document: dict[str, object],
        *,
        created_by: str,
    ) -> int | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT draft_mdl_version FROM drift_reports
                WHERE id = %s::uuid AND project_id = %s::uuid FOR UPDATE
                """,
                (report_id, project_id),
            )
            report = cursor.fetchone()
            if report is None:
                raise KeyError(f"Drift report {report_id} does not exist")
            if report["draft_mdl_version"] is not None:
                return None
            cursor.execute(
                """
                SELECT COALESCE(MAX(version_number), 0) + 1 AS next_version
                FROM mdl_versions WHERE project_id = %s::uuid
                """,
                (project_id,),
            )
            version = int(cast(int, (cursor.fetchone() or {"next_version": 1})["next_version"]))
            cursor.execute(
                """
                INSERT INTO mdl_versions (id, project_id, version_number, status, document)
                VALUES (gen_random_uuid(), %s::uuid, %s, 'pending', %s)
                """,
                (project_id, version, Jsonb(document)),
            )
            cursor.execute(
                "UPDATE drift_reports SET draft_mdl_version = %s WHERE id = %s::uuid",
                (version, report_id),
            )
            cursor.execute(
                """
                INSERT INTO audit_events (actor_id, action, resource_type, resource_id, details)
                VALUES (%s::uuid, 'drift_draft_created', 'project', %s, %s)
                """,
                (created_by, project_id, Jsonb({"reportId": report_id, "revision": version})),
            )
        return version


def _report(row: Row) -> StoredDriftReport:
    return StoredDriftReport(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        mdl_version=int(cast(int, row["mdl_version"])),
        has_drift=bool(row["has_drift"]),
        summary=cast("dict[str, int]", row["summary"]),
        findings=cast("list[dict[str, object]]", row["findings"]),
        draft_mdl_version=cast("int | None", row["draft_mdl_version"]),
        created_by=str(row["created_by"]) if row["created_by"] is not None else None,
        created_at=cast(datetime, row["created_at"]),
        job_id=str(row["job_id"]) if row["job_id"] is not None else None,
    )
