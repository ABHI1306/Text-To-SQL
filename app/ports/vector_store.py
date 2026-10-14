from collections.abc import Sequence
from typing import Protocol

from app.core.types import VectorMatch, VectorRecord


class VectorStorePort(Protocol):
    def ensure_collection(self, vector_size: int) -> None: ...

    def upsert(
        self,
        project_id: str,
        model_version: str,
        records: Sequence[VectorRecord],
    ) -> None: ...

    def search(
        self,
        project_id: str,
        model_version: str,
        vector: Sequence[float],
        *,
        limit: int,
    ) -> list[VectorMatch]: ...

    def delete_version(self, project_id: str, model_version: str) -> None: ...

    def delete_records(
        self, project_id: str, model_version: str, record_ids: Sequence[str]
    ) -> None: ...
