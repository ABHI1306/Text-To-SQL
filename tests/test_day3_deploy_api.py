from collections.abc import Sequence

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes as api_routes
from app.api.dependencies import get_current_context, get_repository
from app.api.routes import router
from app.auth.models import AuthContext
from app.core.types import QueryResult, VectorMatch, VectorRecord
from app.domain.mdl import Mdl
from app.semantic.indexing import chunk_mdl

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

    def generate(self, prompt: str, *, temperature: float = 0.0) -> str:
        del prompt, temperature
        return '{"sql":"SELECT c.customer_id FROM ecommerce.customers AS c"}'


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
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def execute_read_only(
        self, query: object, *, max_rows: int
    ) -> QueryResult:
        assert max_rows == 1000
        assert query.sql == (
            "SELECT c.customer_id FROM ecommerce.customers AS c"
        )
        return QueryResult(("customer_id",), ((101,), (102,)))


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


def test_project_query_endpoint_returns_live_database_result_shape(monkeypatch) -> None:
    repository = FakeRepository()
    repository.active = 2
    context = AuthContext(
        user_id=USER_ID,
        username="dba",
        role_name="DBA",
        capabilities=frozenset({"ask_question"}),
        project_ids=frozenset({PROJECT_ID}),
        must_change_password=False,
    )
    monkeypatch.setattr(api_routes, "QdrantVectorStoreAdapter", FakeVectorStore)
    monkeypatch.setattr(api_routes, "OllamaEmbedderAdapter", lambda *_: FakeEmbedder())
    monkeypatch.setattr(api_routes, "OllamaLLMAdapter", FakeLlm)
    monkeypatch.setattr(api_routes, "PostgresWarehouseAdapter", FakeWarehouse)
    monkeypatch.setattr(
        api_routes,
        "_project_connection_details",
        lambda _repository, _project_id: ({}, "postgresql://warehouse", None),
    )
    monkeypatch.setattr(
        api_routes,
        "get_settings",
        lambda: type(
            "SettingsStub",
            (),
            {
                "qdrant_url": "http://qdrant",
                "ollama_url": "http://ollama",
                "embedding_model": "embed",
                "sql_model": "sql",
                "warehouse_statement_timeout_ms": 10000,
                "warehouse_max_rows": 1000,
            },
        )(),
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_context] = lambda: context
    app.dependency_overrides[get_repository] = lambda: repository

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/projects/{PROJECT_ID}/query",
            json={"question": "Show customer IDs"},
        )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "sql": "SELECT c.customer_id FROM ecommerce.customers AS c",
        "columns": ["customer_id"],
        "rows": [[101], [102]],
        "retrieved_models": ["customers"],
        "model_version": 2,
        "elapsed_ms": response.json()["elapsed_ms"],
        "truncated": False,
        "source": "postgresql",
    }
