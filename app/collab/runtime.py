"""Builds the per-project runtime (deployed model, policies, read-only warehouse) for the API
and the worker, from the metadata repositories and settings."""

from __future__ import annotations

from typing import cast

from psycopg.conninfo import make_conninfo

from app.adapters.metadata.governance import PostgresGovernanceRepository
from app.adapters.metadata.postgres import PostgresMetadataRepository
from app.adapters.warehouse.postgres import PostgresWarehouseAdapter
from app.agent.factory import load_deployed_mdl
from app.api.security import decrypt_connection
from app.collab.service import Invalid, ProjectRuntime, RuntimeFactory
from app.config import Settings


def warehouse_connection(
    repository: PostgresMetadataRepository, settings: Settings, project_id: str
) -> tuple[str, str | None]:
    project = repository.get_project_connection(project_id)
    if project is None:
        raise Invalid("Project does not exist")
    encrypted = project.get("connection_encrypted")
    if not isinstance(encrypted, bytes):
        raise Invalid("Project has no saved connection")
    credentials = decrypt_connection(encrypted, settings)
    certificate = credentials["ssl_ca_certificate"]
    dsn = make_conninfo(
        host=str(project["host"]),
        port=str(cast(int, project["port"])),
        dbname=str(credentials["database"]),
        user=str(credentials["username"]),
        password=str(credentials["password"]),
        connect_timeout="5",
        sslmode="verify-full" if certificate else ("require" if project["ssl_enabled"] else "prefer"),
    )
    return dsn, (str(certificate) if certificate else None)


def runtime_factory(
    repository: PostgresMetadataRepository,
    governance: PostgresGovernanceRepository,
    settings: Settings,
) -> RuntimeFactory:
    def build(project_id: str) -> ProjectRuntime:
        try:
            version, mdl = load_deployed_mdl(repository, project_id)
        except LookupError as error:
            raise Invalid(str(error)) from error
        dsn, certificate = warehouse_connection(repository, settings, project_id)
        warehouse = PostgresWarehouseAdapter(
            dsn,
            statement_timeout_ms=settings.warehouse_statement_timeout_ms,
            max_rows=settings.warehouse_max_rows,
            ssl_ca_certificate=certificate,
            pool_max_size=settings.warehouse_pool_max_size,
        )
        policies = [p.policy for p in governance.list_policies(project_id, enabled_only=True)]
        return ProjectRuntime(version, mdl, policies, warehouse, settings.warehouse_max_rows)

    return build
