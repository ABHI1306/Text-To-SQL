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
from app.knowledge.service import KnowledgeService
from app.ports.knowledge import KnowledgeRepoPort
from app.ports.threads import ThreadStorePort
from app.semantic.retrieval import RetrievalCache


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


_retrieval_cache: RetrievalCache | None = None


def _shared_retrieval_cache(seconds: int) -> RetrievalCache | None:
    """One cache for the whole process, because a service is built for every request."""
    global _retrieval_cache
    if seconds <= 0:
        return None
    if _retrieval_cache is None:
        _retrieval_cache = RetrievalCache(seconds)
    return _retrieval_cache


def build_ask_service(
    dsn: str,
    ca_certificate: str | None,
    *,
    audit: AuditService | None = None,
    threads: ThreadStorePort | None = None,
    knowledge: KnowledgeRepoPort | None = None,
) -> tuple[AskService, Callable[[], None]]:
    """The service and a function that releases its connections."""
    settings = get_settings()
    vector_store = QdrantVectorStoreAdapter(settings.qdrant_url)
    embedder = OllamaEmbedderAdapter(settings.ollama_url, settings.embedding_model)
    small_llm = OllamaLLMAdapter(settings.ollama_url, settings.small_model or settings.sql_model)
    service = AskService(
        embedder,
        vector_store,
        OllamaLLMAdapter(settings.ollama_url, settings.sql_model),
        PostgresWarehouseAdapter(
            dsn,
            statement_timeout_ms=settings.warehouse_statement_timeout_ms,
            max_rows=settings.warehouse_max_rows,
            ssl_ca_certificate=ca_certificate,
            pool_max_size=settings.warehouse_pool_max_size,
        ),
        audit=audit,
        max_rows=settings.warehouse_max_rows,
        small_llm=small_llm,
        summary_llm=small_llm,
        threads=threads,
        knowledge=KnowledgeService(knowledge, embedder, vector_store) if knowledge else None,
        retrieval_cache=_shared_retrieval_cache(settings.retrieval_cache_seconds),
    )
    return service, vector_store.close
