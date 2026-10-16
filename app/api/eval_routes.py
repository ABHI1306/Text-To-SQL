"""Evaluation endpoints: start a run (idempotent) and read its report."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from fastapi.responses import PlainTextResponse

from app.adapters.metadata.collab import PostgresCollabStore
from app.adapters.metadata.governance import PostgresGovernanceRepository
from app.adapters.metadata.postgres import PostgresMetadataRepository
from app.api.dependencies import (
    get_audit_service,
    get_collab_store,
    get_governance_repository,
    get_repository,
    require_capability,
    require_project_access,
)
from app.audit.service import AuditService
from app.auth.models import AuthContext
from app.config import get_settings
from app.evaluation.backend import backend_factory
from app.evaluation.execution import EvalService, render_markdown

router = APIRouter(prefix="/api/v1", tags=["Evaluation"])

RunEvalContext = Annotated[AuthContext, Depends(require_capability("run_eval"))]
StoreDependency = Annotated[PostgresCollabStore, Depends(get_collab_store)]
AuditDependency = Annotated[AuditService, Depends(get_audit_service)]


def get_eval_service(
    store: StoreDependency,
    repository: Annotated[PostgresMetadataRepository, Depends(get_repository)],
    governance: Annotated[PostgresGovernanceRepository, Depends(get_governance_repository)],
    audit: AuditDependency,
) -> EvalService:
    return EvalService(store, backend_factory(repository, governance, get_settings()), audit)


EvalDependency = Annotated[EvalService, Depends(get_eval_service)]


@router.post("/projects/{project_id}/eval/run", status_code=202)
def start_eval(
    project_id: UUID,
    context: RunEvalContext,
    service: EvalDependency,
    idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
) -> dict[str, Any]:
    """Starts an evaluation in the background; the same Idempotency-Key returns the same run."""
    require_project_access(context, project_id)
    run, created = service.start(
        str(project_id), context.user_id, idempotency_key or str(uuid.uuid4())
    )
    return {"run_id": run["id"], "status": run["status"], "created": created}


@router.get("/eval/{run_id}", response_model=None)
def get_eval(
    run_id: UUID,
    context: RunEvalContext,
    service: EvalDependency,
    format: Annotated[Literal["json", "markdown"], Query()] = "json",
) -> Response | dict[str, Any]:
    report = service.report(str(run_id))
    if report is None or str(report["run"]["project_id"]) not in context.project_ids:
        raise HTTPException(status_code=404, detail="Evaluation run not found")
    if format == "markdown":
        return PlainTextResponse(render_markdown(report), media_type="text/markdown")
    return report
