import json
from collections.abc import Sequence

import pytest

from app.core.types import QueryResult, ValidatedQuery, VectorMatch, VectorRecord
from app.domain.mdl import Mdl
from app.semantic.indexing import IndexingService
from app.semantic.query import (
    QueryGenerationError,
    QuestionService,
    _validate_generated_sql,
    serialize_answer,
)
from app.semantic.retrieval import RetrievedModel

MDL = Mdl.model_validate(
    {
        "models": [
            {
                "name": "customers",
                "tableReference": "ecommerce.customers",
                "description": "Customer information",
                "columns": [
                    {
                        "name": "customer_id",
                        "type": "integer",
                        "physicalName": "customer_id",
                        "description": "Customer ID",
                    },
                    {
                        "name": "region",
                        "type": "text",
                        "physicalName": "region",
                        "description": "Sales region",
                    },
                ],
            }
        ],
        "relationships": [],
    }
)
SAFE_SQL = (
    "SELECT c.region, COUNT(*) AS total "
    "FROM ecommerce.customers AS c GROUP BY c.region ORDER BY total DESC"
)


class FakeEmbedder:
    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        return [(1.0, 2.0) for _ in texts]


class FakeVectorStore:
    def __init__(self) -> None:
        self.records: list[VectorRecord] = []

    def ensure_collection(self, vector_size: int) -> None:
        assert vector_size == 2

    def upsert(
        self,
        project_id: str,
        model_version: str,
        records: Sequence[VectorRecord],
    ) -> None:
        assert project_id == "project-a"
        assert model_version == "7"
        self.records.extend(records)

    def search(
        self,
        project_id: str,
        model_version: str,
        vector: Sequence[float],
        *,
        limit: int,
    ) -> list[VectorMatch]:
        assert project_id == "project-a"
        assert model_version == "7"
        assert vector == (1.0, 2.0)
        return [
            VectorMatch(record.record_id, 0.8, record.payload)
            for record in self.records[:limit]
        ]

    def delete_version(self, project_id: str, model_version: str) -> None:
        del project_id, model_version


class FakeLlm:
    def __init__(self, response: str) -> None:
        self.response = response
        self.prompt = ""

    def generate(self, prompt: str, *, temperature: float = 0.0) -> str:
        assert temperature == 0.0
        self.prompt = prompt
        return self.response


class FakeWarehouse:
    def __init__(self) -> None:
        self.executed: ValidatedQuery | None = None

    def execute_read_only(
        self,
        query: ValidatedQuery,
        *,
        max_rows: int,
    ) -> QueryResult:
        self.executed = query
        assert max_rows == 2
        return QueryResult(
            columns=("region", "total"),
            rows=(("West", 8), ("North", 5), ("South", 2)),
        )


def _prepare_store() -> FakeVectorStore:
    store = FakeVectorStore()
    IndexingService(FakeEmbedder(), store).index("project-a", "7", MDL)
    return store


def test_question_service_returns_real_warehouse_rows_and_applies_row_cap() -> None:
    store = _prepare_store()
    llm = FakeLlm(json.dumps({"sql": SAFE_SQL}))
    warehouse = FakeWarehouse()

    answer = QuestionService(
        FakeEmbedder(), store, llm, warehouse, max_rows=2
    ).ask("project-a", 7, "count customers by region", MDL)

    assert answer.sql == SAFE_SQL
    assert answer.columns == ("region", "total")
    assert answer.rows == (("West", 8), ("North", 5))
    assert answer.truncated is True
    assert answer.retrieved_models == ("customers",)
    assert warehouse.executed == ValidatedQuery(SAFE_SQL, {})
    assert "Customer information" in llm.prompt
    assert "untrusted data" in llm.prompt
    serialized = serialize_answer(answer)
    assert serialized["rows"] == [["West", 8], ["North", 5]]
    assert serialized["source"] == "postgresql"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM ecommerce.customers",
        "SELECT email FROM ecommerce.customers",
        "SELECT x.region FROM ecommerce.customers AS c",
        "SELECT region FROM public.users",
        "SELECT pg_sleep(5) FROM ecommerce.customers",
        "WITH data AS (SELECT region FROM ecommerce.customers) SELECT region FROM data",
        "DELETE FROM ecommerce.customers",
        "SELECT region FROM ecommerce.customers; SELECT region FROM ecommerce.customers",
        "SELECT c.region INTO result FROM ecommerce.customers AS c",
    ],
)
def test_generated_sql_rejects_unapproved_or_unsafe_queries(sql: str) -> None:
    retrieved = RetrievedModel(
        name="customers",
        description="Customer information",
        table_reference="ecommerce.customers",
        columns=tuple(MDL.models[0].columns),
        score=1.0,
    )
    with pytest.raises(QueryGenerationError):
        _validate_generated_sql(sql, (retrieved,))


def test_model_response_without_sql_is_rejected_before_warehouse_execution() -> None:
    store = _prepare_store()
    warehouse = FakeWarehouse()
    service = QuestionService(
        FakeEmbedder(), store, FakeLlm('{"message":"not sql"}'), warehouse
    )
    with pytest.raises(QueryGenerationError, match="SQL string"):
        service.ask("project-a", 7, "customers by region", MDL)
    assert warehouse.executed is None
