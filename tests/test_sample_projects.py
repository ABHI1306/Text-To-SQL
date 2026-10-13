"""Sample projects (FR-32): endpoint behaviour and coverage of the golden set."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes as api_routes
from app.api.dependencies import (
    get_audit_service,
    get_auth_service,
    get_current_context,
    get_repository,
)
from app.api.routes import SAMPLE_PROJECT_TEMPLATES, router
from app.api.security import ConnectionTestError
from app.auth.models import AuthContext
from tests.support.audit import RecordingAudit

ROOT = Path(__file__).resolve().parents[1]
USER_ID = "00000000-0000-0000-0000-0000000000a1"


def _demo_tables() -> set[tuple[str, str]]:
    ddl = (ROOT / "scripts/db/init-demo.sql").read_text(encoding="utf-8")
    return set(re.findall(r"CREATE TABLE (\w+)\.(\w+) \(", ddl))


def test_templates_only_select_tables_that_exist_in_the_demo_database() -> None:
    demo = _demo_tables()
    for template in SAMPLE_PROJECT_TEMPLATES.values():
        assert set(template["tables"]) <= demo  # type: ignore[arg-type]


def test_ecommerce_sample_preselects_every_ecommerce_demo_table() -> None:
    selected = set(SAMPLE_PROJECT_TEMPLATES["ecommerce"]["tables"])  # type: ignore[arg-type]
    assert selected == {t for t in _demo_tables() if t[0] == "ecommerce"}


def test_every_golden_case_can_be_retrieved_from_a_sample_project() -> None:
    golden = json.loads((ROOT / "tests/data/retrieval_golden.json").read_text("utf-8"))
    available = {
        table
        for template in SAMPLE_PROJECT_TEMPLATES.values()
        for _schema, table in template["tables"]  # type: ignore[union-attr]
    }
    missing = {
        case["id"]: sorted(set(case["expected_models"]) - available)
        for case in golden["cases"]
        if not set(case["expected_models"]) <= available
    }
    assert not missing, f"Golden cases expect models no sample project contains: {missing}"


class FakeRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def create_project(self, **kwargs: Any) -> None:
        self.calls.append(("create_project", kwargs))

    def set_project_sample(self, project_id: str) -> None:
        self.calls.append(("set_project_sample", project_id))

    def save_selected_tables(self, project_id: str, selections: list[dict[str, str]]) -> None:
        self.calls.append(("save_selected_tables", selections))

    def save_mdl(self, project_id: str, document: dict[str, Any]) -> int:
        self.calls.append(("save_mdl", document))
        return 1


class FakeAuthService:
    def __init__(self) -> None:
        self.invalidated: list[str] = []

    def invalidate_auth_context(self, user_id: str) -> None:
        self.invalidated.append(user_id)


class FakeCatalog:
    available: ClassVar[set[tuple[str, str]]] = set()

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def list_tables(self, schemas: tuple[str, ...]) -> list[Any]:
        return [
            SimpleNamespace(schema_name=schema, table_name=table)
            for schema, table in self.available
            if schema in schemas
        ]


class FakeProfiling:
    requested: tuple[tuple[str, str], ...] = ()

    def __init__(self, _catalog: Any) -> None:
        pass

    def create_draft(self, tables: tuple[tuple[str, str], ...]) -> Any:
        FakeProfiling.requested = tables
        mdl = SimpleNamespace(model_dump=lambda **_: {"models": [], "relationships": []})
        return SimpleNamespace(mdl=mdl)


def _client(monkeypatch: pytest.MonkeyPatch, capabilities: set[str]) -> tuple[
    TestClient, FakeRepository, FakeAuthService
]:
    repository, auth = FakeRepository(), FakeAuthService()
    settings = SimpleNamespace(
        warehouse_dsn="host=demo-db port=5432 dbname=demo user=reader password=pw",
        warehouse_statement_timeout_ms=1000,
    )
    monkeypatch.setattr(api_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(api_routes, "test_connection", lambda *_: None)
    monkeypatch.setattr(api_routes, "encrypt_connection", lambda *_: "encrypted")
    monkeypatch.setattr(api_routes, "PostgresCatalogAdapter", FakeCatalog)
    monkeypatch.setattr(api_routes, "ProfilingService", FakeProfiling)
    FakeCatalog.available = _demo_tables()
    context = AuthContext(
        user_id=USER_ID,
        username="dba",
        role_name="DBA",
        capabilities=frozenset(capabilities),
        project_ids=frozenset(),
        must_change_password=False,
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_context] = lambda: context
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_auth_service] = lambda: auth
    repository.audit = RecordingAudit()  # type: ignore[attr-defined]
    app.dependency_overrides[get_audit_service] = lambda: repository.audit  # type: ignore[attr-defined]
    return TestClient(app), repository, auth


def test_list_templates_returns_both_samples(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = _client(monkeypatch, {"manage_connection"})
    response = client.get("/api/v1/sample-projects")
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == ["ecommerce", "human-resources"]


@pytest.mark.parametrize(
    ("template_id", "name", "table_count"),
    [("ecommerce", "E-commerce sample", 12), ("human-resources", "Human resources sample", 5)],
)
def test_creating_a_sample_project_selects_tables_and_saves_a_draft_model(
    monkeypatch: pytest.MonkeyPatch, template_id: str, name: str, table_count: int
) -> None:
    client, repository, auth = _client(monkeypatch, {"manage_connection"})

    response = client.post(f"/api/v1/sample-projects/{template_id}")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == name and body["revision"] == 1
    names = [call[0] for call in repository.calls]
    assert names == ["create_project", "set_project_sample", "save_selected_tables", "save_mdl"]
    created = repository.calls[0][1]
    assert created["name"] == name
    assert created["encrypted_connection"] == "encrypted"
    assert created["owner_id"] == USER_ID
    assert len(repository.calls[2][1]) == table_count
    assert len(FakeProfiling.requested) == table_count
    assert auth.invalidated == [USER_ID]
    assert repository.audit.actions() == ["sample_project_created"]  # type: ignore[attr-defined]
    assert repository.audit.events[0]["details"]["template"] == template_id  # type: ignore[attr-defined]


def test_unknown_sample_template_is_404_and_saves_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, repository, _ = _client(monkeypatch, {"manage_connection"})
    assert client.post("/api/v1/sample-projects/nope").status_code == 404
    assert repository.calls == []


def test_missing_demo_tables_return_503_and_save_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, repository, _ = _client(monkeypatch, {"manage_connection"})
    FakeCatalog.available = {t for t in _demo_tables() if t != ("ecommerce", "reviews")}

    response = client.post("/api/v1/sample-projects/ecommerce")

    assert response.status_code == 503
    assert "reviews" in response.text
    assert repository.calls == []


def test_unreachable_sample_database_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    client, repository, _ = _client(monkeypatch, {"manage_connection"})

    def refuse(*_args: Any) -> None:
        raise ConnectionTestError("Sample database is unreachable")

    monkeypatch.setattr(api_routes, "test_connection", refuse)
    assert client.post("/api/v1/sample-projects/ecommerce").status_code == 503
    assert repository.calls == []


def test_creating_a_sample_requires_manage_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    client, repository, _ = _client(monkeypatch, {"ask_question"})
    assert client.post("/api/v1/sample-projects/ecommerce").status_code == 403
    assert client.get("/api/v1/sample-projects").status_code == 403
    assert repository.calls == []
