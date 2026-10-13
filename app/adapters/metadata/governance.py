"""PostgreSQL adapter for the audit trail and row policies (one metadata store)."""

from __future__ import annotations

import json
from typing import cast
from uuid import uuid4

import psycopg
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb

from app.core.types import AuditEvent
from app.domain.policy import RowPolicy
from app.ports.audit import AuditFilter, AuditPage, AuditRecord
from app.ports.policy_repo import StoredPolicy

_POLICY_COLUMNS = (
    "id, project_id, model, name, predicate, applies_to, on_missing, enabled, "
    "updated_by, updated_at"
)


class PostgresGovernanceRepository:
    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ValueError("A metadata database URL is required")
        # The API is configured with a SQLAlchemy-style URL; psycopg wants a plain one.
        self._dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)

    def _connect(self) -> psycopg.Connection[DictRow]:
        return psycopg.connect(self._dsn, row_factory=dict_row)

    # ------------------------------------------------------------------ audit

    def append(self, event: AuditEvent) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            _insert_audit(cursor, event)

    def query(
        self, filters: AuditFilter, *, limit: int, before_id: int | None = None
    ) -> AuditPage:
        clauses: list[str] = []
        params: list[object] = []
        for column, value in (
            ("actor_id = %s::uuid", filters.actor_id),
            ("action = %s", filters.action),
            ("resource_type = %s", filters.resource_type),
            ("resource_id = %s", filters.resource_id),
            ("created_at >= %s", filters.since),
            ("created_at < %s", filters.until),
            ("id < %s", before_id),
        ):
            if value is not None:
                clauses.append(column)
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT id, actor_id, action, resource_type, resource_id, details, created_at
                FROM audit_events {where}
                ORDER BY id DESC LIMIT %s
                """,
                (*params, limit + 1),
            )
            rows = cursor.fetchall()
        page = rows[:limit]
        items = tuple(
            AuditRecord(
                id=int(row["id"]),
                actor_id=str(row["actor_id"]) if row["actor_id"] else None,
                action=str(row["action"]),
                resource_type=str(row["resource_type"]),
                resource_id=str(row["resource_id"]),
                details=cast(dict[str, object], row["details"]),
                created_at=row["created_at"],
            )
            for row in page
        )
        next_cursor = items[-1].id if len(rows) > limit and items else None
        return AuditPage(items=items, next_cursor=next_cursor)

    # --------------------------------------------------------------- policies

    def list_policies(
        self, project_id: str, *, enabled_only: bool = False
    ) -> list[StoredPolicy]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT {_POLICY_COLUMNS} FROM row_policies
                WHERE project_id = %s::uuid AND (NOT %s OR enabled)
                ORDER BY model, name
                """,
                (project_id, enabled_only),
            )
            return [_stored_policy(row) for row in cursor.fetchall()]

    def get_policy(self, project_id: str, policy_id: str) -> StoredPolicy | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {_POLICY_COLUMNS} FROM row_policies "
                "WHERE project_id = %s::uuid AND id = %s::uuid",
                (project_id, policy_id),
            )
            row = cursor.fetchone()
        return _stored_policy(row) if row else None

    def create_policy(
        self,
        project_id: str,
        policy: RowPolicy,
        *,
        enabled: bool,
        actor_id: str,
        audit: AuditEvent,
    ) -> StoredPolicy:
        policy_id = str(uuid4())
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO row_policies
                    (id, project_id, model, name, predicate, applies_to, on_missing,
                     enabled, updated_by)
                VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s, %s::uuid)
                RETURNING {_POLICY_COLUMNS}
                """,
                (
                    policy_id,
                    project_id,
                    policy.model,
                    policy.name,
                    policy.predicate,
                    Jsonb(policy.applies_to.model_dump(by_alias=True)),
                    policy.on_missing_attribute.value,
                    enabled,
                    actor_id,
                ),
            )
            row = cursor.fetchone()
            _insert_audit(cursor, _with_resource(audit, policy_id))
        assert row is not None
        return _stored_policy(row)

    def update_policy(
        self,
        project_id: str,
        policy_id: str,
        policy: RowPolicy,
        *,
        enabled: bool,
        actor_id: str,
        audit: AuditEvent,
    ) -> StoredPolicy | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE row_policies
                SET model = %s, name = %s, predicate = %s, applies_to = %s, on_missing = %s,
                    enabled = %s, updated_by = %s::uuid, updated_at = now()
                WHERE project_id = %s::uuid AND id = %s::uuid
                RETURNING {_POLICY_COLUMNS}
                """,
                (
                    policy.model,
                    policy.name,
                    policy.predicate,
                    Jsonb(policy.applies_to.model_dump(by_alias=True)),
                    policy.on_missing_attribute.value,
                    enabled,
                    actor_id,
                    project_id,
                    policy_id,
                ),
            )
            row = cursor.fetchone()
            if row is not None:
                _insert_audit(cursor, audit)
        return _stored_policy(row) if row else None

    def delete_policy(self, project_id: str, policy_id: str, *, audit: AuditEvent) -> bool:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM row_policies WHERE project_id = %s::uuid AND id = %s::uuid",
                (project_id, policy_id),
            )
            deleted = cursor.rowcount > 0
            if deleted:
                _insert_audit(cursor, audit)
        return deleted

    def known_attribute_keys(self) -> frozenset[str]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT DISTINCT key FROM user_attributes")
            return frozenset(str(row["key"]) for row in cursor.fetchall())


def _insert_audit(cursor: psycopg.Cursor[DictRow], event: AuditEvent) -> None:
    cursor.execute(
        """
        INSERT INTO audit_events (actor_id, action, resource_type, resource_id, details)
        VALUES (%s::uuid, %s, %s, %s, %s)
        """,
        (
            event.actor_id,
            event.action,
            event.resource_type,
            event.resource_id,
            Jsonb(json.loads(json.dumps(dict(event.details), default=str))),
        ),
    )


def _with_resource(event: AuditEvent, resource_id: str) -> AuditEvent:
    return AuditEvent(
        actor_id=event.actor_id,
        action=event.action,
        resource_type=event.resource_type,
        resource_id=resource_id,
        details=event.details,
    )


def _stored_policy(row: DictRow) -> StoredPolicy:
    applies_to = cast(dict[str, object], row["applies_to"] or {})
    policy = RowPolicy.model_validate(
        {
            "name": row["name"],
            "model": row["model"],
            "predicate": row["predicate"],
            "appliesTo": applies_to,
            "onMissingAttribute": row["on_missing"],
        }
    )
    return StoredPolicy(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        policy=policy,
        enabled=bool(row["enabled"]),
        updated_by=str(row["updated_by"]) if row["updated_by"] else None,
        updated_at=row["updated_at"],
    )
