from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Turn:
    """One answered (or clarified) exchange: the user's words, what was run, what came back."""

    question: str
    standalone_question: str
    logical_sql: str | None = None
    answer: str | None = None


class ThreadStorePort(Protocol):
    """Conversation memory. A thread belongs to the user who started it and nobody else."""

    def history(self, thread_id: str, user_id: str, *, limit: int) -> list[Turn]:
        """The newest ``limit`` turns, oldest first; empty for an unknown or foreign thread."""
        ...

    def append(self, thread_id: str, user_id: str, project_id: str, turn: Turn) -> None:
        """Add a turn; raises PermissionError when the thread belongs to someone else."""
        ...
