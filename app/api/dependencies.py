from collections.abc import Callable
from functools import lru_cache
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.adapters.metadata.governance import PostgresGovernanceRepository
from app.adapters.metadata.postgres import PostgresMetadataRepository
from app.audit.service import AuditService
from app.auth.models import AuthContext
from app.auth.service import (
    AuthenticationError,
    AuthService,
    PasswordChangeRequired,
)
from app.config import get_settings

bearer = HTTPBearer(auto_error=False)


@lru_cache
def get_repository() -> PostgresMetadataRepository:
    settings = get_settings()
    if not settings.metadata_database_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Metadata database is not configured",
        )
    return PostgresMetadataRepository(settings.metadata_database_url)


@lru_cache
def get_auth_service() -> AuthService:
    settings = get_settings()
    if not settings.jwt_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not configured; set JWT_SECRET",
        )
    return AuthService(
        get_repository(),
        jwt_secret=settings.jwt_secret,
        access_token_ttl_seconds=settings.access_token_ttl_seconds,
        refresh_token_ttl_seconds=settings.refresh_token_ttl_seconds,
        max_failures=settings.login_max_failures,
        lockout_seconds=settings.login_lockout_seconds,
    )


def _current_context(
    credentials: HTTPAuthorizationCredentials | None,
    auth_service: AuthService,
    *,
    allow_password_change: bool,
) -> AuthContext:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return auth_service.authenticate_access_token(
            credentials.credentials,
            allow_password_change=allow_password_change,
        )
    except PasswordChangeRequired as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "PASSWORD_CHANGE_REQUIRED", "message": str(error)},
        ) from error
    except AuthenticationError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(error),
            headers={"WWW-Authenticate": "Bearer"},
        ) from error


def get_current_context(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> AuthContext:
    return _current_context(credentials, auth_service, allow_password_change=False)


def get_password_change_context(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> AuthContext:
    return _current_context(credentials, auth_service, allow_password_change=True)


def require_capability(capability: str) -> Callable[..., AuthContext]:
    def check(context: Annotated[AuthContext, Depends(get_current_context)]) -> AuthContext:
        if capability not in context.capabilities:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing capability: {capability}",
            )
        return context

    check.required_capability = capability  # type: ignore[attr-defined]
    return check


@lru_cache
def get_governance_repository() -> PostgresGovernanceRepository:
    settings = get_settings()
    if not settings.metadata_database_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Metadata database is not configured",
        )
    return PostgresGovernanceRepository(settings.metadata_database_url)


def get_audit_service(
    repository: Annotated[PostgresGovernanceRepository, Depends(get_governance_repository)],
) -> AuditService:
    return AuditService(repository)


def require_project_access(context: AuthContext, project_id: UUID) -> None:
    if str(project_id) not in context.project_ids:
        raise HTTPException(status_code=404, detail="Project not found")
