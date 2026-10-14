from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from app.core.types import AuditEvent

PairStatus = Literal["proposed", "approved", "rejected"]
PairSource = Literal["manual", "feedback"]
Rating = Literal["up", "down"]


@dataclass(frozen=True)
class Instruction:
    id: str
    project_id: str
    text: str
    active: bool
    created_by: str | None
    updated_at: datetime


@dataclass(frozen=True)
class SqlPair:
    id: str
    project_id: str
    question: str
    logical_sql: str
    status: PairStatus
    source: PairSource
    proposed_by: str | None
    created_at: datetime
    decided_by: str | None = None
    decided_at: datetime | None = None


@dataclass(frozen=True)
class Feedback:
    id: str
    project_id: str
    user_id: str
    rating: Rating
    question: str
    logical_sql: str
    thread_id: str | None = None
    message_id: str | None = None


class KnowledgeRepoPort(Protocol):
    """Project knowledge storage. Every write takes the audit events stored in its transaction,
    and every read or write is scoped to one project."""

    def list_instructions(
        self, project_id: str, *, active_only: bool = False
    ) -> list[Instruction]: ...

    def get_instruction(self, project_id: str, instruction_id: str) -> Instruction | None: ...

    def create_instruction(self, instruction: Instruction, *, audit: AuditEvent) -> Instruction: ...

    def update_instruction(
        self,
        project_id: str,
        instruction_id: str,
        *,
        text: str | None,
        active: bool | None,
        audit: AuditEvent,
    ) -> Instruction | None: ...

    def delete_instruction(
        self, project_id: str, instruction_id: str, *, audit: AuditEvent
    ) -> bool: ...

    def list_pairs(self, project_id: str, *, status: PairStatus | None = None) -> list[SqlPair]: ...

    def get_pair(self, project_id: str, pair_id: str) -> SqlPair | None: ...

    def get_pairs(
        self, project_id: str, pair_ids: Sequence[str], *, status: PairStatus
    ) -> list[SqlPair]: ...

    def find_duplicate(
        self, project_id: str, question: str, logical_sql: str
    ) -> SqlPair | None:
        """A proposed or approved pair with the same question and SQL, if one exists."""
        ...

    def create_pair(self, pair: SqlPair, *, audit: AuditEvent) -> SqlPair: ...

    def decide_pair(
        self,
        project_id: str,
        pair_id: str,
        *,
        status: PairStatus,
        actor_id: str,
        audit: AuditEvent,
    ) -> SqlPair | None:
        """Move a *proposed* pair to approved or rejected; None when it is not proposed."""
        ...

    def delete_pair(self, project_id: str, pair_id: str, *, audit: AuditEvent) -> bool: ...

    def record_feedback(
        self,
        feedback: Feedback,
        *,
        pair: SqlPair | None,
        pair_id: str | None,
        audits: Sequence[AuditEvent],
    ) -> None:
        """Store the feedback and, when given, the pair it proposes, in one transaction."""
        ...


@dataclass(frozen=True)
class KnowledgeContext:
    """What retrieval adds to a question: approved example pairs and active instructions."""

    pairs: tuple[SqlPair, ...] = ()
    instructions: tuple[Instruction, ...] = ()


class KnowledgeLookup(Protocol):
    def lookup(self, project_id: str, vector: Sequence[float]) -> KnowledgeContext: ...
