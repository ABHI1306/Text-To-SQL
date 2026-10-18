"""Audit service: record append-only events and browse them with bounded paging."""

from __future__ import annotations

from collections.abc import Mapping

from app.core.types import AuditEvent
from app.ports.audit import AuditFilter, AuditPage, AuditPort

MAX_PAGE_SIZE = 200


class AuditService:
    """Single entry point for audit events; the underlying store is append-only."""

    def __init__(self, port: AuditPort) -> None:
        """Wrap the audit port that stores events."""
        self._port = port

    def record(
        self,
        actor_id: str | None,
        action: str,
        resource_type: str,
        resource_id: str,
        details: Mapping[str, object] | None = None,
    ) -> None:
        """Append one event; ``details`` is copied so later mutation cannot alter it."""
        self._port.append(
            AuditEvent(
                actor_id=actor_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                details=dict(details or {}),
            )
        )

    def browse(
        self,
        filters: AuditFilter,
        *,
        limit: int = 50,
        before_id: int | None = None,
    ) -> AuditPage:
        """One page of events, newest first; ``limit`` is clamped to 1..``MAX_PAGE_SIZE``.

        Raises ValueError when ``since`` is not earlier than ``until``.
        """
        if filters.since and filters.until and filters.since >= filters.until:
            raise ValueError("'since' must be earlier than 'until'")
        return self._port.query(
            filters, limit=max(1, min(limit, MAX_PAGE_SIZE)), before_id=before_id
        )
