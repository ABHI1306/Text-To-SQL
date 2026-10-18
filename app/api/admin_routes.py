"""Day 6 admin routes: capability catalogue, account unlock and password reset."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.adapters.metadata.postgres import PostgresMetadataRepository
from app.api.dependencies import get_audit_service, get_repository, require_capability
from app.api.routes import _CAPABILITIES
from app.audit.service import AuditService
from app.auth.models import AuthContext
from app.auth.service import hash_password

router = APIRouter(prefix="/api/v1", tags=["Admin"])

RepositoryDependency = Annotated[PostgresMetadataRepository, Depends(get_repository)]
AuditDependency = Annotated[AuditService, Depends(get_audit_service)]
ManageRolesContext = Annotated[AuthContext, Depends(require_capability("manage_roles"))]
ManageUsersContext = Annotated[AuthContext, Depends(require_capability("manage_users"))]


class ResetPasswordRequest(BaseModel):
    """Body for an administrator-set password (minimum 12 characters)."""
    model_config = ConfigDict(extra="forbid")

    password: SecretStr = Field(min_length=12, max_length=1024)


def _find_user(repository: PostgresMetadataRepository, user_id: UUID) -> dict[str, object]:
    """Return the user row with ``user_id`` or raise a 404."""
    for user in repository.list_users():
        if str(user["id"]) == str(user_id):
            return user
    raise HTTPException(status_code=404, detail="User not found")


def _require_not_stronger(
    context: AuthContext, repository: PostgresMetadataRepository, user: dict[str, object]
) -> None:
    """A manager cannot act on an account holding capabilities they lack."""
    capabilities = repository.get_role_capabilities(str(user["role"])) or frozenset()
    if not set(capabilities) <= set(context.capabilities):
        raise HTTPException(
            status_code=403,
            detail="Cannot manage an account holding capabilities the current user does not hold",
        )


@router.get("/capabilities")
def list_capabilities(_context: ManageRolesContext) -> dict[str, object]:
    """List every capability name that a role can be granted."""
    return {"items": sorted(_CAPABILITIES)}


@router.post("/users/{user_id}/unlock")
def unlock_user(
    user_id: UUID,
    context: ManageUsersContext,
    repository: RepositoryDependency,
    audit: AuditDependency,
) -> dict[str, object]:
    """Clear the lockout on an account; refused for accounts stronger than the caller."""
    user = _find_user(repository, user_id)
    _require_not_stronger(context, repository, user)
    try:
        repository.unlock_user(str(user_id))
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    audit.record(context.user_id, "user_unlocked", "user", str(user_id), {})
    return {"user_id": str(user_id), "locked": False}


@router.delete("/users/{user_id}")
def delete_user(
    user_id: UUID,
    context: ManageUsersContext,
    repository: RepositoryDependency,
    audit: AuditDependency,
) -> dict[str, object]:
    """Delete an account, refusing self-deletion and removal of the last user manager."""
    if str(user_id) == str(context.user_id):
        raise HTTPException(status_code=422, detail="You cannot delete your own account")
    user = _find_user(repository, user_id)
    _require_not_stronger(context, repository, user)
    capabilities = repository.get_role_capabilities(str(user["role"])) or frozenset()
    if "manage_users" in capabilities and not repository.count_users_with_capability(
        "manage_users", str(user_id)
    ):
        raise HTTPException(status_code=409, detail="Cannot delete the last account that can manage users")
    try:
        repository.delete_user(str(user_id))
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    audit.record(context.user_id, "user_deleted", "user", str(user_id), {"username": user["username"]})
    return {"user_id": str(user_id), "deleted": True}


@router.post("/users/{user_id}/reset-password")
def reset_user_password(
    user_id: UUID,
    body: ResetPasswordRequest,
    context: ManageUsersContext,
    repository: RepositoryDependency,
    audit: AuditDependency,
) -> dict[str, object]:
    """Set a new password on a local account and force a change at next login."""
    user = _find_user(repository, user_id)
    if user.get("auth_source") != "local":
        raise HTTPException(status_code=422, detail="Only local accounts have a password to reset")
    _require_not_stronger(context, repository, user)
    try:
        repository.reset_local_user(
            str(user["username"]), hash_password(body.password.get_secret_value())
        )
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    audit.record(context.user_id, "user_password_reset", "user", str(user_id), {})
    return {"user_id": str(user_id), "must_change_password": True}
