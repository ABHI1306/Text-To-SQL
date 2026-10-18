"""Authentication: password and directory login, refresh rotation, access tokens and limits."""

from __future__ import annotations

import hashlib
import secrets
import time
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.auth.identity import IdentityRepository, resolve_role
from app.auth.models import AuthContext, LoginResult
from app.auth.repository import AuthRepository
from app.core.errors import PortError
from app.core.types import JsonValue
from app.ports.directory import DirectoryPort
from app.ports.identity import VerifiedIdentity

MIN_PASSWORD_LENGTH = 12
_ISSUER = "text2sql-local"
# A clock stepped back by NTP must not reject a token this server issued seconds ago.
CLOCK_SKEW_LEEWAY_SECONDS = 30


class AuthenticationError(ValueError):
    """Raised for generic authentication failures."""


class PasswordChangeRequired(AuthenticationError):
    """Raised when a temporary password is used outside change-password."""


class TokenError(AuthenticationError):
    """Raised when an access token is invalid or expired."""


class PasswordPolicyError(ValueError):
    """Raised when a new password does not satisfy policy."""


class LocalRateLimiter:
    """In-process sliding-window limiter for failed attempts, keyed per address or account."""
    def __init__(
        self, *, max_failures: int, window_seconds: int, address_multiplier: int = 1
    ) -> None:
        """Set the failure budget per window; ``address_multiplier`` widens it for ``ip:`` keys."""
        self._max_failures = max_failures
        # Behind a proxy many people share one address, so an address may fail more often than
        # one account before it is refused; the per-account limit is what stops guessing.
        self._address_multiplier = address_multiplier
        self._window_seconds = window_seconds
        self._attempts: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def allow(self, keys: tuple[str, ...], now: float | None = None) -> bool:
        """True while every key is still under its limit; expired attempts are dropped first."""
        current = time.monotonic() if now is None else now
        with self._lock:
            for key in keys:
                attempts = self._attempts[key]
                while attempts and attempts[0] <= current - self._window_seconds:
                    attempts.popleft()
                limit = self._max_failures * (
                    self._address_multiplier if key.startswith("ip:") else 1
                )
                if len(attempts) >= limit:
                    return False
            return True

    def record_failure(self, keys: tuple[str, ...], now: float | None = None) -> None:
        """Record one failed attempt against each key."""
        current = time.monotonic() if now is None else now
        with self._lock:
            for key in keys:
                attempts = self._attempts[key]
                while attempts and attempts[0] <= current - self._window_seconds:
                    attempts.popleft()
                attempts.append(current)

    def clear(self, keys: tuple[str, ...]) -> None:
        """Forget all recorded attempts for the keys (after a successful login)."""
        with self._lock:
            for key in keys:
                self._attempts.pop(key, None)


class AuthService:
    """Issues and checks sessions; every refusal is generic so callers cannot probe accounts."""
    def __init__(
        self,
        repository: AuthRepository,
        *,
        jwt_secret: str,
        access_token_ttl_seconds: int = 900,
        refresh_token_ttl_seconds: int = 28_800,
        max_failures: int = 5,
        failure_window_seconds: int = 900,
        lockout_seconds: int = 900,
        directory: DirectoryPort | None = None,
        identity_repository: IdentityRepository | None = None,
    ) -> None:
        """Configure token lifetimes and lockout; the JWT secret must be at least 32 characters."""
        if len(jwt_secret) < 32:
            raise ValueError("JWT_SECRET must contain at least 32 characters")
        self._repository = repository
        self._directory = directory
        self._identity = identity_repository
        self._jwt_secret = jwt_secret
        self._access_ttl = access_token_ttl_seconds
        self._refresh_ttl = refresh_token_ttl_seconds
        self._max_failures = max_failures
        self._window_seconds = failure_window_seconds
        self._lockout_seconds = lockout_seconds
        self._hasher = PasswordHasher()
        self._dummy_hash = self._hasher.hash(secrets.token_urlsafe(32))
        self._limiter = LocalRateLimiter(
            max_failures=max_failures,
            window_seconds=failure_window_seconds,
            address_multiplier=10,
        )
        # Starting a single sign-on attempt costs a stored row, so it is limited per address.
        self._begin_limiter = LocalRateLimiter(max_failures=20, window_seconds=60)
        self._context_cache: dict[str, tuple[float, AuthContext]] = {}
        self._context_cache_lock = Lock()

    def login(self, username: str, password: str, client_key: str) -> LoginResult:
        """Verify a local or LDAP login and open a session.

        A dummy hash is verified for unknown users so timing does not reveal which usernames
        exist; failures are rate-limited, counted toward lockout and audited.
        """
        normalized_username = username.strip().casefold()
        rate_keys = (f"ip:{client_key}", f"user:{normalized_username}")
        now = datetime.now(UTC)
        if not self._limiter.allow(rate_keys):
            self._repository.append_auth_audit(
                None, "login_rate_limited", normalized_username, {"source": "local"}
            )
            raise AuthenticationError("Invalid username or password")

        user = self._repository.get_user_by_username(normalized_username)
        if self._directory is not None and (user is None or user.auth_source == "ldap"):
            return self._login_directory(normalized_username, password, rate_keys, user, now)
        valid_password = self._verify_password(
            user.password_hash if user and user.password_hash else self._dummy_hash,
            password,
        )
        if (
            user is None
            or not valid_password
            or user.status != "active"
            or (user.locked_until is not None and user.locked_until > now)
        ):
            self._limiter.record_failure(rate_keys)
            if user is not None and user.status == "active":
                self._repository.record_login_failure(
                    user.user_id,
                    now=now,
                    max_failures=self._max_failures,
                    window_seconds=self._window_seconds,
                    lockout_seconds=self._lockout_seconds,
                )
            self._repository.append_auth_audit(
                user.user_id if user else None,
                "login_failed",
                normalized_username,
                {"source": "local"},
            )
            raise AuthenticationError("Invalid username or password")

        self._limiter.clear(rate_keys)
        self._repository.clear_login_failures(user.user_id)
        return self._open_session(user.user_id, normalized_username, "local", now)

    def _login_directory(
        self,
        username: str,
        password: str,
        rate_keys: tuple[str, ...],
        user: Any,
        now: datetime,
    ) -> LoginResult:
        """LDAP login: the directory verifies the password; nothing about it is stored."""

        assert self._directory is not None
        try:
            identity = self._directory.authenticate(username, password)
        except PortError as error:
            raise self._refuse("ldap", username, rate_keys, "directory_unavailable") from error
        if identity is None:
            raise self._refuse(
                "ldap", username, rate_keys, "invalid_credentials", user.user_id if user else None
            )
        return self._finish_external(
            "ldap", username, identity.groups, identity.attributes, rate_keys, user, now
        )

    def login_external(self, identity: VerifiedIdentity, client_key: str) -> LoginResult:
        """Federated (OIDC) login: the provider has verified the person; open the local session.

        The role comes from the DBA-managed group mappings for the identity's source and is
        re-evaluated on every login, so a removed group takes effect at the next sign-in.
        """
        rate_keys = (f"ip:{client_key}", f"user:{identity.username}")
        if not self._limiter.allow(rate_keys):
            self._repository.append_auth_audit(
                None, "login_rate_limited", identity.username, {"source": identity.source}
            )
            raise AuthenticationError("Invalid username or password")
        user = self._repository.get_user_by_username(identity.username)
        return self._finish_external(
            identity.source, identity.username, identity.groups, identity.attributes,
            rate_keys, user, datetime.now(UTC),
            # An OIDC "sub" never changes for a person; a directory DN can, so only OIDC pins it.
            subject=identity.subject if identity.source == "oidc" else None,
        )

    def record_external_failure(self, source: str, reason: str, client_key: str) -> None:
        """Audit a refused federated login that never produced a verified identity."""
        self._limiter.record_failure((f"ip:{client_key}",))
        self._repository.append_auth_audit(
            None, "login_failed", source, {"source": source, "reason": reason}
        )

    def external_login_allowed(self, client_key: str) -> bool:
        """False once this address has failed too often to try a federated login."""
        return self._limiter.allow((f"ip:{client_key}",))

    def _refuse(
        self,
        source: str,
        username: str,
        rate_keys: tuple[str, ...],
        reason: str,
        actor: str | None = None,
    ) -> AuthenticationError:
        """Count and audit a refused external login and return the generic error to raise."""
        self._limiter.record_failure(rate_keys)
        self._repository.append_auth_audit(
            actor, "login_failed", username, {"source": source, "reason": reason}
        )
        return AuthenticationError("Invalid username or password")

    def _finish_external(
        self,
        source: str,
        username: str,
        groups: Sequence[str],
        attributes: Mapping[str, JsonValue],
        rate_keys: tuple[str, ...],
        user: Any,
        now: datetime,
        *,
        subject: str | None = None,
    ) -> LoginResult:
        """Map groups to a role, provision the account, check it is usable and open a session.

        Every refusal reason is audited but the caller only sees the generic error.
        """
        if self._identity is None:
            raise self._refuse(source, username, rate_keys, "identity_mappings_unavailable")
        role_name = resolve_role(groups, self._identity.get_identity_mappings(source))
        if role_name is None:
            raise self._refuse(
                source, username, rate_keys, "no_role_mapping", user.user_id if user else None
            )
        try:
            extra = {"subject": subject} if subject is not None else {}
            user_id = self._identity.provision_external_user(
                username, source, role_name, attributes, **extra
            )
        except ValueError as error:
            raise self._refuse(source, username, rate_keys, "account_conflict") from error
        refreshed = self._repository.get_user_by_username(username)
        if (
            refreshed is None
            or refreshed.status != "active"
            or (refreshed.locked_until is not None and refreshed.locked_until > now)
        ):
            raise self._refuse(source, username, rate_keys, "account_disabled", user_id)
        self.invalidate_auth_context(user_id)
        self._limiter.clear(rate_keys)
        return self._open_session(user_id, username, source, now)

    def _open_session(
        self, user_id: str, username: str, source: str, now: datetime
    ) -> LoginResult:
        """Create a session with a fresh refresh-token family and issue the access token."""
        context = self._repository.get_auth_context(user_id)
        if context is None:
            raise AuthenticationError("Invalid username or password")
        session_id = secrets.token_hex(16)
        family_id = secrets.token_hex(16)
        refresh_token = secrets.token_urlsafe(32)
        self._repository.create_session(
            session_id=session_id,
            user_id=user_id,
            family_id=family_id,
            refresh_token_hash=_token_hash(refresh_token),
            expires_at=now + timedelta(seconds=self._refresh_ttl),
        )
        access_token = self._create_access_token(context, session_id, now)
        self._repository.append_auth_audit(
            user_id, "login_succeeded", username, {"source": source}
        )
        return LoginResult(
            access_token,
            refresh_token,
            replace(context, session_id=session_id),
            self._access_ttl,
        )

    def refresh(self, refresh_token: str) -> LoginResult:
        """Rotate a refresh token and issue new tokens; an invalid or reused one is audited."""
        now = datetime.now(UTC)
        new_refresh_token = secrets.token_urlsafe(32)
        new_session_id = secrets.token_hex(16)
        status, user_id, session_id = self._repository.rotate_refresh_token(
            old_token_hash=_token_hash(refresh_token),
            new_session_id=new_session_id,
            new_token_hash=_token_hash(new_refresh_token),
            new_expires_at=now + timedelta(seconds=self._refresh_ttl),
            now=now,
        )
        if status != "rotated" or user_id is None:
            self._repository.append_auth_audit(
                user_id, "refresh_rejected", user_id or "unknown", {"reason": status}
            )
            raise AuthenticationError("Refresh token is invalid; sign in again")
        context = self._repository.get_auth_context(user_id)
        if context is None:
            raise AuthenticationError("Refresh token is invalid; sign in again")
        self._repository.append_auth_audit(
            user_id, "refresh_succeeded", user_id, {"session_id": session_id}
        )
        return LoginResult(
            self._create_access_token(context, new_session_id, now),
            new_refresh_token,
            replace(context, session_id=new_session_id),
            self._access_ttl,
        )

    def logout(self, refresh_token: str) -> None:
        """Revoke the session family that owns this refresh token."""
        now = datetime.now(UTC)
        self._repository.revoke_refresh_token(_token_hash(refresh_token), now)

    def authenticate_access_token(self, token: str, *, allow_password_change: bool = False) -> AuthContext:
        """Validate a bearer token and return the caller's context.

        Checks signature, issuer and required claims, that the session is still alive, and that a
        temporary password has been changed unless ``allow_password_change`` is set.
        """
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                self._jwt_secret,
                algorithms=["HS256"],
                issuer=_ISSUER,
                leeway=CLOCK_SKEW_LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "iss", "sub", "jti"]},
            )
        except jwt.InvalidTokenError as error:
            raise TokenError("Access token is invalid or expired") from error
        # A token dies with its session: logout, "sign out other sessions" and refresh-token
        # reuse revoke the session family, and the token must stop working at once.
        if not self._repository.session_is_alive(str(claims["jti"]), datetime.now(UTC)):
            raise TokenError("Access token is invalid or expired")
        context = self._cached_auth_context(str(claims["sub"]))
        if context is None:
            raise TokenError("Access token is invalid or expired")
        if context.must_change_password and not allow_password_change:
            raise PasswordChangeRequired("Change the temporary password before continuing")
        return replace(context, session_id=str(claims["jti"]))

    def _cached_auth_context(self, user_id: str) -> AuthContext | None:
        """The user's context from a 30 second cache, so most requests skip the database."""
        now = time.monotonic()
        with self._context_cache_lock:
            cached = self._context_cache.get(user_id)
            if cached is not None and cached[0] > now:
                return cached[1]
        context = self._repository.get_auth_context(user_id)
        with self._context_cache_lock:
            if context is None:
                self._context_cache.pop(user_id, None)
            else:
                self._context_cache[user_id] = (now + 30, context)
        return context

    def external_begin_allowed(self, client_key: str) -> bool:
        """Count one single sign-on start; False once an address starts too many per minute."""
        keys = (f"begin:{client_key}",)
        if not self._begin_limiter.allow(keys):
            return False
        self._begin_limiter.record_failure(keys)
        return True

    def invalidate_auth_context(self, user_id: str) -> None:
        """Drop one user's cached context so role or status changes apply immediately."""
        with self._context_cache_lock:
            self._context_cache.pop(user_id, None)

    def invalidate_all_auth_contexts(self) -> None:
        """After a change that can touch many accounts (a role's capabilities or projects)."""
        with self._context_cache_lock:
            self._context_cache.clear()

    def change_password(
        self,
        context: AuthContext,
        session_id: str,
        current_password: str,
        new_password: str,
    ) -> None:
        """Replace the password after verifying the current one; other sessions are revoked."""
        _validate_new_password(current_password, new_password)
        user = self._repository.get_user_by_username(context.username.casefold())
        if (
            user is None
            or user.user_id != context.user_id
            or user.password_hash is None
            or not self._verify_password(user.password_hash, current_password)
        ):
            self._repository.append_auth_audit(
                context.user_id,
                "password_change_failed",
                context.username,
                {},
            )
            raise AuthenticationError("Current password is incorrect")
        self._repository.change_password(
            context.user_id,
            self._hasher.hash(new_password),
            session_id,
            datetime.now(UTC),
        )
        self.invalidate_auth_context(context.user_id)
        self._repository.append_auth_audit(
            context.user_id, "password_changed", context.username, {}
        )

    def create_password_hash(self, password: str) -> str:
        """Hash a new password after enforcing the length policy."""
        return hash_password(password)

    def _create_access_token(
        self, context: AuthContext, session_id: str, now: datetime
    ) -> str:
        """Sign a short-lived HS256 token whose ``jti`` is the session id."""
        return jwt.encode(
            {
                "sub": context.user_id,
                "role": context.role_name,
                "jti": session_id,
                "iat": now,
                "exp": now + timedelta(seconds=self._access_ttl),
                "iss": _ISSUER,
            },
            self._jwt_secret,
            algorithm="HS256",
        )

    def _verify_password(self, password_hash: str, password: str) -> bool:
        """True only when the password matches; any hash error counts as a mismatch."""
        try:
            return self._hasher.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False


def _token_hash(token: str) -> str:
    """SHA-256 hex of a refresh token; only the hash is stored."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    """Argon2 hash of a password that meets the minimum length."""
    _validate_password_length(password)
    return PasswordHasher().hash(password)


def _validate_password_length(password: str) -> None:
    """Raise ``PasswordPolicyError`` when the password is shorter than the minimum."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError("Password must be at least 12 characters")


def _validate_new_password(current_password: str, new_password: str) -> None:
    """Require a long enough new password that differs from the current one."""
    _validate_password_length(new_password)
    if secrets.compare_digest(current_password, new_password):
        raise PasswordPolicyError("New password must differ from the current password")
