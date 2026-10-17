"""Operator tasks that have no screen: rotate the connection-encryption key, re-index a project.

    python -m app.cli.maintenance rotate-connection-key [--skip-unreadable]  # needs OLD_CONNECTION_ENCRYPTION_KEY
    python -m app.cli.maintenance reindex --project-id <uuid>

Both are run inside the api container (see docs/RUNBOOK.md).
"""

from __future__ import annotations

import argparse
import os
import sys

import psycopg
from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


def reencrypt(blob: bytes, old: Fernet, new: Fernet) -> bytes:
    """The same plaintext under the new key; raises ``InvalidToken`` if ``blob`` is not under ``old``."""
    return new.encrypt(old.decrypt(blob))


def _fernet(name: str, value: str | None) -> Fernet:
    if not value:
        raise ValueError(f"{name} is not set")
    try:
        return Fernet(value.encode("ascii"))
    except (ValueError, TypeError) as error:
        raise ValueError(f"{name} is not a valid Fernet key") from error


def rotate_connection_key(
    dsn: str, old: Fernet, new: Fernet, *, skip_unreadable: bool = False
) -> tuple[int, list[str]]:
    """Re-encrypt every saved project connection in one transaction.

    Returns (rotated count, names of projects skipped). A connection that ``old`` cannot read
    is already unusable; by default that aborts the whole run with nothing changed, and with
    ``skip_unreadable`` those projects are left as they are and reported.
    """
    with psycopg.connect(dsn.replace("postgresql+psycopg://", "postgresql://", 1)) as connection:
        rows = connection.execute(
            "SELECT id, name, connection_encrypted FROM projects"
            " WHERE connection_encrypted IS NOT NULL ORDER BY created_at"
        ).fetchall()
        updated: list[tuple[bytes, object]] = []
        unreadable: list[str] = []
        for project_id, name, blob in rows:
            try:
                updated.append((reencrypt(bytes(blob), old, new), project_id))
            except InvalidToken:
                unreadable.append(f"{name} ({project_id})")
        if unreadable and not updated:
            raise ValueError(
                "No saved connection is readable with OLD_CONNECTION_ENCRYPTION_KEY, so it is "
                "probably not the key that was in use. Nothing was changed"
            )
        if unreadable and not skip_unreadable:
            shown = "; ".join(unreadable[:5]) + (" ..." if len(unreadable) > 5 else "")
            raise ValueError(
                f"{len(unreadable)} saved connection(s) are not readable with "
                f"OLD_CONNECTION_ENCRYPTION_KEY (first: {shown}). Nothing was changed. Check OLD is "
                "the key that was in use; if those projects are already unusable (for example left by "
                "tests), repeat with --skip-unreadable"
            )
        with connection.cursor() as cursor:
            cursor.executemany("UPDATE projects SET connection_encrypted = %s WHERE id = %s", updated)
    return len(updated), unreadable


def _rotate(skip_unreadable: bool) -> int:
    settings = get_settings()
    if not settings.metadata_database_url:
        raise ValueError("METADATA_DATABASE_URL is not set")
    old = _fernet("OLD_CONNECTION_ENCRYPTION_KEY", os.getenv("OLD_CONNECTION_ENCRYPTION_KEY"))
    new = _fernet("CONNECTION_ENCRYPTION_KEY", settings.connection_encryption_key)
    count, skipped = rotate_connection_key(
        settings.metadata_database_url, old, new, skip_unreadable=skip_unreadable
    )
    print(f"Re-encrypted {count} saved connection(s). Restart the api and worker.")
    if skipped:
        print(f"Left unchanged because they were already unreadable ({len(skipped)}):")
        for item in skipped:
            print(f"  {item}")
    return 0


def _reindex(project_id: str) -> int:
    from app.adapters.llm.ollama_embedder import OllamaEmbedderAdapter
    from app.adapters.metadata.postgres import PostgresMetadataRepository
    from app.adapters.vector_store.qdrant import QdrantVectorStoreAdapter
    from app.domain.mdl import Mdl
    from app.semantic.indexing import IndexingService

    settings = get_settings()
    if not settings.metadata_database_url:
        raise ValueError("METADATA_DATABASE_URL is not set")
    repository = PostgresMetadataRepository(settings.metadata_database_url)
    version = repository.get_active_mdl_version(project_id)
    if version is None:
        raise ValueError("The project has no deployed model; deploy one first")
    document = repository.get_mdl(project_id, version)
    if document is None:
        raise ValueError("The deployed model revision is missing")
    mdl = Mdl.model_validate({k: v for k, v in document.items() if k not in {"project_id", "revision"}})
    embedder = OllamaEmbedderAdapter(settings.ollama_url, settings.embedding_model)
    embedder.embed(["ping"])  # fail now, before the index is touched, if the model is not available
    store = QdrantVectorStoreAdapter(settings.qdrant_url)
    try:
        chunks = IndexingService(embedder, store).index(project_id, str(version), mdl)
    finally:
        store.close()
    print(f"Re-indexed project {project_id}, model version {version}: {chunks} chunk(s).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Text-to-SQL operator tasks")
    commands = parser.add_subparsers(dest="command", required=True)
    rotate = commands.add_parser("rotate-connection-key", help="re-encrypt saved connections under a new key")
    rotate.add_argument(
        "--skip-unreadable", action="store_true",
        help="leave connections the old key cannot read (already unusable) instead of aborting",
    )
    reindex = commands.add_parser("reindex", help="rebuild a project's vector index from its deployed model")
    reindex.add_argument("--project-id", required=True)
    args = parser.parse_args()
    try:
        return _rotate(args.skip_unreadable) if args.command == "rotate-connection-key" else _reindex(args.project_id)
    except (ValueError, RuntimeError, psycopg.Error) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
