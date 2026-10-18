"""Question routes: every question goes through the agent graph (retrieve, compile, validate)."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.adapters.metadata.governance import PostgresGovernanceRepository
from app.adapters.metadata.knowledge import PostgresKnowledgeRepository
from app.adapters.metadata.postgres import PostgresMetadataRepository
from app.adapters.metadata.threads import PostgresThreadStore
from app.agent.factory import NoDeployedModel, build_ask_service, load_deployed_mdl
from app.agent.service import AskEvent, AskService
from app.api.dependencies import (
    get_audit_service,
    get_governance_repository,
    get_repository,
    get_thread_store,
    require_capability,
    require_project_access,
)
from app.api.rate_limit import ASK_PER_MINUTE
from app.audit.service import AuditService
from app.auth.models import AuthContext
from app.config import get_settings
from app.core.types import Principal
from app.domain.mdl import Mdl

router = APIRouter(prefix="/api/v1", tags=["Query"])

RepositoryDependency = Annotated[PostgresMetadataRepository, Depends(get_repository)]
GovernanceDependency = Annotated[PostgresGovernanceRepository, Depends(get_governance_repository)]
ThreadsDependency = Annotated[PostgresThreadStore, Depends(get_thread_store)]
AuditDependency = Annotated[AuditService, Depends(get_audit_service)]
AskQuestionContext = Annotated[AuthContext, Depends(require_capability("ask_question", per_minute=ASK_PER_MINUTE))]

# Failure codes that are the system's fault rather than the question's.
_UNAVAILABLE = {"LLM_UNAVAILABLE"}


class AskQuestionRequest(BaseModel):
    """Body for a question: the text and an optional thread to continue."""
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)
    thread_id: UUID | None = None


class _Prepared:
    """A ready-to-run ask service bound to a project's deployed MDL and warehouse."""
    def __init__(
        self, service: AskService, mdl: Mdl, version: int, close: Callable[[], None]
    ) -> None:
        """Hold the service, deployed MDL, its version and the cleanup callback."""
        self.service = service
        self.mdl = mdl
        self.version = version
        self.close = close


def _principal(context: AuthContext) -> Principal:
    """Convert the authenticated context into the ``Principal`` the compiler enforces."""
    return Principal(
        user_id=context.user_id,
        role_name=context.role_name,
        capabilities=context.capabilities,
        project_ids=context.project_ids,
        attributes=context.attributes or {},
    )


def _prepare(
    project_id: UUID,
    repository: PostgresMetadataRepository,
    audit: AuditService,
) -> _Prepared:
    """Load the deployed MDL and build an ask service; 409 when nothing is deployed."""
    from app.api.routes import _project_connection_details

    try:
        version, mdl = load_deployed_mdl(repository, str(project_id))
    except NoDeployedModel as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    _, dsn, ca_certificate = _project_connection_details(repository, project_id)
    settings = get_settings()
    service, close = build_ask_service(
        dsn,
        ca_certificate,
        audit=audit,
        threads=PostgresThreadStore(settings.metadata_database_url),
        knowledge=PostgresKnowledgeRepository(settings.metadata_database_url),
    )
    return _Prepared(service, mdl, version, close)


def _sse(event: AskEvent) -> str:
    """Format one agent event as a server-sent-events frame."""
    return f"event: {event.type}\ndata: {json.dumps(event.data, default=str)}\n\n"


def _check_thread(
    threads: PostgresThreadStore, context: AuthContext, project_id: UUID, thread_id: UUID | None
) -> None:
    """Before any work runs: a thread id must be the caller's own, in this project."""
    if thread_id is None:
        return
    record = threads.get(str(thread_id))
    if record is None or record.user_id != context.user_id or record.project_id != str(project_id):
        raise HTTPException(status_code=404, detail="Thread not found")


@router.post("/projects/{project_id}/query")
def ask_project_question(
    project_id: UUID,
    body: AskQuestionRequest,
    context: AskQuestionContext,
    repository: RepositoryDependency,
    governance: GovernanceDependency,
    audit: AuditDependency,
    threads: ThreadsDependency,
) -> dict[str, Any]:
    """Answer a question with SQL and rows (requires ``ask_question``).

    The SQL is generated, compiled with the caller's row policies and validated before it runs.
    Compiled ``sql`` is returned only to callers holding ``rerun_sql``.
    """
    require_project_access(context, project_id)
    _check_thread(threads, context, project_id, body.thread_id)
    prepared = _prepare(project_id, repository, audit)
    try:
        policies = [p.policy for p in governance.list_policies(str(project_id), enabled_only=True)]
        result = prepared.service.ask(
            str(project_id),
            prepared.version,
            prepared.mdl,
            _principal(context),
            policies,
            body.question,
            include_compiled="rerun_sql" in context.capabilities,
            thread_id=str(body.thread_id) if body.thread_id else None,
        )
    finally:
        prepared.close()
    if result.failure is not None:
        failure = result.failure
        status_code = 503 if failure.code in _UNAVAILABLE else 422
        raise HTTPException(
            status_code=status_code,
            detail={
                "code": failure.code,
                "message": failure.message,
                "retryable": failure.retryable,
            },
        )
    response: dict[str, Any] = {
        "logical_sql": result.logical_sql,
        "columns": result.columns,
        "rows": result.rows,
        "retrieved_models": result.retrieved_models,
        "model_version": prepared.version,
        "elapsed_ms": result.elapsed_ms,
        "truncated": result.truncated,
        "policy_context": result.policy_context,
        "source": "postgresql",
        "thread_id": result.thread_id,
        "intent": result.intent,
        "message": result.reply,
        "summary": result.summary,
        "chart": result.chart,
    }
    if "rerun_sql" in context.capabilities:
        response["sql"] = result.sql
    return response


@router.post("/projects/{project_id}/query/stream")
def ask_project_question_stream(
    project_id: UUID,
    body: AskQuestionRequest,
    context: AskQuestionContext,
    repository: RepositoryDependency,
    governance: GovernanceDependency,
    audit: AuditDependency,
    threads: ThreadsDependency,
) -> StreamingResponse:
    """Server-sent events: retrieval, sql_candidate, compilation, validation, rows, done/error."""
    require_project_access(context, project_id)
    _check_thread(threads, context, project_id, body.thread_id)
    prepared = _prepare(project_id, repository, audit)
    policies = [p.policy for p in governance.list_policies(str(project_id), enabled_only=True)]
    principal = _principal(context)
    include_compiled = "rerun_sql" in context.capabilities

    def events() -> Iterator[str]:
        """Yield SSE frames for the question, always closing the service; fail closed on error."""
        try:
            yield from (
                _sse(event)
                for event in prepared.service.stream(
                    str(project_id),
                    prepared.version,
                    prepared.mdl,
                    principal,
                    policies,
                    body.question,
                    include_compiled=include_compiled,
                    thread_id=str(body.thread_id) if body.thread_id else None,
                )
            )
        except Exception:  # noqa: BLE001 - fail closed into a typed failure
            yield _sse(
                AskEvent(
                    "error",
                    {"code": "INTERNAL", "message": "The question failed.", "retryable": True},
                )
            )
        finally:
            prepared.close()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
