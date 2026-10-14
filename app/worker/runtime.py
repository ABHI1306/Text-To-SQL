"""Wiring for the worker process: adapters, handlers and the scheduler."""

from __future__ import annotations

import os
import socket
from typing import cast

from psycopg.conninfo import make_conninfo

from app.adapters.metadata.drift import PostgresDriftRepository
from app.adapters.metadata.governance import PostgresGovernanceRepository
from app.adapters.metadata.postgres import PostgresMetadataRepository
from app.adapters.metadata.scheduler import PostgresJobStore
from app.adapters.warehouse.postgres_catalog import PostgresCatalogAdapter
from app.api.security import decrypt_connection
from app.audit.service import AuditService
from app.config import Settings
from app.ports.catalog import CatalogPort
from app.ports.scheduler import JOB_KIND_PROFILE_DRIFT
from app.scheduler.runner import SlotRunner
from app.scheduler.service import SchedulerService
from app.semantic.drift_job import DriftJobHandler


def worker_id(settings: Settings) -> str:
    return settings.scheduler_worker_id or f"{socket.gethostname()}-{os.getpid()}"


def project_catalog(
    repository: PostgresMetadataRepository, settings: Settings, project_id: str
) -> CatalogPort:
    """A catalog adapter on the project's saved warehouse connection."""
    project = repository.get_project_connection(project_id)
    if project is None:
        raise ValueError("Project does not exist")
    encrypted = project.get("connection_encrypted")
    if not isinstance(encrypted, bytes):
        raise TypeError("Project has no saved connection")
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
    return PostgresCatalogAdapter(
        dsn,
        statement_timeout_ms=settings.warehouse_statement_timeout_ms,
        ssl_ca_certificate=str(certificate) if certificate else None,
    )


def build_scheduler(settings: Settings) -> SchedulerService:
    if not settings.metadata_database_url:
        raise ValueError("METADATA_DATABASE_URL is required for the worker")
    repository = PostgresMetadataRepository(settings.metadata_database_url)
    handler = DriftJobHandler(
        repository,
        lambda project_id: project_catalog(repository, settings, project_id),
        PostgresDriftRepository(settings.metadata_database_url),
    )
    runner = SlotRunner(
        PostgresJobStore(settings.metadata_database_url),
        repository,
        {JOB_KIND_PROFILE_DRIFT: handler},
        worker_id=worker_id(settings),
        lease_seconds=settings.scheduler_lease_seconds,
        audit=AuditService(PostgresGovernanceRepository(settings.metadata_database_url)),
    )
    return SchedulerService(runner, poll_seconds=settings.scheduler_poll_seconds)
