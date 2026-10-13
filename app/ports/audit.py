from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.core.types import AuditEvent


@dataclass(frozen=True)
class AuditFilter:
    actor_id: str | None = None
    action: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    since: datetime | None = None
    until: datetime | None = None


@dataclass(frozen=True)
class AuditRecord:
    id: int
    actor_id: str | None
    action: str
    resource_type: str
    resource_id: str
    details: Mapping[str, object]
    created_at: datetime


@dataclass(frozen=True)
class AuditPage:
    items: tuple[AuditRecord, ...]
    next_cursor: int | None


class AuditPort(Protocol):
    """Append-only audit trail: there is deliberately no update or delete."""

    def append(self, event: AuditEvent) -> None: ...

    def query(
        self, filters: AuditFilter, *, limit: int, before_id: int | None = None
    ) -> AuditPage: ...
