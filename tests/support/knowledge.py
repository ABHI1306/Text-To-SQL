"""Deterministic embedder and in-memory vector store for knowledge tests."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence

from app.core.types import VectorMatch, VectorRecord

DIM = 32


class WordEmbedder:
    """Hashed bag of words: similar wording gives a high cosine, unrelated wording near zero."""

    def __init__(self) -> None:
        self.fail = False

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        if self.fail:
            raise RuntimeError("embedder down")
        return [self._one(text) for text in texts]

    @staticmethod
    def _one(text: str) -> tuple[float, ...]:
        vector = [0.0] * DIM
        for word in re.findall(r"[a-z0-9]+", text.casefold()):
            slot = int(hashlib.sha1(word.encode()).hexdigest(), 16) % DIM
            vector[slot] += 1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return tuple(v / norm for v in vector)


class MemoryVectorStore:
    def __init__(self) -> None:
        self.points: dict[tuple[str, str], dict[str, VectorRecord]] = {}

    def ensure_collection(self, vector_size: int) -> None:
        return None

    def upsert(self, project_id: str, model_version: str, records: Sequence[VectorRecord]) -> None:
        bucket = self.points.setdefault((project_id, model_version), {})
        for record in records:
            bucket[record.record_id] = record

    def search(
        self, project_id: str, model_version: str, vector: Sequence[float], *, limit: int
    ) -> list[VectorMatch]:
        bucket = self.points.get((project_id, model_version), {})
        scored = [
            VectorMatch(r.record_id, sum(a * b for a, b in zip(r.vector, vector, strict=True)),
                        r.payload)
            for r in bucket.values()
        ]
        return sorted(scored, key=lambda m: m.score, reverse=True)[:limit]

    def delete_version(self, project_id: str, model_version: str) -> None:
        self.points.pop((project_id, model_version), None)

    def delete_records(
        self, project_id: str, model_version: str, record_ids: Sequence[str]
    ) -> None:
        for record_id in record_ids:
            self.points.get((project_id, model_version), {}).pop(record_id, None)
