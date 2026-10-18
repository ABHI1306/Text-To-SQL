"""Port for the append-only audit trail."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.core.types import AuditEvent


@dataclass(frozen=True)
class AuditFilter:
    """Optional equality and time-range filters for an audit query; unset fields match all."""
    actor_id: str | None = None
    action: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    since: datetime | None = None
    until: datetime | None = None


@dataclass(frozen=True)
class AuditRecord:
    """One stored audit row, as an ``AuditEvent`` plus its database id and timestamp."""
    id: int
    actor_id: str | None
    action: str
    resource_type: str
    resource_id: str
    details: Mapping[str, object]
    created_at: datetime


@dataclass(frozen=True)
class AuditPage:
    """One page of audit records; ``next_cursor`` is the ``before_id`` for the next page."""
    items: tuple[AuditRecord, ...]
    next_cursor: int | None


class AuditPort(Protocol):
    """Append-only audit trail: there is deliberately no update or delete."""

    def append(self, event: AuditEvent) -> None:
        """Persist one event; implementations must not update or drop earlier events."""
        ...

    def query(
        self, filters: AuditFilter, *, limit: int, before_id: int | None = None
    ) -> AuditPage:
        """Return up to ``limit`` matching records, newest first, older than ``before_id``."""
        ...
