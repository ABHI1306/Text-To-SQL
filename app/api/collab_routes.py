"""Saved questions, dashboards, scheduled questions, shares, notifications and host settings."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.adapters.metadata.collab import PostgresCollabStore
from app.adapters.metadata.threads import PostgresThreadStore
from app.api.dependencies import (
    ALLOWED_HOSTS_SETTING,
    get_audit_service,
    get_collab_service,
    get_collab_store,
    get_current_context,
    get_thread_store,
    require_capability,
)
from app.api.security import validate_allowed_hosts
from app.audit.service import AuditService
from app.auth.models import AuthContext
from app.collab.service import CollabService, Invalid, NotFound, QueryFailed
from app.config import get_settings
from app.ports.scheduler import ScheduledJob

router = APIRouter(prefix="/api/v1")

CollabDependency = Annotated[CollabService, Depends(get_collab_service)]
StoreDependency = Annotated[PostgresCollabStore, Depends(get_collab_store)]
ThreadsDependency = Annotated[PostgresThreadStore, Depends(get_thread_store)]
AuditDependency = Annotated[AuditService, Depends(get_audit_service)]
BuildContext = Annotated[AuthContext, Depends(require_capability("create_dashboard"))]
ViewContext = Annotated[AuthContext, Depends(require_capability("view_shared"))]
ScheduleContext = Annotated[AuthContext, Depends(require_capability("schedule_question"))]
ShareContext = Annotated[AuthContext, Depends(require_capability("share"))]
ConnectionContext = Annotated[AuthContext, Depends(require_capability("manage_connection"))]
SignedIn = Annotated[AuthContext, Depends(get_current_context)]

MAX_ROWS_CEILING = 10_000


@contextmanager
def _mapped() -> Iterator[None]:
    try:
        yield
    except NotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except Invalid as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except QueryFailed as error:
        failure = error.failure
        raise HTTPException(
            status_code=503 if failure.code == "LLM_UNAVAILABLE" else 422,
            detail={"code": failure.code, "message": failure.message,
                    "retryable": failure.retryable},
        ) from error


# ------------------------------------------------------------------ saved questions


class SavedQuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)
    logical_sql: str = Field(min_length=1, max_length=20_000)
    chart_spec: dict[str, Any] | None = None


@router.get("/projects/{project_id}/saved-questions", tags=["Dashboards"])
def list_saved_questions(
    project_id: UUID, context: BuildContext, service: CollabDependency
) -> list[dict[str, Any]]:
    with _mapped():
        return service.list_saved_questions(context, str(project_id))


@router.post("/projects/{project_id}/saved-questions", tags=["Dashboards"], status_code=201)
def create_saved_question(
    project_id: UUID, body: SavedQuestionRequest, context: BuildContext, service: CollabDependency
) -> dict[str, Any]:
    with _mapped():
        return service.save_question(
            context, str(project_id), body.question, body.logical_sql, body.chart_spec
        )


@router.delete("/projects/{project_id}/saved-questions/{question_id}", tags=["Dashboards"],
               status_code=204)
def delete_saved_question(
    project_id: UUID, question_id: UUID, context: BuildContext, service: CollabDependency
) -> Response:
    with _mapped():
        record = service.get_saved_question(context, str(question_id))
        if record["project_id"] != str(project_id):
            raise NotFound("Saved question not found")
        service.delete_saved_question(context, str(question_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/saved-questions/{question_id}/result", tags=["Dashboards"])
def saved_question_result(
    question_id: UUID,
    context: ViewContext,
    service: CollabDependency,
    max_rows: Annotated[int, Query(ge=1, le=MAX_ROWS_CEILING)] = 1000,
) -> dict[str, Any]:
    """Always computed under the caller's own policies."""
    with _mapped():
        return service.saved_question_result(context, str(question_id), max_rows)


# ------------------------------------------------------------------ dashboards


class DashboardCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    title: str = Field(min_length=1, max_length=200)
    layout: dict[str, Any] = Field(default_factory=dict)


class DashboardPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    layout: dict[str, Any] | None = None


class TileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    saved_question_id: UUID
    chart_spec: dict[str, Any] | None = None
    position: dict[str, Any] = Field(default_factory=dict)
    refresh_schedule: str | None = Field(default=None, max_length=100)


class TilesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tiles: list[TileIn] = Field(max_length=24)


@router.get("/dashboards", tags=["Dashboards"])
def list_dashboards(
    project_id: Annotated[UUID, Query()], context: BuildContext, service: CollabDependency
) -> list[dict[str, Any]]:
    with _mapped():
        return service.list_dashboards(context, str(project_id))


@router.post("/dashboards", tags=["Dashboards"], status_code=201)
def create_dashboard(
    body: DashboardCreate, context: BuildContext, service: CollabDependency
) -> dict[str, Any]:
    with _mapped():
        return service.create_dashboard(context, str(body.project_id), body.title, body.layout)


@router.get("/dashboards/{dashboard_id}", tags=["Dashboards"])
def get_dashboard(
    dashboard_id: UUID, context: ViewContext, service: CollabDependency
) -> dict[str, Any]:
    with _mapped():
        return service.get_dashboard(context, str(dashboard_id))


@router.patch("/dashboards/{dashboard_id}", tags=["Dashboards"])
def patch_dashboard(
    dashboard_id: UUID, body: DashboardPatch, context: BuildContext, service: CollabDependency
) -> dict[str, Any]:
    with _mapped():
        return service.update_dashboard(context, str(dashboard_id), body.title, body.layout)


@router.delete("/dashboards/{dashboard_id}", tags=["Dashboards"], status_code=204)
def delete_dashboard(
    dashboard_id: UUID, context: BuildContext, service: CollabDependency
) -> Response:
    with _mapped():
        service.delete_dashboard(context, str(dashboard_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/dashboards/{dashboard_id}/tiles", tags=["Dashboards"])
def put_tiles(
    dashboard_id: UUID, body: TilesRequest, context: BuildContext, service: CollabDependency
) -> list[dict[str, Any]]:
    tiles = [
        {"saved_question_id": str(t.saved_question_id), "chart_spec": t.chart_spec,
         "position": t.position, "refresh_schedule": t.refresh_schedule}
        for t in body.tiles
    ]
    with _mapped():
        return service.put_tiles(context, str(dashboard_id), tiles)


@router.post("/dashboards/{dashboard_id}/refresh", tags=["Dashboards"])
def refresh_dashboard(
    dashboard_id: UUID, context: BuildContext, service: CollabDependency
) -> dict[str, int]:
    with _mapped():
        return {"refreshed_tiles": service.refresh_dashboard(context, str(dashboard_id))}


@router.get("/dashboards/{dashboard_id}/tiles/{tile_id}/result", tags=["Dashboards"])
def tile_result(
    dashboard_id: UUID,
    tile_id: UUID,
    context: ViewContext,
    service: CollabDependency,
    max_rows: Annotated[int, Query(ge=1, le=MAX_ROWS_CEILING)] = 1000,
) -> dict[str, Any]:
    """A tile's rows, always computed under the viewer's own policies."""
    with _mapped():
        return service.tile_result(context, str(dashboard_id), str(tile_id), max_rows)


# ------------------------------------------------------------------ scheduled questions


class ScheduleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    saved_question_id: UUID
    schedule: str = Field(min_length=1, max_length=100)
    timezone: str = Field(default="UTC", max_length=64)


class SchedulePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schedule: str | None = Field(default=None, min_length=1, max_length=100)
    timezone: str | None = Field(default=None, max_length=64)


def _job_json(job: ScheduledJob) -> dict[str, Any]:
    return {
        "id": job.id, "project_id": job.project_id, "saved_question_id": job.saved_question_id,
        "schedule": job.schedule, "timezone": job.timezone, "status": job.status,
        "paused_reason": job.paused_reason, "next_run_at": job.next_run_at,
        "last_run_at": job.last_run_at, "last_status": job.last_status,
        "failed": job.last_status == "failed",
    }


@router.get("/scheduled-questions", tags=["Scheduled questions"])
def list_scheduled(context: ScheduleContext, service: CollabDependency) -> list[dict[str, Any]]:
    return [_job_json(job) for job in service.list_scheduled(context)]


@router.post("/scheduled-questions", tags=["Scheduled questions"], status_code=201)
def create_scheduled(
    body: ScheduleCreate, context: ScheduleContext, service: CollabDependency
) -> dict[str, Any]:
    with _mapped():
        return _job_json(
            service.schedule_question(
                context, str(body.saved_question_id), body.schedule, body.timezone
            )
        )


@router.get("/scheduled-questions/{job_id}", tags=["Scheduled questions"])
def get_scheduled(job_id: UUID, context: ScheduleContext, service: CollabDependency) -> dict[str, Any]:
    with _mapped():
        return _job_json(service.get_scheduled(context, str(job_id)))


@router.patch("/scheduled-questions/{job_id}", tags=["Scheduled questions"])
def patch_scheduled(
    job_id: UUID, body: SchedulePatch, context: ScheduleContext, service: CollabDependency
) -> dict[str, Any]:
    with _mapped():
        return _job_json(service.update_schedule(context, str(job_id), body.schedule, body.timezone))


@router.delete("/scheduled-questions/{job_id}", tags=["Scheduled questions"], status_code=204)
def delete_scheduled(job_id: UUID, context: ScheduleContext, service: CollabDependency) -> Response:
    with _mapped():
        service.delete_scheduled(context, str(job_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/scheduled-questions/{job_id}/run-now", tags=["Scheduled questions"])
def run_now(
    job_id: UUID,
    context: ScheduleContext,
    service: CollabDependency,
    idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
) -> dict[str, Any]:
    with _mapped():
        return service.run_now(context, str(job_id), idempotency_key)


@router.post("/scheduled-questions/{job_id}/pause", tags=["Scheduled questions"], status_code=204)
def pause_scheduled(job_id: UUID, context: ScheduleContext, service: CollabDependency) -> Response:
    with _mapped():
        service.pause(context, str(job_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/scheduled-questions/{job_id}/resume", tags=["Scheduled questions"])
def resume_scheduled(
    job_id: UUID, context: ScheduleContext, service: CollabDependency
) -> dict[str, Any]:
    with _mapped():
        return _job_json(service.resume(context, str(job_id)))


@router.get("/scheduled-questions/{job_id}/runs", tags=["Scheduled questions"])
def scheduled_runs(
    job_id: UUID, context: ScheduleContext, service: CollabDependency
) -> list[dict[str, Any]]:
    with _mapped():
        return service.list_runs(context, str(job_id))


# ------------------------------------------------------------------ shares


class ShareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_type: Literal["dashboard", "saved_question", "thread"]
    resource_id: UUID
    grantee_type: Literal["user", "role"]
    grantee_id: str = Field(min_length=1, max_length=200)


def _thread_owner(threads: PostgresThreadStore) -> Any:
    def lookup(thread_id: str) -> tuple[str, str] | None:
        record = threads.get(thread_id)
        return (record.user_id, record.project_id) if record else None

    return lookup


@router.get("/shares", tags=["Sharing"])
def list_shares(
    resource_type: Annotated[Literal["dashboard", "saved_question", "thread"], Query()],
    resource_id: Annotated[UUID, Query()],
    context: ShareContext,
    service: CollabDependency,
    threads: ThreadsDependency,
) -> list[dict[str, Any]]:
    with _mapped():
        return service.list_shares(
            context, resource_type, str(resource_id), thread_owner=_thread_owner(threads)
        )


@router.post("/shares", tags=["Sharing"], status_code=201)
def create_share(
    body: ShareRequest, context: ShareContext, service: CollabDependency, threads: ThreadsDependency
) -> dict[str, Any]:
    with _mapped():
        return service.create_share(
            context, body.resource_type, str(body.resource_id), body.grantee_type,
            body.grantee_id, thread_owner=_thread_owner(threads),
        )


@router.delete("/shares/{share_id}", tags=["Sharing"], status_code=204)
def delete_share(
    share_id: UUID, context: ShareContext, service: CollabDependency, threads: ThreadsDependency
) -> Response:
    with _mapped():
        service.revoke_share(context, str(share_id), thread_owner=_thread_owner(threads))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/shared-with-me", tags=["Sharing"])
def shared_with_me(context: ViewContext, service: CollabDependency) -> list[dict[str, Any]]:
    return service.shared_with_me(context)


# ------------------------------------------------------------------ notifications


@router.get("/notifications", tags=["Notifications"])
def list_notifications(
    context: SignedIn, store: StoreDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[dict[str, Any]]:
    return store.list_notifications(context.user_id, limit)


@router.post("/notifications/{notification_id}/read", tags=["Notifications"], status_code=204)
def mark_notification_read(
    notification_id: UUID, context: SignedIn, store: StoreDependency
) -> Response:
    if not store.mark_notification_read(context.user_id, str(notification_id)):
        raise HTTPException(status_code=404, detail="Notification not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------------ installation settings


class AllowedHostsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hosts: list[str] = Field(max_length=100)


@router.get("/settings/allowed-hosts", tags=["Admin"])
def get_allowed_hosts(context: ConnectionContext, store: StoreDependency) -> dict[str, Any]:
    stored = store.get_setting(ALLOWED_HOSTS_SETTING)
    seeded = isinstance(stored, list)
    return {
        "hosts": stored if seeded else list(get_settings().allowed_database_hosts),
        "source": "database" if seeded else "environment",
    }


@router.put("/settings/allowed-hosts", tags=["Admin"])
def put_allowed_hosts(
    body: AllowedHostsRequest, context: ConnectionContext, store: StoreDependency,
    audit: AuditDependency,
) -> dict[str, Any]:
    try:
        hosts = validate_allowed_hosts(body.hosts)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    store.set_setting(ALLOWED_HOSTS_SETTING, hosts, context.user_id)
    audit.record(context.user_id, "allowed_hosts_changed", "setting", ALLOWED_HOSTS_SETTING,
                 {"hosts": hosts})
    return {"hosts": hosts, "source": "database"}
