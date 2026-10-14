"""The behaviour every identity adapter (LDAP, OIDC, whatever comes next) must share.

A harness wraps one adapter and its fake backend; ``IdentityContract`` holds the assertions.
Subclass it in a ``Test*`` class that provides ``harness()``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

import pytest

from app.core.errors import PortUnavailable
from app.core.types import JsonValue
from app.ports.identity import VerifiedIdentity


class IdentityHarness(Protocol):
    source: str

    def add_user(
        self,
        username: str,
        secret: str,
        groups: tuple[str, ...],
        attributes: Mapping[str, JsonValue],
    ) -> None: ...

    def verify(self, username: str, secret: str) -> VerifiedIdentity | None:
        """Run the adapter's full login for these credentials; None when it refuses them."""
        ...

    def break_backend(self) -> None: ...


class IdentityContract:
    def harness(self) -> IdentityHarness:  # pragma: no cover - overridden
        raise NotImplementedError

    def test_valid_credentials_yield_a_verified_identity(self) -> None:
        h = self.harness()
        h.add_user("casey", "right-secret", ("auditors", "emea"), {"region": "West"})
        identity = h.verify("casey", "right-secret")
        assert identity is not None
        assert identity.source == h.source
        assert identity.username == "casey"
        assert identity.subject
        assert {"auditors", "emea"} <= set(identity.groups)
        assert identity.attributes["region"] == "West"

    def test_usernames_are_casefolded_for_the_local_account(self) -> None:
        h = self.harness()
        h.add_user("Mixed.Case", "right-secret", (), {})
        identity = h.verify("Mixed.Case", "right-secret")
        assert identity is not None and identity.username == "mixed.case"

    def test_a_person_without_groups_is_still_verified_with_no_groups(self) -> None:
        h = self.harness()
        h.add_user("mina", "right-secret", (), {})
        identity = h.verify("mina", "right-secret")
        assert identity is not None and identity.groups == ()

    def test_wrong_proof_and_unknown_people_are_refused_without_raising(self) -> None:
        h = self.harness()
        h.add_user("casey", "right-secret", ("auditors",), {})
        assert h.verify("casey", "wrong-secret") is None
        assert h.verify("ghost", "right-secret") is None

    def test_the_identity_never_carries_the_credential(self) -> None:
        h = self.harness()
        h.add_user("casey", "right-secret-xyz", ("auditors",), {})
        identity = h.verify("casey", "right-secret-xyz")
        assert identity is not None
        assert "right-secret-xyz" not in repr(identity)

    def test_an_unreachable_provider_raises_a_port_error_not_a_login(self) -> None:
        h = self.harness()
        h.add_user("casey", "right-secret", ("auditors",), {})
        h.break_backend()
        with pytest.raises(PortUnavailable):
            h.verify("casey", "right-secret")
