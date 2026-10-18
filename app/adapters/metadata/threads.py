"""Thread (chat history) stores: in-memory for tests and PostgreSQL for the API."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.ports.threads import MessageRecord, ThreadCatalogPort, ThreadRecord, ThreadSummary, Turn


class InMemoryThreadStore:
    """Process-local thread store for tests and the CLI; the API uses the PostgreSQL one."""

    def __init__(self) -> None:
        """Create empty owner, project, turn and message-id maps."""
        self._owners: dict[str, str] = {}
        self._projects: dict[str, str] = {}
        self._turns: dict[str, list[Turn]] = defaultdict(list)
        self._ids: dict[str, list[str]] = defaultdict(list)
        self._counter = 0

    def create(self, thread_id: str, project_id: str, user_id: str) -> None:
        """Record the thread's project and owner; an existing thread is left unchanged."""
        self._owners.setdefault(thread_id, user_id)
        self._projects.setdefault(thread_id, project_id)

    def get(self, thread_id: str) -> ThreadRecord | None:
        """Return the thread's project and owner, or ``None``."""
        if thread_id not in self._owners:
            return None
        return ThreadRecord(thread_id, self._projects.get(thread_id, ""), self._owners[thread_id])

    def messages(self, thread_id: str) -> list[MessageRecord]:
        """Return every turn of a thread in order."""
        return [
            MessageRecord(message_id, thread_id, turn)
            for message_id, turn in zip(self._ids[thread_id], self._turns[thread_id], strict=True)
        ]

    def get_message(self, message_id: str) -> tuple[ThreadRecord, MessageRecord] | None:
        """Return one message with its thread, or ``None``."""
        for thread_id, ids in self._ids.items():
            if message_id in ids:
                record = self.get(thread_id)
                message = next(m for m in self.messages(thread_id) if m.id == message_id)
                return (record, message) if record else None
        return None

    def history(self, thread_id: str, user_id: str, *, limit: int) -> list[Turn]:
        """Return the owner's recent answered turns (failures excluded), oldest first."""
        if self._owners.get(thread_id) != user_id:
            return []
        answered = [t for t in self._turns[thread_id] if t.failure_code is None]
        return answered[-limit:]

    def list_threads(self, project_id: str, user_id: str, limit: int) -> list[ThreadSummary]:
        """List the user's threads in a project, newest activity first."""
        mine = [
            tid for tid, owner in self._owners.items()
            if owner == user_id and self._projects.get(tid) == project_id and self._turns[tid]
        ]
        # Newest activity first: the in-memory ids grow, so the last turn id orders them.
        mine.sort(key=lambda tid: int(self._ids[tid][-1]), reverse=True)
        return [
            ThreadSummary(tid, self._turns[tid][0].question, len(self._turns[tid]), "")
            for tid in mine[:limit]
        ]

    def append(self, thread_id: str, user_id: str, project_id: str, turn: Turn) -> str | None:
        """Add a turn, refusing another user's thread; returns the new message id."""
        if self._owners.setdefault(thread_id, user_id) != user_id:
            raise PermissionError("thread belongs to another user")
        self._projects.setdefault(thread_id, project_id)
        self._counter += 1
        self._turns[thread_id].append(turn)
        self._ids[thread_id].append(str(self._counter))
        return str(self._counter)


_check: type[ThreadCatalogPort] = InMemoryThreadStore


_TURN_FIELDS = (
    "question, standalone_question, logical_sql, answer, summary, chart, suggestions, latency_ms,"
    " failure_code, failure_message, trace, model_version"
)
_TURN_COLUMNS = _TURN_FIELDS
_TURN_COLUMNS_M = ", ".join(f"m.{name.strip()}" for name in _TURN_FIELDS.split(","))


def _turn(row: tuple[Any, ...]) -> Turn:
    """Build a ``Turn`` from a row of the turn columns."""
    (question, standalone, sql, answer, summary, chart, suggestions, latency, code, message,
     trace, model_version) = row
    return Turn(
        question, standalone, sql, answer, summary, chart, tuple(suggestions or ()), latency,
        code, message, trace, model_version,
    )


class PostgresThreadStore:
    """Chat history in Postgres, with every read and write tied to the owning user."""
    def __init__(self, dsn: str) -> None:
        """Store the metadata DSN, normalising the SQLAlchemy-style scheme."""
        if not dsn:
            raise ValueError("A metadata database URL is required")
        self._dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)

    def history(self, thread_id: str, user_id: str, *, limit: int) -> list[Turn]:
        """Return the owner's recent answered turns (failures excluded), oldest first."""
        with psycopg.connect(self._dsn) as connection:
            rows = connection.execute(
                """
                SELECT t.question, t.standalone_question, t.logical_sql, t.answer
                FROM thread_turns t JOIN threads ON threads.id = t.thread_id
                WHERE t.thread_id = %s::uuid AND threads.user_id = %s::uuid
                  AND t.failure_code IS NULL
                ORDER BY t.id DESC LIMIT %s
                """,
                (thread_id, user_id, limit),
            ).fetchall()
        return [Turn(*row) for row in reversed(rows)]

    def create(self, thread_id: str, project_id: str, user_id: str) -> None:
        """Record the thread's project and owner; an existing thread is left unchanged."""
        with psycopg.connect(self._dsn) as connection:
            connection.execute(
                "INSERT INTO threads (id, project_id, user_id) VALUES (%s::uuid, %s::uuid, %s::uuid)"
                " ON CONFLICT (id) DO NOTHING",
                (thread_id, project_id, user_id),
            )

    def get(self, thread_id: str) -> ThreadRecord | None:
        """Return the thread's project and owner, or ``None``."""
        with psycopg.connect(self._dsn) as connection:
            row = connection.execute(
                "SELECT id::text, project_id::text, user_id::text FROM threads WHERE id = %s::uuid",
                (thread_id,),
            ).fetchone()
        return ThreadRecord(*row) if row else None

    def list_threads(self, project_id: str, user_id: str, limit: int) -> list[ThreadSummary]:
        """List the user's threads in a project, newest activity first."""
        with psycopg.connect(self._dsn) as connection:
            rows = connection.execute(
                """
                SELECT t.id::text,
                       (SELECT question FROM thread_turns f WHERE f.thread_id = t.id
                        ORDER BY f.id LIMIT 1) AS title,
                       count(m.id), max(m.created_at)
                FROM threads t JOIN thread_turns m ON m.thread_id = t.id
                WHERE t.project_id = %s::uuid AND t.user_id = %s::uuid
                GROUP BY t.id ORDER BY max(m.created_at) DESC LIMIT %s
                """,
                (project_id, user_id, limit),
            ).fetchall()
        return [ThreadSummary(r[0], r[1], r[2], r[3].isoformat()) for r in rows]

    def messages(self, thread_id: str) -> list[MessageRecord]:
        """Return every turn of a thread in order."""
        with psycopg.connect(self._dsn) as connection:
            rows = connection.execute(
                f"""
                SELECT id::text, thread_id::text, {_TURN_COLUMNS}, created_at
                FROM thread_turns WHERE thread_id = %s::uuid ORDER BY id
                """,
                (thread_id,),
            ).fetchall()
        return [MessageRecord(r[0], r[1], _turn(r[2:-1]), r[-1].isoformat()) for r in rows]

    def get_message(self, message_id: str) -> tuple[ThreadRecord, MessageRecord] | None:
        """Return one message with its thread, or ``None``."""
        if not message_id.isdigit():
            return None
        with psycopg.connect(self._dsn) as connection:
            row = connection.execute(
                f"""
                SELECT t.id::text, t.project_id::text, t.user_id::text,
                       m.id::text, {_TURN_COLUMNS_M}, m.created_at
                FROM thread_turns m JOIN threads t ON t.id = m.thread_id
                WHERE m.id = %s
                """,
                (int(message_id),),
            ).fetchone()
        if row is None:
            return None
        thread = ThreadRecord(row[0], row[1], row[2])
        return thread, MessageRecord(row[3], row[0], _turn(row[4:-1]), row[-1].isoformat())

    def append(self, thread_id: str, user_id: str, project_id: str, turn: Turn) -> str | None:
        """Add a turn, refusing another user's thread; returns the new message id."""
        with psycopg.connect(self._dsn) as connection:
            connection.execute(
                "INSERT INTO threads (id, project_id, user_id) VALUES (%s::uuid, %s::uuid, %s::uuid)"
                " ON CONFLICT (id) DO NOTHING",
                (thread_id, project_id, user_id),
            )
            owner = connection.execute(
                "SELECT user_id::text FROM threads WHERE id = %s::uuid", (thread_id,)
            ).fetchone()
            if owner is None or owner[0] != user_id:
                raise PermissionError("thread belongs to another user")
            row = connection.execute(
                """
                INSERT INTO thread_turns (thread_id, question, standalone_question, logical_sql,
                    answer, summary, chart, suggestions, latency_ms, failure_code, failure_message,
                    trace, model_version)
                VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id::text
                """,
                (
                    thread_id, turn.question, turn.standalone_question, turn.logical_sql,
                    turn.answer, turn.summary,
                    Jsonb(turn.chart) if turn.chart is not None else None,
                    Jsonb(list(turn.suggestions)) if turn.suggestions else None,
                    turn.latency_ms, turn.failure_code, turn.failure_message,
                    Jsonb(turn.trace) if turn.trace is not None else None, turn.model_version,
                ),
            ).fetchone()
        return row[0] if row else None
