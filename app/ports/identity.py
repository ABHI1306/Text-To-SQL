"""Ports and value types for password, directory and federated (OIDC) identity."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from app.core.errors import PortUnavailable
from app.core.types import JsonValue, Principal


class IdentityPort(Protocol):
    """Resolves who a caller is as a ``Principal``."""
    def authenticate_password(
        self, username: str, password: str
    ) -> Principal | None:
        """Return the principal for a correct username and password, else None."""
        ...

    def resolve_external_identity(
        self,
        provider: str,
        subject: str,
        claims: Mapping[str, JsonValue],
    ) -> Principal:
        """Map an already verified external identity to a local ``Principal``."""
        ...


@dataclass(frozen=True)
class VerifiedIdentity:
    """What every external identity adapter (LDAP, OIDC) hands to the local login.

    Adapters have already proven the identity; the auth service only maps groups to a role and
    opens the same local session as a password login. No credential is ever part of this value.
    """

    source: str  # matches identity_mappings.source: 'ldap' or 'oidc'
    username: str  # casefolded local account name
    subject: str  # stable id at the provider (LDAP DN, OIDC "sub")
    groups: tuple[str, ...]
    attributes: Mapping[str, JsonValue] = field(default_factory=dict)


class IdentityUnavailable(PortUnavailable):
    """The identity provider could not be reached or answered with a protocol error."""


class IdentityRejected(ValueError):
    """The provider's proof (ID token, code, state) failed validation; never carries details."""


@dataclass(frozen=True)
class AuthorizationRequest:
    """What the browser needs to start a redirect login; the other fields are kept to finish it."""
    url: str
    state: str
    nonce: str
    code_verifier: str


class FederatedIdentityPort(Protocol):
    """Redirect-based login (OIDC authorization code flow with PKCE)."""

    source: str

    def begin(self) -> AuthorizationRequest:
        """Start a login and return the redirect URL with its state, nonce and PKCE verifier."""
        ...

    def complete(self, *, code: str, nonce: str, code_verifier: str) -> VerifiedIdentity:
        """Exchange the code and validate the result.

        Raises IdentityRejected for any validation failure and IdentityUnavailable when the
        provider cannot be reached.
        """
        ...
