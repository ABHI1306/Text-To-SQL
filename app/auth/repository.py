from datetime import datetime
from typing import Protocol

from app.auth.models import AuthContext, AuthUser


class AuthRepository(Protocol):
    def get_user_by_username(self, username: str) -> AuthUser | None: ...

    def get_auth_context(self, user_id: str) -> AuthContext | None: ...

    def record_login_failure(
        self,
        user_id: str,
        *,
        now: datetime,
        max_failures: int,
        window_seconds: int,
        lockout_seconds: int,
    ) -> None: ...

    def clear_login_failures(self, user_id: str) -> None: ...

    def create_session(
        self,
        *,
        session_id: str,
        user_id: str,
        family_id: str,
        refresh_token_hash: str,
        expires_at: datetime,
    ) -> None: ...

    def rotate_refresh_token(
        self,
        *,
        old_token_hash: str,
        new_session_id: str,
        new_token_hash: str,
        new_expires_at: datetime,
        now: datetime,
    ) -> tuple[str, str | None, str | None]: ...

    def revoke_refresh_token(self, refresh_token_hash: str, now: datetime) -> None: ...

    def revoke_other_sessions(self, user_id: str, keep_session_id: str, now: datetime) -> None: ...

    def change_password(
        self, user_id: str, password_hash: str, keep_session_id: str, now: datetime
    ) -> None: ...

    def append_auth_audit(
        self, actor_id: str | None, action: str, username: str, details: dict[str, object]
    ) -> None: ...

    def bootstrap_dba(self, username: str, password_hash: str) -> str: ...

    def create_local_user(self, username: str, role_name: str, password_hash: str) -> str: ...

    def reset_local_user(self, username: str, password_hash: str) -> str: ...
