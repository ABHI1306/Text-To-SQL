"""Command-line management of local accounts: bootstrap the first DBA, create, reset."""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from argon2.exceptions import HashingError

from app.adapters.metadata.postgres import PostgresMetadataRepository
from app.auth.service import hash_password
from app.config import get_settings


def _repository() -> PostgresMetadataRepository:
    """Open the metadata repository from settings, or raise if the database is not set."""
    settings = get_settings()
    if not settings.metadata_database_url:
        raise RuntimeError("Set METADATA_DATABASE_URL before managing users")
    return PostgresMetadataRepository(settings.metadata_database_url)


def _confirmed_password(password: str | None = None) -> str:
    """Prompt twice for a temporary password (unless given) and require a match."""
    first = password if password is not None else getpass.getpass("Temporary password: ")
    second = (
        password
        if password is not None
        else getpass.getpass("Confirm temporary password: ")
    )
    if first != second:
        raise ValueError("Passwords do not match")
    return first


def _add_user(args: argparse.Namespace) -> int:
    """Run the bootstrap, create or reset command and print the resulting user id."""
    repo = _repository()
    if args.command == "bootstrap-dba":
        username = os.getenv("BOOTSTRAP_DBA_USERNAME", "").strip()
        password = os.getenv("BOOTSTRAP_DBA_PASSWORD")
        if not username or not password:
            raise ValueError(
                "Set BOOTSTRAP_DBA_USERNAME and BOOTSTRAP_DBA_PASSWORD for bootstrap"
            )
        user_id = repo.bootstrap_dba(username, hash_password(password))
    elif args.command == "create":
        password = _confirmed_password()
        user_id = repo.create_local_user(args.username, args.role, hash_password(password))
    else:
        password = _confirmed_password()
        user_id = repo.reset_local_user(args.username, hash_password(password))
    print(f"User operation complete: {user_id}")
    if args.command == "bootstrap-dba":
        print("Remove BOOTSTRAP_DBA_USERNAME and BOOTSTRAP_DBA_PASSWORD after bootstrap.")
    return 0


def main() -> int:
    """Parse the subcommand and run it; returns 0, or 2 after printing an error."""
    parser = argparse.ArgumentParser(description="Manage local Text-to-SQL accounts")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("bootstrap-dba", help="Create the first DBA from environment")
    create = subparsers.add_parser("create", help="Create a local account with a role")
    create.add_argument("username")
    create.add_argument("--role", default="DBA")
    reset = subparsers.add_parser("reset-dba", help="Reset a local user's temporary password")
    reset.add_argument("username")
    args = parser.parse_args()
    try:
        return _add_user(args)
    except (ValueError, RuntimeError, HashingError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
