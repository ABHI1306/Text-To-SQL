"""FastAPI dependencies: cached stores and services, auth context and capability checks."""

import dataclasses
from collections.abc import Callable
from functools import lru_cache
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.adapters.identity.ldap import LdapConfig, LdapDirectory
from app.adapters.identity.oidc import OidcConfig, OidcProvider
from app.adapters.llm.ollama_embedder import OllamaEmbedderAdapter
from app.adapters.metadata.collab import PostgresCollabStore
from app.adapters.metadata.governance import PostgresGovernanceRepository
from app.adapters.metadata.knowledge import PostgresKnowledgeRepository
from app.adapters.metadata.oidc_state import PostgresOidcStateStore
from app.adapters.metadata.postgres import PostgresMetadataRepository
from app.adapters.metadata.threads import PostgresThreadStore
from app.adapters.vector_store.qdrant import QdrantVectorStoreAdapter
from app.api.rate_limit import limiter
from app.audit.service import AuditService
from app.auth.models import AuthContext
from app.auth.oidc import OidcLoginService
from app.auth.service import (
    AuthenticationError,
    AuthService,
    PasswordChangeRequired,
)
from app.collab.runtime import runtime_factory
from app.collab.service import CollabService
from app.config import Settings, get_settings
from app.knowledge.service import KnowledgeService

bearer = HTTPBearer(auto_error=False)


@lru_cache
def get_repository() -> PostgresMetadataRepository:
    """Return the cached metadata repository; 503 when no metadata database is set."""
    settings = get_settings()
    if not settings.metadata_database_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Metadata database is not configured",
        )
    return PostgresMetadataRepository(settings.metadata_database_url)


def build_directory(settings: Settings) -> LdapDirectory | None:
    """The LDAP directory when LDAP_URI is set; local accounts only otherwise."""
    if not settings.ldap_uri:
        return None
    attribute_map = parse_attribute_map(settings.ldap_attribute_map)
    return LdapDirectory(
        LdapConfig(
            uri=settings.ldap_uri,
            service_dn=settings.ldap_service_dn,
            service_password=settings.ldap_service_password,
            user_base_dn=settings.ldap_user_base_dn,
            user_filter=settings.ldap_user_filter,
            group_base_dn=settings.ldap_group_base_dn,
            group_filter=settings.ldap_group_filter,
            attribute_map=attribute_map,
            start_tls=settings.ldap_start_tls,
            allow_insecure=settings.ldap_allow_insecure,
            ca_cert_file=settings.ldap_ca_cert_file or None,
        )
    )


@lru_cache
def get_auth_service() -> AuthService:
    """Return the cached ``AuthService``; 503 when ``JWT_SECRET`` is not set."""
    settings = get_settings()
    if not settings.jwt_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not configured; set JWT_SECRET",
        )
    repository = get_repository()
    return AuthService(
        repository,
        directory=build_directory(settings),
        identity_repository=repository,
        jwt_secret=settings.jwt_secret,
        access_token_ttl_seconds=settings.access_token_ttl_seconds,
        refresh_token_ttl_seconds=settings.refresh_token_ttl_seconds,
        max_failures=settings.login_max_failures,
        lockout_seconds=settings.login_lockout_seconds,
    )


def parse_attribute_map(raw: str) -> dict[str, str]:
    """Parse ``name=value,name=value`` pairs into a dict, skipping malformed pairs."""
    return dict(pair.split("=", 1) for pair in raw.split(",") if "=" in pair)


def build_oidc_provider(settings: Settings) -> OidcProvider | None:
    """The OIDC provider when OIDC_ISSUER is set; password and LDAP login only otherwise."""
    if not settings.oidc_issuer:
        return None
    return OidcProvider(
        OidcConfig(
            issuer=settings.oidc_issuer,
            client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret,
            redirect_uri=settings.oidc_redirect_uri,
            discovery_url=settings.oidc_discovery_url,
            scopes=tuple(settings.oidc_scopes.split()),
            username_claim=settings.oidc_username_claim,
            groups_claim=settings.oidc_groups_claim,
            attribute_map=parse_attribute_map(settings.oidc_attribute_map),
            allow_insecure=settings.oidc_allow_insecure,
        )
    )


@lru_cache
def get_oidc_service() -> OidcLoginService | None:
    """Return the cached OIDC login service, or None when OIDC is not configured."""
    settings = get_settings()
    provider = build_oidc_provider(settings)
    if provider is None:
        return None
    return OidcLoginService(
        provider,
        PostgresOidcStateStore(settings.metadata_database_url),
        get_auth_service(),
    )


def _current_context(
    credentials: HTTPAuthorizationCredentials | None,
    auth_service: AuthService,
    *,
    allow_password_change: bool,
) -> AuthContext:
    """Authenticate the bearer token; map auth failures to 401 and 403 responses."""
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
    """Authenticate the caller; refuses tokens that must first change password."""
    return _current_context(credentials, auth_service, allow_password_change=False)


def get_password_change_context(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> AuthContext:
    """Authenticate the caller, accepting tokens that must change password."""
    return _current_context(credentials, auth_service, allow_password_change=True)


def require_capability(
    capability: str, *, per_minute: int | None = None
) -> Callable[..., AuthContext]:
    """Require a capability; ``per_minute`` also caps how often one user may call the route."""

    def check(context: Annotated[AuthContext, Depends(get_current_context)]) -> AuthContext:
        """Reject callers lacking the capability (403) or over the per-minute limit (429)."""
        if capability not in context.capabilities:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing capability: {capability}",
            )
        if per_minute is not None:
            wait = limiter.check(f"{capability}:{per_minute}:{context.user_id}", per_minute)
            if wait is not None:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many requests. Try again shortly.",
                    headers={"Retry-After": str(int(wait) + 1)},
                )
        return context

    check.required_capability = capability  # type: ignore[attr-defined]
    return check


@lru_cache
def get_governance_repository() -> PostgresGovernanceRepository:
    """Return the cached governance repository; 503 without a metadata database."""
    settings = get_settings()
    if not settings.metadata_database_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Metadata database is not configured",
        )
    return PostgresGovernanceRepository(settings.metadata_database_url)


@lru_cache
def get_thread_store() -> PostgresThreadStore:
    """Return the cached thread store; 503 without a metadata database."""
    settings = get_settings()
    if not settings.metadata_database_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Metadata database is not configured",
        )
    return PostgresThreadStore(settings.metadata_database_url)


@lru_cache
def get_knowledge_repository() -> PostgresKnowledgeRepository:
    """Return the cached knowledge repository; 503 without a metadata database."""
    settings = get_settings()
    if not settings.metadata_database_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Metadata database is not configured",
        )
    return PostgresKnowledgeRepository(settings.metadata_database_url)


@lru_cache
def _knowledge_index() -> tuple[OllamaEmbedderAdapter, QdrantVectorStoreAdapter]:
    """Return the cached embedder and vector store used to index knowledge."""
    settings = get_settings()
    return (
        OllamaEmbedderAdapter(settings.ollama_url, settings.embedding_model),
        QdrantVectorStoreAdapter(settings.qdrant_url),
    )


def get_knowledge_service(
    repository: Annotated[PostgresKnowledgeRepository, Depends(get_knowledge_repository)],
) -> KnowledgeService:
    """Build the ``KnowledgeService`` over the repository and vector index."""
    embedder, vector_store = _knowledge_index()
    return KnowledgeService(repository, embedder, vector_store)


def get_audit_service(
    repository: Annotated[PostgresGovernanceRepository, Depends(get_governance_repository)],
) -> AuditService:
    """Build the ``AuditService`` over the governance repository."""
    return AuditService(repository)


def require_project_access(context: AuthContext, project_id: UUID) -> None:
    """Raise 404 unless the project is in the caller's allowed projects."""
    if str(project_id) not in context.project_ids:
        raise HTTPException(status_code=404, detail="Project not found")


@lru_cache
def get_collab_store() -> PostgresCollabStore:
    """Return the cached collaboration store; 503 without a metadata database."""
    settings = get_settings()
    if not settings.metadata_database_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Metadata database is not configured",
        )
    return PostgresCollabStore(settings.metadata_database_url)


ALLOWED_HOSTS_SETTING = "allowed_database_hosts"


def effective_settings() -> Settings:
    """Settings with the DBA-edited host allowlist applied over DB_HOST_ALLOWLIST."""
    settings = get_settings()
    try:
        hosts = get_collab_store().get_setting(ALLOWED_HOSTS_SETTING)
    except Exception:  # noqa: BLE001 - fall back to the environment seed
        return settings
    if isinstance(hosts, list) and all(isinstance(host, str) for host in hosts):
        return dataclasses.replace(settings, allowed_database_hosts=tuple(hosts))
    return settings


def get_collab_service(
    store: Annotated[PostgresCollabStore, Depends(get_collab_store)],
    repository: Annotated[PostgresMetadataRepository, Depends(get_repository)],
    governance: Annotated[PostgresGovernanceRepository, Depends(get_governance_repository)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
) -> CollabService:
    """Build the ``CollabService`` with its job store, runtime factory and audit."""
    from app.adapters.metadata.scheduler import PostgresJobStore

    settings = get_settings()
    return CollabService(
        store,
        PostgresJobStore(settings.metadata_database_url),
        runtime_factory(repository, governance, settings),
        audit,
        user_exists=lambda user_id: repository.get_auth_context(user_id) is not None,
        role_exists=lambda name: repository.get_role_capabilities(name) is not None,
        cache_seconds=settings.result_cache_seconds,
    )
