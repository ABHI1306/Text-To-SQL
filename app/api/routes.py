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
from app.adapters.warehouse.postgres import PostgresWarehouseAdapter
from app.adapters.warehouse.postgres_catalog import PostgresCatalogAdapter
from app.api.dependencies import (
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
from app.semantic.query import (
    QueryGenerationError,
    QuestionService,
    serialize_answer,
)

router = APIRouter(prefix="/api/v1")

AuthServiceDependency = Annotated[AuthService, Depends(get_auth_service)]
RepositoryDependency = Annotated[PostgresMetadataRepository, Depends(get_repository)]
AuditDependency = Annotated[AuditService, Depends(get_audit_service)]
CurrentContext = Annotated[AuthContext, Depends(get_current_context)]
PasswordChangeContext = Annotated[AuthContext, Depends(get_password_change_context)]
ManageConnectionContext = Annotated[
    AuthContext, Depends(require_capability("manage_connection"))
]
EditMdlContext = Annotated[AuthContext, Depends(require_capability("edit_mdl"))]
DeployMdlContext = Annotated[AuthContext, Depends(require_capability("deploy_mdl"))]
AskQuestionContext = Annotated[AuthContext, Depends(require_capability("ask_question"))]
ManageUsersContext = Annotated[AuthContext, Depends(require_capability("manage_users"))]
ManageRolesContext = Annotated[AuthContext, Depends(require_capability("manage_roles"))]


SAMPLE_PROJECT_TEMPLATES: dict[str, dict[str, object]] = {
    "ecommerce": {
        "name": "E-commerce sample",
        "tables": [
            ("ecommerce", "customers"), ("ecommerce", "addresses"),
            ("ecommerce", "categories"), ("ecommerce", "products"),
            ("ecommerce", "orders"), ("ecommerce", "order_items"),
            ("ecommerce", "payments"), ("ecommerce", "shipments"),
            ("ecommerce", "reviews"), ("ecommerce", "coupons"),
            ("ecommerce", "order_coupons"), ("ecommerce", "inventory"),
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


class AskQuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)


_CAPABILITIES = frozenset(
    {
        "ask_question", "give_feedback", "view_shared", "view_all_threads",
        "rerun_sql", "manage_connection", "edit_mdl", "deploy_mdl",
        "manage_knowledge", "approve_pairs", "manage_policies", "manage_profiling",
        "create_dashboard", "schedule_question", "share", "run_eval",
        "manage_users", "manage_roles", "view_audit",
    }
)


class RoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9 _-]*$")
    description: str = Field(default="", max_length=500)
    capabilities: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("capabilities")
    @classmethod
    def unique_capabilities(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("Capabilities must be unique")
        if not set(values) <= _CAPABILITIES:
            raise ValueError("Unsupported capability")
        return values


class UserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=128, pattern=r"^[A-Za-z0-9_.@-]+$")
    password: SecretStr = Field(min_length=12, max_length=1024)
    role: str = Field(default="User", min_length=1, max_length=64)


class UserRoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str = Field(min_length=1, max_length=64)


class ProjectAccessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_ids: list[UUID] = Field(max_length=500)


class UserAttributesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attributes: dict[str, object] = Field(max_length=100)

    @field_validator("attributes")
    @classmethod
    def validate_attribute_keys(cls, values: dict[str, object]) -> dict[str, object]:
        if any(not key or len(key) > 128 for key in values):
            raise ValueError("Attribute keys must contain 1 to 128 characters")
        return values


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
    certificate = credentials["ssl_ca_certificate"]
    return (
        project,
        dsn,
        str(certificate) if certificate else None,
    )


def _require_project_access(context: AuthContext, project_id: UUID) -> None:
    if str(project_id) not in context.project_ids:
        raise HTTPException(status_code=404, detail="Project not found")


def _require_delegable_capabilities(
    context: AuthContext, capabilities: list[str]
) -> None:
    if not set(capabilities) <= context.capabilities:
        raise HTTPException(
            status_code=403,
            detail="Cannot grant capabilities the current user does not hold",
        )


def _require_delegable_projects(
    context: AuthContext, project_ids: list[UUID]
) -> None:
    if context.role_name != "DBA" and not {
        str(project_id) for project_id in project_ids
    } <= context.project_ids:
        raise HTTPException(
            status_code=403,
            detail="Cannot grant access to projects the current user cannot access",
        )


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
    auth_service: AuthServiceDependency,
    audit: AuditDependency,
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
    auth_service.invalidate_auth_context(context.user_id)
    audit.record(context.user_id, "project_created", "project", str(project_id), {"name": body.display_name, "database": body.database})
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
    context: EditMdlContext,
    repository: RepositoryDependency,
    version: Annotated[int | None, Query(ge=1)] = None,
) -> dict[str, object]:
    _require_project_access(context, project_id)
    document = repository.get_mdl(str(project_id), version)
    if document is None:
        raise HTTPException(status_code=404, detail="Project has no saved MDL")
    return document


@router.put("/projects/{project_id}/mdl")
def save_mdl(
    project_id: UUID,
    body: Mdl,
    context: EditMdlContext,
    repository: RepositoryDependency,
    audit: AuditDependency,
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
    audit.record(context.user_id, "mdl_saved", "project", str(project_id), {"revision": version, "models": len(body.models), "metrics": len(body.metrics)})
    return {
        "project_id": str(project_id),
        "revision": version,
        **body.model_dump(by_alias=True),
    }


@router.post("/projects/{project_id}/deploy")
def deploy_project_mdl(
    project_id: UUID,
    context: DeployMdlContext,
    repository: RepositoryDependency,
    revision: Annotated[int | None, Query(ge=1)] = None,
) -> dict[str, object]:
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
            key: value for key, value in document.items()
            if key not in {"project_id", "revision"}
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


@router.post("/projects/{project_id}/query")
def ask_project_question(
    project_id: UUID,
    body: AskQuestionRequest,
    context: AskQuestionContext,
    repository: RepositoryDependency,
) -> dict[str, object]:
    _require_project_access(context, project_id)
    version = repository.get_active_mdl_version(str(project_id))
    if version is None:
        raise HTTPException(
            status_code=409,
            detail="Deploy a semantic model before asking questions",
        )
    document = repository.get_mdl(str(project_id), version)
    if document is None:
        raise HTTPException(status_code=409, detail="The deployed semantic model is missing")
    mdl_document = {
        key: value for key, value in document.items()
        if key not in {"project_id", "revision"}
    }
    try:
        mdl = Mdl.model_validate(mdl_document)
        _, dsn, ca_certificate = _project_connection_details(repository, project_id)
        settings = get_settings()
        vector_store = QdrantVectorStoreAdapter(settings.qdrant_url)
        service = QuestionService(
            OllamaEmbedderAdapter(settings.ollama_url, settings.embedding_model),
            vector_store,
            OllamaLLMAdapter(settings.ollama_url, settings.sql_model),
            PostgresWarehouseAdapter(
                dsn,
                statement_timeout_ms=settings.warehouse_statement_timeout_ms,
                max_rows=settings.warehouse_max_rows,
                ssl_ca_certificate=ca_certificate,
            ),
            max_rows=settings.warehouse_max_rows,
        )
        answer = service.ask(str(project_id), version, body.question, mdl)
    except QueryGenerationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except psycopg.Error as error:
        raise HTTPException(status_code=502, detail="PostgreSQL could not execute the query") from error
    except (RuntimeError, TypeError, ValueError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    finally:
        if "vector_store" in locals():
            vector_store.close()
    return {**serialize_answer(answer)}


@router.get("/roles")
def list_roles(
    _context: ManageRolesContext,
    repository: RepositoryDependency,
) -> dict[str, object]:
    return {"items": repository.list_roles()}


@router.post("/roles", status_code=status.HTTP_201_CREATED)
def create_role(
    body: RoleRequest,
    context: ManageRolesContext,
    repository: RepositoryDependency,
    audit: AuditDependency,
) -> dict[str, object]:
    _require_delegable_capabilities(context, body.capabilities)
    try:
        role_id = repository.create_role(body.name, body.description, body.capabilities)
    except psycopg.errors.UniqueViolation as error:
        raise HTTPException(status_code=409, detail="Role name already exists") from error
    audit.record(context.user_id, "role_created", "role", str(role_id), {"name": body.name, "capabilities": sorted(body.capabilities)})
    return {"id": role_id, **body.model_dump()}


@router.put("/roles/{role_id}")
def update_role(
    role_id: UUID,
    body: RoleRequest,
    context: ManageRolesContext,
    repository: RepositoryDependency,
    audit: AuditDependency,
) -> dict[str, object]:
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
    audit.record(context.user_id, "role_updated", "role", str(role_id), {"name": body.name, "capabilities": sorted(body.capabilities)})
    return {"id": str(role_id), **body.model_dump()}


@router.delete("/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_role(
    role_id: UUID,
    context: ManageRolesContext,
    repository: RepositoryDependency,
    audit: AuditDependency,
) -> Response:
    try:
        repository.delete_role(str(role_id))
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    audit.record(context.user_id, "role_deleted", "role", str(role_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/users")
def list_users(
    _context: ManageUsersContext,
    repository: RepositoryDependency,
) -> dict[str, object]:
    return {"items": repository.list_users()}


@router.post("/users", status_code=status.HTTP_201_CREATED)
def create_user(
    body: UserRequest,
    context: ManageUsersContext,
    repository: RepositoryDependency,
) -> dict[str, object]:
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


@router.put("/users/{user_id}/role")
def assign_user_role(
    user_id: UUID,
    body: UserRoleRequest,
    context: ManageUsersContext,
    repository: RepositoryDependency,
    audit: AuditDependency,
) -> dict[str, object]:
    capabilities = repository.get_role_capabilities(body.role)
    if capabilities is None:
        raise HTTPException(status_code=404, detail="Role does not exist")
    _require_delegable_capabilities(context, list(capabilities))
    try:
        repository.set_user_role(str(user_id), body.role)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    audit.record(context.user_id, "user_role_changed", "user", str(user_id), {"role": body.role})
    return {"user_id": str(user_id), "role": body.role}


@router.get("/users/{user_id}/project-access")
def get_user_project_access(
    user_id: UUID,
    _context: ManageUsersContext,
    repository: RepositoryDependency,
) -> dict[str, object]:
    if not any(user["id"] == user_id for user in repository.list_users()):
        raise HTTPException(status_code=404, detail="User not found")
    return {"project_ids": repository.get_user_project_access(str(user_id))}


@router.put("/users/{user_id}/project-access")
def set_user_project_access(
    user_id: UUID,
    body: ProjectAccessRequest,
    context: ManageUsersContext,
    repository: RepositoryDependency,
    audit: AuditDependency,
) -> dict[str, object]:
    _require_delegable_projects(context, body.project_ids)
    try:
        repository.replace_user_project_access(
            str(user_id), [str(project_id) for project_id in body.project_ids]
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    audit.record(context.user_id, "user_project_access_changed", "user", str(user_id), {"project_ids": [str(p) for p in body.project_ids]})
    return {"user_id": str(user_id), "project_ids": body.project_ids}


@router.get("/roles/{role_id}/project-access")
def get_role_project_access(
    role_id: UUID,
    _context: ManageRolesContext,
    repository: RepositoryDependency,
) -> dict[str, object]:
    if not any(role["id"] == role_id for role in repository.list_roles()):
        raise HTTPException(status_code=404, detail="Role not found")
    return {"project_ids": repository.get_role_project_access(str(role_id))}


@router.put("/roles/{role_id}/project-access")
def set_role_project_access(
    role_id: UUID,
    body: ProjectAccessRequest,
    context: ManageRolesContext,
    repository: RepositoryDependency,
    audit: AuditDependency,
) -> dict[str, object]:
    _require_delegable_projects(context, body.project_ids)
    try:
        repository.replace_role_project_access(
            str(role_id), [str(project_id) for project_id in body.project_ids]
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    audit.record(context.user_id, "role_project_access_changed", "role", str(role_id), {"project_ids": [str(p) for p in body.project_ids]})
    return {"role_id": str(role_id), "project_ids": body.project_ids}


@router.get("/users/{user_id}/attributes")
def get_user_attributes(
    user_id: UUID,
    _context: ManageUsersContext,
    repository: RepositoryDependency,
) -> dict[str, object]:
    if not any(user["id"] == user_id for user in repository.list_users()):
        raise HTTPException(status_code=404, detail="User not found")
    return {"attributes": repository.get_user_attributes(str(user_id))}


@router.put("/users/{user_id}/attributes")
def set_user_attributes(
    user_id: UUID,
    body: UserAttributesRequest,
    context: ManageUsersContext,
    repository: RepositoryDependency,
    audit: AuditDependency,
) -> dict[str, object]:
    try:
        repository.replace_user_attributes(str(user_id), body.attributes)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    audit.record(context.user_id, "user_attributes_changed", "user", str(user_id), {"keys": sorted(body.attributes)})
    return {"user_id": str(user_id), "attributes": body.attributes}


@router.get("/sample-projects")
def list_sample_project_templates(
    _context: ManageConnectionContext,
) -> dict[str, object]:
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


@router.post("/sample-projects/{template_id}", status_code=status.HTTP_201_CREATED)
def create_sample_project(
    template_id: str,
    context: ManageConnectionContext,
    repository: RepositoryDependency,
    auth_service: AuthServiceDependency,
    audit: AuditDependency,
) -> dict[str, object]:
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
        value = dsn.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"Sample PostgreSQL connection is missing {key}")
        return value

    def optional_value(key: str, default: str) -> str:
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
            (table.schema_name, table.table_name)
            for table in catalog.list_tables(schemas)
        }
        selections = [
            {"schema": schema_name, "table": table_name}
            for schema_name, table_name in table_pairs
        ]
        unknown = {
            (item["schema"], item["table"]) for item in selections
        } - available
        if unknown:
            raise ConnectionTestError(
                f"Sample database is missing required table(s): {sorted(unknown)}"
            )
        draft = ProfilingService(catalog).create_draft(
            tuple(table_pairs)
        )
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
    audit.record(context.user_id, "sample_project_created", "project", project_id, {"template": template_id, "revision": revision})
    return {"id": project_id, "name": template_name, "revision": revision}

