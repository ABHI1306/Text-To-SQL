"""Day 6 routes: the profiling schedule, drift reports and drafts created from them."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.adapters.metadata.drift import PostgresDriftRepository
from app.adapters.metadata.postgres import PostgresMetadataRepository
from app.adapters.metadata.scheduler import PostgresJobStore
from app.api.dependencies import (
    get_audit_service,
    get_repository,
    require_capability,
    require_project_access,
)
from app.audit.service import AuditService
from app.auth.models import AuthContext
from app.config import get_settings
from app.ports.drift import DriftRepoPort, StoredDriftReport
from app.ports.scheduler import JOB_KIND_PROFILE_DRIFT, JobStorePort, ScheduledJob
from app.scheduler.cron import validate_schedule
from app.semantic.drift import DriftApplyError, DriftFinding, apply_drift
from app.semantic.drift_job import load_mdl

router = APIRouter(prefix="/api/v1", tags=["Drift & Profiling"])

RepositoryDependency = Annotated[PostgresMetadataRepository, Depends(get_repository)]
AuditDependency = Annotated[AuditService, Depends(get_audit_service)]
ManageProfilingContext = Annotated[AuthContext, Depends(require_capability("manage_profiling"))]
EditMdlContext = Annotated[AuthContext, Depends(require_capability("edit_mdl"))]


@lru_cache
def get_drift_repository() -> PostgresDriftRepository:
    settings = get_settings()
    if not settings.metadata_database_url:
        raise HTTPException(status_code=503, detail="Metadata database is not configured")
    return PostgresDriftRepository(settings.metadata_database_url)


@lru_cache
def get_job_store() -> PostgresJobStore:
    settings = get_settings()
    if not settings.metadata_database_url:
        raise HTTPException(status_code=503, detail="Metadata database is not configured")
    return PostgresJobStore(settings.metadata_database_url)


DriftDependency = Annotated[DriftRepoPort, Depends(get_drift_repository)]
JobStoreDependency = Annotated[JobStorePort, Depends(get_job_store)]


class ScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schedule: str = Field(min_length=9, max_length=100, description="Five-field cron expression")
    timezone: str = Field(default="UTC", min_length=1, max_length=64)


def _job_json(job: ScheduledJob) -> dict[str, object]:
    return {
        "id": job.id,
        "projectId": job.project_id,
        "ownerId": job.owner_id,
        "kind": job.kind,
        "schedule": job.schedule,
        "timezone": job.timezone,
        "status": job.status,
        "pausedReason": job.paused_reason,
        "nextRunAt": job.next_run_at.isoformat(),
        "lastRunAt": job.last_run_at.isoformat() if job.last_run_at else None,
        "lastStatus": job.last_status,
    }


def _report_json(report: StoredDriftReport) -> dict[str, object]:
    return {
        "id": report.id,
        "projectId": report.project_id,
        "mdlVersion": report.mdl_version,
        "hasDrift": report.has_drift,
        "summary": report.summary,
        "findings": report.findings,
        "draftMdlVersion": report.draft_mdl_version,
        "createdBy": report.created_by,
        "createdAt": report.created_at.isoformat(),
    }


@router.get("/projects/{project_id}/profiling-schedule")
def get_profiling_schedule(
    project_id: UUID, context: ManageProfilingContext, jobs: JobStoreDependency
) -> dict[str, object]:
    require_project_access(context, project_id)
    job = jobs.get_for_project(str(project_id), JOB_KIND_PROFILE_DRIFT)
    if job is None:
        raise HTTPException(status_code=404, detail="No profiling schedule is set")
    return _job_json(job)


@router.put("/projects/{project_id}/profiling-schedule")
def set_profiling_schedule(
    project_id: UUID,
    body: ScheduleRequest,
    context: ManageProfilingContext,
    jobs: JobStoreDependency,
    audit: AuditDependency,
) -> dict[str, object]:
    """Schedule profiling and drift detection; the job runs as the caller (its owner)."""
    require_project_access(context, project_id)
    try:
        first_slot = validate_schedule(body.schedule, body.timezone, datetime.now(UTC))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    job = jobs.upsert_for_project(
        str(project_id),
        context.user_id,
        JOB_KIND_PROFILE_DRIFT,
        " ".join(body.schedule.split()),
        body.timezone,
        first_slot,
    )
    audit.record(
        context.user_id,
        "profiling_schedule_set",
        "project",
        str(project_id),
        {"schedule": job.schedule, "timezone": job.timezone},
    )
    return _job_json(job)


@router.delete("/projects/{project_id}/profiling-schedule", status_code=status.HTTP_204_NO_CONTENT)
def delete_profiling_schedule(
    project_id: UUID,
    context: ManageProfilingContext,
    jobs: JobStoreDependency,
    audit: AuditDependency,
) -> Response:
    require_project_access(context, project_id)
    if not jobs.delete_for_project(str(project_id), JOB_KIND_PROFILE_DRIFT):
        raise HTTPException(status_code=404, detail="No profiling schedule is set")
    audit.record(context.user_id, "profiling_schedule_removed", "project", str(project_id), {})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/projects/{project_id}/drift-reports")
def list_drift_reports(
    project_id: UUID,
    context: ManageProfilingContext,
    reports: DriftDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[datetime | None, Query(description="createdAt of the last item seen")] = None,
) -> dict[str, object]:
    require_project_access(context, project_id)
    items = reports.list_reports(str(project_id), limit=limit, before=cursor)
    return {
        "items": [_report_json(item) for item in items],
        "nextCursor": items[-1].created_at.isoformat() if len(items) == limit else None,
    }


@router.post(
    "/projects/{project_id}/drift-reports/{report_id}/draft",
    status_code=status.HTTP_201_CREATED,
)
def create_draft_from_drift(
    project_id: UUID,
    report_id: UUID,
    context: EditMdlContext,
    reports: DriftDependency,
    repository: RepositoryDependency,
) -> dict[str, object]:
    """Apply a report's findings to the MDL it compared and save the result as a new draft."""
    require_project_access(context, project_id)
    report = reports.get_report(str(project_id), str(report_id))
    if report is None:
        raise HTTPException(status_code=404, detail="Drift report not found")
    if not report.has_drift:
        raise HTTPException(status_code=409, detail="The report found no drift; nothing to draft")
    if report.draft_mdl_version is not None:
        raise HTTPException(
            status_code=409,
            detail=f"A draft (revision {report.draft_mdl_version}) was already created",
        )
    document = repository.get_mdl(str(project_id), report.mdl_version)
    if document is None:
        raise HTTPException(status_code=409, detail="The compared MDL revision no longer exists")
    try:
        draft = apply_drift(
            load_mdl(document),
            tuple(DriftFinding.from_json(item) for item in report.findings),
        )
    except DriftApplyError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    revision = reports.create_draft(
        str(project_id),
        str(report_id),
        draft.model_dump(by_alias=True, mode="json"),
        created_by=context.user_id,
    )
    if revision is None:  # lost a race with another request for the same report
        raise HTTPException(status_code=409, detail="A draft was already created for this report")
    return {
        "projectId": str(project_id),
        "reportId": str(report_id),
        "revision": revision,
        "baseRevision": report.mdl_version,
        "status": "draft",
    }
