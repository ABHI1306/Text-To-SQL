"""Port for storing and searching embedding vectors per project and model version."""

from collections.abc import Sequence
from typing import Protocol

from app.core.types import VectorMatch, VectorRecord


class VectorStorePort(Protocol):
    """Vector index scoped by project and embedding model version."""

    def ensure_collection(self, vector_size: int) -> None:
        """Create the collection for ``vector_size``-dimensional vectors if absent."""
        ...

    def upsert(
        self,
        project_id: str,
        model_version: str,
        records: Sequence[VectorRecord],
    ) -> None:
        """Insert or replace ``records`` in this project and model version."""
        ...

    def search(
        self,
        project_id: str,
        model_version: str,
        vector: Sequence[float],
        *,
        limit: int,
    ) -> list[VectorMatch]:
        """Return up to ``limit`` nearest matches to ``vector`` in this project and version."""
        ...

    def delete_version(self, project_id: str, model_version: str) -> None:
        """Remove every record of this project and model version."""
        ...

    def delete_records(
        self, project_id: str, model_version: str, record_ids: Sequence[str]
    ) -> None:
        """Remove only the records named by ``record_ids``."""
        ...
