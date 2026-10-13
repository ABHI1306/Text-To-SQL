from collections.abc import Mapping

import httpx
import pytest

from app.adapters.llm.ollama_embedder import OllamaEmbedderAdapter
from app.adapters.vector_store.qdrant import QdrantVectorStoreAdapter
from app.core.types import VectorRecord


def test_ollama_embedder_validates_response_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: httpx.Response(
            200,
            json={"embeddings": [[0.1, 0.2], [0.3, 0.4]]},
            request=httpx.Request("POST", "http://ollama/api/embed"),
        ),
    )

    vectors = OllamaEmbedderAdapter("http://ollama", "nomic-embed-text").embed(
        ["first", "second"]
    )

    assert vectors == [(0.1, 0.2), (0.3, 0.4)]


def test_ollama_embedder_rejects_mismatched_vector_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: httpx.Response(
            200,
            json={"embeddings": [[0.1, 0.2]]},
            request=httpx.Request("POST", "http://ollama/api/embed"),
        ),
    )

    with pytest.raises(TypeError, match="invalid embeddings"):
        OllamaEmbedderAdapter("http://ollama", "nomic-embed-text").embed(
            ["first", "second"]
        )


class StubQdrant:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    def get(self, path: str) -> httpx.Response:
        self.calls.append(("GET", path, None))
        return httpx.Response(
            200,
            json={"result": {"config": {"params": {"vectors": {"size": 2}}}}},
            request=httpx.Request("GET", f"http://qdrant{path}"),
        )

    def put(self, path: str, **kwargs: object) -> httpx.Response:
        self.calls.append(("PUT", path, kwargs))
        return httpx.Response(
            200,
            json={"result": {"status": "acknowledged"}},
            request=httpx.Request("PUT", f"http://qdrant{path}"),
        )

    def post(self, path: str, **kwargs: object) -> httpx.Response:
        self.calls.append(("POST", path, kwargs))
        return httpx.Response(
            200,
            json={"result": []},
            request=httpx.Request("POST", f"http://qdrant{path}"),
        )

    def close(self) -> None:
        return None


def test_qdrant_filters_every_operation_by_project_and_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StubQdrant()
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client)
    store = QdrantVectorStoreAdapter("http://qdrant")
    store.ensure_collection(2)
    store.upsert(
        "project-a",
        "4",
        [VectorRecord("point-a", (0.1, 0.2), {"kind": "model"})],
    )
    store.search("project-b", "9", (0.1, 0.2), limit=3)
    store.delete_version("project-c", "2")

    upsert = client.calls[1][2]
    assert isinstance(upsert, Mapping)
    points = upsert["json"]["points"]  # type: ignore[index]
    assert points[0]["payload"]["project_id"] == "project-a"
    assert points[0]["payload"]["model_version"] == "4"

    search = client.calls[2][2]
    assert isinstance(search, Mapping)
    search_filter = search["json"]["filter"]  # type: ignore[index]
    assert search_filter["must"] == [
        {"key": "project_id", "match": {"value": "project-b"}},
        {"key": "model_version", "match": {"value": "9"}},
    ]

    deletion = client.calls[3][2]
    assert isinstance(deletion, Mapping)
    deletion_filter = deletion["json"]["filter"]  # type: ignore[index]
    assert [item["match"]["value"] for item in deletion_filter["must"]] == [
        "project-c",
        "2",
    ]


def test_qdrant_refuses_existing_collection_with_wrong_dimension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StubQdrant()
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client)
    store = QdrantVectorStoreAdapter("http://qdrant")

    with pytest.raises(ValueError, match="different vector dimension"):
        store.ensure_collection(3)
