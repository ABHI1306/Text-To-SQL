import json
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from app.core.types import VectorMatch, VectorRecord
from app.domain.mdl import Mdl
from app.semantic.indexing import DeployService, IndexingService, chunk_mdl
from app.semantic.retrieval import RetrievalService
from scripts.evaluate_retrieval import main as evaluate_retrieval

MODEL = Mdl.model_validate(
    {
        "models": [
            {
                "name": "customers",
                "tableReference": "ecommerce.customers",
                "description": "Customer profile and region",
                "primaryKey": "customer_id",
                "columns": [
                    {
                        "name": "customer_id",
                        "type": "integer",
                        "description": "Customer identifier",
                        "isPrimaryKey": True,
                    },
                    {
                        "name": "region",
                        "type": "text",
                        "description": "Customer sales region",
                    },
                    {"name": "email", "type": "text", "description": "Email"},
                ],
            },
            {
                "name": "orders",
                "tableReference": "ecommerce.orders",
                "description": "Customer orders",
                "columns": [
                    {"name": "order_id", "type": "integer", "description": "Order ID"},
                    {
                        "name": "customer_id",
                        "type": "integer",
                        "description": "Customer identifier",
                    },
                ],
            },
        ],
        "relationships": [
            {
                "name": "customer_orders",
                "models": ["customers", "orders"],
                "joinType": "ONE_TO_MANY",
                "condition": "customers.customer_id = orders.customer_id",
            }
        ],
    }
)


class FakeEmbedder:
    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        return [(float(len(text)), 1.0) for text in texts]


class FakeVectorStore:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], list[VectorRecord]] = {}
        self.deleted: list[tuple[str, str]] = []

    def ensure_collection(self, vector_size: int) -> None:
        assert vector_size == 2

    def upsert(
        self, project_id: str, model_version: str, records: Sequence[VectorRecord]
    ) -> None:
        self.records.setdefault((project_id, model_version), []).extend(records)

    def search(
        self,
        project_id: str,
        model_version: str,
        vector: Sequence[float],
        *,
        limit: int,
    ) -> list[VectorMatch]:
        del vector
        return [
            VectorMatch(record.record_id, 0.7, record.payload)
            for record in self.records.get((project_id, model_version), [])[:limit]
        ]

    def delete_version(self, project_id: str, model_version: str) -> None:
        self.deleted.append((project_id, model_version))
        self.records.pop((project_id, model_version), None)


def test_chunks_and_indexes_models_columns_and_relationships_by_project_version() -> None:
    chunks = chunk_mdl(MODEL)
    assert [chunk.payload["kind"] for chunk in chunks].count("model") == 2
    assert [chunk.payload["kind"] for chunk in chunks].count("column") == 5
    assert [chunk.payload["kind"] for chunk in chunks].count("relationship") == 1

    store = FakeVectorStore()
    count = IndexingService(FakeEmbedder(), store, batch_size=2).index(
        "project-a", "4", MODEL
    )
    assert count == len(chunks)
    assert len(store.records[("project-a", "4")]) == len(chunks)
    project_b_count = IndexingService(FakeEmbedder(), store).index(
        "project-b", "4", MODEL
    )
    assert project_b_count == count
    assert {
        record.record_id for record in store.records[("project-a", "4")]
    }.isdisjoint(
        record.record_id for record in store.records[("project-b", "4")]
    )


def test_retrieval_hybrid_ranks_expands_relationships_and_prunes_columns() -> None:
    store = FakeVectorStore()
    IndexingService(FakeEmbedder(), store).index("project-a", "4", MODEL)

    result = RetrievalService(FakeEmbedder(), store).retrieve(
        "project-a", "4", "customer region", MODEL, limit=1, max_columns_per_model=2
    )

    assert {model.name for model in result.models} == {"customers", "orders"}
    customer = next(model for model in result.models if model.name == "customers")
    assert {column.name for column in customer.columns} == {"customer_id", "region"}
    assert [relationship.name for relationship in result.relationships] == ["customer_orders"]


class FakeDeploymentRepository:
    def __init__(self) -> None:
        self.active = 3
        self.restore_calls: list[tuple[str, int, int]] = []

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
        self.restore_calls.append((project_id, new_version, previous_version))
        self.active = previous_version


class FailingCleanupStore(FakeVectorStore):
    def delete_version(self, project_id: str, model_version: str) -> None:
        if model_version == "3":
            raise RuntimeError("simulated cleanup failure")
        super().delete_version(project_id, model_version)


def test_deploy_restores_old_active_revision_if_old_index_cleanup_fails() -> None:
    store = FailingCleanupStore()
    repository = FakeDeploymentRepository()
    deploy = DeployService(IndexingService(FakeEmbedder(), store), store, repository)

    with pytest.raises(RuntimeError, match="simulated cleanup failure"):
        deploy.deploy("project-a", 4, MODEL, deployed_by="user-a")

    assert repository.active == 3
    assert repository.restore_calls == [("project-a", 4, 3)]
    assert ("project-a", "4") in store.deleted


def test_candidate_golden_set_has_40_unapproved_review_cases(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    golden_path = Path(__file__).parent / "data/retrieval_golden.json"
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    assert len(golden["cases"]) == 40
    assert all(case["approval_status"] == "pending" for case in golden["cases"])
    assert all(case["reference_sql"] is None for case in golden["cases"])

    monkeypatch.setattr(
        sys,
        "argv",
        ["evaluate_retrieval.py", "--project-id", "00000000-0000-0000-0000-000000000001"],
    )
    assert evaluate_retrieval() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "not_measurable"
    assert output["approved_cases"] == 0
    assert output["pending_cases"] == 40
    assert output["recall_at_k"] is None
