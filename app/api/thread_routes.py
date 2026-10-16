"""Thread routes: create, read, ask (SSE), rerun edited SQL, and per-message feedback."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Iterator
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.adapters.metadata.collab import PostgresCollabStore
from app.adapters.metadata.governance import PostgresGovernanceRepository
from app.adapters.metadata.postgres import PostgresMetadataRepository
from app.adapters.metadata.threads import PostgresThreadStore
from app.agent.service import AskEvent
from app.api.ask_routes import _prepare, _principal
from app.api.dependencies import (
    get_audit_service,
    get_collab_store,
    get_governance_repository,
    get_knowledge_service,
    get_repository,
    get_thread_store,
    require_capability,
    require_project_access,
)
from app.api.governance_routes import _load_principal
from app.audit.service import AuditService
from app.auth.models import AuthContext, to_principal
from app.config import get_settings
from app.core.types import Principal
from app.knowledge.service import KnowledgeService
from app.ports.threads import ThreadRecord

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["Threads"])

RepositoryDependency = Annotated[PostgresMetadataRepository, Depends(get_repository)]
GovernanceDependency = Annotated[PostgresGovernanceRepository, Depends(get_governance_repository)]
AuditDependency = Annotated[AuditService, Depends(get_audit_service)]
ThreadsDependency = Annotated[PostgresThreadStore, Depends(get_thread_store)]
KnowledgeDependency = Annotated[KnowledgeService, Depends(get_knowledge_service)]
AskQuestionContext = Annotated[AuthContext, Depends(require_capability("ask_question"))]
RerunSqlContext = Annotated[AuthContext, Depends(require_capability("rerun_sql"))]
FeedbackContext = Annotated[AuthContext, Depends(require_capability("give_feedback"))]

# Hard ceiling for a caller-supplied max_rows (the service also clamps to its configured limit).
MAX_ROWS_CEILING = 10_000


class ThreadAskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)
    max_rows: int = Field(default=1000, ge=1, le=MAX_ROWS_CEILING)
    preview_as: UUID | None = None


class RerunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    logical_sql: str = Field(min_length=1, max_length=20_000)
    max_rows: int = Field(default=1000, ge=1, le=MAX_ROWS_CEILING)


class MessageFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rating: Literal["up", "down"]


def _sse(event: AskEvent) -> str:
    return f"event: {event.type}\ndata: {json.dumps(event.data, default=str)}\n\n"


def _owned_thread(threads: PostgresThreadStore, context: AuthContext, thread_id: UUID) -> ThreadRecord:
    """A thread the caller owns, in a project they can use; anything else looks like 'not found'."""
    record = threads.get(str(thread_id))
    if (
        record is None
        or record.user_id != context.user_id
        or record.project_id not in context.project_ids
    ):
        raise HTTPException(status_code=404, detail="Thread not found")
    return record


def can_read_thread(
    context: AuthContext,
    record: ThreadRecord,
    *,
    shared: Callable[[], bool] = lambda: False,
) -> bool:
    """Owner, a holder of view_all_threads in that project, or a viewer it was shared with."""
    if record.project_id not in context.project_ids:
        return False
    if record.user_id == context.user_id:
        return True
    if "view_all_threads" in context.capabilities:
        return True
    return "view_shared" in context.capabilities and shared()


@router.post("/projects/{project_id}/threads", status_code=201)
def create_thread(
    project_id: UUID,
    context: AskQuestionContext,
    threads: ThreadsDependency,
    audit: AuditDependency,
) -> dict[str, str]:
    require_project_access(context, project_id)
    thread_id = str(uuid.uuid4())
    threads.create(thread_id, str(project_id), context.user_id)
    audit.record(context.user_id, "thread_created", "thread", thread_id, {})
    return {"id": thread_id, "project_id": str(project_id)}


@router.get("/projects/{project_id}/threads")
def list_threads(
    project_id: UUID,
    context: AskQuestionContext,
    threads: ThreadsDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    """The caller's own conversations in this project, newest activity first."""
    require_project_access(context, project_id)
    items = threads.list_threads(str(project_id), context.user_id, limit)
    return {
        "items": [
            {"id": t.id, "title": t.title, "turns": t.turns, "last_activity": t.last_activity}
            for t in items
        ]
    }


@router.get("/threads/{thread_id}")
def get_thread(
    thread_id: UUID,
    context: AskQuestionContext,
    threads: ThreadsDependency,
    store: Annotated[PostgresCollabStore, Depends(get_collab_store)],
) -> dict[str, Any]:
    record = threads.get(str(thread_id))
    if record is None or not can_read_thread(
        context,
        record,
        shared=lambda: store.has_share("thread", record.id, context.user_id, context.role_name),
    ):
        raise HTTPException(status_code=404, detail="Thread not found")
    return {
        "id": record.id,
        "project_id": record.project_id,
        "owner_id": record.user_id,
        "messages": [
            {
                "id": message.id,
                "question": message.turn.question,
                "standalone_question": message.turn.standalone_question,
                # Logical SQL uses business names and is safe for every reader; compiled SQL is
                # never stored on a message.
                "logical_sql": message.turn.logical_sql,
                "answer": message.turn.answer,
                "summary": message.turn.summary,
                "chart": message.turn.chart,
                "suggestions": list(message.turn.suggestions),
                "latency_ms": message.turn.latency_ms,
                "trace": message.turn.trace,
                "model_version": message.turn.model_version,
                "failure": (
                    {"code": message.turn.failure_code, "message": message.turn.failure_message}
                    if message.turn.failure_code
                    else None
                ),
                "created_at": message.created_at,
            }
            for message in threads.messages(record.id)
        ],
    }


@router.post("/threads/{thread_id}/ask")
def ask_in_thread(
    thread_id: UUID,
    body: ThreadAskRequest,
    context: AskQuestionContext,
    repository: RepositoryDependency,
    governance: GovernanceDependency,
    audit: AuditDependency,
    threads: ThreadsDependency,
) -> StreamingResponse:
    """text/event-stream in the section 8 contract; ``preview_as`` needs the rerun_sql capability."""
    record = _owned_thread(threads, context, thread_id)
    project_id = UUID(record.project_id)
    can_see_sql = "rerun_sql" in context.capabilities
    principal: Principal = _principal(context)
    preview = body.preview_as is not None and str(body.preview_as) != context.user_id
    if body.preview_as is not None:
        if not can_see_sql:
            raise HTTPException(status_code=403, detail="Missing capability: rerun_sql")
        if preview:
            principal = _load_principal(repository, context, body.preview_as)
            if record.project_id not in principal.project_ids:
                raise HTTPException(status_code=404, detail="User not found")
            audit.record(
                context.user_id, "preview_as_user", "thread", record.id,
                {"as_user_id": principal.user_id, "question": body.question[:500]},
            )
    prepared = _prepare(project_id, repository, audit)
    policies = [p.policy for p in governance.list_policies(str(project_id), enabled_only=True)]

    def events() -> Iterator[str]:
        try:
            for event in prepared.service.stream(
                str(project_id),
                prepared.version,
                prepared.mdl,
                principal,
                policies,
                body.question,
                include_compiled=can_see_sql,
                thread_id=record.id,
                max_rows=body.max_rows,
                history_user_id=context.user_id,
                remember=not preview,
                contract=True,
            ):
                yield _sse(event)  # flushed per event: StreamingResponse writes each chunk
        except Exception:
            logger.exception("Ask stream failed", extra={"thread_id": record.id})
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


@router.post("/threads/{thread_id}/rerun-sql")
def rerun_sql(
    thread_id: UUID,
    body: RerunRequest,
    context: RerunSqlContext,
    repository: RepositoryDependency,
    governance: GovernanceDependency,
    audit: AuditDependency,
    threads: ThreadsDependency,
) -> dict[str, Any]:
    """Run edited logical SQL through the same compiler, validator and limits as a question."""
    record = _owned_thread(threads, context, thread_id)
    project_id = UUID(record.project_id)
    prepared = _prepare(project_id, repository, audit)
    try:
        policies = [p.policy for p in governance.list_policies(str(project_id), enabled_only=True)]
        events, failure = prepared.service.rerun(
            str(project_id), prepared.version, prepared.mdl, _principal(context), policies,
            body.logical_sql, max_rows=body.max_rows,
        )
    finally:
        prepared.close()
    by_type = {event.type: event.data for event in events}
    if failure is not None:
        raise HTTPException(
            status_code=503 if failure.code == "LLM_UNAVAILABLE" else 422,
            detail={
                "code": failure.code,
                "message": failure.message,
                "retryable": failure.retryable,
                "errors": by_type.get("compilation", {}).get("errors", []),
            },
        )
    rows = by_type["rows"]
    return {
        "logical_sql": body.logical_sql,
        "sql": by_type.get("compilation", {}).get("sql"),
        "columns": rows["columns"],
        "rows": rows["rows"],
        "truncated": rows["truncated"],
        "row_count": rows["row_count"],
    }


@router.post("/messages/{message_id}/feedback", status_code=201)
def message_feedback(
    message_id: str,
    body: MessageFeedbackRequest,
    context: FeedbackContext,
    repository: RepositoryDependency,
    threads: ThreadsDependency,
    knowledge: KnowledgeDependency,
) -> dict[str, object]:
    """Thumbs on one answer; a thumbs-up proposes its question and SQL as a pair for review."""
    found = threads.get_message(message_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Message not found")
    record, message = found
    if record.user_id != context.user_id or record.project_id not in context.project_ids:
        raise HTTPException(status_code=404, detail="Message not found")
    if not message.turn.logical_sql:
        raise HTTPException(status_code=422, detail="This message has no SQL to rate")
    from app.api.knowledge_routes import _deployed_mdl, _mapped, _pair_json

    mdl = _deployed_mdl(repository, UUID(record.project_id))
    with _mapped():
        feedback, pair = knowledge.submit_feedback(
            record.project_id,
            user_id=context.user_id,
            rating=body.rating,
            question=message.turn.standalone_question,
            logical_sql=message.turn.logical_sql,
            thread_id=record.id,
            message_id=message.id,
            mdl=mdl,
        )
    return {
        "id": feedback.id,
        "rating": feedback.rating,
        "proposal": _pair_json(pair) if pair else None,
    }


@router.post("/messages/{message_id}/run")
def run_message(
    message_id: str,
    context: AskQuestionContext,
    repository: RepositoryDependency,
    governance: GovernanceDependency,
    threads: ThreadsDependency,
    audit: AuditDependency,
    max_rows: Annotated[int, Query(ge=1, le=MAX_ROWS_CEILING)] = 1000,
) -> dict[str, Any]:
    """Recompute a saved answer's rows now, under the caller's own policies (no model call).

    Rows are never stored with a chat, so a reopened conversation uses this to show its table.
    """
    from app.collab.runtime import runtime_factory
    from app.collab.service import Invalid
    from app.core.jsonable import json_value
    from app.semantic.runner import run_logical_sql

    found = threads.get_message(message_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Message not found")
    record, message = found
    if record.user_id != context.user_id or record.project_id not in context.project_ids:
        raise HTTPException(status_code=404, detail="Message not found")
    if not message.turn.logical_sql:
        raise HTTPException(status_code=422, detail="This message has no SQL to run")
    try:
        runtime = runtime_factory(repository, governance, get_settings())(record.project_id)
    except Invalid as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    try:
        outcome = run_logical_sql(
            message.turn.logical_sql, runtime.mdl, to_principal(context), runtime.policies,
            runtime.warehouse, min(max_rows, runtime.max_rows),
        )
    finally:
        runtime.close()
    audit.record(context.user_id, "message_rerun", "thread", record.id,
                 {"message_id": message.id, "ok": outcome.ok})
    if outcome.failure is not None:
        raise HTTPException(
            status_code=422,
            detail={"code": outcome.failure.code, "message": outcome.failure.message,
                    "retryable": outcome.failure.retryable},
        )
    rows = [[json_value(cell) for cell in row] for row in outcome.rows]
    response: dict[str, Any] = {
        "columns": outcome.columns, "rows": rows, "truncated": outcome.truncated,
        "row_count": len(rows),
    }
    if "rerun_sql" in context.capabilities:
        response["sql"] = outcome.sql  # compiled SQL is shown to accounts that may see it
    return response
