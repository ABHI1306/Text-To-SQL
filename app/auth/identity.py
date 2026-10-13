from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from app.core.types import JsonValue


class IdentityRepository(Protocol):
    def get_identity_mappings(self, source: str) -> list[tuple[str, str]]:
        """(match, role name) pairs in the order a DBA created them."""
        ...

    def provision_external_user(
        self,
        username: str,
        source: str,
        role_name: str,
        attributes: Mapping[str, JsonValue],
    ) -> str:
        """Create or refresh a directory account and return its id.

        Raises ValueError when the name belongs to an account from another source.
        """
        ...


def resolve_role(groups: Sequence[str], mappings: Sequence[tuple[str, str]]) -> str | None:
    """The role of the first mapping (in creation order) that matches one of the groups."""
    held = {group.casefold() for group in groups}
    for match, role_name in mappings:
        if match.casefold() in held:
            return role_name
    return None
