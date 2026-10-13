from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class RecordingAudit:
    """Stands in for AuditService in API tests and keeps what was recorded."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record(
        self,
        actor_id: str | None,
        action: str,
        resource_type: str,
        resource_id: str,
        details: Mapping[str, object] | None = None,
    ) -> None:
        self.events.append(
            {
                "actor_id": actor_id,
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "details": dict(details or {}),
            }
        )

    def actions(self) -> list[str]:
        return [event["action"] for event in self.events]
