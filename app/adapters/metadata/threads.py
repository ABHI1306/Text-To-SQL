from __future__ import annotations

from collections import defaultdict

import psycopg

from app.ports.threads import ThreadStorePort, Turn


class InMemoryThreadStore:
    """Process-local thread store for tests and the CLI; the API uses the PostgreSQL one."""

    def __init__(self) -> None:
        self._owners: dict[str, str] = {}
        self._turns: dict[str, list[Turn]] = defaultdict(list)

    def history(self, thread_id: str, user_id: str, *, limit: int) -> list[Turn]:
        if self._owners.get(thread_id) != user_id:
            return []
        return list(self._turns[thread_id][-limit:])

    def append(self, thread_id: str, user_id: str, project_id: str, turn: Turn) -> None:
        if self._owners.setdefault(thread_id, user_id) != user_id:
            raise PermissionError("thread belongs to another user")
        self._turns[thread_id].append(turn)


_check: type[ThreadStorePort] = InMemoryThreadStore


class PostgresThreadStore:
    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ValueError("A metadata database URL is required")
        self._dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)

    def history(self, thread_id: str, user_id: str, *, limit: int) -> list[Turn]:
        with psycopg.connect(self._dsn) as connection:
            rows = connection.execute(
                """
                SELECT t.question, t.standalone_question, t.logical_sql, t.answer
                FROM thread_turns t JOIN threads ON threads.id = t.thread_id
                WHERE t.thread_id = %s::uuid AND threads.user_id = %s::uuid
                ORDER BY t.id DESC LIMIT %s
                """,
                (thread_id, user_id, limit),
            ).fetchall()
        return [Turn(*row) for row in reversed(rows)]

    def append(self, thread_id: str, user_id: str, project_id: str, turn: Turn) -> None:
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
            connection.execute(
                "INSERT INTO thread_turns (thread_id, question, standalone_question, logical_sql, answer)"
                " VALUES (%s::uuid, %s, %s, %s, %s)",
                (thread_id, turn.question, turn.standalone_question, turn.logical_sql, turn.answer),
            )
