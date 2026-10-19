"""Core API routes: auth, projects and setup, MDL save and deploy, users, roles, samples."""

from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID, uuid4

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.adapters.llm.ollama import OllamaLLMAdapter
from app.adapters.llm.ollama_embedder import OllamaEmbedderAdapter
from app.adapters.metadata.postgres import PostgresMetadataRepository
from app.adapters.vector_store.qdrant import QdrantVectorStoreAdapter
from app.adapters.warehouse.postgres_catalog import PostgresCatalogAdapter
from app.api.dependencies import (
    effective_settings,
    get_audit_service,
    get_auth_service,
    get_current_context,
    get_password_change_context,
    get_repository,
    require_capability,
)
from app.api.security import (
    ConnectionRequest,
    ConnectionTestError,
    decrypt_connection,
    encrypt_connection,
    test_connection,
)
from app.audit.service import AuditService
from app.core.jsonable import json_value
from app.auth.models import AuthContext, LoginResult
from app.auth.service import (
    AuthenticationError,
    AuthService,
    PasswordPolicyError,
    hash_password,
)
from app.config import get_settings
from app.domain.mdl import Mdl, MdlValidationError, validate_mdl_against_catalog
from app.semantic.indexing import DeployService, IndexingService
from app.semantic.profiling import ProfiledDraft, ProfilingService

router = APIRouter(prefix="/api/v1")

AuthServiceDependency = Annotated[AuthService, Depends(get_auth_service)]
RepositoryDependency = Annotated[PostgresMetadataRepository, Depends(get_repository)]
AuditDependency = Annotated[AuditService, Depends(get_audit_service)]
CurrentContext = Annotated[AuthContext, Depends(get_current_context)]
PasswordChangeContext = Annotated[AuthContext, Depends(get_password_change_context)]
ManageConnectionContext = Annotated[AuthContext, Depends(require_capability("manage_connection"))]
EditMdlContext = Annotated[AuthContext, Depends(require_capability("edit_mdl"))]
DeployMdlContext = Annotated[AuthContext, Depends(require_capability("deploy_mdl"))]
AskQuestionContext = Annotated[AuthContext, Depends(require_capability("ask_question"))]
ManageUsersContext = Annotated[AuthContext, Depends(require_capability("manage_users"))]
ManageRolesContext = Annotated[AuthContext, Depends(require_capability("manage_roles"))]


SAMPLE_PROJECT_TEMPLATES: dict[str, dict[str, object]] = {
    "ecommerce": {
        "name": "E-commerce sample",
        "tables": [
            ("ecommerce", "customers"),
            ("ecommerce", "addresses"),
            ("ecommerce", "categories"),
            ("ecommerce", "products"),
            ("ecommerce", "orders"),
            ("ecommerce", "order_items"),
            ("ecommerce", "payments"),
            ("ecommerce", "shipments"),
            ("ecommerce", "reviews"),
            ("ecommerce", "coupons"),
            ("ecommerce", "order_coupons"),
            ("ecommerce", "inventory"),
        ],
    },
    "human-resources": {
        "name": "Human resources sample",
        "tables": [
            ("human_resources", "departments"),
            ("human_resources", "employees"),
            ("human_resources", "salary_history"),
            ("human_resources", "leave_requests"),
            ("human_resources", "performance_reviews"),
        ],
    },
}


class LoginRequest(BaseModel):
    """Credentials for a local or directory login."""
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=128)
    password: SecretStr = Field(min_length=1, max_length=1024)


class ChangePasswordRequest(BaseModel):
    """Current and new password (new one at least 12 characters)."""
    model_config = ConfigDict(extra="forbid")

    current_password: SecretStr = Field(min_length=1, max_length=1024, alias="currentPassword")
    new_password: SecretStr = Field(min_length=12, max_length=1024, alias="newPassword")


class TableSelection(BaseModel):
    """One warehouse table, by schema and table name."""
    model_config = ConfigDict(extra="forbid")

    schema_name: str = Field(min_length=1, alias="schema")
    table_name: str = Field(min_length=1, alias="table")


class SelectedTablesRequest(BaseModel):
    """Body listing the tables a project's model is built from."""
    tables: list[TableSelection] = Field(min_length=1, max_length=500)


class CatalogTableResponse(BaseModel):
    """A visible warehouse table and its exact live row count."""

    schema_name: str = Field(alias="schema")
    table_name: str = Field(alias="table")
    row_count: int
    column_count: int


class CatalogTablesResponse(BaseModel):
    """One page of visible warehouse tables."""

    items: list[CatalogTableResponse]
    page: int
    page_size: int
    total: int


class CatalogPreviewResponse(BaseModel):
    """Column metadata and a bounded sample of live warehouse data."""

    schema_name: str = Field(alias="schema")
    table_name: str = Field(alias="table")
    columns: list[str]
    column_types: list[str]
    rows: list[list[object]]
    row_count: int
    limit: int
    truncated: bool


class AskQuestionRequest(BaseModel):
    """Body carrying a single natural-language question."""
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)


_CAPABILITIES = frozenset(
    {
        "ask_question",
        "give_feedback",
        "view_shared",
        "view_all_threads",
        "rerun_sql",
        "manage_connection",
        "edit_mdl",
        "deploy_mdl",
        "manage_knowledge",
        "approve_pairs",
        "manage_policies",
        "manage_profiling",
        "create_dashboard",
        "schedule_question",
        "share",
        "run_eval",
        "manage_users",
        "manage_roles",
        "view_audit",
    }
)


class RoleRequest(BaseModel):
    """Body for creating or replacing a role; capabilities must be known and unique."""
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9 _-]*$")
    description: str = Field(default="", max_length=500)
    capabilities: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("capabilities")
    @classmethod
    def unique_capabilities(cls, values: list[str]) -> list[str]:
        """Reject duplicate or unsupported capability names."""
        if len(set(values)) != len(values):
            raise ValueError("Capabilities must be unique")
        if not set(values) <= _CAPABILITIES:
            raise ValueError("Unsupported capability")
        return values


class UserRequest(BaseModel):
    """Body for a new local user: username, initial password and role."""
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=128, pattern=r"^[A-Za-z0-9_.@-]+$")
    password: SecretStr = Field(min_length=12, max_length=1024)
    role: str = Field(default="User", min_length=1, max_length=64)


class UserRoleRequest(BaseModel):
    """Body naming the role to assign to a user."""
    model_config = ConfigDict(extra="forbid")

    role: str = Field(min_length=1, max_length=64)


class ProjectAccessRequest(BaseModel):
    """Body listing the projects a user or role may access."""
    model_config = ConfigDict(extra="forbid")

    project_ids: list[UUID] = Field(max_length=500)


class UserAttributesRequest(BaseModel):
    """Body with a user's attributes, which row policies may reference."""
    model_config = ConfigDict(extra="forbid")

    attributes: dict[str, object] = Field(max_length=100)

    @field_validator("attributes")
    @classmethod
    def validate_attribute_keys(cls, values: dict[str, object]) -> dict[str, object]:
        """Require every attribute key to be 1 to 128 characters."""
        if any(not key or len(key) > 128 for key in values):
            raise ValueError("Attribute keys must contain 1 to 128 characters")
        return values


def _set_refresh_cookie(response: Response, refresh_token: str) -> None:
    """Set the HttpOnly, SameSite=Strict refresh cookie (``Secure`` outside dev)."""
    settings = get_settings()
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=settings.app_env.lower() not in {"development", "test"},
        samesite="strict",
        path="/api/v1/auth",
        max_age=settings.refresh_token_ttl_seconds,
    )


def _clear_refresh_cookie(response: Response) -> None:
    """Delete the refresh-token cookie."""
    response.delete_cookie(
        key="refresh_token",
        httponly=True,
        secure=get_settings().app_env.lower() not in {"development", "test"},
        samesite="strict",
        path="/api/v1/auth",
    )


def _auth_response(result: LoginResult) -> dict[str, object]:
    """Build the token response body; the refresh token travels only as a cookie."""
    return {
        "access_token": result.access_token,
        "token_type": "bearer",
        "expires_in": result.expires_in,
        "must_change_password": result.user.must_change_password,
    }


def _project_connection(
    repository: PostgresMetadataRepository,
    project_id: UUID,
) -> tuple[dict[str, object], PostgresCatalogAdapter]:
    """Return the project row and a catalog adapter on its warehouse."""
    project, dsn, ca_certificate = _project_connection_details(repository, project_id)
    settings = get_settings()
    return project, PostgresCatalogAdapter(
        dsn,
        statement_timeout_ms=settings.warehouse_statement_timeout_ms,
        ssl_ca_certificate=ca_certificate,
    )


def _project_connection_details(
    repository: PostgresMetadataRepository,
    project_id: UUID,
) -> tuple[dict[str, object], str, str | None]:
    """Decrypt a project's stored credentials into a DSN.

    Returns the project row, the DSN (``verify-full`` TLS when a CA is stored) and the CA text.
    """
    project = repository.get_project_connection(str(project_id))
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    encrypted = project.get("connection_encrypted")
    if not isinstance(encrypted, bytes):
        raise HTTPException(status_code=409, detail="Project has no saved connection")
    settings = get_settings()
    try:
        credentials = decrypt_connection(encrypted, settings)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    parameters: dict[str, str] = {
        "host": str(project["host"]),
        "port": str(cast(int, project["port"])),
        "dbname": str(credentials["database"]),
        "user": str(credentials["username"]),
        "password": str(credentials["password"]),
        "connect_timeout": "5",
        "sslmode": "verify-full"
        if credentials["ssl_ca_certificate"]
        else ("require" if project["ssl_enabled"] else "prefer"),
    }
    dsn = make_conninfo(**parameters)
    certificate = credentials["ssl_ca_certificate"]
    return (
        project,
        dsn,
        str(certificate) if certificate else None,
    )


def _require_project_access(context: AuthContext, project_id: UUID) -> None:
    """Raise 404 unless the project is in the caller's allowed projects."""
    if str(project_id) not in context.project_ids:
        raise HTTPException(status_code=404, detail="Project not found")


def _require_delegable_capabilities(context: AuthContext, capabilities: list[str]) -> None:
    """Refuse granting capabilities the caller does not hold."""
    if not set(capabilities) <= context.capabilities:
        raise HTTPException(
            status_code=403,
            detail="Cannot grant capabilities the current user does not hold",
        )


def _require_manageable_user(
    context: AuthContext, repository: PostgresMetadataRepository, user_id: UUID
) -> None:
    """A manager may not change their own access, nor act on an account stronger than theirs.

    Roles, attributes and project access decide which rows an account sees, so editing one's own
    is the same as editing one's own row policy. A DBA may still manage every account.
    """
    if context.role_name == "DBA":
        return
    if str(user_id) == context.user_id:
        raise HTTPException(status_code=403, detail="Cannot change your own access")
    target = repository.get_auth_context(str(user_id))
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if not set(repository.get_role_capabilities(target.role_name) or ()) <= context.capabilities:
        raise HTTPException(
            status_code=403,
            detail="Cannot manage an account holding capabilities the current user does not hold",
        )


def _require_delegable_projects(context: AuthContext, project_ids: list[UUID]) -> None:
    """Refuse granting projects the caller cannot access (DBA excepted)."""
    if (
        context.role_name != "DBA"
        and not {str(project_id) for project_id in project_ids} <= context.project_ids
    ):
        raise HTTPException(
            status_code=403,
            detail="Cannot grant access to projects the current user cannot access",
        )


def _profile_service(catalog: PostgresCatalogAdapter) -> ProfilingService:
    """Build a profiling service over the catalog and the local SQL model."""
    settings = get_settings()
    return ProfilingService(
        catalog,
        OllamaLLMAdapter(settings.ollama_url, settings.sql_model, timeout_seconds=90),
    )


def _serialize_draft(draft: ProfiledDraft) -> dict[str, object]:
    """Serialise a profiled draft (MDL, tables, summary) with API aliases."""
    return {
        "mdl": draft.mdl.model_dump(by_alias=True),
        "tables": [table.model_dump(by_alias=True) for table in draft.tables],
        "summary": draft.summary.model_dump(by_alias=True),
    }


@router.post("/auth/login", tags=["Auth"])
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    auth_service: AuthServiceDependency,
) -> dict[str, object]:
    """Sign in with username and password; sets the refresh cookie and returns an access token."""
    client_key = request.client.host if request.client else "unknown"
    try:
        result = auth_service.login(
            body.username,
            body.password.get_secret_value(),
            client_key,
        )
    except AuthenticationError as error:
        raise HTTPException(status_code=401, detail="Invalid username or password") from error
    _set_refresh_cookie(response, result.refresh_token)
    return _auth_response(result)


@router.post("/auth/refresh", tags=["Auth"])
def refresh(
    request: Request,
    response: Response,
    auth_service: AuthServiceDependency,
) -> dict[str, object]:
    """Exchange the refresh cookie for a new access token; clears the cookie if invalid."""
    refresh_token = request.cookies.get("refresh_token")
    if not refresh_token:
        raise HTTPException(status_code=401, detail="Refresh token is invalid; sign in again")
    try:
        result = auth_service.refresh(refresh_token)
    except AuthenticationError as error:
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=401, detail=str(error)) from error
    _set_refresh_cookie(response, result.refresh_token)
    return _auth_response(result)


@router.post("/auth/logout", tags=["Auth"], status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    response: Response,
    auth_service: AuthServiceDependency,
) -> Response:
    """End the session behind the refresh cookie and clear the cookie."""
    refresh_token = request.cookies.get("refresh_token")
    if refresh_token:
        auth_service.logout(refresh_token)
    _clear_refresh_cookie(response)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.post("/auth/change-password", tags=["Auth"], status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    body: ChangePasswordRequest,
    context: PasswordChangeContext,
    auth_service: AuthServiceDependency,
) -> Response:
    """Change the caller's password; also usable when a change is forced."""
    if context.session_id is None:
        raise HTTPException(status_code=401, detail="Session is not valid")
    try:
        auth_service.change_password(
            context,
            context.session_id,
            body.current_password.get_secret_value(),
            body.new_password.get_secret_value(),
        )
    except PasswordPolicyError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except AuthenticationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", tags=["Auth"])
def get_me(
    context: CurrentContext,
    repository: RepositoryDependency,
) -> dict[str, object]:
    """Return the signed-in user's role, capabilities and accessible projects."""
    projects = repository.list_projects(context.user_id)
    return {
        "user_id": context.user_id,
        "username": context.username,
        "role": context.role_name,
        "capabilities": sorted(context.capabilities),
        "projects": [
            {"id": str(project["id"]), "name": str(project["name"])} for project in projects
        ],
        "must_change_password": context.must_change_password,
    }


@router.post("/connections/test", tags=["Projects"])
def post_connection_test(
    body: ConnectionRequest,
    _context: ManageConnectionContext,
) -> dict[str, object]:
    """Test a warehouse connection read-only without saving it (``manage_connection``)."""
    try:
        result = test_connection(body, effective_settings())
    except ConnectionTestError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"server_version": result.server_version, "latency_ms": result.latency_ms}


@router.get("/projects", tags=["Projects"])
def list_projects(
    context: CurrentContext,
    repository: RepositoryDependency,
) -> dict[str, object]:
    """List the projects the caller can access."""
    projects = repository.list_projects(context.user_id)
    return {
        "items": [
            {
                "id": str(project["id"]),
                "name": str(project["name"]),
                "setup_step": str(project["setup_step"]),
            }
            for project in projects
        ]
    }


@router.post("/projects", tags=["Projects"], status_code=status.HTTP_201_CREATED)
def create_project(
    body: ConnectionRequest,
    context: ManageConnectionContext,
    repository: RepositoryDependency,
    auth_service: AuthServiceDependency,
    audit: AuditDependency,
) -> dict[str, object]:
    """Test, encrypt and save a warehouse connection as a new project (``manage_connection``)."""
    try:
        test_connection(body, effective_settings())
        encrypted = encrypt_connection(body, get_settings())
    except ConnectionTestError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    project_id = uuid4()
    repository.create_project(
        project_id=str(project_id),
        name=body.display_name,
        host=body.host,
        port=body.port,
        database_name=body.database,
        ssl_enabled=body.ssl,
        encrypted_connection=encrypted,
        owner_id=context.user_id,
    )
    auth_service.invalidate_auth_context(context.user_id)
    audit.record(
        context.user_id,
        "project_created",
        "project",
        str(project_id),
        {"name": body.display_name, "database": body.database},
    )
    return {"id": str(project_id), "name": body.display_name, "setup_step": "tables"}


@router.delete("/projects/{project_id}", tags=["Projects"])
def delete_project(
    project_id: UUID,
    context: ManageConnectionContext,
    repository: RepositoryDependency,
    auth_service: AuthServiceDependency,
    audit: AuditDependency,
) -> dict[str, object]:
    """Delete a project and clean up its search vectors (``manage_connection``)."""
    _require_project_access(context, project_id)
    project = repository.get_project_connection(str(project_id))
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        versions = repository.delete_project(str(project_id))
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Project not found") from error
    auth_service.invalidate_all_auth_contexts()
    # The database rows are gone; search vectors are cleaned up best-effort.
    cleanup_failed = False
    try:
        vector_store = QdrantVectorStoreAdapter(get_settings().qdrant_url)
        for namespace in (
            *(str(version) for version in versions),
            "knowledge:sql_pairs",
            "knowledge:instructions",
        ):
            vector_store.delete_version(str(project_id), namespace)
    except Exception:  # noqa: BLE001 - orphaned vectors are harmless, the delete already happened
        cleanup_failed = True
    audit.record(
        context.user_id,
        "project_deleted",
        "project",
        str(project_id),
        {"name": str(project["name"]), "vector_cleanup_failed": cleanup_failed},
    )
    return {"id": str(project_id), "deleted": True}


@router.get("/projects/{project_id}/setup", tags=["Projects"])
def get_setup_state(
    project_id: UUID,
    context: ManageConnectionContext,
    repository: RepositoryDependency,
) -> dict[str, object]:
    """Return the project's setup step and selected tables (``manage_connection``)."""
    _require_project_access(context, project_id)
    project = repository.get_project_connection(str(project_id))
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return {
        "project_id": str(project_id),
        "step": project["setup_step"],
        "selected_tables": project["selected_tables"],
    }


@router.get("/projects/{project_id}/catalog/tables", tags=["Projects"], response_model=CatalogTablesResponse)
def list_catalog_tables(
    project_id: UUID,
    context: ManageConnectionContext,
    repository: RepositoryDependency,
    search: Annotated[str, Query(max_length=128)] = "",
    schema: Annotated[str | None, Query(max_length=128)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, object]:
    """List and search warehouse tables, paged (requires ``manage_connection``)."""
    _require_project_access(context, project_id)
    _, catalog = _project_connection(repository, project_id)
    try:
        tables = catalog.list_tables((schema,) if schema else ())
    except psycopg.Error as error:
        raise HTTPException(status_code=502, detail="Could not read database catalog") from error
    filtered = [
        table
        for table in tables
        if search.casefold() in f"{table.schema_name}.{table.table_name}".casefold()
    ]
    offset = (page - 1) * page_size
    page_tables = filtered[offset : offset + page_size]
    try:
        exact_counts = catalog.count_tables(
            tuple((item.schema_name, item.table_name) for item in page_tables)
        )
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except psycopg.Error as error:
        raise HTTPException(status_code=502, detail="Could not count catalog table rows") from error
    return {
        "items": [
            {
                "schema": table.schema_name,
                "table": table.table_name,
                "row_count": exact_count,
                "column_count": table.column_count,
            }
            for table, exact_count in zip(page_tables, exact_counts, strict=True)
        ],
        "page": page,
        "page_size": page_size,
        "total": len(filtered),
    }


@router.get("/projects/{project_id}/catalog/preview", tags=["Projects"], response_model=CatalogPreviewResponse)
def preview_catalog_table(
    project_id: UUID,
    context: ManageConnectionContext,
    repository: RepositoryDependency,
    schema: Annotated[str, Query(min_length=1, max_length=128)],
    table: Annotated[str, Query(min_length=1, max_length=128)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, object]:
    """Show a bounded read-only sample of real warehouse values to connection managers."""
    _require_project_access(context, project_id)
    _, catalog = _project_connection(repository, project_id)
    try:
        columns, column_types, rows, row_count = catalog.preview_table(schema, table, limit=limit)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except psycopg.Error as error:
        raise HTTPException(status_code=502, detail="Could not preview this table") from error
    return {"schema": schema, "table": table, "columns": columns, "column_types": column_types, "row_count": row_count,
            "rows": [[json_value(value) for value in row] for row in rows],
            "limit": limit, "truncated": row_count > len(rows)}


@router.put("/projects/{project_id}/selected-tables", tags=["Projects"])
def save_selected_tables(
    project_id: UUID,
    body: SelectedTablesRequest,
    context: ManageConnectionContext,
    repository: RepositoryDependency,
) -> dict[str, object]:
    """Save the chosen tables after checking they exist (``manage_connection``)."""
    _require_project_access(context, project_id)
    _, catalog = _project_connection(repository, project_id)
    available = {(table.schema_name, table.table_name) for table in catalog.list_tables()}
    selected = [(table.schema_name, table.table_name) for table in body.tables]
    if len(set(selected)) != len(selected):
        raise HTTPException(status_code=422, detail="Selected tables must be unique")
    unknown = set(selected) - available
    if unknown:
        raise HTTPException(status_code=422, detail={"unknown_tables": sorted(unknown)})
    repository.save_selected_tables(
        str(project_id),
        [{"schema": schema_name, "table": table_name} for schema_name, table_name in selected],
    )
    return {"project_id": str(project_id), "step": "relationships", "selected_count": len(selected)}


@router.post("/projects/{project_id}/introspect", tags=["Projects"])
def introspect_project(
    project_id: UUID,
    context: ManageConnectionContext,
    repository: RepositoryDependency,
) -> dict[str, object]:
    """Profile the selected tables and draft an MDL with descriptions (``manage_connection``)."""
    _require_project_access(context, project_id)
    selected = repository.parse_project_table_selection(str(project_id))
    tables = tuple((table["schema"], table["table"]) for table in selected)
    _, catalog = _project_connection(repository, project_id)
    try:
        service = _profile_service(catalog)
        draft = service.create_draft(tables)
        mdl, warnings = service.draft_descriptions_best_effort(draft.mdl)
    except (TypeError, ValueError, RuntimeError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except psycopg.Error as error:
        raise HTTPException(status_code=502, detail="Could not profile selected tables") from error
    return {
        **_serialize_draft(ProfiledDraft(mdl=mdl, tables=draft.tables, summary=draft.summary)),
        "warnings": warnings,
    }


@router.get("/projects/{project_id}/mdl", tags=["Projects"])
def get_mdl(
    project_id: UUID,
    context: EditMdlContext,
    repository: RepositoryDependency,
    version: Annotated[int | None, Query(ge=1)] = None,
) -> dict[str, object]:
    """Read a saved MDL revision, latest by default (requires ``edit_mdl``)."""
    _require_project_access(context, project_id)
    document = repository.get_mdl(str(project_id), version)
    if document is None:
        raise HTTPException(status_code=404, detail="Project has no saved MDL")
    return document


@router.put("/projects/{project_id}/mdl", tags=["Projects"])
def save_mdl(
    project_id: UUID,
    body: Mdl,
    context: EditMdlContext,
    repository: RepositoryDependency,
    audit: AuditDependency,
) -> dict[str, object]:
    """Validate an MDL against the live catalog and save it as a new revision (``edit_mdl``)."""
    _require_project_access(context, project_id)
    project = repository.get_project_connection(str(project_id))
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    selected = repository.parse_project_table_selection(str(project_id))
    table_pairs = tuple((item["schema"], item["table"]) for item in selected)
    _, catalog = _project_connection(repository, project_id)
    try:
        profiles = catalog.profile_tables(table_pairs, sample_limit=0)
        catalog_columns = {
            f"{profile.table.schema_name}.{profile.table.table_name}": {
                column.name: column.data_type for column in profile.columns
            }
            for profile in profiles
        }
        validate_mdl_against_catalog(body, catalog_columns)
    except (MdlValidationError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except psycopg.Error as error:
        raise HTTPException(
            status_code=502, detail="Could not validate MDL against catalog"
        ) from error
    version = repository.save_mdl(
        str(project_id),
        body.model_dump(by_alias=True, mode="json"),
    )
    audit.record(
        context.user_id,
        "mdl_saved",
        "project",
        str(project_id),
        {"revision": version, "models": len(body.models), "metrics": len(body.metrics)},
    )
    return {
        "project_id": str(project_id),
        "revision": version,
        **body.model_dump(by_alias=True),
    }


@router.post("/projects/{project_id}/deploy", tags=["Projects"])
def deploy_project_mdl(
    project_id: UUID,
    context: DeployMdlContext,
    repository: RepositoryDependency,
    revision: Annotated[int | None, Query(ge=1)] = None,
) -> dict[str, object]:
    """Index and activate an MDL revision for questions (requires ``deploy_mdl``)."""
    _require_project_access(context, project_id)
    document = repository.get_mdl(str(project_id), revision)
    if document is None:
        raise HTTPException(status_code=404, detail="MDL revision not found")
    revision_number = cast(int, document["revision"])
    if repository.get_active_mdl_version(str(project_id)) == revision_number:
        raise HTTPException(status_code=409, detail="MDL revision is already deployed")
    vector_store: QdrantVectorStoreAdapter | None = None
    try:
        mdl_document = {
            key: value for key, value in document.items() if key not in {"project_id", "revision"}
        }
        mdl = Mdl.model_validate(mdl_document)
        settings = get_settings()
        vector_store = QdrantVectorStoreAdapter(settings.qdrant_url)
        service = DeployService(
            IndexingService(
                OllamaEmbedderAdapter(settings.ollama_url, settings.embedding_model),
                vector_store,
            ),
            vector_store,
            repository,
        )
        result = service.deploy(
            str(project_id),
            revision_number,
            mdl,
            deployed_by=context.user_id,
        )
    except psycopg.Error as error:
        raise HTTPException(status_code=503, detail="Could not persist MDL deployment") from error
    except (RuntimeError, TypeError, ValueError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    finally:
        if vector_store is not None:
            vector_store.close()
    return result


@router.get("/roles", tags=["Users & Roles"])
def list_roles(
    _context: ManageRolesContext,
    repository: RepositoryDependency,
) -> dict[str, object]:
    """List roles and their capabilities (requires ``manage_roles``)."""
    return {"items": repository.list_roles()}


@router.post("/roles", tags=["Users & Roles"], status_code=status.HTTP_201_CREATED)
def create_role(
    body: RoleRequest,
    context: ManageRolesContext,
    repository: RepositoryDependency,
    audit: AuditDependency,
) -> dict[str, object]:
    """Create a role; the caller can only grant capabilities they hold (``manage_roles``)."""
    _require_delegable_capabilities(context, body.capabilities)
    try:
        role_id = repository.create_role(body.name, body.description, body.capabilities)
    except psycopg.errors.UniqueViolation as error:
        raise HTTPException(status_code=409, detail="Role name already exists") from error
    audit.record(
        context.user_id,
        "role_created",
        "role",
        str(role_id),
        {"name": body.name, "capabilities": sorted(body.capabilities)},
    )
    return {"id": role_id, **body.model_dump()}


@router.put("/roles/{role_id}", tags=["Users & Roles"])
def update_role(
    role_id: UUID,
    body: RoleRequest,
    context: ManageRolesContext,
    repository: RepositoryDependency,
    audit: AuditDependency,
    auth_service: AuthServiceDependency,
) -> dict[str, object]:
    """Replace a role's name, description and capabilities (``manage_roles``)."""
    _require_delegable_capabilities(context, body.capabilities)
    try:
        repository.update_role(
            str(role_id),
            name=body.name,
            description=body.description,
            capabilities=body.capabilities,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except psycopg.errors.UniqueViolation as error:
        raise HTTPException(status_code=409, detail="Role name already exists") from error
    auth_service.invalidate_all_auth_contexts()
    audit.record(
        context.user_id,
        "role_updated",
        "role",
        str(role_id),
        {"name": body.name, "capabilities": sorted(body.capabilities)},
    )
    return {"id": str(role_id), **body.model_dump()}


@router.delete("/roles/{role_id}", tags=["Users & Roles"], status_code=status.HTTP_204_NO_CONTENT)
def delete_role(
    role_id: UUID,
    context: ManageRolesContext,
    repository: RepositoryDependency,
    audit: AuditDependency,
    auth_service: AuthServiceDependency,
) -> Response:
    """Delete a role that is not in use (requires ``manage_roles``)."""
    try:
        repository.delete_role(str(role_id))
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    auth_service.invalidate_all_auth_contexts()
    audit.record(context.user_id, "role_deleted", "role", str(role_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/users", tags=["Users & Roles"])
def list_users(
    _context: ManageUsersContext,
    repository: RepositoryDependency,
) -> dict[str, object]:
    """List users (requires ``manage_users``)."""
    return {"items": repository.list_users()}


@router.post("/users", tags=["Users & Roles"], status_code=status.HTTP_201_CREATED)
def create_user(
    body: UserRequest,
    context: ManageUsersContext,
    repository: RepositoryDependency,
) -> dict[str, object]:
    """Create a local user with a role the caller may delegate (``manage_users``)."""
    role_capabilities = repository.get_role_capabilities(body.role)
    if role_capabilities is None:
        raise HTTPException(status_code=404, detail="Role does not exist")
    _require_delegable_capabilities(context, list(role_capabilities))
    try:
        user_id = repository.create_local_user(
            body.username,
            body.role,
            hash_password(body.password.get_secret_value()),
            actor_id=context.user_id,
        )
    except psycopg.errors.UniqueViolation as error:
        raise HTTPException(status_code=409, detail="Username already exists") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"id": user_id, "username": body.username.casefold(), "role": body.role}


@router.put("/users/{user_id}/role", tags=["Users & Roles"])
def assign_user_role(
    user_id: UUID,
    body: UserRoleRequest,
    context: ManageUsersContext,
    repository: RepositoryDependency,
    audit: AuditDependency,
    auth_service: AuthServiceDependency,
) -> dict[str, object]:
    """Change a user's role, within the caller's own authority (``manage_users``)."""
    _require_manageable_user(context, repository, user_id)
    capabilities = repository.get_role_capabilities(body.role)
    if capabilities is None:
        raise HTTPException(status_code=404, detail="Role does not exist")
    _require_delegable_capabilities(context, list(capabilities))
    try:
        repository.set_user_role(str(user_id), body.role)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    auth_service.invalidate_auth_context(str(user_id))
    audit.record(context.user_id, "user_role_changed", "user", str(user_id), {"role": body.role})
    return {"user_id": str(user_id), "role": body.role}


@router.get("/users/{user_id}/project-access", tags=["Users & Roles"])
def get_user_project_access(
    user_id: UUID,
    _context: ManageUsersContext,
    repository: RepositoryDependency,
) -> dict[str, object]:
    """List the projects granted directly to a user (``manage_users``)."""
    if not any(user["id"] == user_id for user in repository.list_users()):
        raise HTTPException(status_code=404, detail="User not found")
    return {"project_ids": repository.get_user_project_access(str(user_id))}


@router.put("/users/{user_id}/project-access", tags=["Users & Roles"])
def set_user_project_access(
    user_id: UUID,
    body: ProjectAccessRequest,
    context: ManageUsersContext,
    repository: RepositoryDependency,
    audit: AuditDependency,
    auth_service: AuthServiceDependency,
) -> dict[str, object]:
    """Replace a user's project access, within the caller's own (``manage_users``)."""
    _require_manageable_user(context, repository, user_id)
    _require_delegable_projects(context, body.project_ids)
    try:
        repository.replace_user_project_access(
            str(user_id), [str(project_id) for project_id in body.project_ids]
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    auth_service.invalidate_auth_context(str(user_id))
    audit.record(
        context.user_id,
        "user_project_access_changed",
        "user",
        str(user_id),
        {"project_ids": [str(p) for p in body.project_ids]},
    )
    return {"user_id": str(user_id), "project_ids": body.project_ids}


@router.get("/roles/{role_id}/project-access", tags=["Users & Roles"])
def get_role_project_access(
    role_id: UUID,
    _context: ManageRolesContext,
    repository: RepositoryDependency,
) -> dict[str, object]:
    """List the projects granted to a role (requires ``manage_roles``)."""
    if not any(role["id"] == role_id for role in repository.list_roles()):
        raise HTTPException(status_code=404, detail="Role not found")
    return {"project_ids": repository.get_role_project_access(str(role_id))}


@router.put("/roles/{role_id}/project-access", tags=["Users & Roles"])
def set_role_project_access(
    role_id: UUID,
    body: ProjectAccessRequest,
    context: ManageRolesContext,
    repository: RepositoryDependency,
    audit: AuditDependency,
    auth_service: AuthServiceDependency,
) -> dict[str, object]:
    """Replace a role's project access, within the caller's own (``manage_roles``)."""
    _require_delegable_projects(context, body.project_ids)
    try:
        repository.replace_role_project_access(
            str(role_id), [str(project_id) for project_id in body.project_ids]
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    auth_service.invalidate_all_auth_contexts()
    audit.record(
        context.user_id,
        "role_project_access_changed",
        "role",
        str(role_id),
        {"project_ids": [str(p) for p in body.project_ids]},
    )
    return {"role_id": str(role_id), "project_ids": body.project_ids}


@router.get("/users/{user_id}/attributes", tags=["Users & Roles"])
def get_user_attributes(
    user_id: UUID,
    _context: ManageUsersContext,
    repository: RepositoryDependency,
) -> dict[str, object]:
    """Read a user's attributes used by row policies (requires ``manage_users``)."""
    if not any(user["id"] == user_id for user in repository.list_users()):
        raise HTTPException(status_code=404, detail="User not found")
    return {"attributes": repository.get_user_attributes(str(user_id))}


@router.put("/users/{user_id}/attributes", tags=["Users & Roles"])
def set_user_attributes(
    user_id: UUID,
    body: UserAttributesRequest,
    context: ManageUsersContext,
    repository: RepositoryDependency,
    audit: AuditDependency,
    auth_service: AuthServiceDependency,
) -> dict[str, object]:
    """Replace a user's attributes, which change their row policies (``manage_users``)."""
    _require_manageable_user(context, repository, user_id)
    try:
        repository.replace_user_attributes(str(user_id), body.attributes)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    auth_service.invalidate_auth_context(str(user_id))
    audit.record(
        context.user_id,
        "user_attributes_changed",
        "user",
        str(user_id),
        {"keys": sorted(body.attributes)},
    )
    return {"user_id": str(user_id), "attributes": body.attributes}


@router.get("/sample-projects", tags=["Projects"])
def list_sample_project_templates(
    _context: ManageConnectionContext,
) -> dict[str, object]:
    """List the built-in sample projects (requires ``manage_connection``)."""
    return {
        "items": [
            {"id": "ecommerce", "name": "E-commerce", "schemas": ["ecommerce"]},
            {
                "id": "human-resources",
                "name": "Human resources",
                "schemas": ["human_resources"],
            },
        ]
    }


@router.post(
    "/sample-projects/{template_id}", tags=["Projects"], status_code=status.HTTP_201_CREATED
)
def create_sample_project(
    template_id: str,
    context: ManageConnectionContext,
    repository: RepositoryDependency,
    auth_service: AuthServiceDependency,
    audit: AuditDependency,
) -> dict[str, object]:
    """Create a sample project from the bundled warehouse with a drafted MDL."""
    template = SAMPLE_PROJECT_TEMPLATES.get(template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Unknown sample project")
    template_name = cast(str, template["name"])
    table_pairs = cast(list[tuple[str, str]], template["tables"])
    settings = get_settings()
    if not settings.warehouse_dsn:
        raise HTTPException(status_code=503, detail="Sample PostgreSQL is not configured")
    dsn = conninfo_to_dict(settings.warehouse_dsn)

    def required_value(key: str) -> str:
        """Return a non-empty DSN setting or raise ``ValueError``."""
        value = dsn.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"Sample PostgreSQL connection is missing {key}")
        return value

    def optional_value(key: str, default: str) -> str:
        """Return a DSN setting, or ``default`` when it is missing or empty."""
        value = dsn.get(key)
        return value if isinstance(value, str) and value else default

    try:
        connection = ConnectionRequest(
            displayName=template_name,
            host=optional_value("host", "localhost"),
            port=int(str(dsn.get("port") or "5432")),
            username=required_value("user"),
            password=SecretStr(required_value("password")),
            database=required_value("dbname"),
            ssl=dsn.get("sslmode") in {"require", "verify-ca", "verify-full"},
        )
        test_connection(connection, settings)
        encrypted = encrypt_connection(connection, settings)
        catalog = PostgresCatalogAdapter(
            settings.warehouse_dsn,
            statement_timeout_ms=settings.warehouse_statement_timeout_ms,
        )
        schemas = (table_pairs[0][0],)
        available = {
            (table.schema_name, table.table_name) for table in catalog.list_tables(schemas)
        }
        selections = [
            {"schema": schema_name, "table": table_name} for schema_name, table_name in table_pairs
        ]
        unknown = {(item["schema"], item["table"]) for item in selections} - available
        if unknown:
            raise ConnectionTestError(
                f"Sample database is missing required table(s): {sorted(unknown)}"
            )
        draft = ProfilingService(catalog).create_draft(tuple(table_pairs))
    except (ConnectionTestError, ValueError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except psycopg.Error as error:
        raise HTTPException(
            status_code=502, detail="Could not inspect sample database schema"
        ) from error
    project_id = str(uuid4())
    repository.create_project(
        project_id=project_id,
        name=template_name,
        host=connection.host,
        port=connection.port,
        database_name=connection.database,
        ssl_enabled=connection.ssl,
        encrypted_connection=encrypted,
        owner_id=context.user_id,
    )
    auth_service.invalidate_auth_context(context.user_id)
    repository.set_project_sample(project_id)
    repository.save_selected_tables(project_id, selections)
    revision = repository.save_mdl(
        project_id,
        draft.mdl.model_dump(by_alias=True, mode="json"),
    )
    audit.record(
        context.user_id,
        "sample_project_created",
        "project",
        project_id,
        {"template": template_id, "revision": revision},
    )
    return {"id": project_id, "name": template_name, "revision": revision}
