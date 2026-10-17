"""Dependency checks for ``/health/ready``: each reports up or down, never raises."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import psycopg

from app.config import Settings

Check = Callable[[], None]


def check_metadata_db(dsn: str) -> None:
    with psycopg.connect(dsn.replace("postgresql+psycopg://", "postgresql://", 1), connect_timeout=3) as c:
        c.execute("SELECT 1")


def check_http(url: str) -> None:
    httpx.get(url, timeout=3).raise_for_status()


def run_checks(checks: dict[str, Check]) -> dict[str, str]:
    results: dict[str, str] = {}
    for name, check in checks.items():
        try:
            check()
            results[name] = "up"
        except Exception:  # noqa: BLE001 - any failure means the dependency is down
            results[name] = "down"
    return results


def configured_checks(settings: Settings) -> dict[str, Check]:
    checks: dict[str, Check] = {}
    if settings.metadata_database_url:
        dsn = settings.metadata_database_url
        checks["metadata_db"] = lambda: check_metadata_db(dsn)
    checks["model_server"] = lambda: check_http(f"{settings.ollama_url.rstrip('/')}/api/tags")
    checks["vector_store"] = lambda: check_http(f"{settings.qdrant_url.rstrip('/')}/collections")
    return checks
