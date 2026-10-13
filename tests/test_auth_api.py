from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dependencies import get_auth_service, get_current_context, get_repository
from app.api.routes import router
from app.auth.models import AuthContext, AuthUser
from app.auth.service import AuthService, hash_password


class LoginRepository:
    def __init__(self) -> None:
        self.user = AuthUser(
            user_id="00000000-0000-0000-0000-000000000001",
            username="demo",
            role_name="DBA",
            password_hash=hash_password("temporary-strong-password"),
            status="active",
            must_change_password=True,
            locked_until=None,
        )
        self.context = AuthContext(
            user_id=self.user.user_id,
            username=self.user.username,
            role_name=self.user.role_name,
            capabilities=frozenset(),
            project_ids=frozenset(),
            must_change_password=True,
        )

    def get_user_by_username(self, username: str) -> AuthUser | None:
        return self.user if username == self.user.username else None

    def get_auth_context(self, user_id: str) -> AuthContext | None:
        return self.context if user_id == self.context.user_id else None

    def clear_login_failures(self, user_id: str) -> None:
        del user_id

    def create_session(
        self,
        *,
        session_id: str,
        user_id: str,
        family_id: str,
        refresh_token_hash: str,
        expires_at: datetime,
    ) -> None:
        del session_id, user_id, family_id, refresh_token_hash, expires_at

    def append_auth_audit(
        self, actor_id: str | None, action: str, username: str, details: dict[str, object]
    ) -> None:
        del actor_id, action, username, details

    def record_login_failure(
        self,
        user_id: str,
        *,
        now: datetime,
        max_failures: int,
        window_seconds: int,
        lockout_seconds: int,
    ) -> None:
        del user_id, now, max_failures, window_seconds, lockout_seconds

    def revoke_refresh_token(self, refresh_token_hash: str, now: datetime) -> None:
        del refresh_token_hash, now


def test_login_sets_http_only_refresh_cookie_and_returns_forced_change_flag(
    monkeypatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    repository = LoginRepository()
    service = AuthService(
        repository,
        jwt_secret="unit-test-secret-that-is-at-least-32-bytes",
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_auth_service] = lambda: service

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "demo", "password": "temporary-strong-password"},
        )

    assert response.status_code == 200
    assert response.json()["must_change_password"] is True
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "path=/api/v1/auth" in cookie


def test_login_does_not_disclose_unknown_username(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    repository = LoginRepository()
    service = AuthService(
        repository,
        jwt_secret="unit-test-secret-that-is-at-least-32-bytes",
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_auth_service] = lambda: service

    with TestClient(app) as client:
        unknown = client.post(
            "/api/v1/auth/login",
            json={"username": "missing", "password": "temporary-strong-password"},
        )
        wrong_password = client.post(
            "/api/v1/auth/login",
            json={"username": "demo", "password": "incorrect-password"},
        )

    assert unknown.status_code == wrong_password.status_code == 401
    assert unknown.json() == wrong_password.json()


def test_main_openapi_includes_auth_and_setup_routes() -> None:
    app = FastAPI()
    app.include_router(router)
    paths = app.openapi()["paths"]
    assert "/api/v1/auth/refresh" in paths
    assert "/api/v1/projects/{project_id}/selected-tables" in paths
    assert "/api/v1/projects/{project_id}/introspect" in paths
    assert "/api/v1/projects/{project_id}/deploy" in paths
    assert "/api/v1/projects/{project_id}/query" in paths
    assert "/api/v1/roles" in paths
    assert "/api/v1/sample-projects/{template_id}" in paths


def test_role_routes_enforce_manage_roles_capability() -> None:
    class RoleRepository:
        def list_roles(self) -> list[dict[str, object]]:
            return [{"id": "role-1", "name": "User", "capabilities": []}]

    app = FastAPI()
    app.include_router(router)
    context = AuthContext(
        user_id="00000000-0000-0000-0000-000000000001",
        username="demo",
        role_name="User",
        capabilities=frozenset(),
        project_ids=frozenset(),
        must_change_password=False,
    )
    app.dependency_overrides[get_current_context] = lambda: context
    app.dependency_overrides[get_repository] = lambda: RoleRepository()

    with TestClient(app) as client:
        denied = client.get("/api/v1/roles")
        context_with_capability = AuthContext(
            user_id=context.user_id,
            username=context.username,
            role_name="DBA",
            capabilities=frozenset({"manage_roles"}),
            project_ids=frozenset(),
            must_change_password=False,
        )
        app.dependency_overrides[get_current_context] = lambda: context_with_capability
        allowed = client.get("/api/v1/roles")

    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert allowed.json()["items"][0]["name"] == "User"


def test_user_manager_cannot_assign_a_role_with_unheld_capabilities() -> None:
    class RoleRepository:
        def get_role_capabilities(self, role_name: str) -> frozenset[str] | None:
            return frozenset({"manage_users", "manage_roles", "deploy_mdl"})

        def set_user_role(self, user_id: str, role_name: str) -> None:
            raise AssertionError("Escalating assignment must be refused")

    app = FastAPI()
    app.include_router(router)
    context = AuthContext(
        user_id="00000000-0000-0000-0000-000000000001",
        username="manager",
        role_name="Custom manager",
        capabilities=frozenset({"manage_users"}),
        project_ids=frozenset(),
        must_change_password=False,
    )
    app.dependency_overrides[get_current_context] = lambda: context
    app.dependency_overrides[get_repository] = lambda: RoleRepository()

    with TestClient(app) as client:
        response = client.put(
            "/api/v1/users/00000000-0000-0000-0000-000000000002/role",
            json={"role": "Administrator"},
        )

    assert response.status_code == 403
