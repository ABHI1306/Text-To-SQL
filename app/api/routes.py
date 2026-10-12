from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID, uuid4

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from psycopg.conninfo import make_conninfo
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.adapters.llm.ollama import OllamaLLMAdapter
from app.adapters.metadata.postgres import PostgresMetadataRepository
from app.adapters.warehouse.postgres_catalog import PostgresCatalogAdapter
from app.api.dependencies import (
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
from app.auth.models import AuthContext, LoginResult
from app.auth.service import (
    AuthenticationError,
    AuthService,
    PasswordPolicyError,
)
from app.config import get_settings
from app.domain.mdl import Mdl, MdlValidationError, validate_mdl_against_catalog
from app.semantic.profiling import ProfiledDraft, ProfilingService

router = APIRouter(prefix="/api/v1")

AuthServiceDependency = Annotated[AuthService, Depends(get_auth_service)]
RepositoryDependency = Annotated[PostgresMetadataRepository, Depends(get_repository)]
CurrentContext = Annotated[AuthContext, Depends(get_current_context)]
PasswordChangeContext = Annotated[AuthContext, Depends(get_password_change_context)]
ManageConnectionContext = Annotated[
    AuthContext, Depends(require_capability("manage_connection"))
]
EditMdlContext = Annotated[AuthContext, Depends(require_capability("edit_mdl"))]


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=128)
    password: SecretStr = Field(min_length=1, max_length=1024)


class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: SecretStr = Field(min_length=1, max_length=1024, alias="currentPassword")
    new_password: SecretStr = Field(min_length=12, max_length=1024, alias="newPassword")


class TableSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_name: str = Field(min_length=1, alias="schema")
    table_name: str = Field(min_length=1, alias="table")


class SelectedTablesRequest(BaseModel):
    tables: list[TableSelection] = Field(min_length=1, max_length=500)


def _set_refresh_cookie(response: Response, refresh_token: str) -> None:
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
    response.delete_cookie(
        key="refresh_token",
        httponly=True,
        secure=get_settings().app_env.lower() not in {"development", "test"},
        samesite="strict",
        path="/api/v1/auth",
    )


def _auth_response(result: LoginResult) -> dict[str, object]:
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
        "sslmode": "verify-full" if credentials["ssl_ca_certificate"] else (
            "require" if project["ssl_enabled"] else "prefer"
        ),
    }
    dsn = make_conninfo(**parameters)
    return project, PostgresCatalogAdapter(
        dsn,
        statement_timeout_ms=settings.warehouse_statement_timeout_ms,
        ssl_ca_certificate=(
            str(credentials["ssl_ca_certificate"])
            if credentials["ssl_ca_certificate"]
            else None
        ),
    )


def _require_project_access(context: AuthContext, project_id: UUID) -> None:
    if str(project_id) not in context.project_ids:
        raise HTTPException(status_code=404, detail="Project not found")


def _profile_service(catalog: PostgresCatalogAdapter) -> ProfilingService:
    settings = get_settings()
    return ProfilingService(
        catalog,
        OllamaLLMAdapter(settings.ollama_url, settings.sql_model),
    )


def _serialize_draft(draft: ProfiledDraft) -> dict[str, object]:
    return {
        "mdl": draft.mdl.model_dump(by_alias=True),
        "tables": [table.model_dump(by_alias=True) for table in draft.tables],
        "summary": draft.summary.model_dump(by_alias=True),
    }


@router.post("/auth/login")
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    auth_service: AuthServiceDependency,
) -> dict[str, object]:
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


@router.post("/auth/refresh")
def refresh(
    request: Request,
    response: Response,
    auth_service: AuthServiceDependency,
) -> dict[str, object]:
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


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    response: Response,
    auth_service: AuthServiceDependency,
) -> Response:
    refresh_token = request.cookies.get("refresh_token")
    if refresh_token:
        auth_service.logout(refresh_token)
    _clear_refresh_cookie(response)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.post("/auth/change-password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    body: ChangePasswordRequest,
    context: PasswordChangeContext,
    auth_service: AuthServiceDependency,
) -> Response:
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


@router.get("/me")
def get_me(
    context: CurrentContext,
    repository: RepositoryDependency,
) -> dict[str, object]:
    projects = repository.list_projects(context.user_id)
    return {
        "user_id": context.user_id,
        "username": context.username,
        "role": context.role_name,
        "capabilities": sorted(context.capabilities),
        "projects": [
            {"id": str(project["id"]), "name": str(project["name"])}
            for project in projects
        ],
        "must_change_password": context.must_change_password,
    }


@router.post("/connections/test")
def post_connection_test(
    body: ConnectionRequest,
    _context: ManageConnectionContext,
) -> dict[str, object]:
    try:
        result = test_connection(body, get_settings())
    except ConnectionTestError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"server_version": result.server_version, "latency_ms": result.latency_ms}


@router.get("/projects")
def list_projects(
    context: CurrentContext,
    repository: RepositoryDependency,
) -> dict[str, object]:
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


@router.post("/projects", status_code=status.HTTP_201_CREATED)
def create_project(
    body: ConnectionRequest,
    context: ManageConnectionContext,
    repository: RepositoryDependency,
) -> dict[str, object]:
    try:
        test_connection(body, get_settings())
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
    return {"id": str(project_id), "name": body.display_name, "setup_step": "tables"}


@router.get("/projects/{project_id}/setup")
def get_setup_state(
    project_id: UUID,
    context: ManageConnectionContext,
    repository: RepositoryDependency,
) -> dict[str, object]:
    _require_project_access(context, project_id)
    project = repository.get_project_connection(str(project_id))
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return {
        "project_id": str(project_id),
        "step": project["setup_step"],
        "selected_tables": project["selected_tables"],
    }


@router.get("/projects/{project_id}/catalog/tables")
def list_catalog_tables(
    project_id: UUID,
    context: ManageConnectionContext,
    repository: RepositoryDependency,
    search: Annotated[str, Query(max_length=128)] = "",
    schema: Annotated[str | None, Query(max_length=128)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, object]:
    _require_project_access(context, project_id)
    _, catalog = _project_connection(repository, project_id)
    try:
        tables = catalog.list_tables((schema,) if schema else ())
    except psycopg.Error as error:
        raise HTTPException(status_code=502, detail="Could not read database catalog") from error
    filtered = [
        table for table in tables
        if search.casefold() in f"{table.schema_name}.{table.table_name}".casefold()
    ]
    offset = (page - 1) * page_size
    return {
        "items": [
            {
                "schema": table.schema_name,
                "table": table.table_name,
                "approx_rows": table.approx_rows,
                "column_count": table.column_count,
            }
            for table in filtered[offset : offset + page_size]
        ],
        "page": page,
        "page_size": page_size,
        "total": len(filtered),
    }


@router.put("/projects/{project_id}/selected-tables")
def save_selected_tables(
    project_id: UUID,
    body: SelectedTablesRequest,
    context: ManageConnectionContext,
    repository: RepositoryDependency,
) -> dict[str, object]:
    _require_project_access(context, project_id)
    _, catalog = _project_connection(repository, project_id)
    available = {
        (table.schema_name, table.table_name) for table in catalog.list_tables()
    }
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


@router.post("/projects/{project_id}/introspect")
def introspect_project(
    project_id: UUID,
    context: ManageConnectionContext,
    repository: RepositoryDependency,
) -> dict[str, object]:
    _require_project_access(context, project_id)
    selected = repository.parse_project_table_selection(str(project_id))
    tables = tuple((table["schema"], table["table"]) for table in selected)
    _, catalog = _project_connection(repository, project_id)
    try:
        draft = _profile_service(catalog).create_draft(tables)
        mdl = _profile_service(catalog).draft_descriptions(draft.mdl)
    except (TypeError, ValueError, RuntimeError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except psycopg.Error as error:
        raise HTTPException(status_code=502, detail="Could not profile selected tables") from error
    return _serialize_draft(ProfiledDraft(mdl=mdl, tables=draft.tables, summary=draft.summary))


@router.get("/projects/{project_id}/mdl")
def get_mdl(
    project_id: UUID,
    context: CurrentContext,
    repository: RepositoryDependency,
) -> dict[str, object]:
    _require_project_access(context, project_id)
    document = repository.get_mdl(str(project_id))
    if document is None:
        raise HTTPException(status_code=404, detail="Project has no saved MDL")
    return document


@router.put("/projects/{project_id}/mdl")
def save_mdl(
    project_id: UUID,
    body: Mdl,
    context: EditMdlContext,
    repository: RepositoryDependency,
) -> dict[str, object]:
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
        raise HTTPException(status_code=502, detail="Could not validate MDL against catalog") from error
    version = repository.save_mdl(
        str(project_id),
        body.model_dump(by_alias=True, mode="json"),
    )
    return {
        "project_id": str(project_id),
        "revision": version,
        **body.model_dump(by_alias=True),
    }
