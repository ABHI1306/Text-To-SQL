"""Ask a question from the command line, as a real account, through the full pipeline.

    python -m app.cli.ask --project-id <uuid> --username <name> "How many orders per region?"

The question goes retrieve -> generate -> compile (row policies of that account) -> validate ->
execute, exactly like the API. Compiled SQL is printed only with --show-compiled.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any

from app.adapters.metadata.governance import PostgresGovernanceRepository
from app.adapters.metadata.postgres import PostgresMetadataRepository
from app.agent.factory import NoDeployedModel, build_ask_service, load_deployed_mdl
from app.agent.service import AskResult, AskService
from app.audit.service import AuditService
from app.config import get_settings
from app.core.types import Principal
from app.domain.mdl import Mdl
from app.domain.policy import RowPolicy


@dataclass
class CliContext:
    service: AskService
    close: Any
    project_id: str
    version: int
    mdl: Mdl
    principal: Principal
    policies: list[RowPolicy]


def open_context(project_id: str, username: str, *, audit: bool = True) -> CliContext:
    """Resolve the account, the deployed model, its policies and the ask service."""
    from psycopg.conninfo import make_conninfo

    from app.api.security import decrypt_connection

    settings = get_settings()
    if not settings.metadata_database_url:
        raise RuntimeError("Set METADATA_DATABASE_URL")
    repository = PostgresMetadataRepository(settings.metadata_database_url)
    governance = PostgresGovernanceRepository(settings.metadata_database_url)
    user = repository.get_user_by_username(username)
    if user is None:
        raise LookupError(f"Unknown account {username!r}")
    auth = repository.get_auth_context(user.user_id)
    if auth is None:
        raise LookupError(f"Account {username!r} is not active")
    if project_id not in auth.project_ids:
        raise LookupError(f"Account {username!r} has no access to project {project_id}")
    version, mdl = load_deployed_mdl(repository, project_id)
    project = repository.get_project_connection(project_id)
    if project is None or not isinstance(project.get("connection_encrypted"), bytes):
        raise LookupError("Project has no saved connection")
    credentials = decrypt_connection(project["connection_encrypted"], settings)  # type: ignore[arg-type]
    dsn = make_conninfo(
        host=str(project["host"]),
        port=str(project["port"]),
        dbname=str(credentials["database"]),
        user=str(credentials["username"]),
        password=str(credentials["password"]),
        connect_timeout="5",
        sslmode="verify-full" if credentials["ssl_ca_certificate"] else (
            "require" if project["ssl_enabled"] else "prefer"
        ),
    )
    service, close = build_ask_service(
        dsn,
        credentials["ssl_ca_certificate"] or None,
        audit=AuditService(governance) if audit else None,
    )
    return CliContext(
        service=service,
        close=close,
        project_id=project_id,
        version=version,
        mdl=mdl,
        principal=Principal(
            user_id=auth.user_id,
            role_name=auth.role_name,
            capabilities=auth.capabilities,
            project_ids=auth.project_ids,
            attributes=auth.attributes or {},
        ),
        policies=[p.policy for p in governance.list_policies(project_id, enabled_only=True)],
    )


def ask(context: CliContext, question: str, *, include_compiled: bool) -> AskResult:
    return context.service.ask(
        context.project_id,
        context.version,
        context.mdl,
        context.principal,
        context.policies,
        question,
        include_compiled=include_compiled,
    )


def _print_table(columns: list[str], rows: list[list[Any]]) -> None:
    widths = [
        max([len(column)] + [len(str(row[i])) for row in rows]) for i, column in enumerate(columns)
    ]
    print("  ".join(column.ljust(widths[i]) for i, column in enumerate(columns)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("question")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--username", required=True, help="Account to ask as (its role, attributes and row policies apply)")
    parser.add_argument("--show-compiled", action="store_true", help="Also print the compiled SQL")
    parser.add_argument("--events", action="store_true", help="Print every pipeline event")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    args = parser.parse_args(argv)
    try:
        context = open_context(args.project_id, args.username)
    except (LookupError, RuntimeError, NoDeployedModel) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    try:
        result = ask(context, args.question, include_compiled=args.show_compiled)
    finally:
        context.close()
    if args.json:
        print(json.dumps({
            "ok": result.ok,
            "logical_sql": result.logical_sql,
            "sql": result.sql,
            "columns": result.columns,
            "rows": result.rows,
            "attempts": result.attempts,
            "policy_context": result.policy_context,
            "failure": None if result.failure is None else {
                "code": result.failure.code, "message": result.failure.message,
                "retryable": result.failure.retryable,
            },
        }, indent=2, default=str))
        return 0 if result.ok else 1
    if args.events:
        for event in result.events:
            print(f"[{event.type}] {json.dumps(event.data, default=str)[:300]}")
        print()
    print(f"Logical SQL:\n  {result.logical_sql}\n")
    if args.show_compiled and result.sql:
        print(f"Compiled SQL:\n  {result.sql}\n")
    if result.failure is not None:
        print(f"E_{result.failure.code}: {result.failure.message}", file=sys.stderr)
        return 1
    _print_table(result.columns, result.rows)
    print(f"\n{len(result.rows)} row(s) in {result.elapsed_ms} ms, {result.attempts} correction(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
