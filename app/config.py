import os
from dataclasses import dataclass


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
    )
