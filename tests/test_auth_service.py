from dataclasses import replace
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.auth.models import AuthContext, AuthUser
from app.auth.service import (
    AuthenticationError,
    AuthService,
    PasswordChangeRequired,
    PasswordPolicyError,
    TokenError,
    hash_password,
)


class MemoryAuthRepository:
    def __init__(self, *, must_change_password: bool = False) -> None:
        password_hash = hash_password("temporary-strong-password")
        self.user = AuthUser(
            "user-1",
            "demo",
            "DBA",
            password_hash,
            "active",
            must_change_password,
            None,
        )
        self.context = AuthContext(
            "user-1",
            "demo",
            "DBA",
            frozenset({"manage_connection", "edit_mdl"}),
            frozenset(),
            must_change_password,
        )
        self.sessions: dict[str, dict[str, object]] = {}
        self.audits: list[tuple[str, str | None, str, dict[str, object]]] = []
        self.failed_logins: list[dict[str, object]] = []
        self.rotated_family: str | None = None

    def get_user_by_username(self, username: str) -> AuthUser | None:
        return self.user if username == self.user.username else None

    def get_auth_context(self, user_id: str) -> AuthContext | None:
        return self.context if user_id == self.context.user_id else None

    def record_login_failure(
        self,
        user_id: str,
        *,
        now: datetime,
        max_failures: int,
        window_seconds: int,
        lockout_seconds: int,
    ) -> None:
        self.failed_logins.append(
            {
                "user_id": user_id,
                "now": now,
                "max_failures": max_failures,
                "window_seconds": window_seconds,
                "lockout_seconds": lockout_seconds,
            }
        )

    def clear_login_failures(self, user_id: str) -> None:
        del user_id

    def session_is_alive(self, session_id: str, now: datetime) -> bool:
        family = next(
            (s["family_id"] for s in self.sessions.values() if s["session_id"] == session_id), None
        )
        return any(
            s["family_id"] == family and not s["revoked"] and s["expires_at"] > now
            for s in self.sessions.values()
        )

    def create_session(
        self,
        *,
        session_id: str,
        user_id: str,
        family_id: str,
        refresh_token_hash: str,
        expires_at: datetime,
    ) -> None:
        self.sessions[refresh_token_hash] = {
            "session_id": session_id,
            "user_id": user_id,
            "family_id": family_id,
            "expires_at": expires_at,
            "revoked": False,
        }

    def rotate_refresh_token(
        self,
        *,
        old_token_hash: str,
        new_session_id: str,
        new_token_hash: str,
        new_expires_at: datetime,
        now: datetime,
    ) -> tuple[str, str | None, str | None]:
        old = self.sessions.get(old_token_hash)
        if old is None:
            return "invalid", None, None
        if old["revoked"]:
            self.rotated_family = str(old["family_id"])
            for session in self.sessions.values():
                if session["family_id"] == self.rotated_family:
                    session["revoked"] = True
            return "reused", str(old["user_id"]), str(old["session_id"])
        if old["expires_at"] <= now:
            return "expired", str(old["user_id"]), str(old["session_id"])
        old["revoked"] = True
        user_id = str(old["user_id"])
        family_id = str(old["family_id"])
        self.sessions[new_token_hash] = {
            "session_id": new_session_id,
            "user_id": user_id,
            "family_id": family_id,
            "expires_at": new_expires_at,
            "revoked": False,
        }
        return "rotated", user_id, str(old["session_id"])

    def revoke_refresh_token(self, refresh_token_hash: str, now: datetime) -> None:
        del now
        session = self.sessions.get(refresh_token_hash)
        if session:
            session["revoked"] = True

    def revoke_other_sessions(self, user_id: str, keep_session_id: str, now: datetime) -> None:
        del user_id, keep_session_id, now

    def change_password(
        self, user_id: str, password_hash: str, keep_session_id: str, now: datetime
    ) -> None:
        del keep_session_id, now
        self.user = replace(
            self.user,
            password_hash=password_hash,
            must_change_password=False,
        )
        self.context = replace(self.context, must_change_password=False)

    def append_auth_audit(
        self, actor_id: str | None, action: str, username: str, details: dict[str, object]
    ) -> None:
        self.audits.append((action, actor_id, username, details))

    def bootstrap_dba(self, username: str, password_hash: str) -> str:
        del username, password_hash
        return "user-1"

    def create_local_user(self, username: str, role_name: str, password_hash: str) -> str:
        del username, role_name, password_hash
        return "user-1"

    def reset_local_user(self, username: str, password_hash: str) -> str:
        del username, password_hash
        return "user-1"


@pytest.fixture
def repository() -> MemoryAuthRepository:
    return MemoryAuthRepository()


@pytest.fixture
def auth_service(repository: MemoryAuthRepository) -> AuthService:
    return AuthService(
        repository,
        jwt_secret="unit-test-secret-that-is-at-least-32-bytes",
        access_token_ttl_seconds=60,
        max_failures=2,
    )


def test_login_uses_generic_error_for_unknown_users_and_wrong_password(
    auth_service: AuthService,
) -> None:
    with pytest.raises(AuthenticationError, match="Invalid username or password"):
        auth_service.login("missing", "not-the-password", "127.0.0.1")
    with pytest.raises(AuthenticationError, match="Invalid username or password"):
        auth_service.login("demo", "not-the-password", "127.0.0.1")


def test_login_lockout_after_configured_failed_attempts(
    auth_service: AuthService, repository: MemoryAuthRepository
) -> None:
    for _ in range(2):
        with pytest.raises(AuthenticationError):
            auth_service.login("demo", "wrong-password", "127.0.0.2")
    assert len(repository.failed_logins) == 2
    assert repository.failed_logins[-1]["max_failures"] == 2


def test_login_issues_access_token_and_refresh_rotation_revokes_reused_family(
    auth_service: AuthService,
) -> None:
    result = auth_service.login("demo", "temporary-strong-password", "127.0.0.3")
    context = auth_service.authenticate_access_token(result.access_token)
    assert context.user_id == "user-1"
    rotated = auth_service.refresh(result.refresh_token)
    assert rotated.refresh_token != result.refresh_token

    with pytest.raises(AuthenticationError, match="sign in again"):
        auth_service.refresh(result.refresh_token)
    with pytest.raises(AuthenticationError):
        auth_service.refresh(rotated.refresh_token)


def test_temporary_password_token_is_limited_to_password_change(
    repository: MemoryAuthRepository,
) -> None:
    repository.context = AuthContext(
        "user-1", "demo", "DBA", frozenset(), frozenset(), True
    )
    service = AuthService(repository, jwt_secret="unit-test-secret-that-is-at-least-32-bytes")
    result = service.login("demo", "temporary-strong-password", "127.0.0.4")
    with pytest.raises(PasswordChangeRequired):
        service.authenticate_access_token(result.access_token)
    assert service.authenticate_access_token(
        result.access_token, allow_password_change=True
    ).must_change_password


def test_password_change_invalidates_cached_temporary_password_context(
    repository: MemoryAuthRepository,
) -> None:
    repository.context = replace(repository.context, must_change_password=True)
    repository.user = replace(repository.user, must_change_password=True)
    service = AuthService(
        repository,
        jwt_secret="unit-test-secret-that-is-at-least-32-bytes",
    )
    result = service.login("demo", "temporary-strong-password", "127.0.0.4")
    context = service.authenticate_access_token(
        result.access_token,
        allow_password_change=True,
    )

    service.change_password(
        context,
        context.session_id or "",
        "temporary-strong-password",
        "a-new-strong-password",
    )

    updated = service.authenticate_access_token(result.access_token)
    assert not updated.must_change_password
    assert not service.login("demo", "a-new-strong-password", "127.0.0.4").user.must_change_password


def test_invalidating_auth_context_refreshes_project_access(
    repository: MemoryAuthRepository,
) -> None:
    service = AuthService(
        repository,
        jwt_secret="unit-test-secret-that-is-at-least-32-bytes",
    )
    result = service.login("demo", "temporary-strong-password", "127.0.0.4")
    assert not service.authenticate_access_token(result.access_token).project_ids

    repository.context = replace(repository.context, project_ids=frozenset({"project-1"}))
    service.invalidate_auth_context("user-1")

    assert service.authenticate_access_token(result.access_token).project_ids == {
        "project-1"
    }


def test_password_policy_rejects_short_password(auth_service: AuthService) -> None:
    with pytest.raises(PasswordPolicyError):
        auth_service.create_password_hash("too-short")


def test_expired_access_tokens_are_rejected(repository: MemoryAuthRepository) -> None:
    service = AuthService(repository, jwt_secret="unit-test-secret-that-is-at-least-32-bytes")
    expired = jwt.encode(
        {
            "sub": "user-1",
            "jti": "expired",
            "iat": int((datetime.now(UTC) - timedelta(minutes=5)).timestamp()),
            "exp": int((datetime.now(UTC) - timedelta(minutes=1)).timestamp()),
            "iss": "text2sql-local",
        },
        "unit-test-secret-that-is-at-least-32-bytes",
        algorithm="HS256",
    )
    with pytest.raises(TokenError, match="invalid or expired"):
        service.authenticate_access_token(expired)


def test_authorization_context_cache_expires_within_one_minute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.auth.service as auth_service_module

    repository = MemoryAuthRepository()
    reads = 0
    original_get_context = repository.get_auth_context

    def get_context(user_id: str) -> AuthContext | None:
        nonlocal reads
        reads += 1
        return original_get_context(user_id)

    repository.get_auth_context = get_context  # type: ignore[method-assign]
    service = AuthService(
        repository,
        jwt_secret="unit-test-secret-that-is-at-least-32-bytes",
    )
    clock = 100.0
    monkeypatch.setattr(auth_service_module.time, "monotonic", lambda: clock)
    assert service._cached_auth_context("user-1") is not None
    assert service._cached_auth_context("user-1") is not None
    assert reads == 1

    clock = 130.1
    assert service._cached_auth_context("user-1") is not None
    assert reads == 2


def test_logout_ends_the_access_token_immediately() -> None:
    repository = MemoryAuthRepository()
    service = AuthService(repository, jwt_secret="unit-test-secret-that-is-at-least-32-bytes")
    result = service.login("demo", "temporary-strong-password", "127.0.0.1")
    assert service.authenticate_access_token(result.access_token).user_id == "user-1"

    service.logout(result.refresh_token)

    with pytest.raises(TokenError):
        service.authenticate_access_token(result.access_token)


def test_one_address_is_not_locked_out_by_a_few_failures_but_one_account_is() -> None:
    repository = MemoryAuthRepository()
    service = AuthService(
        repository, jwt_secret="unit-test-secret-that-is-at-least-32-bytes", max_failures=5
    )
    for _ in range(5):
        with pytest.raises(AuthenticationError):
            service.login("demo", "wrong-password", "10.0.0.1")
    # The account is now throttled, even for the right password...
    with pytest.raises(AuthenticationError):
        service.login("demo", "temporary-strong-password", "10.0.0.1")
    # ...but another account from the same address (a shared proxy) is not.
    with pytest.raises(AuthenticationError):
        service.login("someone-else", "x", "10.0.0.1")
    assert service._limiter.allow(("ip:10.0.0.1",))


def test_starting_single_sign_on_is_limited_per_address() -> None:
    service = AuthService(
        MemoryAuthRepository(), jwt_secret="unit-test-secret-that-is-at-least-32-bytes"
    )
    assert all(service.external_begin_allowed("10.0.0.2") for _ in range(20))
    assert not service.external_begin_allowed("10.0.0.2")
    assert service.external_begin_allowed("10.0.0.3")
