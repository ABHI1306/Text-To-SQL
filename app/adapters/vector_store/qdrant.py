from collections.abc import Mapping, Sequence
from typing import cast
from urllib.parse import quote

import httpx

from app.core.types import JsonValue, VectorMatch, VectorRecord
from app.ports.vector_store import VectorStorePort


class QdrantVectorStoreAdapter(VectorStorePort):
    def __init__(
        self,
        base_url: str,
        *,
        collection: str = "text2sql",
        timeout_seconds: float = 30,
    ) -> None:
        if not base_url:
            raise ValueError("QDRANT_URL is required")
        if not collection:
            raise ValueError("Qdrant collection name is required")
        self._base_url = base_url.rstrip("/")
        self._collection = quote(collection, safe="")
        self._timeout = timeout_seconds
        self._client = httpx.Client(base_url=self._base_url, timeout=self._timeout)

    def ensure_collection(self, vector_size: int) -> None:
        if vector_size < 1:
            raise ValueError("Vector size must be positive")
        response = self._client.get(f"/collections/{self._collection}")
        if response.status_code == 200:
            result = response.json().get("result")
            vectors = result.get("config", {}).get("params", {}).get("vectors") if isinstance(result, dict) else None
            if not isinstance(vectors, dict) or vectors.get("size") != vector_size:
                raise ValueError(
                    "Existing Qdrant collection has a different vector dimension"
                )
            return
        if response.status_code != 404:
            self._raise_for_status(response)
        response = self._client.put(
            f"/collections/{self._collection}",
            params={"wait": "true"},
            json={
                "vectors": {"size": vector_size, "distance": "Cosine"},
                "on_disk_payload": True,
            },
        )
        self._raise_for_status(response)

    def upsert(
        self,
        project_id: str,
        model_version: str,
        records: Sequence[VectorRecord],
    ) -> None:
        if not records:
            return
        payload = []
        for record in records:
            point_payload = dict(record.payload)
            point_payload.update(
                {"project_id": project_id, "model_version": model_version}
            )
            payload.append(
                {
                    "id": record.record_id,
                    "vector": list(record.vector),
                    "payload": point_payload,
                }
            )
        response = self._client.put(
            f"/collections/{self._collection}/points",
            params={"wait": "true"},
            json={"points": payload},
        )
        self._raise_for_status(response)

    def search(
        self,
        project_id: str,
        model_version: str,
        vector: Sequence[float],
        *,
        limit: int,
    ) -> list[VectorMatch]:
        if limit < 1:
            raise ValueError("Search limit must be positive")
        response = self._client.post(
            f"/collections/{self._collection}/points/search",
            json={
                "vector": list(vector),
                "limit": limit,
                "with_payload": True,
                "filter": {
                    "must": [
                        {"key": "project_id", "match": {"value": project_id}},
                        {"key": "model_version", "match": {"value": model_version}},
                    ]
                },
            },
        )
        self._raise_for_status(response)
        result = response.json().get("result")
        if not isinstance(result, list):
            raise TypeError("Qdrant returned an invalid search result")
        matches: list[VectorMatch] = []
        for item in result:
            if not isinstance(item, dict) or not isinstance(item.get("payload"), dict):
                raise TypeError("Qdrant returned a malformed point")
            matches.append(
                VectorMatch(
                    record_id=str(item.get("id", "")),
                    score=float(item.get("score", 0)),
                    payload=cast(Mapping[str, JsonValue], item["payload"]),
                )
            )
        return matches

    def delete_version(self, project_id: str, model_version: str) -> None:
        response = self._client.post(
            f"/collections/{self._collection}/points/delete",
            params={"wait": "true"},
            json={
                "filter": {
                    "must": [
                        {"key": "project_id", "match": {"value": project_id}},
                        {"key": "model_version", "match": {"value": model_version}},
                    ]
                }
            },
        )
        if response.status_code == 404:
            return
        self._raise_for_status(response)

    def delete_records(
        self, project_id: str, model_version: str, record_ids: Sequence[str]
    ) -> None:
        if not record_ids:
            return
        response = self._client.post(
            f"/collections/{self._collection}/points/delete",
            params={"wait": "true"},
            json={
                "filter": {
                    "must": [
                        {"key": "project_id", "match": {"value": project_id}},
                        {"key": "model_version", "match": {"value": model_version}},
                        {"has_id": list(record_ids)},
                    ]
                }
            },
        )
        if response.status_code == 404:
            return
        self._raise_for_status(response)

    def close(self) -> None:
        self._client.close()

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise RuntimeError(
                f"Qdrant request failed with status {response.status_code}"
            ) from error
