"""OIDC login orchestration: one-time state, then the same local session as any other login.

``begin`` stores the nonce and PKCE verifier under the SHA-256 of a random ``state`` (the raw
state only ever lives in the redirect URL and a short-lived browser cookie). ``complete``
consumes that row atomically, so a state can be used once, and only until it expires.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Protocol

from app.auth.models import LoginResult
from app.auth.service import AuthenticationError, AuthService
from app.ports.identity import (
    AuthorizationRequest,
    FederatedIdentityPort,
    IdentityRejected,
    IdentityUnavailable,
)

STATE_TTL_SECONDS = 600
MAX_PENDING_LOGINS = 10_000


class OidcStateStore(Protocol):
    def put(
        self, state_hash: str, nonce: str, code_verifier: str, expires_at: datetime
    ) -> bool:
        """Store a pending login; False when the store is full (callers refuse the login)."""
        ...

    def consume(self, state_hash: str, now: datetime) -> tuple[str, str] | None:
        """Atomically remove and return (nonce, code_verifier) if present and unexpired."""
        ...


class InMemoryOidcStateStore:
    """Single-process store; use the Postgres store when more than one API replica runs."""

    def __init__(self, capacity: int = MAX_PENDING_LOGINS) -> None:
        self._capacity = capacity
        self._rows: dict[str, tuple[str, str, datetime]] = {}
        self._lock = Lock()

    def put(
        self, state_hash: str, nonce: str, code_verifier: str, expires_at: datetime
    ) -> bool:
        now = datetime.now(UTC)
        with self._lock:
            for key in [k for k, row in self._rows.items() if row[2] <= now]:
                del self._rows[key]
            if len(self._rows) >= self._capacity:
                return False
            self._rows[state_hash] = (nonce, code_verifier, expires_at)
            return True

    def consume(self, state_hash: str, now: datetime) -> tuple[str, str] | None:
        with self._lock:
            row = self._rows.pop(state_hash, None)
        if row is None or row[2] <= now:
            return None
        return row[0], row[1]


def state_hash(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


class OidcLoginService:
    def __init__(
        self,
        provider: FederatedIdentityPort,
        store: OidcStateStore,
        auth: AuthService,
        *,
        ttl_seconds: int = STATE_TTL_SECONDS,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._provider = provider
        self._store = store
        self._auth = auth
        self._ttl = ttl_seconds
        self._clock = clock

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    def begin(self, client_key: str) -> AuthorizationRequest:
        if not self._auth.external_login_allowed(client_key):
            raise AuthenticationError("Too many sign-in attempts")
        if not self._auth.external_begin_allowed(client_key):
            raise AuthenticationError("Too many sign-in attempts")
        try:
            request = self._provider.begin()
        except IdentityUnavailable as error:
            self._auth.record_external_failure("oidc", "provider_unavailable", client_key)
            raise AuthenticationError("Single sign-on is unavailable") from error
        stored = self._store.put(
            state_hash(request.state),
            request.nonce,
            request.code_verifier,
            self._clock() + timedelta(seconds=self._ttl),
        )
        if not stored:
            raise AuthenticationError("Too many sign-in attempts")
        return request

    def complete(
        self,
        *,
        code: str | None,
        state: str | None,
        browser_state: str | None,
        client_key: str,
    ) -> LoginResult:
        def refuse(reason: str, error: Exception | None = None) -> AuthenticationError:
            self._auth.record_external_failure("oidc", reason, client_key)
            failure = AuthenticationError("Single sign-on failed")
            failure.__cause__ = error
            return failure

        if not self._auth.external_login_allowed(client_key):
            raise AuthenticationError("Too many sign-in attempts")
        if not state or not code:
            raise refuse("missing_parameters")
        # The cookie ties the callback to the browser that started the login (login CSRF).
        if not browser_state or not hmac.compare_digest(state.encode(), browser_state.encode()):
            raise refuse("state_cookie_mismatch")
        pending = self._store.consume(state_hash(state), self._clock())
        if pending is None:
            raise refuse("state_unknown_or_expired")
        nonce, verifier = pending
        try:
            identity = self._provider.complete(code=code, nonce=nonce, code_verifier=verifier)
        except IdentityRejected as error:
            raise refuse("token_rejected", error) from error
        except IdentityUnavailable as error:
            raise refuse("provider_unavailable", error) from error
        return self._auth.login_external(identity, client_key)

