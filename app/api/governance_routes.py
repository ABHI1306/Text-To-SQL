"""Day 4 routes: compile preview, row policies and the audit browser."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, cast
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.adapters.metadata.governance import PostgresGovernanceRepository
from app.adapters.metadata.postgres import PostgresMetadataRepository
from app.api.dependencies import (
    get_audit_service,
    get_governance_repository,
    get_repository,
    require_capability,
    require_project_access,
)
from app.audit.service import AuditService
from app.auth.models import AuthContext
from app.core.types import JsonValue, Principal
from app.domain.mdl import Mdl
from app.domain.policy import PolicyScope, RowPolicy
from app.policy.service import PolicyNotFound, PolicyRejected, PolicyService, SavedPolicy
from app.ports.audit import AuditFilter, AuditRecord
from app.ports.policy_repo import StoredPolicy
from app.semantic.compiler import CompiledQuery, CompileError, compile_query
from app.semantic.deploy_report import build_deploy_report
from app.semantic.validator import validate_compiled_sql

router = APIRouter(prefix="/api/v1")

SAMPLE_ROWS = 10

RepositoryDependency = Annotated[PostgresMetadataRepository, Depends(get_repository)]
GovernanceDependency = Annotated[PostgresGovernanceRepository, Depends(get_governance_repository)]
AuditDependency = Annotated[AuditService, Depends(get_audit_service)]
EditMdlContext = Annotated[AuthContext, Depends(require_capability("edit_mdl"))]
ManagePoliciesContext = Annotated[AuthContext, Depends(require_capability("manage_policies"))]
DeployMdlContext = Annotated[AuthContext, Depends(require_capability("deploy_mdl"))]
ManageRolesContext = Annotated[AuthContext, Depends(require_capability("manage_roles"))]
ViewAuditContext = Annotated[AuthContext, Depends(require_capability("view_audit"))]


class CompilePreviewRequest(BaseModel):
    """Body for a compile preview: logical SQL, optional draft MDL and preview user."""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    logical_sql: str = Field(min_length=1, max_length=20_000, alias="logicalSql")
    mdl: Mdl | None = None
    preview_as_user_id: UUID | None = Field(default=None, alias="previewAsUserId")
    sample: bool = False


class PolicyRequest(BaseModel):
    """Body for a row policy, plus the user it is test-compiled as."""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=64)
    predicate: str = Field(min_length=1, max_length=2000)
    applies_to: PolicyScope = Field(default_factory=PolicyScope, alias="appliesTo")
    enabled: bool = True
    test_user_id: UUID = Field(alias="testUserId")

    def to_policy(self) -> RowPolicy:
        """Build the domain ``RowPolicy``; raises ``ValueError`` if the policy is invalid."""
        return RowPolicy(
            name=self.name,
            model=self.model,
            predicate=self.predicate,
            appliesTo=self.applies_to,
        )


def _error_json(error: CompileError) -> dict[str, object]:
    """Serialise a compiler error with its code, identifier and suggestions."""
    return {
        "code": error.code.value,
        "message": error.message,
        "identifier": error.identifier,
        "suggestions": list(error.suggestions),
    }


def _mdl_from_document(document: dict[str, object]) -> Mdl:
    """Build an ``Mdl`` from a stored document, dropping storage-only keys."""
    return Mdl.model_validate(
        {key: value for key, value in document.items() if key not in {"project_id", "revision"}}
    )


def _principal(context: AuthContext) -> Principal:
    """Convert the authenticated context into the ``Principal`` the compiler enforces."""
    return Principal(
        user_id=context.user_id,
        role_name=context.role_name,
        capabilities=context.capabilities,
        project_ids=context.project_ids,
        attributes=cast(dict[str, JsonValue], context.attributes or {}),
    )


def _load_principal(
    repository: PostgresMetadataRepository, context: AuthContext, user_id: UUID
) -> Principal:
    """Load another account's principal to preview as it, if the caller may.

    The target must share a project (DBA excepted). Non-self previews need ``manage_policies``
    and an account that holds no capability the caller lacks.
    """
    target = repository.get_auth_context(str(user_id))
    if target is None or not (
        context.role_name == "DBA" or set(target.project_ids) & set(context.project_ids)
    ):
        raise HTTPException(status_code=404, detail="User not found")
    if str(user_id) != context.user_id and context.role_name != "DBA":
        # Seeing another account's rows is a row-policy decision: it is for policy managers, and
        # never for an account the caller could not already act as.
        if "manage_policies" not in context.capabilities:
            raise HTTPException(status_code=403, detail="Missing capability: manage_policies")
        if not set(target.capabilities) <= set(context.capabilities):
            raise HTTPException(
                status_code=403, detail="Cannot preview as an account holding more capabilities"
            )
    return _principal(target)


def _policy_json(stored: StoredPolicy) -> dict[str, object]:
    """Serialise a stored row policy into its API shape."""
    return {
        "id": stored.id,
        "name": stored.policy.name,
        "model": stored.policy.model,
        "predicate": stored.policy.predicate,
        "appliesTo": stored.policy.applies_to.model_dump(by_alias=True),
        "onMissingAttribute": stored.policy.on_missing_attribute.value,
        "enabled": stored.enabled,
        "updatedBy": stored.updated_by,
        "updatedAt": stored.updated_at.isoformat(),
    }


def _saved_json(saved: SavedPolicy) -> dict[str, object]:
    """Serialise a saved policy with its validation warnings and test compilation."""
    compilation = saved.validation.test_compilation
    return {
        **_policy_json(saved.stored),
        "validation": {
            "warnings": list(saved.validation.warnings),
            "testCompilation": _compiled_json(compilation) if compilation else None,
        },
    }


def _compiled_json(compiled: CompiledQuery) -> dict[str, object]:
    """Serialise a compile result: SQL, parameters, applied policies and errors."""
    return {
        "ok": compiled.ok,
        "sql": compiled.sql,
        "parameters": dict(compiled.parameters),
        "policyContext": compiled.policy_context,
        "appliedPolicies": list(compiled.applied_policies),
        "missingAttributes": list(compiled.missing_attributes),
        "physicalRelations": list(compiled.physical_relations),
        "errors": [_error_json(error) for error in compiled.errors],
    }


@router.post("/projects/{project_id}/compile-preview", tags=["Governance"])
def compile_preview(
    project_id: UUID,
    body: CompilePreviewRequest,
    context: EditMdlContext,
    repository: RepositoryDependency,
    governance: GovernanceDependency,
    audit: AuditDependency,
) -> dict[str, object]:
    """Compile logical SQL for a project, optionally as another user (``edit_mdl``).

    A sample run needs a preview user and the saved model, and every as-user preview is audited.
    """
    require_project_access(context, project_id)
    if body.sample and (body.preview_as_user_id is None or body.mdl is not None):
        # A sample reads the warehouse, so it must run as a named account's row policies and
        # against the saved model, never against a model or identity the caller supplies.
        raise HTTPException(
            status_code=422,
            detail="A sample needs a preview user and uses the saved model",
        )
    if body.mdl is not None:
        mdl = body.mdl
    else:
        document = repository.get_mdl(str(project_id), None)
        if document is None:
            raise HTTPException(status_code=404, detail="Project has no saved MDL")
        mdl = _mdl_from_document(document)
    principal: Principal | None = None
    policies: list[RowPolicy] = []
    if body.preview_as_user_id is not None:
        principal = _load_principal(repository, context, body.preview_as_user_id)
        policies = [
            stored.policy for stored in governance.list_policies(str(project_id), enabled_only=True)
        ]
    compiled = compile_query(body.logical_sql, mdl, principal, policies)
    response: dict[str, object] = {
        **_compiled_json(compiled),
        "preview": {
            "asUserId": str(body.preview_as_user_id) if body.preview_as_user_id else None,
            "labeled": principal is not None,
        },
    }
    if principal is not None:
        audit.record(
            context.user_id,
            "preview_as_user",
            "project",
            str(project_id),
            {
                "as_user_id": principal.user_id,
                "logical_sql": body.logical_sql[:500],
                "ok": compiled.ok,
                "policy_context": compiled.policy_context,
                "missing_attributes": list(compiled.missing_attributes),
            },
        )
    if compiled.ok and body.sample:
        response["sample"] = _run_sample(repository, project_id, compiled, mdl)
    return response


def _run_sample(
    repository: PostgresMetadataRepository, project_id: UUID, compiled: CompiledQuery, mdl: Mdl
) -> dict[str, object]:
    """Run the compiled SQL read-only for at most ``SAMPLE_ROWS`` rows after validating it."""
    from app.adapters.warehouse.postgres import PostgresWarehouseAdapter
    from app.api.routes import _project_connection_details
    from app.config import get_settings
    from app.core.types import ValidatedQuery

    checked = validate_compiled_sql(compiled.sql, mdl)
    if not checked.ok:
        return {"error": "The compiled query did not pass validation"}
    settings = get_settings()
    _, dsn, ca_certificate = _project_connection_details(repository, project_id)
    try:
        result = PostgresWarehouseAdapter(
            dsn,
            statement_timeout_ms=settings.warehouse_statement_timeout_ms,
            max_rows=SAMPLE_ROWS,
            ssl_ca_certificate=ca_certificate,
        ).execute_read_only(
            ValidatedQuery(sql=compiled.sql, parameters=compiled.parameters),
            max_rows=SAMPLE_ROWS,
        )
    except psycopg.Error as error:
        return {"error": f"PostgreSQL could not run the compiled query: {error.__class__.__name__}"}
    return {
        "columns": list(result.columns),
        "rows": [[_json_cell(cell) for cell in row] for row in result.rows[:SAMPLE_ROWS]],
        "truncated": result.truncated or len(result.rows) > SAMPLE_ROWS,
    }


def _json_cell(value: object) -> object:
    """Pass JSON-native cells through; stringify everything else."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


@router.get("/projects/{project_id}/deploy-report", tags=["Governance"])
def deploy_report(
    project_id: UUID,
    context: DeployMdlContext,
    repository: RepositoryDependency,
    revision: Annotated[int | None, Query(ge=1)] = None,
) -> dict[str, object]:
    """Report for a saved revision (latest by default) before it is deployed."""
    require_project_access(context, project_id)
    document = repository.get_mdl(str(project_id), revision)
    if document is None:
        raise HTTPException(status_code=404, detail="MDL revision not found")
    return {
        "project_id": str(project_id),
        "revision": document["revision"],
        **build_deploy_report(_mdl_from_document(document)),
    }


def _policy_mdl(repository: PostgresMetadataRepository, project_id: UUID) -> Mdl:
    """Load the active MDL that a policy is checked against; 409 if none is saved."""
    version = repository.get_active_mdl_version(str(project_id))
    document = repository.get_mdl(str(project_id), version)
    if document is None:
        raise HTTPException(status_code=409, detail="Save a semantic model before adding policies")
    return _mdl_from_document(document)


@router.get("/projects/{project_id}/row-policies", tags=["Governance"])
def list_row_policies(
    project_id: UUID,
    context: ManagePoliciesContext,
    governance: GovernanceDependency,
) -> dict[str, object]:
    """List the project's row-level security policies (``manage_policies``)."""
    require_project_access(context, project_id)
    return {
        "items": [_policy_json(item) for item in PolicyService(governance).list(str(project_id))]
    }


@router.post(
    "/projects/{project_id}/row-policies", tags=["Governance"], status_code=status.HTTP_201_CREATED
)
def create_row_policy(
    project_id: UUID,
    body: PolicyRequest,
    context: ManagePoliciesContext,
    repository: RepositoryDependency,
    governance: GovernanceDependency,
) -> dict[str, object]:
    """Create a row policy after validating and test-compiling it (``manage_policies``)."""
    require_project_access(context, project_id)
    test_principal = _load_principal(repository, context, body.test_user_id)
    try:
        saved = PolicyService(governance).create(
            str(project_id),
            _policy_or_422(body),
            enabled=body.enabled,
            mdl=_policy_mdl(repository, project_id),
            test_principal=test_principal,
            actor_id=context.user_id,
            known_roles=_role_names(repository),
        )
    except PolicyRejected as error:
        raise _rejection(error) from error
    except psycopg.errors.UniqueViolation as error:
        raise HTTPException(status_code=409, detail="Policy name already exists") from error
    return _saved_json(saved)


@router.put("/projects/{project_id}/row-policies/{policy_id}", tags=["Governance"])
def update_row_policy(
    project_id: UUID,
    policy_id: UUID,
    body: PolicyRequest,
    context: ManagePoliciesContext,
    repository: RepositoryDependency,
    governance: GovernanceDependency,
) -> dict[str, object]:
    """Replace a row policy after validating and test-compiling it (``manage_policies``)."""
    require_project_access(context, project_id)
    test_principal = _load_principal(repository, context, body.test_user_id)
    try:
        saved = PolicyService(governance).update(
            str(project_id),
            str(policy_id),
            _policy_or_422(body),
            enabled=body.enabled,
            mdl=_policy_mdl(repository, project_id),
            test_principal=test_principal,
            actor_id=context.user_id,
            known_roles=_role_names(repository),
        )
    except PolicyNotFound as error:
        raise HTTPException(status_code=404, detail="Policy not found") from error
    except PolicyRejected as error:
        raise _rejection(error) from error
    except psycopg.errors.UniqueViolation as error:
        raise HTTPException(status_code=409, detail="Policy name already exists") from error
    return _saved_json(saved)


@router.delete(
    "/projects/{project_id}/row-policies/{policy_id}",
    tags=["Governance"],
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_row_policy(
    project_id: UUID,
    policy_id: UUID,
    context: ManagePoliciesContext,
    governance: GovernanceDependency,
) -> Response:
    """Delete a row policy (requires ``manage_policies``)."""
    require_project_access(context, project_id)
    try:
        PolicyService(governance).delete(str(project_id), str(policy_id), actor_id=context.user_id)
    except PolicyNotFound as error:
        raise HTTPException(status_code=404, detail="Policy not found") from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _role_names(repository: PostgresMetadataRepository) -> list[str]:
    """Names of every defined role."""
    return [str(role["name"]) for role in repository.list_roles()]


def _policy_or_422(body: PolicyRequest) -> RowPolicy:
    """Build the ``RowPolicy`` from the request, turning invalid input into a 422."""
    try:
        return body.to_policy()
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


def _rejection(error: PolicyRejected) -> HTTPException:
    """Turn a rejected policy into a 422 listing its compiler errors."""
    return HTTPException(
        status_code=422,
        detail={
            "code": "POLICY_INVALID",
            "errors": [_error_json(item) for item in error.validation.errors],
        },
    )


def _audit_json(record: AuditRecord) -> dict[str, Any]:
    """Serialise an audit record into its camelCase API shape."""
    return {
        "id": record.id,
        "actorId": record.actor_id,
        "action": record.action,
        "resourceType": record.resource_type,
        "resourceId": record.resource_id,
        "details": dict(record.details),
        "createdAt": record.created_at.isoformat(),
    }


@router.get("/audit", tags=["Audit"])
@router.get("/audit-events", tags=["Audit"])
def browse_audit(
    _context: ViewAuditContext,
    audit: AuditDependency,
    actor_id: Annotated[UUID | None, Query()] = None,
    action: Annotated[str | None, Query(max_length=128)] = None,
    resource_type: Annotated[str | None, Query(max_length=64)] = None,
    resource_id: Annotated[str | None, Query(max_length=128)] = None,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[int | None, Query(ge=1)] = None,
) -> dict[str, object]:
    """Browse the audit log with filters, newest first, paged by cursor (``view_audit``)."""
    try:
        page = audit.browse(
            AuditFilter(
                actor_id=str(actor_id) if actor_id else None,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                since=since,
                until=until,
            ),
            limit=limit,
            before_id=cursor,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"items": [_audit_json(item) for item in page.items], "nextCursor": page.next_cursor}


EXPORT_ROW_LIMIT = 10_000
_CSV_COLUMNS = ("id", "created_at", "actor_id", "action", "resource_type", "resource_id", "details")


def _csv_cell(value: object) -> str:
    """One CSV cell; a leading =, +, - or @ would run as a formula in a spreadsheet."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in {"=", "+", "-", "@", "\t", "\r"} else text


@router.get("/audit-events/export.csv", tags=["Audit"])
def export_audit_csv(
    _context: ViewAuditContext,
    audit: AuditDependency,
    actor_id: Annotated[UUID | None, Query()] = None,
    action: Annotated[str | None, Query(max_length=128)] = None,
    resource_type: Annotated[str | None, Query(max_length=64)] = None,
    resource_id: Annotated[str | None, Query(max_length=128)] = None,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
) -> Response:
    """The filtered audit log as CSV, newest first, capped at EXPORT_ROW_LIMIT rows."""
    import csv
    import io
    import json

    filters = AuditFilter(
        actor_id=str(actor_id) if actor_id else None,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        since=since,
        until=until,
    )
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(_CSV_COLUMNS)
    written, cursor = 0, None
    try:
        while written < EXPORT_ROW_LIMIT:
            page = audit.browse(filters, limit=200, before_id=cursor)
            for item in page.items:
                writer.writerow([
                    item.id, item.created_at.isoformat(), _csv_cell(item.actor_id),
                    _csv_cell(item.action), _csv_cell(item.resource_type),
                    _csv_cell(item.resource_id),
                    _csv_cell(json.dumps(dict(item.details), default=str, sort_keys=True)),
                ])
                written += 1
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    audit.record(_context.user_id, "audit_exported", "audit", "csv", {"rows": written})
    return Response(
        out.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="audit-events.csv"'},
    )


class MappingItem(BaseModel):
    """One directory group (or claim value) and the role it maps to."""
    model_config = ConfigDict(extra="forbid")

    match: str = Field(min_length=1, max_length=512)
    role: str = Field(min_length=1, max_length=128)


class IdentityMappingsRequest(BaseModel):
    """Body replacing all group-to-role mappings for one identity source."""
    model_config = ConfigDict(extra="forbid")

    source: str = Field(pattern="^(ldap|oidc)$")
    mappings: list[MappingItem] = Field(max_length=200)


@router.get("/identity/mappings", tags=["Governance"])
def list_identity_mappings(
    _context: ManageRolesContext,
    repository: RepositoryDependency,
) -> dict[str, object]:
    """List directory and SSO group-to-role mappings (``manage_roles``)."""
    return {"items": repository.list_identity_mappings()}


@router.put("/identity/mappings", tags=["Governance"])
def replace_identity_mappings(
    body: IdentityMappingsRequest,
    context: ManageRolesContext,
    repository: RepositoryDependency,
) -> dict[str, object]:
    """Replace a source's group-to-role mappings; they are re-applied at each login."""
    matches = [item.match.casefold() for item in body.mappings]
    if len(set(matches)) != len(matches):
        raise HTTPException(status_code=422, detail="Each group may be mapped once")
    for item in body.mappings:
        capabilities = repository.get_role_capabilities(item.role)
        if capabilities is None:
            raise HTTPException(status_code=422, detail=f"Role {item.role!r} does not exist")
        if not capabilities <= context.capabilities:
            raise HTTPException(
                status_code=403,
                detail="Cannot map a role holding capabilities the current user does not hold",
            )
    repository.replace_identity_mappings(
        body.source, [(item.match, item.role) for item in body.mappings], actor_id=context.user_id
    )
    return {"source": body.source, "mappings": [item.model_dump() for item in body.mappings]}
