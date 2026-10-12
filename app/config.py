import os
from dataclasses import dataclass

from cryptography.fernet import Fernet


@dataclass(frozen=True)
class Settings:
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


def get_settings() -> Settings:
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
    )


def validate_runtime_secrets(settings: Settings) -> None:
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
