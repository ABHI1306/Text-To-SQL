from __future__ import annotations

import json
from datetime import datetime
from typing import TypeAlias, cast

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.auth.models import AuthContext, AuthUser

Row: TypeAlias = dict[str, object]


class PostgresMetadataRepository:
    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ValueError("A metadata database URL is required")
        self._dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)

    def _connect(self) -> psycopg.Connection[Row]:
        return psycopg.connect(self._dsn, row_factory=dict_row)

    def get_user_by_username(self, username: str) -> AuthUser | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT users.id, users.username, roles.name AS role_name,
                       users.password_hash, users.status, users.must_change_password,
                       users.locked_until
                FROM users JOIN roles ON roles.id = users.role_id
                WHERE lower(users.username) = %s
                """,
                (username.casefold(),),
            )
            row = cursor.fetchone()
        return _auth_user(row) if row else None

    def get_auth_context(self, user_id: str) -> AuthContext | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT users.id, users.username, roles.name AS role_name,
                       users.must_change_password
                FROM users JOIN roles ON roles.id = users.role_id
                WHERE users.id = %s::uuid AND users.status = 'active'
                  AND (users.locked_until IS NULL OR users.locked_until <= now())
                """,
                (user_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            cursor.execute(
                "SELECT capability FROM role_capabilities WHERE role_id = "
                "(SELECT role_id FROM users WHERE id = %s::uuid)",
                (user_id,),
            )
            capabilities = frozenset(str(item["capability"]) for item in cursor.fetchall())
            cursor.execute(
                "SELECT project_id FROM project_members WHERE user_id = %s::uuid",
                (user_id,),
            )
            project_ids = frozenset(str(item["project_id"]) for item in cursor.fetchall())
        return AuthContext(
            user_id=str(row["id"]),
            username=str(row["username"]),
            role_name=str(row["role_name"]),
            capabilities=capabilities,
            project_ids=project_ids,
            must_change_password=bool(row["must_change_password"]),
        )

    def record_login_failure(
        self,
        user_id: str,
        *,
        now: datetime,
        max_failures: int,
        window_seconds: int,
        lockout_seconds: int,
    ) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE users
                SET failed_login_count = CASE
                        WHEN failure_window_started_at IS NULL
                          OR failure_window_started_at < %s - (%s * interval '1 second')
                        THEN 1 ELSE failed_login_count + 1 END,
                    failure_window_started_at = CASE
                        WHEN failure_window_started_at IS NULL
                          OR failure_window_started_at < %s - (%s * interval '1 second')
                        THEN %s ELSE failure_window_started_at END,
                    locked_until = CASE
                        WHEN (CASE
                            WHEN failure_window_started_at IS NULL
                              OR failure_window_started_at < %s - (%s * interval '1 second')
                            THEN 1 ELSE failed_login_count + 1 END) >= %s
                        THEN %s + (%s * interval '1 second') ELSE locked_until END
                WHERE id = %s::uuid
                """,
                (
                    now,
                    window_seconds,
                    now,
                    window_seconds,
                    now,
                    now,
                    window_seconds,
                    max_failures,
                    now,
                    lockout_seconds,
                    user_id,
                ),
            )

    def clear_login_failures(self, user_id: str) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE users
                SET failed_login_count = 0, failure_window_started_at = NULL,
                    locked_until = NULL
                WHERE id = %s::uuid
                """,
                (user_id,),
            )

    def create_session(
        self,
        *,
        session_id: str,
        user_id: str,
        family_id: str,
        refresh_token_hash: str,
        expires_at: datetime,
    ) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO sessions (
                    id, user_id, family_id, refresh_token_hash, expires_at
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s)
                """,
                (session_id, user_id, family_id, refresh_token_hash, expires_at),
            )

    def rotate_refresh_token(
        self,
        *,
        old_token_hash: str,
        new_session_id: str,
        new_token_hash: str,
        new_expires_at: datetime,
        now: datetime,
    ) -> tuple[str, str | None, str | None]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, user_id, family_id, expires_at, revoked_at
                FROM sessions WHERE refresh_token_hash = %s FOR UPDATE
                """,
                (old_token_hash,),
            )
            session = cursor.fetchone()
            if session is None:
                return ("invalid", None, None)
            family_id = str(session["family_id"])
            user_id = str(session["user_id"])
            if session["revoked_at"] is not None:
                cursor.execute(
                    """
                    UPDATE sessions SET revoked_at = COALESCE(revoked_at, %s)
                    WHERE family_id = %s::uuid
                    """,
                    (now, family_id),
                )
                return ("reused", user_id, None)
            if cast(datetime, session["expires_at"]) <= now:
                cursor.execute(
                    "UPDATE sessions SET revoked_at = %s WHERE id = %s",
                    (now, session["id"]),
                )
                return ("expired", user_id, None)
            cursor.execute(
                """
                INSERT INTO sessions (
                    id, user_id, family_id, refresh_token_hash, expires_at
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s)
                """,
                (new_session_id, user_id, family_id, new_token_hash, new_expires_at),
            )
            cursor.execute(
                "UPDATE sessions SET revoked_at = %s, rotated_to = %s::uuid WHERE id = %s",
                (now, new_session_id, session["id"]),
            )
        return ("rotated", user_id, str(session["id"]))

    def revoke_refresh_token(self, refresh_token_hash: str, now: datetime) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE sessions SET revoked_at = %s
                WHERE refresh_token_hash = %s AND revoked_at IS NULL
                """,
                (now, refresh_token_hash),
            )

    def revoke_other_sessions(self, user_id: str, keep_session_id: str, now: datetime) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE sessions SET revoked_at = %s
                WHERE user_id = %s::uuid AND id <> %s::uuid AND revoked_at IS NULL
                """,
                (now, user_id, keep_session_id),
            )

    def change_password(
        self, user_id: str, password_hash: str, keep_session_id: str, now: datetime
    ) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE users SET password_hash = %s, must_change_password = FALSE,
                    failed_login_count = 0, failure_window_started_at = NULL,
                    locked_until = NULL
                WHERE id = %s::uuid
                """,
                (password_hash, user_id),
            )
            cursor.execute(
                """
                UPDATE sessions SET revoked_at = %s
                WHERE user_id = %s::uuid AND id <> %s::uuid AND revoked_at IS NULL
                """,
                (now, user_id, keep_session_id),
            )

    def append_auth_audit(
        self, actor_id: str | None, action: str, username: str, details: dict[str, object]
    ) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO audit_events (actor_id, action, resource_type, resource_id, details)
                VALUES (%s::uuid, %s, 'user', %s, %s)
                """,
                (actor_id, action, username, Jsonb(details)),
            )

    def bootstrap_dba(self, username: str, password_hash: str) -> str:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM roles WHERE name = 'DBA'"
            )
            role = cursor.fetchone()
            if role is None:
                raise RuntimeError("DBA system role is missing; run database migrations first")
            cursor.execute(
                """
                INSERT INTO users (id, username, password_hash, role_id, must_change_password)
                VALUES (gen_random_uuid(), %s, %s, %s, TRUE)
                ON CONFLICT (username) DO NOTHING
                RETURNING id
                """,
                (username.casefold(), password_hash, role["id"]),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError(f"User {username!r} already exists")
            return str(row["id"])

    def create_local_user(self, username: str, role_name: str, password_hash: str) -> str:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM roles WHERE name = %s", (role_name,))
            role = cursor.fetchone()
            if role is None:
                raise ValueError(f"Role {role_name!r} does not exist")
            cursor.execute(
                """
                INSERT INTO users (
                    id, username, password_hash, role_id, must_change_password
                ) VALUES (gen_random_uuid(), %s, %s, %s, TRUE)
                RETURNING id
                """,
                (username.casefold(), password_hash, role["id"]),
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("Database did not return a created user")
            user_id = str(row["id"])
            cursor.execute(
                """
                INSERT INTO audit_events (actor_id, action, resource_type, resource_id, details)
                VALUES (NULL, 'user_created', 'user', %s, %s)
                """,
                (username.casefold(), Jsonb({"role": role_name})),
            )
            return user_id

    def reset_local_user(self, username: str, password_hash: str) -> str:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE users
                SET password_hash = %s, must_change_password = TRUE,
                    failed_login_count = 0, failure_window_started_at = NULL,
                    locked_until = NULL
                WHERE lower(username) = %s AND auth_source = 'local'
                RETURNING id
                """,
                (password_hash, username.casefold()),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError(f"Local user {username!r} does not exist")
            user_id = str(row["id"])
            cursor.execute(
                "UPDATE sessions SET revoked_at = now() WHERE user_id = %s::uuid "
                "AND revoked_at IS NULL",
                (user_id,),
            )
            cursor.execute(
                """
                INSERT INTO audit_events (actor_id, action, resource_type, resource_id, details)
                VALUES (NULL, 'password_reset', 'user', %s, '{}'::jsonb)
                """,
                (username.casefold(),),
            )
            return user_id

    def create_project(
        self,
        *,
        project_id: str,
        name: str,
        host: str,
        port: int,
        database_name: str,
        ssl_enabled: bool,
        encrypted_connection: bytes,
        owner_id: str,
    ) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO projects (
                    id, name, host, port, database_name, ssl_enabled,
                    connection_encrypted, setup_step
                ) VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, 'tables')
                """,
                (
                    project_id,
                    name,
                    host,
                    port,
                    database_name,
                    ssl_enabled,
                    encrypted_connection,
                ),
            )
            cursor.execute(
                """
                INSERT INTO project_members (project_id, user_id)
                VALUES (%s::uuid, %s::uuid)
                """,
                (project_id, owner_id),
            )

    def get_project_connection(self, project_id: str) -> dict[str, object] | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, name, host, port, database_name, ssl_enabled,
                       connection_encrypted, selected_tables, setup_step
                FROM projects WHERE id = %s::uuid
                """,
                (project_id,),
            )
            row = cursor.fetchone()
        return dict(row) if row else None

    def save_selected_tables(
        self, project_id: str, tables: list[dict[str, str]]
    ) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE projects SET selected_tables = %s, setup_step = 'relationships'
                WHERE id = %s::uuid
                """,
                (Jsonb(tables), project_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Project {project_id} does not exist")

    def save_mdl(self, project_id: str, document: dict[str, object]) -> int:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COALESCE(MAX(version_number), 0) + 1 AS next_version
                FROM mdl_versions WHERE project_id = %s::uuid
                """,
                (project_id,),
            )
            row = cursor.fetchone()
            version = int(cast(int, row["next_version"])) if row else 1
            cursor.execute(
                """
                INSERT INTO mdl_versions (id, project_id, version_number, status, document)
                VALUES (gen_random_uuid(), %s::uuid, %s, 'pending', %s)
                """,
                (project_id, version, Jsonb(document)),
            )
            cursor.execute(
                """
                UPDATE projects SET setup_step = 'complete'
                WHERE id = %s::uuid
                """,
                (project_id,),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Project {project_id} does not exist")
        return version

    def get_mdl(self, project_id: str) -> dict[str, object] | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT version_number, document FROM mdl_versions
                WHERE project_id = %s::uuid
                ORDER BY version_number DESC LIMIT 1
                """,
                (project_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        document = cast(dict[str, object], row["document"])
        return {
            **document,
            "project_id": project_id,
            "revision": cast(int, row["version_number"]),
        }

    def list_projects(self, user_id: str) -> list[dict[str, object]]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT projects.id, projects.name, projects.host, projects.database_name,
                       projects.setup_step
                FROM projects JOIN project_members
                  ON project_members.project_id = projects.id
                WHERE project_members.user_id = %s::uuid
                ORDER BY projects.created_at
                """,
                (user_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def audit_authentication(self, action: str, user_id: str, details: dict[str, object]) -> None:
        self.append_auth_audit(user_id, action, user_id, details)

    def parse_project_table_selection(self, project_id: str) -> list[dict[str, str]]:
        project = self.get_project_connection(project_id)
        if project is None:
            raise KeyError(f"Project {project_id} does not exist")
        selection = project["selected_tables"]
        if isinstance(selection, str):
            decoded = json.loads(selection)
        else:
            decoded = selection
        if not isinstance(decoded, list):
            raise TypeError("Stored project table selection is invalid")
        return cast(list[dict[str, str]], decoded)


def _auth_user(row: Row) -> AuthUser:
    return AuthUser(
        user_id=str(row["id"]),
        username=str(row["username"]),
        role_name=str(row["role_name"]),
        password_hash=(
            str(row["password_hash"]) if row["password_hash"] is not None else None
        ),
        status=str(row["status"]),
        must_change_password=bool(row["must_change_password"]),
        locked_until=cast(datetime | None, row["locked_until"]),
    )
