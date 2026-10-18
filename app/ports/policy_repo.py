"""Port for storing row-level security policies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.core.types import AuditEvent
from app.domain.policy import RowPolicy


@dataclass(frozen=True)
class StoredPolicy:
    """A persisted row policy with its enabled flag and last-update audit fields."""

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
    ) -> list[StoredPolicy]:
        """List a project's policies, only the enabled ones when ``enabled_only``."""
        ...

    def get_policy(self, project_id: str, policy_id: str) -> StoredPolicy | None:
        """Return the policy, or None when missing or in another project."""
        ...

    def create_policy(
        self,
        project_id: str,
        policy: RowPolicy,
        *,
        enabled: bool,
        actor_id: str,
        audit: AuditEvent,
    ) -> StoredPolicy:
        """Store the policy and its audit event together; return the stored row."""
        ...

    def update_policy(
        self,
        project_id: str,
        policy_id: str,
        policy: RowPolicy,
        *,
        enabled: bool,
        actor_id: str,
        audit: AuditEvent,
    ) -> StoredPolicy | None:
        """Replace the policy and its enabled flag with the audit event; None when missing."""
        ...

    def delete_policy(
        self, project_id: str, policy_id: str, *, audit: AuditEvent
    ) -> bool:
        """Delete the policy with its audit event; False when missing."""
        ...

    def known_attribute_keys(self) -> frozenset[str]:
        """Return the user attribute keys in use, for validating policy predicates."""
        ...
