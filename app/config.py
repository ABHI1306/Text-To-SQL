"""Runtime settings read from environment variables."""

import os
from dataclasses import dataclass

from cryptography.fernet import Fernet


@dataclass(frozen=True)
class Settings:
    """Immutable runtime configuration; every field comes from an environment variable."""
    app_env: str
    log_level: str
    ollama_url: str
    qdrant_url: str
    warehouse_dsn: str
    warehouse_statement_timeout_ms: int
    warehouse_max_rows: int
    metadata_database_url: str
    metadata_migration_url: str
    jwt_secret: str
    connection_encryption_key: str
    bootstrap_dba_username: str
    bootstrap_dba_password: str
    allowed_database_hosts: tuple[str, ...]
    access_token_ttl_seconds: int
    refresh_token_ttl_seconds: int
    login_max_failures: int
    login_lockout_seconds: int
    sql_model: str
    embedding_model: str = "nomic-embed-text:latest"
    small_model: str = ""
    ldap_uri: str = ""
    ldap_start_tls: bool = False
    ldap_allow_insecure: bool = False
    ldap_ca_cert_file: str = ""
    ldap_service_dn: str = ""
    ldap_service_password: str = ""
    ldap_user_base_dn: str = ""
    ldap_user_filter: str = "(uid={username})"
    ldap_group_base_dn: str = ""
    ldap_group_filter: str = "(member={dn})"
    ldap_attribute_map: str = ""
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_redirect_uri: str = ""
    oidc_discovery_url: str = ""
    oidc_scopes: str = "openid profile email"
    oidc_username_claim: str = "preferred_username"
    oidc_groups_claim: str = "groups"
    oidc_attribute_map: str = ""
    oidc_allow_insecure: bool = False
    scheduler_poll_seconds: int = 15
    result_cache_seconds: int = 300
    scheduler_lease_seconds: int = 300
    scheduler_worker_id: str = ""
    retrieval_cache_seconds: int = 60
    warehouse_pool_max_size: int = 5


def get_settings() -> Settings:
    """Read the environment into ``Settings``, applying the defaults."""
    return Settings(
        app_env=os.getenv("APP_ENV", "development"),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        ollama_url=os.getenv("OLLAMA_URL", "http://127.0.0.1:11434"),
        qdrant_url=os.getenv("QDRANT_URL", "http://127.0.0.1:6333"),
        warehouse_dsn=os.getenv("WAREHOUSE_DSN", ""),
        warehouse_statement_timeout_ms=int(
            os.getenv("WAREHOUSE_STATEMENT_TIMEOUT_MS", "10000")
        ),
        warehouse_max_rows=int(os.getenv("WAREHOUSE_MAX_ROWS", "1000")),
        metadata_database_url=os.getenv("METADATA_DATABASE_URL", ""),
        metadata_migration_url=os.getenv("METADATA_MIGRATION_URL", ""),
        jwt_secret=os.getenv("JWT_SECRET", ""),
        connection_encryption_key=os.getenv("CONNECTION_ENCRYPTION_KEY", ""),
        bootstrap_dba_username=os.getenv("BOOTSTRAP_DBA_USERNAME", ""),
        bootstrap_dba_password=os.getenv("BOOTSTRAP_DBA_PASSWORD", ""),
        allowed_database_hosts=tuple(
            host.strip()
            for host in os.getenv(
                "DB_HOST_ALLOWLIST", "demo-db,localhost,127.0.0.1"
            ).split(",")
            if host.strip()
        ),
        access_token_ttl_seconds=int(os.getenv("ACCESS_TOKEN_TTL_SECONDS", "900")),
        refresh_token_ttl_seconds=int(os.getenv("REFRESH_TOKEN_TTL_SECONDS", "28800")),
        login_max_failures=int(os.getenv("LOGIN_MAX_FAILURES", "5")),
        login_lockout_seconds=int(os.getenv("LOGIN_LOCKOUT_SECONDS", "900")),
        sql_model=os.getenv("SQL_MODEL", "llama3:latest"),
        small_model=os.getenv("SMALL_MODEL", ""),
        embedding_model=os.getenv("EMBEDDING_MODEL", "nomic-embed-text:latest"),
        ldap_uri=os.getenv("LDAP_URI", ""),
        ldap_start_tls=os.getenv("LDAP_START_TLS", "").lower() in {"1", "true", "yes"},
        ldap_allow_insecure=os.getenv("LDAP_ALLOW_INSECURE", "").lower() in {"1", "true", "yes"},
        ldap_ca_cert_file=os.getenv("LDAP_CA_CERT_FILE", ""),
        ldap_service_dn=os.getenv("LDAP_SERVICE_DN", ""),
        ldap_service_password=os.getenv("LDAP_SERVICE_PASSWORD", ""),
        ldap_user_base_dn=os.getenv("LDAP_USER_BASE_DN", ""),
        ldap_user_filter=os.getenv("LDAP_USER_FILTER", "(uid={username})"),
        ldap_group_base_dn=os.getenv("LDAP_GROUP_BASE_DN", ""),
        ldap_group_filter=os.getenv("LDAP_GROUP_FILTER", "(member={dn})"),
        ldap_attribute_map=os.getenv("LDAP_ATTRIBUTE_MAP", ""),
        oidc_issuer=os.getenv("OIDC_ISSUER", ""),
        oidc_client_id=os.getenv("OIDC_CLIENT_ID", ""),
        oidc_client_secret=os.getenv("OIDC_CLIENT_SECRET", ""),
        oidc_redirect_uri=os.getenv("OIDC_REDIRECT_URI", ""),
        oidc_discovery_url=os.getenv("OIDC_DISCOVERY_URL", ""),
        oidc_scopes=os.getenv("OIDC_SCOPES", "openid profile email"),
        oidc_username_claim=os.getenv("OIDC_USERNAME_CLAIM", "preferred_username"),
        oidc_groups_claim=os.getenv("OIDC_GROUPS_CLAIM", "groups"),
        oidc_attribute_map=os.getenv("OIDC_ATTRIBUTE_MAP", ""),
        oidc_allow_insecure=os.getenv("OIDC_ALLOW_INSECURE", "").lower() in {"1", "true", "yes"},
        scheduler_poll_seconds=int(os.getenv("SCHEDULER_POLL_SECONDS", "15")),
        result_cache_seconds=int(os.getenv("RESULT_CACHE_SECONDS", "300")),
        scheduler_lease_seconds=int(os.getenv("SCHEDULER_LEASE_SECONDS", "300")),
        scheduler_worker_id=os.getenv("SCHEDULER_WORKER_ID", ""),
        retrieval_cache_seconds=int(os.getenv("RETRIEVAL_CACHE_SECONDS", "60")),
        warehouse_pool_max_size=int(os.getenv("WAREHOUSE_POOL_MAX_SIZE", "5")),
    )


def validate_runtime_secrets(settings: Settings) -> None:
    """Outside development and test, require a long JWT secret and a valid Fernet key."""
    if settings.app_env.lower() in {"development", "test"}:
        return
    if len(settings.jwt_secret) < 32:
        raise ValueError("JWT_SECRET must contain at least 32 characters outside development")
    try:
        Fernet(settings.connection_encryption_key.encode("ascii"))
    except (ValueError, TypeError, UnicodeEncodeError) as error:
        raise ValueError(
            "Set CONNECTION_ENCRYPTION_KEY to a valid Fernet key outside development"
        ) from error
