from __future__ import annotations

from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import dependencies
from app.api.admin_routes import router
from app.auth.models import AuthContext

TARGET = "00000000-0000-0000-0000-0000000000bb"
PASSWORD = "a-new-temporary-password"


class Repo:
    def __init__(self, auth_source: str = "local", role: str = "User") -> None:
        self.unlocked: list[str] = []
        self.reset: list[str] = []
        self.users = [{"id": TARGET, "username": "maria", "role": role, "auth_source": auth_source}]
        self.roles = {"User": frozenset({"ask_question"}), "DBA": frozenset({"ask_question", "edit_mdl"})}

    def list_users(self) -> list[dict[str, object]]:
        return self.users

    def get_role_capabilities(self, role: str) -> frozenset[str] | None:
        return self.roles.get(role)

    def unlock_user(self, user_id: str) -> None:
        self.unlocked.append(user_id)

    def reset_local_user(self, username: str, password_hash: str) -> str:
        assert password_hash != PASSWORD
        self.reset.append(username)
        return TARGET


def client(capabilities: set[str], repo: Repo | None = None) -> tuple[TestClient, Repo, MagicMock]:
    repo = repo or Repo()
    audit = MagicMock()
    context = AuthContext("u1", "admin", "Admin", frozenset(capabilities), frozenset(), False)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[dependencies.get_current_context] = lambda: context
    app.dependency_overrides[dependencies.get_repository] = lambda: repo
    app.dependency_overrides[dependencies.get_audit_service] = lambda: audit
    return TestClient(app), repo, audit


def test_capability_catalogue_lists_all_capabilities_for_role_managers() -> None:
    c, _, _ = client({"manage_roles"})
    items = c.get("/api/v1/capabilities").json()["items"]
    assert len(items) == 19 and "manage_knowledge" in items and items == sorted(items)


def test_capability_catalogue_requires_manage_roles() -> None:
    c, _, _ = client({"manage_users"})
    assert c.get("/api/v1/capabilities").status_code == 403


def test_unlock_clears_the_lock_and_is_audited() -> None:
    c, repo, audit = client({"manage_users", "ask_question"})
    assert c.post(f"/api/v1/users/{TARGET}/unlock").status_code == 200
    assert repo.unlocked == [TARGET]
    assert audit.record.call_args.args[:4] == ("u1", "user_unlocked", "user", TARGET)


def test_reset_password_forces_a_change_and_is_audited() -> None:
    c, repo, audit = client({"manage_users", "ask_question"})
    response = c.post(f"/api/v1/users/{TARGET}/reset-password", json={"password": PASSWORD})
    assert response.status_code == 200 and response.json()["must_change_password"] is True
    assert repo.reset == ["maria"]
    assert audit.record.call_args.args[1] == "user_password_reset"


def test_reset_rejects_short_passwords_ldap_accounts_and_unknown_users() -> None:
    c, repo, _ = client({"manage_users", "ask_question"})
    assert c.post(f"/api/v1/users/{TARGET}/reset-password", json={"password": "short"}).status_code == 422
    other = "00000000-0000-0000-0000-0000000000cc"
    assert c.post(f"/api/v1/users/{other}/unlock").status_code == 404
    c2, repo2, _ = client({"manage_users", "ask_question"}, Repo(auth_source="ldap"))
    response = c2.post(f"/api/v1/users/{TARGET}/reset-password", json={"password": PASSWORD})
    assert response.status_code == 422 and repo2.reset == [] and repo.reset == []


def test_manager_cannot_act_on_a_stronger_account() -> None:
    c, repo, _ = client({"manage_users", "ask_question"}, Repo(role="DBA"))
    assert c.post(f"/api/v1/users/{TARGET}/unlock").status_code == 403
    assert repo.unlocked == []


def test_unlock_and_reset_require_manage_users() -> None:
    c, _, _ = client({"ask_question"})
    assert c.post(f"/api/v1/users/{TARGET}/unlock").status_code == 403
    assert c.post(f"/api/v1/users/{TARGET}/reset-password", json={"password": PASSWORD}).status_code == 403
