"""Composition of the ask service from settings: shared by the API, the CLI and evaluation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from app.adapters.llm.ollama import OllamaLLMAdapter
from app.adapters.llm.ollama_embedder import OllamaEmbedderAdapter
from app.adapters.vector_store.qdrant import QdrantVectorStoreAdapter
from app.adapters.warehouse.postgres import PostgresWarehouseAdapter
from app.agent.service import AskService
from app.audit.service import AuditService
from app.config import get_settings
from app.domain.mdl import Mdl


class MdlSource(Protocol):
    def get_active_mdl_version(self, project_id: str) -> int | None: ...

    def get_mdl(self, project_id: str, version: int | None = None) -> dict[str, object] | None: ...


class NoDeployedModel(LookupError):
    pass


def load_deployed_mdl(repository: MdlSource, project_id: str) -> tuple[int, Mdl]:
    version = repository.get_active_mdl_version(project_id)
    if version is None:
        raise NoDeployedModel("Deploy a semantic model before asking questions")
    document = repository.get_mdl(project_id, version)
    if document is None:
        raise NoDeployedModel("The deployed semantic model is missing")
    return version, Mdl.model_validate(
        {key: value for key, value in document.items() if key not in {"project_id", "revision"}}
    )


def build_ask_service(
    dsn: str,
    ca_certificate: str | None,
    *,
    audit: AuditService | None = None,
) -> tuple[AskService, Callable[[], None]]:
    """The service and a function that releases its connections."""
    settings = get_settings()
    vector_store = QdrantVectorStoreAdapter(settings.qdrant_url)
    service = AskService(
        OllamaEmbedderAdapter(settings.ollama_url, settings.embedding_model),
        vector_store,
        OllamaLLMAdapter(settings.ollama_url, settings.sql_model),
        PostgresWarehouseAdapter(
            dsn,
            statement_timeout_ms=settings.warehouse_statement_timeout_ms,
            max_rows=settings.warehouse_max_rows,
            ssl_ca_certificate=ca_certificate,
        ),
        audit=audit,
        max_rows=settings.warehouse_max_rows,
    )
    return service, vector_store.close
