"""Wiring for the worker process: adapters, handlers and the scheduler."""

from __future__ import annotations

import os
import socket
from typing import cast

from psycopg.conninfo import make_conninfo

from app.adapters.metadata.collab import PostgresCollabStore
from app.adapters.metadata.drift import PostgresDriftRepository
from app.adapters.metadata.governance import PostgresGovernanceRepository
from app.adapters.metadata.postgres import PostgresMetadataRepository
from app.adapters.metadata.scheduler import PostgresJobStore
from app.adapters.warehouse.postgres_catalog import PostgresCatalogAdapter
from app.api.security import decrypt_connection
from app.audit.service import AuditService
from app.collab.jobs import SavedQuestionJobHandler
from app.collab.runtime import runtime_factory
from app.collab.service import CollabService
from app.config import Settings
from app.ports.catalog import CatalogPort
from app.ports.scheduler import JOB_KIND_PROFILE_DRIFT, JOB_KIND_SAVED_QUESTION
from app.scheduler.runner import SlotRunner
from app.scheduler.service import SchedulerService
from app.semantic.drift_job import DriftJobHandler


def worker_id(settings: Settings) -> str:
    """The configured worker id, or ``hostname-pid`` when none is set."""
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
    """Wire repositories, job handlers and the slot runner into a ``SchedulerService``."""
    if not settings.metadata_database_url:
        raise ValueError("METADATA_DATABASE_URL is required for the worker")
    repository = PostgresMetadataRepository(settings.metadata_database_url)
    store = PostgresCollabStore(settings.metadata_database_url)
    governance = PostgresGovernanceRepository(settings.metadata_database_url)
    audit = AuditService(governance)
    jobs = PostgresJobStore(settings.metadata_database_url)
    handler = DriftJobHandler(
        repository,
        lambda project_id: project_catalog(repository, settings, project_id),
        PostgresDriftRepository(settings.metadata_database_url),
        notify=store.add_notification,
    )
    collab = CollabService(
        store, jobs, runtime_factory(repository, governance, settings), audit,
        cache_seconds=settings.result_cache_seconds,
    )
    runner = SlotRunner(
        jobs,
        repository,
        {JOB_KIND_PROFILE_DRIFT: handler, JOB_KIND_SAVED_QUESTION: SavedQuestionJobHandler(collab)},
        worker_id=worker_id(settings),
        lease_seconds=settings.scheduler_lease_seconds,
        audit=audit,
    )
    return SchedulerService(runner, poll_seconds=settings.scheduler_poll_seconds)
