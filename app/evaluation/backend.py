"""The production EvalBackend: the real ask service and warehouse, as the starting account."""

from __future__ import annotations

from typing import Any

from app.adapters.metadata.governance import PostgresGovernanceRepository
from app.adapters.metadata.knowledge import PostgresKnowledgeRepository
from app.adapters.metadata.postgres import PostgresMetadataRepository
from app.adapters.metadata.threads import InMemoryThreadStore
from app.agent.factory import build_ask_service
from app.agent.service import AskResult
from app.auth.models import to_principal
from app.collab.runtime import runtime_factory, warehouse_connection
from app.config import Settings
from app.evaluation.execution import BackendFactory
from app.semantic.runner import run_logical_sql


class AskEvalBackend:
    def __init__(self, repository: PostgresMetadataRepository,
                 governance: PostgresGovernanceRepository, settings: Settings,
                 project_id: str, user_id: str) -> None:
        context = repository.get_auth_context(user_id)
        if context is None or project_id not in context.project_ids:
            raise LookupError("The account that started the run no longer has access")
        self._project_id = project_id
        self._principal = to_principal(context)
        self._runtime = runtime_factory(repository, governance, settings)(project_id)
        dsn, certificate = warehouse_connection(repository, settings, project_id)
        # Evaluation runs are not audited per question and keep no conversation memory.
        self._service, self._close = build_ask_service(
            dsn, certificate, audit=None, threads=InMemoryThreadStore(),
            knowledge=PostgresKnowledgeRepository(settings.metadata_database_url),
        )

    def ask(self, question: str) -> AskResult:
        return self._service.ask(
            self._project_id, self._runtime.version, self._runtime.mdl, self._principal,
            self._runtime.policies, question,
        )

    def reference_rows(self, logical_sql: str) -> list[list[Any]] | str:
        outcome = run_logical_sql(
            logical_sql, self._runtime.mdl, self._principal, self._runtime.policies,
            self._runtime.warehouse, self._runtime.max_rows,
        )
        if outcome.failure is not None:
            return f"{outcome.failure.code}: {outcome.failure.message}"
        return outcome.rows

    def close(self) -> None:
        self._close()
        self._runtime.close()


def backend_factory(repository: PostgresMetadataRepository,
                    governance: PostgresGovernanceRepository, settings: Settings) -> BackendFactory:
    def build(project_id: str, user_id: str) -> AskEvalBackend:
        return AskEvalBackend(repository, governance, settings, project_id, user_id)

    return build

