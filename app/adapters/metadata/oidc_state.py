"""Pending OIDC logins in the metadata database (shared by every API replica)."""

from __future__ import annotations

from datetime import datetime

import psycopg

from app.auth.oidc import MAX_PENDING_LOGINS


class PostgresOidcStateStore:
    """Holds in-flight OIDC login state in Postgres so any replica can finish a login."""
    def __init__(self, dsn: str, capacity: int = MAX_PENDING_LOGINS) -> None:
        """Store the metadata DSN and the cap on pending logins."""
        if not dsn:
            raise ValueError("A metadata database URL is required")
        self._dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)
        self._capacity = capacity

    def put(
        self, state_hash: str, nonce: str, code_verifier: str, expires_at: datetime
    ) -> bool:
        """Save a pending login after purging expired ones; False when at capacity."""
        with psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM oidc_login_states WHERE expires_at <= now()")
            cursor.execute("SELECT count(*) FROM oidc_login_states")
            row = cursor.fetchone()
            if row is not None and int(row[0]) >= self._capacity:
                return False
            cursor.execute(
                """
                INSERT INTO oidc_login_states (state_hash, nonce, code_verifier, expires_at)
                VALUES (%s, %s, %s, %s)
                """,
                (state_hash, nonce, code_verifier, expires_at),
            )
        return True

    def consume(self, state_hash: str, now: datetime) -> tuple[str, str] | None:
        # DELETE ... RETURNING is atomic: of two concurrent callbacks only one gets the row.
        """Atomically take a pending login (single use); ``None`` if unknown or expired."""
        with psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM oidc_login_states WHERE state_hash = %s
                RETURNING nonce, code_verifier, expires_at
                """,
                (state_hash,),
            )
            row = cursor.fetchone()
        if row is None or row[2] <= now:
            return None
        return str(row[0]), str(row[1])
