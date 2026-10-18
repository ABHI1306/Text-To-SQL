"""Row-policy service: validate, store and audit policy changes."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from app.core.types import AuditEvent, Principal
from app.domain.mdl import Mdl
from app.domain.policy import RowPolicy
from app.policy.validation import PolicyValidation, validate_policy
from app.ports.policy_repo import PolicyRepoPort, StoredPolicy


class PolicyRejected(ValueError):
    """Raised when a policy fails validation; carries the ``PolicyValidation``."""
    def __init__(self, validation: PolicyValidation) -> None:
        """Join the validation error messages into the exception text."""
        super().__init__("; ".join(error.message for error in validation.errors))
        self.validation = validation


class PolicyNotFound(LookupError):
    """Raised when a policy id does not exist in the project."""


@dataclass(frozen=True)
class SavedPolicy:
    """A stored policy together with the validation it passed."""
    stored: StoredPolicy
    validation: PolicyValidation


class PolicyService:
    """Row-policy changes: validated for a test user, stored and audited atomically."""

    def __init__(self, repository: PolicyRepoPort) -> None:
        """Keep the policy repository that stores policies and audit events."""
        self._repository = repository

    def list(self, project_id: str) -> list[StoredPolicy]:
        """Policies of the project."""
        return self._repository.list_policies(project_id)

    def create(
        self,
        project_id: str,
        policy: RowPolicy,
        *,
        enabled: bool,
        mdl: Mdl,
        test_principal: Principal,
        actor_id: str,
        known_roles: Collection[str] | None = None,
    ) -> SavedPolicy:
        """Validate and store a new policy, writing the audit event in the same call."""
        validation = self._validate(policy, mdl, test_principal, known_roles)
        stored = self._repository.create_policy(
            project_id,
            policy,
            enabled=enabled,
            actor_id=actor_id,
            audit=_event(actor_id, "row_policy_created", project_id, policy, enabled, test_principal),
        )
        return SavedPolicy(stored, validation)

    def update(
        self,
        project_id: str,
        policy_id: str,
        policy: RowPolicy,
        *,
        enabled: bool,
        mdl: Mdl,
        test_principal: Principal,
        actor_id: str,
        known_roles: Collection[str] | None = None,
    ) -> SavedPolicy:
        """Validate and replace a policy, auditing the previous predicate, state and scope."""
        existing = self._repository.get_policy(project_id, policy_id)
        if existing is None:
            raise PolicyNotFound(policy_id)
        validation = self._validate(policy, mdl, test_principal, known_roles)
        audit = _event(
            actor_id,
            "row_policy_updated",
            project_id,
            policy,
            enabled,
            test_principal,
            policy_id,
            previous={
                "predicate": existing.policy.predicate,
                "enabled": existing.enabled,
                "appliesTo": existing.policy.applies_to.model_dump(by_alias=True),
            },
        )
        stored = self._repository.update_policy(
            project_id, policy_id, policy, enabled=enabled, actor_id=actor_id, audit=audit
        )
        if stored is None:
            raise PolicyNotFound(policy_id)
        return SavedPolicy(stored, validation)

    def delete(self, project_id: str, policy_id: str, *, actor_id: str) -> None:
        """Delete a policy with an audit event; raises ``PolicyNotFound`` if it is gone."""
        existing = self._repository.get_policy(project_id, policy_id)
        if existing is None:
            raise PolicyNotFound(policy_id)
        audit = AuditEvent(
            actor_id=actor_id,
            action="row_policy_deleted",
            resource_type="row_policy",
            resource_id=policy_id,
            details={
                "project_id": project_id,
                "name": existing.policy.name,
                "model": existing.policy.model,
                "predicate": existing.policy.predicate,
            },
        )
        if not self._repository.delete_policy(project_id, policy_id, audit=audit):
            raise PolicyNotFound(policy_id)

    def _validate(
        self,
        policy: RowPolicy,
        mdl: Mdl,
        test_principal: Principal,
        known_roles: Collection[str] | None = None,
    ) -> PolicyValidation:
        """Run ``validate_policy`` with the known attributes; raise ``PolicyRejected`` on errors."""
        validation = validate_policy(
            policy, mdl, test_principal, self._repository.known_attribute_keys(), known_roles
        )
        if not validation.ok:
            raise PolicyRejected(validation)
        return validation


def _event(
    actor_id: str,
    action: str,
    project_id: str,
    policy: RowPolicy,
    enabled: bool,
    test_principal: Principal,
    policy_id: str = "",
    **extra: object,
) -> AuditEvent:
    """Audit event for a policy change; ``extra`` is merged into the details."""
    return AuditEvent(
        actor_id=actor_id,
        action=action,
        resource_type="row_policy",
        resource_id=policy_id,
        details={
            "project_id": project_id,
            "name": policy.name,
            "model": policy.model,
            "predicate": policy.predicate,
            "appliesTo": policy.applies_to.model_dump(by_alias=True),
            "enabled": enabled,
            "test_user_id": test_principal.user_id,
            **extra,
        },
    )
