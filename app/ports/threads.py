from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ThreadRecord:
    id: str
    project_id: str
    user_id: str


@dataclass(frozen=True)
class ThreadSummary:
    """One row of a person's conversation list."""

    id: str
    title: str  # the first question
    turns: int
    last_activity: str


@dataclass(frozen=True)
class MessageRecord:
    """A stored turn; ``id`` is the message id the feedback endpoint takes."""

    id: str
    thread_id: str
    turn: Turn
    created_at: str = ""


@dataclass(frozen=True)
class Turn:
    """One answered (or clarified) exchange: the user's words, what was run, what came back."""

    question: str
    standalone_question: str
    logical_sql: str | None = None
    answer: str | None = None
    summary: str | None = None
    chart: dict[str, Any] | None = None
    suggestions: tuple[str, ...] = ()
    latency_ms: int | None = None
    failure_code: str | None = None  # set when the question ended in a typed failure
    failure_message: str | None = None
    trace: dict[str, Any] | None = None  # {'steps': [...], 'models': [...]} as streamed
    model_version: int | None = None


class ThreadStorePort(Protocol):
    """Conversation memory. A thread belongs to the user who started it and nobody else."""

    def history(self, thread_id: str, user_id: str, *, limit: int) -> list[Turn]:
        """The newest ``limit`` turns, oldest first; empty for an unknown or foreign thread."""
        ...

    def append(self, thread_id: str, user_id: str, project_id: str, turn: Turn) -> str | None:
        """Add a turn and return its message id; raises PermissionError for a foreign thread."""
        ...


class ThreadCatalogPort(ThreadStorePort, Protocol):
    """Threads as resources: created, listed and read by the API (history stays owner-only)."""

    def create(self, thread_id: str, project_id: str, user_id: str) -> None: ...

    def list_threads(self, project_id: str, user_id: str, limit: int) -> list[ThreadSummary]:
        """The user's own conversations in a project, newest activity first; empty ones skipped."""
        ...

    def get(self, thread_id: str) -> ThreadRecord | None: ...

    def messages(self, thread_id: str) -> list[MessageRecord]:
        """Every turn of a thread, oldest first; the caller has already checked access."""
        ...

    def get_message(self, message_id: str) -> tuple[ThreadRecord, MessageRecord] | None: ...
