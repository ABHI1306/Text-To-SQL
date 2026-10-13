from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import dependencies
from app.api.governance_routes import router
from app.auth.models import AuthContext


class Repo:
    def __init__(self) -> None:
        self.saved: list[tuple[str, list[tuple[str, str]], str]] = []
        self.roles = {"DBA": frozenset({"manage_roles", "edit_mdl"}), "Analyst": frozenset({"ask_question"})}

    def get_role_capabilities(self, role: str) -> frozenset[str] | None:
        return self.roles.get(role)

    def list_identity_mappings(self) -> list[dict[str, object]]:
        return [{"id": "1", "source": "ldap", "match": "dbas", "role": "DBA"}]

    def replace_identity_mappings(self, source: str, mappings: list[tuple[str, str]], *, actor_id: str) -> None:
        self.saved.append((source, mappings, actor_id))


def client(capabilities: set[str]) -> tuple[TestClient, Repo]:
    repo = Repo()
    context = AuthContext("u1", "dba", "DBA", frozenset(capabilities), frozenset(), False)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[dependencies.get_current_context] = lambda: context
    app.dependency_overrides[dependencies.get_repository] = lambda: repo
    return TestClient(app), repo


def put(c: TestClient, body: dict[str, Any]) -> Any:
    return c.put("/api/v1/identity/mappings", json=body)


def test_mappings_are_listed_and_replaced_for_a_source() -> None:
    c, repo = client({"manage_roles", "ask_question"})
    assert c.get("/api/v1/identity/mappings").json()["items"][0]["match"] == "dbas"
    response = put(c, {"source": "ldap", "mappings": [{"match": "analysts", "role": "Analyst"}]})
    assert response.status_code == 200
    assert repo.saved == [("ldap", [("analysts", "Analyst")], "u1")]


def test_unknown_roles_duplicates_and_bad_sources_are_rejected() -> None:
    c, repo = client({"manage_roles", "ask_question"})
    assert put(c, {"source": "ldap", "mappings": [{"match": "g", "role": "Ghost"}]}).status_code == 422
    two = [{"match": "G", "role": "Analyst"}, {"match": "g", "role": "Analyst"}]
    assert put(c, {"source": "ldap", "mappings": two}).status_code == 422
    assert put(c, {"source": "saml", "mappings": []}).status_code == 422
    assert repo.saved == []


def test_a_manager_cannot_map_a_group_to_a_role_stronger_than_their_own() -> None:
    c, repo = client({"manage_roles", "ask_question"})  # lacks edit_mdl, which DBA has
    response = put(c, {"source": "ldap", "mappings": [{"match": "g", "role": "DBA"}]})
    assert response.status_code == 403 and repo.saved == []


def test_mapping_routes_require_manage_roles() -> None:
    c, _ = client({"ask_question"})
    assert c.get("/api/v1/identity/mappings").status_code == 403
    assert put(c, {"source": "ldap", "mappings": []}).status_code == 403
