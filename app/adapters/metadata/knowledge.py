"""PostgreSQL (and in-memory) storage for instructions, SQL pairs and feedback."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

import psycopg
from psycopg.rows import DictRow, dict_row

from app.adapters.metadata.governance import _insert_audit
from app.core.types import AuditEvent
from app.ports.knowledge import (
    Feedback,
    Instruction,
    KnowledgeRepoPort,
    PairStatus,
    SqlPair,
)

_INSTRUCTION = "id::text, project_id::text, text, active, created_by::text, updated_at"
_PAIR = (
    "id::text, project_id::text, question, logical_sql, status, source, "
    "proposed_by::text, created_at, decided_by::text, decided_at"
)


class PostgresKnowledgeRepository:
    """Stores instructions, SQL pairs and feedback; each write is audited in its transaction."""
    def __init__(self, dsn: str) -> None:
        """Store the metadata DSN, normalising the SQLAlchemy-style scheme."""
        if not dsn:
            raise ValueError("A metadata database URL is required")
        self._dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)

    def _connect(self) -> psycopg.Connection[DictRow]:
        """Open a connection that returns rows as dicts."""
        return psycopg.connect(self._dsn, row_factory=dict_row)

    # ------------------------------------------------------------ instructions

    def list_instructions(
        self, project_id: str, *, active_only: bool = False
    ) -> list[Instruction]:
        """List a project's instructions, optionally only active ones."""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {_INSTRUCTION} FROM project_instructions"
                " WHERE project_id = %s::uuid AND (active OR NOT %s) ORDER BY created_at, id",
                (project_id, active_only),
            ).fetchall()
        return [_instruction(row) for row in rows]

    def get_instruction(self, project_id: str, instruction_id: str) -> Instruction | None:
        """Return one instruction scoped to its project, or ``None``."""
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {_INSTRUCTION} FROM project_instructions"
                " WHERE project_id = %s::uuid AND id = %s::uuid",
                (project_id, instruction_id),
            ).fetchone()
        return _instruction(row) if row else None

    def create_instruction(self, instruction: Instruction, *, audit: AuditEvent) -> Instruction:
        """Store an instruction and its audit event together."""
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO project_instructions (id, project_id, text, active, created_by)"
                " VALUES (%s::uuid, %s::uuid, %s, %s, %s::uuid)"
                f" RETURNING {_INSTRUCTION}",
                (
                    instruction.id,
                    instruction.project_id,
                    instruction.text,
                    instruction.active,
                    instruction.created_by,
                ),
            )
            row = cursor.fetchone()
            _insert_audit(cursor, audit)
        assert row is not None
        return _instruction(row)

    def update_instruction(
        self,
        project_id: str,
        instruction_id: str,
        *,
        text: str | None,
        active: bool | None,
        audit: AuditEvent,
    ) -> Instruction | None:
        """Change text and/or active flag (``None`` keeps a field) with an audit event."""
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE project_instructions SET text = COALESCE(%s, text),"
                " active = COALESCE(%s, active), updated_at = now()"
                " WHERE project_id = %s::uuid AND id = %s::uuid"
                f" RETURNING {_INSTRUCTION}",
                (text, active, project_id, instruction_id),
            )
            row = cursor.fetchone()
            if row is not None:
                _insert_audit(cursor, audit)
        return _instruction(row) if row else None

    def delete_instruction(
        self, project_id: str, instruction_id: str, *, audit: AuditEvent
    ) -> bool:
        """Delete an instruction with an audit event; true if it existed."""
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM project_instructions WHERE project_id = %s::uuid AND id = %s::uuid",
                (project_id, instruction_id),
            )
            deleted = cursor.rowcount > 0
            if deleted:
                _insert_audit(cursor, audit)
        return deleted

    # ------------------------------------------------------------------- pairs

    def list_pairs(self, project_id: str, *, status: PairStatus | None = None) -> list[SqlPair]:
        """List a project's SQL pairs, optionally filtered by status."""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {_PAIR} FROM sql_pairs WHERE project_id = %s::uuid"
                " AND (%s::text IS NULL OR status = %s::text) ORDER BY created_at, id",
                (project_id, status, status),
            ).fetchall()
        return [_pair(row) for row in rows]

    def get_pair(self, project_id: str, pair_id: str) -> SqlPair | None:
        """Return one SQL pair scoped to its project, or ``None``."""
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {_PAIR} FROM sql_pairs WHERE project_id = %s::uuid AND id = %s::uuid",
                (project_id, pair_id),
            ).fetchone()
        return _pair(row) if row else None

    def get_pairs(
        self, project_id: str, pair_ids: Sequence[str], *, status: PairStatus
    ) -> list[SqlPair]:
        """Return the pairs with the given ids and status, scoped to the project."""
        if not pair_ids:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {_PAIR} FROM sql_pairs WHERE project_id = %s::uuid"
                " AND status = %s AND id = ANY(%s::uuid[])",
                (project_id, status, list(pair_ids)),
            ).fetchall()
        return [_pair(row) for row in rows]

    def find_duplicate(
        self, project_id: str, question: str, logical_sql: str
    ) -> SqlPair | None:
        """Find a proposed or approved pair with the same question and SQL."""
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {_PAIR} FROM sql_pairs WHERE project_id = %s::uuid"
                " AND question = %s AND logical_sql = %s AND status IN ('proposed', 'approved')"
                " ORDER BY created_at LIMIT 1",
                (project_id, question, logical_sql),
            ).fetchone()
        return _pair(row) if row else None

    def create_pair(self, pair: SqlPair, *, audit: AuditEvent) -> SqlPair:
        """Store a SQL pair and its audit event together."""
        with self._connect() as connection, connection.cursor() as cursor:
            _insert_pair(cursor, pair)
            _insert_audit(cursor, audit)
        return pair

    def decide_pair(
        self,
        project_id: str,
        pair_id: str,
        *,
        status: PairStatus,
        actor_id: str,
        audit: AuditEvent,
    ) -> SqlPair | None:
        """Approve or reject a still-proposed pair with an audit event; else ``None``."""
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE sql_pairs SET status = %s, decided_by = %s::uuid, decided_at = now()"
                " WHERE project_id = %s::uuid AND id = %s::uuid AND status = 'proposed'"
                f" RETURNING {_PAIR}",
                (status, actor_id, project_id, pair_id),
            )
            row = cursor.fetchone()
            if row is not None:
                _insert_audit(cursor, audit)
        return _pair(row) if row else None

    def delete_pair(self, project_id: str, pair_id: str, *, audit: AuditEvent) -> bool:
        """Delete a SQL pair with an audit event; true if it existed."""
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM sql_pairs WHERE project_id = %s::uuid AND id = %s::uuid",
                (project_id, pair_id),
            )
            deleted = cursor.rowcount > 0
            if deleted:
                _insert_audit(cursor, audit)
        return deleted

    def record_feedback(
        self,
        feedback: Feedback,
        *,
        pair: SqlPair | None,
        pair_id: str | None,
        audits: Sequence[AuditEvent],
    ) -> None:
        """Store feedback, any pair it creates, and the audit events together."""
        with self._connect() as connection, connection.cursor() as cursor:
            if pair is not None:
                _insert_pair(cursor, pair)
            cursor.execute(
                "INSERT INTO feedback (id, project_id, user_id, thread_id, message_id, rating,"
                " question, logical_sql, pair_id) VALUES (%s::uuid, %s::uuid, %s::uuid,"
                " %s::uuid, %s, %s, %s, %s, %s::uuid)",
                (
                    feedback.id,
                    feedback.project_id,
                    feedback.user_id,
                    feedback.thread_id,
                    feedback.message_id,
                    feedback.rating,
                    feedback.question,
                    feedback.logical_sql,
                    pair.id if pair else pair_id,
                ),
            )
            for audit in audits:
                _insert_audit(cursor, audit)


def _insert_pair(cursor: psycopg.Cursor[DictRow], pair: SqlPair) -> None:
    """Insert a SQL pair using the caller's cursor (so it joins their transaction)."""
    cursor.execute(
        "INSERT INTO sql_pairs (id, project_id, question, logical_sql, status, source,"
        " proposed_by, decided_by, decided_at) VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s,"
        " %s::uuid, %s::uuid, %s)",
        (
            pair.id,
            pair.project_id,
            pair.question,
            pair.logical_sql,
            pair.status,
            pair.source,
            pair.proposed_by,
            pair.decided_by,
            pair.decided_at,
        ),
    )


def _instruction(row: Any) -> Instruction:
    """Convert a database row to an ``Instruction``."""
    return Instruction(
        id=row["id"],
        project_id=row["project_id"],
        text=row["text"],
        active=row["active"],
        created_by=row["created_by"],
        updated_at=row["updated_at"],
    )


def _pair(row: Any) -> SqlPair:
    """Convert a database row to a ``SqlPair``."""
    return SqlPair(
        id=row["id"],
        project_id=row["project_id"],
        question=row["question"],
        logical_sql=row["logical_sql"],
        status=cast(PairStatus, row["status"]),
        source=row["source"],
        proposed_by=row["proposed_by"],
        created_at=row["created_at"],
        decided_by=row["decided_by"],
        decided_at=row["decided_at"],
    )


class InMemoryKnowledgeRepository:
    """Process-local repository for tests; same project scoping and audit-with-write contract."""

    def __init__(self) -> None:
        """Create empty stores for instructions, pairs, feedback and audit events."""
        self.instructions: dict[str, Instruction] = {}
        self.pairs: dict[str, SqlPair] = {}
        self.feedback: list[Feedback] = []
        self.audit: list[AuditEvent] = []

    def list_instructions(
        self, project_id: str, *, active_only: bool = False
    ) -> list[Instruction]:
        """List a project's instructions, optionally only active ones."""
        return [
            i
            for i in self.instructions.values()
            if i.project_id == project_id and (i.active or not active_only)
        ]

    def get_instruction(self, project_id: str, instruction_id: str) -> Instruction | None:
        """Return one instruction scoped to its project, or ``None``."""
        found = self.instructions.get(instruction_id)
        return found if found and found.project_id == project_id else None

    def create_instruction(self, instruction: Instruction, *, audit: AuditEvent) -> Instruction:
        """Store an instruction and its audit event together."""
        self.instructions[instruction.id] = instruction
        self.audit.append(audit)
        return instruction

    def update_instruction(
        self,
        project_id: str,
        instruction_id: str,
        *,
        text: str | None,
        active: bool | None,
        audit: AuditEvent,
    ) -> Instruction | None:
        """Change text and/or active flag (``None`` keeps a field) with an audit event."""
        current = self.get_instruction(project_id, instruction_id)
        if current is None:
            return None
        updated = replace(
            current,
            text=current.text if text is None else text,
            active=current.active if active is None else active,
            updated_at=datetime.now(UTC),
        )
        self.instructions[instruction_id] = updated
        self.audit.append(audit)
        return updated

    def delete_instruction(
        self, project_id: str, instruction_id: str, *, audit: AuditEvent
    ) -> bool:
        """Delete an instruction with an audit event; true if it existed."""
        if self.get_instruction(project_id, instruction_id) is None:
            return False
        del self.instructions[instruction_id]
        self.audit.append(audit)
        return True

    def list_pairs(self, project_id: str, *, status: PairStatus | None = None) -> list[SqlPair]:
        """List a project's SQL pairs, optionally filtered by status."""
        return [
            p
            for p in self.pairs.values()
            if p.project_id == project_id and (status is None or p.status == status)
        ]

    def get_pair(self, project_id: str, pair_id: str) -> SqlPair | None:
        """Return one SQL pair scoped to its project, or ``None``."""
        found = self.pairs.get(pair_id)
        return found if found and found.project_id == project_id else None

    def get_pairs(
        self, project_id: str, pair_ids: Sequence[str], *, status: PairStatus
    ) -> list[SqlPair]:
        """Return the pairs with the given ids and status, scoped to the project."""
        return [
            p
            for p in (self.get_pair(project_id, i) for i in pair_ids)
            if p is not None and p.status == status
        ]

    def find_duplicate(
        self, project_id: str, question: str, logical_sql: str
    ) -> SqlPair | None:
        """Find a proposed or approved pair with the same question and SQL."""
        for pair in self.list_pairs(project_id):
            if (
                pair.question == question
                and pair.logical_sql == logical_sql
                and pair.status in {"proposed", "approved"}
            ):
                return pair
        return None

    def create_pair(self, pair: SqlPair, *, audit: AuditEvent) -> SqlPair:
        """Store a SQL pair and its audit event together."""
        self.pairs[pair.id] = pair
        self.audit.append(audit)
        return pair

    def decide_pair(
        self,
        project_id: str,
        pair_id: str,
        *,
        status: PairStatus,
        actor_id: str,
        audit: AuditEvent,
    ) -> SqlPair | None:
        """Approve or reject a still-proposed pair with an audit event; else ``None``."""
        current = self.get_pair(project_id, pair_id)
        if current is None or current.status != "proposed":
            return None
        decided = replace(
            current, status=status, decided_by=actor_id, decided_at=datetime.now(UTC)
        )
        self.pairs[pair_id] = decided
        self.audit.append(audit)
        return decided

    def delete_pair(self, project_id: str, pair_id: str, *, audit: AuditEvent) -> bool:
        """Delete a SQL pair with an audit event; true if it existed."""
        if self.get_pair(project_id, pair_id) is None:
            return False
        del self.pairs[pair_id]
        self.audit.append(audit)
        return True

    def record_feedback(
        self,
        feedback: Feedback,
        *,
        pair: SqlPair | None,
        pair_id: str | None,
        audits: Sequence[AuditEvent],
    ) -> None:
        """Store feedback, any pair it creates, and the audit events together."""
        if pair is not None:
            self.pairs[pair.id] = pair
        self.feedback.append(feedback)
        self.audit.extend(audits)


_check: type[KnowledgeRepoPort] = InMemoryKnowledgeRepository
