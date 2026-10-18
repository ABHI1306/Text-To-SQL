"""Row-level security policy model."""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

POLICY_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
ATTRIBUTE_PLACEHOLDER = re.compile(r"\{\{\s*user\.([a-z][a-z0-9_]*)\s*\}\}")


class OnMissingAttribute(StrEnum):
    """What to do when a user lacks an attribute the predicate needs; only deny exists."""
    DENY = "deny"


class PolicyScope(BaseModel):
    """Which roles a policy applies to, and which are exempt from it."""
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    roles: list[str] = Field(default_factory=list)
    exempt_roles: list[str] = Field(default_factory=list, alias="exemptRoles")


class RowPolicy(BaseModel):
    """A row-level predicate template attached to one semantic model.

    An empty ``appliesTo.roles`` means every role. No role is exempt unless listed.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    name: str
    model: str
    predicate: str
    applies_to: PolicyScope = Field(default_factory=PolicyScope, alias="appliesTo")
    on_missing_attribute: OnMissingAttribute = Field(
        default=OnMissingAttribute.DENY, alias="onMissingAttribute"
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        """Require a lowercase identifier name."""
        if not POLICY_NAME_PATTERN.fullmatch(value):
            raise ValueError("policy name must match [a-z][a-z0-9_]*")
        return value

    @field_validator("predicate")
    @classmethod
    def validate_predicate_not_blank(cls, value: str) -> str:
        """Reject an empty or whitespace-only predicate."""
        if not value.strip():
            raise ValueError("predicate must not be blank")
        return value

    def applies_to_role(self, role_name: str) -> bool:
        """Whether the policy applies to ``role_name`` (case-insensitive); exemptions win."""
        role = role_name.casefold()
        if role in {name.casefold() for name in self.applies_to.exempt_roles}:
            return False
        roles = {name.casefold() for name in self.applies_to.roles}
        return not roles or role in roles

    def attributes(self) -> tuple[str, ...]:
        """Attribute names referenced by the predicate template, in order of first use."""
        return tuple(dict.fromkeys(ATTRIBUTE_PLACEHOLDER.findall(self.predicate)))
