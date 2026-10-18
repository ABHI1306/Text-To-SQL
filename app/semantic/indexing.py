"""Index a semantic model into the vector store and deploy it as the active version."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Protocol, cast

from app.core.types import JsonValue, VectorRecord
from app.domain.mdl import Mdl, MdlColumn, MdlModel
from app.ports.embedder import EmbedderPort
from app.ports.vector_store import VectorStorePort

_RECORD_NAMESPACE = uuid.UUID("60b78015-f8dc-4ec9-b840-1a4ecbfe8ce1")


class DeploymentRepository(Protocol):
    """Metadata store operations that mark an MDL version active or restore the previous one."""

    def get_active_mdl_version(self, project_id: str) -> int | None:
        """Currently active MDL version of the project, or None."""
        ...

    def activate_mdl_version(
        self,
        project_id: str,
        version: int,
        *,
        deployed_by: str,
        expected_previous_version: int | None,
    ) -> None:
        """Make ``version`` active if ``expected_previous_version`` is still active."""
        ...

    def restore_active_mdl_version(
        self, project_id: str, new_version: int, previous_version: int
    ) -> None:
        """Roll the active version back from ``new_version`` to ``previous_version``."""
        ...


@dataclass(frozen=True)
class MdlChunk:
    """One embeddable piece of the MDL: a stable id, its text and a search payload."""
    record_id: str
    text: str
    payload: dict[str, JsonValue]


def chunk_mdl(mdl: Mdl) -> list[MdlChunk]:
    """Split the MDL into model, column and relationship chunks for embedding."""
    chunks: list[MdlChunk] = []
    for model in mdl.models:
        model_chunk = _model_chunk(model)
        chunks.append(model_chunk)
        for column in model.columns:
            chunks.append(_column_chunk(model, column))
    for relationship in mdl.relationships:
        payload: dict[str, JsonValue] = {
            "kind": "relationship",
            "name": relationship.name,
            "models": list(relationship.models),
            "text": (
                f"Relationship {relationship.name}: {relationship.models[0]} joins "
                f"{relationship.models[1]} using {relationship.condition}"
            ),
        }
        chunks.append(_make_chunk(payload))
    return chunks


def _model_chunk(model: MdlModel) -> MdlChunk:
    """Chunk describing a whole model: table, description and column list."""
    column_text = "; ".join(
        f"{column.name} ({column.type}){', primary key' if column.is_primary_key else ''}"
        for column in model.columns
    )
    return _make_chunk(
        {
            "kind": "model",
            "model": model.name,
            "table_reference": model.table_reference,
            "name": model.name,
            "text": (
                f"Model {model.name}. Table {model.table_reference}. "
                f"{model.description}. Columns: {column_text}"
            ),
            "columns": [column.name for column in model.columns],
        }
    )


def _column_chunk(model: MdlModel, column: MdlColumn) -> MdlChunk:
    """Chunk describing one column of ``model``."""
    return _make_chunk(
        {
            "kind": "column",
            "model": model.name,
            "name": column.name,
            "column": column.name,
            "type": column.type,
            "text": (
                f"Column {column.name} in model {model.name}. Type {column.type}. "
                f"{column.description}"
            ),
            "is_primary_key": column.is_primary_key,
        }
    )


def _make_chunk(payload: dict[str, JsonValue]) -> MdlChunk:
    """Wrap ``payload`` in a chunk whose id is a content hash of its text."""
    text = str(payload["text"]).strip()
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    identifier = str(uuid.uuid5(_RECORD_NAMESPACE, digest))
    return MdlChunk(identifier, text, payload)


class IndexingService:
    """Embeds MDL chunks in batches and stores them under a project and version."""
    def __init__(
        self,
        embedder: EmbedderPort,
        vector_store: VectorStorePort,
        *,
        batch_size: int = 32,
    ) -> None:
        """Set the embedder, vector store and a positive batch size."""
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self._embedder = embedder
        self._vector_store = vector_store
        self._batch_size = batch_size

    def index(self, project_id: str, model_version: str, mdl: Mdl) -> int:
        """Embed and upsert every chunk; on any failure delete the partial version and re-raise."""
        chunks = chunk_mdl(mdl)
        if not chunks:
            raise ValueError("Cannot index an empty semantic model")
        try:
            for offset in range(0, len(chunks), self._batch_size):
                batch = chunks[offset : offset + self._batch_size]
                vectors = self._embedder.embed([chunk.text for chunk in batch])
                if len(vectors) != len(batch):
                    raise ValueError("Embedder returned an unexpected vector count")
                dimensions = len(vectors[0]) if vectors else 0
                if dimensions < 1 or any(len(vector) != dimensions for vector in vectors):
                    raise ValueError("Embedder returned invalid vector dimensions")
                self._vector_store.ensure_collection(dimensions)
                records = [
                    VectorRecord(
                        record_id=_scoped_record_id(
                            project_id, model_version, chunk.record_id
                        ),
                        vector=vector,
                        payload=_json_payload(chunk.payload),
                    )
                    for chunk, vector in zip(batch, vectors, strict=True)
                ]
                self._vector_store.upsert(project_id, model_version, records)
        except Exception:
            self._vector_store.delete_version(project_id, model_version)
            raise
        return len(chunks)


def _json_payload(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Copy ``payload`` through JSON so it holds only JSON-safe values."""
    return cast(dict[str, JsonValue], json.loads(json.dumps(payload)))


def _scoped_record_id(project_id: str, model_version: str, chunk_id: str) -> str:
    """Vector record id unique to a project, model version and chunk."""
    return str(uuid.uuid5(_RECORD_NAMESPACE, f"{project_id}:{model_version}:{chunk_id}"))


class DeployService:
    """Indexes a new MDL version, activates it and removes the superseded index."""
    def __init__(
        self,
        indexing: IndexingService,
        vector_store: VectorStorePort,
        metadata_repository: DeploymentRepository,
    ) -> None:
        """Set the indexing service, vector store and deployment metadata store."""
        self._indexing = indexing
        self._vector_store = vector_store
        self._metadata_repository = metadata_repository

    def deploy(
        self,
        project_id: str,
        version: int,
        mdl: Mdl,
        *,
        deployed_by: str,
    ) -> dict[str, object]:
        """Index and activate ``version``, undoing partial work if a later step fails."""
        version_id = str(version)
        previous = self._metadata_repository.get_active_mdl_version(project_id)
        previous_version = str(previous) if previous is not None else None
        self._indexing.index(project_id, version_id, mdl)
        try:
            self._metadata_repository.activate_mdl_version(
                project_id,
                version,
                deployed_by=deployed_by,
                expected_previous_version=previous,
            )
        except Exception:
            self._vector_store.delete_version(project_id, version_id)
            raise
        if previous_version is not None and previous_version != version_id:
            try:
                self._vector_store.delete_version(project_id, previous_version)
            except Exception:
                try:
                    self._metadata_repository.restore_active_mdl_version(
                        project_id,
                        version,
                        int(previous_version),
                    )
                except Exception as rollback_error:
                    raise RuntimeError(
                        "Deployment cleanup failed and the previous active version "
                        "could not be restored"
                    ) from rollback_error
                self._vector_store.delete_version(project_id, version_id)
                raise
        return {
            "project_id": project_id,
            "version": version,
            "indexed_chunks": len(chunk_mdl(mdl)),
            "previous_version": previous_version,
        }
