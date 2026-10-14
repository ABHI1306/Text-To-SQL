from collections.abc import Sequence
from typing import ClassVar

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.metadata.threads import InMemoryThreadStore
from app.agent import factory
from app.api import ask_routes
from app.api import routes as api_routes
from app.api.dependencies import (
    get_audit_service,
    get_current_context,
    get_governance_repository,
    get_repository,
)
from app.api.routes import router
from app.auth.models import AuthContext
from app.core.types import QueryResult, VectorMatch, VectorRecord
from app.domain.mdl import Mdl
from app.semantic.indexing import chunk_mdl
from tests.support.audit import RecordingAudit

PROJECT_ID = "00000000-0000-0000-0000-000000000020"
USER_ID = "00000000-0000-0000-0000-000000000021"
DOCUMENT = {
    "models": [
        {
            "name": "customers",
            "tableReference": "ecommerce.customers",
            "description": "Customer information",
            "columns": [
                {
                    "name": "customer_id",
                    "type": "integer",
                    "description": "Customer identifier",
                }
            ],
        }
    ],
    "relationships": [],
    "metrics": [],
    "instructions": [],
    "layout": {},
}


class FakeRepository:
    def __init__(self) -> None:
        self.active: int | None = None

    def get_mdl(
        self, project_id: str, version: int | None = None
    ) -> dict[str, object] | None:
        del project_id
        if version not in (None, 2):
            return None
        return {**DOCUMENT, "project_id": PROJECT_ID, "revision": 2}

    def get_active_mdl_version(self, project_id: str) -> int | None:
        del project_id
        return self.active

    def activate_mdl_version(
        self,
        project_id: str,
        version: int,
        *,
        deployed_by: str,
        expected_previous_version: int | None,
    ) -> None:
        del project_id, deployed_by
        assert expected_previous_version == self.active
        self.active = version

    def restore_active_mdl_version(
        self, project_id: str, new_version: int, previous_version: int
    ) -> None:
        del project_id, new_version
        self.active = previous_version


class FakeEmbedder:
    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        return [(1.0, 2.0) for _ in texts]


class FakeLlm:
    def __init__(self, _url: str, _model: str) -> None:
        pass

    def generate(self, prompt: str, *, temperature: float = 0.0, schema: object = None) -> str:
        del temperature, schema
        if "<message>" in prompt:
            return '{"intent":"data_question","message":""}'
        return '{"logical_sql":"SELECT customer_id FROM customers"}'


class FakeVectorStore:
    def __init__(self, _url: str) -> None:
        self.records = [
            VectorRecord(
                "customer-model",
                (1.0, 2.0),
                {
                    "kind": "model",
                    "model": "customers",
                    "text": "Customer information customer identifier",
                },
            )
        ]

    def ensure_collection(self, vector_size: int) -> None:
        assert vector_size == 2

    def upsert(
        self,
        project_id: str,
        model_version: str,
        records: Sequence[VectorRecord],
    ) -> None:
        assert project_id == PROJECT_ID
        assert model_version == "2"
        self.records.extend(records)

    def search(
        self, project_id: str, model_version: str, vector: Sequence[float], *, limit: int
    ) -> list[VectorMatch]:
        assert project_id == PROJECT_ID
        assert model_version == "2"
        assert vector == (1.0, 2.0)
        return [
            VectorMatch(record.record_id, 0.8, record.payload)
            for record in self.records[:limit]
        ]

    def delete_version(self, project_id: str, model_version: str) -> None:
        del project_id, model_version

    def close(self) -> None:
        return None


class FakeWarehouse:
    queries: ClassVar[list[object]] = []

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def explain(self, query: object) -> None:
        del query

    def execute_read_only(self, query: object, *, max_rows: int) -> QueryResult:
        assert max_rows == 1000
        FakeWarehouse.queries.append(query)
        return QueryResult(("customer_id",), ((101,), (102,)))


class FakeGovernance:
    def list_policies(self, project_id: str, *, enabled_only: bool = False) -> list[object]:
        del project_id, enabled_only
        return []


def test_deploy_endpoint_indexes_and_activates_saved_revision(monkeypatch) -> None:
    repository = FakeRepository()
    context = AuthContext(
        user_id=USER_ID,
        username="dba",
        role_name="DBA",
        capabilities=frozenset({"deploy_mdl"}),
        project_ids=frozenset({PROJECT_ID}),
        must_change_password=False,
    )
    monkeypatch.setattr(api_routes, "QdrantVectorStoreAdapter", FakeVectorStore)
    monkeypatch.setattr(api_routes, "OllamaEmbedderAdapter", lambda *_: FakeEmbedder())
    monkeypatch.setattr(
        api_routes,
        "get_settings",
        lambda: type(
            "SettingsStub",
            (),
            {"qdrant_url": "http://qdrant", "ollama_url": "http://ollama",
             "embedding_model": "embed"},
        )(),
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_context] = lambda: context
    app.dependency_overrides[get_repository] = lambda: repository

    with TestClient(app) as client:
        response = client.post(f"/api/v1/projects/{PROJECT_ID}/deploy")

    assert response.status_code == 200, response.text
    assert response.json()["version"] == 2
    assert response.json()["indexed_chunks"] == len(chunk_mdl(Mdl.model_validate(DOCUMENT)))
    assert repository.active == 2


def _ask_client(monkeypatch, capabilities: set[str]):  # type: ignore[no-untyped-def]
    repository = FakeRepository()
    repository.active = 2
    context = AuthContext(
        user_id=USER_ID,
        username="dba",
        role_name="DBA",
        capabilities=frozenset(capabilities),
        project_ids=frozenset({PROJECT_ID}),
        must_change_password=False,
    )
    monkeypatch.setattr(factory, "QdrantVectorStoreAdapter", FakeVectorStore)
    monkeypatch.setattr(factory, "OllamaEmbedderAdapter", lambda *_: FakeEmbedder())
    monkeypatch.setattr(factory, "OllamaLLMAdapter", FakeLlm)
    monkeypatch.setattr(factory, "PostgresWarehouseAdapter", FakeWarehouse)
    monkeypatch.setattr(
        api_routes,
        "_project_connection_details",
        lambda _repository, _project_id: ({}, "postgresql://warehouse", None),
    )
    monkeypatch.setattr(
        factory,
        "get_settings",
        lambda: type(
            "SettingsStub",
            (),
            {
                "qdrant_url": "http://qdrant",
                "ollama_url": "http://ollama",
                "embedding_model": "embed",
                "sql_model": "sql",
                "small_model": "",
                "warehouse_statement_timeout_ms": 10000,
                "warehouse_max_rows": 1000,
            },
        )(),
    )
    monkeypatch.setattr(
        ask_routes, "get_settings", lambda: type("S", (), {"metadata_database_url": "postgresql://x"})()
    )
    monkeypatch.setattr(ask_routes, "PostgresThreadStore", lambda _dsn: InMemoryThreadStore())
    audit = RecordingAudit()
    app = FastAPI()
    app.include_router(ask_routes.router)
    app.dependency_overrides[get_current_context] = lambda: context
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_governance_repository] = lambda: FakeGovernance()
    app.dependency_overrides[get_audit_service] = lambda: audit
    FakeWarehouse.queries.clear()
    return TestClient(app), audit


def test_project_query_goes_through_the_compiler_and_hides_compiled_sql(monkeypatch) -> None:
    client, audit = _ask_client(monkeypatch, {"ask_question"})
    response = client.post(f"/api/v1/projects/{PROJECT_ID}/query", json={"question": "Show customer IDs"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["logical_sql"] == "SELECT customer_id FROM customers"
    assert "sql" not in body  # compiled SQL needs rerun_sql
    assert body["columns"] == ["customer_id"] and body["rows"] == [[101], [102]]
    assert body["retrieved_models"] == ["customers"] and body["model_version"] == 2
    executed = FakeWarehouse.queries[0].sql  # type: ignore[attr-defined]
    assert "FROM ecommerce.customers" in executed and "m_customers" in executed
    assert audit.actions() == ["question_asked"]
    assert audit.events[0]["details"]["ok"] is True


def test_accounts_with_rerun_sql_also_get_the_compiled_sql(monkeypatch) -> None:
    client, _ = _ask_client(monkeypatch, {"ask_question", "rerun_sql"})
    body = client.post(f"/api/v1/projects/{PROJECT_ID}/query", json={"question": "Show customer IDs"}).json()
    assert body["sql"].startswith("WITH m_customers AS")


def test_query_stream_emits_the_ordered_events(monkeypatch) -> None:
    client, _ = _ask_client(monkeypatch, {"ask_question"})
    with client.stream(
        "POST", f"/api/v1/projects/{PROJECT_ID}/query/stream", json={"question": "Show customer IDs"}
    ) as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        raw = "".join(response.iter_text())
    events = [line.removeprefix("event: ") for line in raw.splitlines() if line.startswith("event: ")]
    assert events == ["retrieval", "sql_candidate", "compilation", "validation", "rows", "done"]
    assert '"sql"' not in raw.split("event: compilation")[1].split("event: validation")[0]
