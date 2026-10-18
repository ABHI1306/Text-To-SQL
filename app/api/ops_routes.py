"""Operations views: model usage per question and scheduler health."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response

from app.adapters.metadata.scheduler import PostgresJobStore
from app.api.dependencies import require_capability
from app.auth.models import AuthContext
from app.config import get_settings
from app.observability.readiness import configured_checks, run_checks
from app.observability.usage import METRICS

router = APIRouter(prefix="/api/v1", tags=["Operations"])
health_router = APIRouter(tags=["Health"])

ViewOps = Annotated[AuthContext, Depends(require_capability("view_audit"))]
OVERDUE_GRACE_SECONDS = 120


@health_router.get("/health/ready")
def ready(response: Response) -> dict[str, Any]:
    """Up or down for each dependency; 503 when any is down. Names only, no details."""
    results = run_checks(configured_checks(get_settings()))
    ok = all(value == "up" for value in results.values())
    if not ok:
        response.status_code = 503
    return {"status": "ok" if ok else "degraded", "dependencies": results}


@router.get("/ops/usage")
def usage(_context: ViewOps) -> dict[str, object]:
    """Tokens and model busy time per question, since this API process started."""
    return METRICS.snapshot()


@router.get("/ops/scheduler")
def scheduler_health(_context: ViewOps) -> dict[str, Any]:
    """Report scheduler job health; degraded when jobs are overdue or stale."""
    settings = get_settings()
    if not settings.metadata_database_url:
        raise HTTPException(status_code=503, detail="Metadata database is not configured")
    now = datetime.now(UTC)
    try:
        counts = dict(
            PostgresJobStore(settings.metadata_database_url).health(
                now, grace_seconds=max(OVERDUE_GRACE_SECONDS, settings.scheduler_poll_seconds * 4)
            )
        )
    except Exception as error:
        raise HTTPException(status_code=503, detail="Scheduler state is unavailable") from error
    # a worker that is up polls every poll interval, so overdue or stale jobs mean it is not
    healthy = not counts.get("overdue") and not counts.get("stale")
    return {"status": "ok" if healthy else "degraded", "checked_at": now.isoformat(), **counts}
