"""Persistence contract for accounts, sessions and refresh tokens used by ``AuthService``."""

from datetime import datetime
from typing import Protocol

from app.auth.models import AuthContext, AuthUser


class AuthRepository(Protocol):
    """Storage port for the auth service; implementations must be atomic where noted."""

    def get_user_by_username(self, username: str) -> AuthUser | None:
        """The account with this (casefolded) username, or None."""
        ...

    def get_auth_context(self, user_id: str) -> AuthContext | None:
        """The current role, capabilities, projects and attributes for the user, or None."""
        ...

    def record_login_failure(
        self,
        user_id: str,
        *,
        now: datetime,
        max_failures: int,
        window_seconds: int,
        lockout_seconds: int,
    ) -> None:
        """Count a failed login and lock the account once ``max_failures`` occur in the window."""
        ...

    def clear_login_failures(self, user_id: str) -> None:
        """Reset the failure counter and lock after a successful login."""
        ...

    def session_is_alive(self, session_id: str, now: datetime) -> bool:
        """True while the session's refresh-token family has an unrevoked, unexpired member."""
        ...

    def create_session(
        self,
        *,
        session_id: str,
        user_id: str,
        family_id: str,
        refresh_token_hash: str,
        expires_at: datetime,
    ) -> None:
        """Store a new session and the hash of its first refresh token."""
        ...

    def rotate_refresh_token(
        self,
        *,
        old_token_hash: str,
        new_session_id: str,
        new_token_hash: str,
        new_expires_at: datetime,
        now: datetime,
    ) -> tuple[str, str | None, str | None]:
        """Atomically swap a refresh token for a new one.

        Returns (status, user_id, session_id); only status ``rotated`` means success.
        """
        ...

    def revoke_refresh_token(self, refresh_token_hash: str, now: datetime) -> None:
        """Revoke the session family that owns this refresh-token hash."""
        ...

    def revoke_other_sessions(self, user_id: str, keep_session_id: str, now: datetime) -> None:
        """Revoke every session of the user except ``keep_session_id``."""
        ...

    def change_password(
        self, user_id: str, password_hash: str, keep_session_id: str, now: datetime
    ) -> None:
        """Store the new hash and revoke the user's other sessions."""
        ...

    def append_auth_audit(
        self, actor_id: str | None, action: str, username: str, details: dict[str, object]
    ) -> None:
        """Append an authentication event to the audit log."""
        ...

    def bootstrap_dba(self, username: str, password_hash: str) -> str:
        """Create the first DBA account and return its id."""
        ...

    def create_local_user(self, username: str, role_name: str, password_hash: str) -> str:
        """Create a local password account with the named role and return its id."""
        ...

    def reset_local_user(self, username: str, password_hash: str) -> str:
        """Set a new password for a local account and return its id."""
        ...
