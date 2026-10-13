from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.core.types import AuditEvent
from app.domain.policy import RowPolicy


@dataclass(frozen=True)
class StoredPolicy:
    id: str
    project_id: str
    policy: RowPolicy
    enabled: bool
    updated_by: str | None
    updated_at: datetime


class PolicyRepoPort(Protocol):
    """Row-policy storage. Every write takes the audit event stored in the same transaction."""

    def list_policies(
        self, project_id: str, *, enabled_only: bool = False
    ) -> list[StoredPolicy]: ...

    def get_policy(self, project_id: str, policy_id: str) -> StoredPolicy | None: ...

    def create_policy(
        self,
        project_id: str,
        policy: RowPolicy,
        *,
        enabled: bool,
        actor_id: str,
        audit: AuditEvent,
    ) -> StoredPolicy: ...

    def update_policy(
        self,
        project_id: str,
        policy_id: str,
        policy: RowPolicy,
        *,
        enabled: bool,
        actor_id: str,
        audit: AuditEvent,
    ) -> StoredPolicy | None: ...

    def delete_policy(
        self, project_id: str, policy_id: str, *, audit: AuditEvent
    ) -> bool: ...

    def known_attribute_keys(self) -> frozenset[str]: ...
