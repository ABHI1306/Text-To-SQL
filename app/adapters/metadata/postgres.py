from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import TypeAlias, cast
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.auth.models import AuthContext, AuthUser
from app.core.types import JsonValue

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
                       users.locked_until, users.auth_source
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
            project_ids = {str(item["project_id"]) for item in cursor.fetchall()}
            cursor.execute(
                """
                SELECT project_role_members.project_id
                FROM project_role_members
                JOIN users ON users.role_id = project_role_members.role_id
                WHERE users.id = %s::uuid
                """,
                (user_id,),
            )
            project_ids.update(str(item["project_id"]) for item in cursor.fetchall())
            if row["role_name"] == "DBA":
                cursor.execute("SELECT id FROM projects")
                project_ids.update(str(item["id"]) for item in cursor.fetchall())
            cursor.execute(
                "SELECT key, value FROM user_attributes WHERE user_id = %s::uuid",
                (user_id,),
            )
            attributes = {
                str(item["key"]): cast(JsonValue, item["value"])
                for item in cursor.fetchall()
            }
        return AuthContext(
            user_id=str(row["id"]),
            username=str(row["username"]),
            role_name=str(row["role_name"]),
            capabilities=capabilities,
            project_ids=frozenset(project_ids),
            must_change_password=bool(row["must_change_password"]),
            attributes=attributes,
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

    def create_local_user(
        self,
        username: str,
        role_name: str,
        password_hash: str,
        *,
        actor_id: str | None = None,
    ) -> str:
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
                VALUES (%s::uuid, 'user_created', 'user', %s, %s)
                """,
                (actor_id, username.casefold(), Jsonb({"role": role_name})),
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

    def get_mdl(
        self, project_id: str, version: int | None = None
    ) -> dict[str, object] | None:
        with self._connect() as connection, connection.cursor() as cursor:
            if version is None:
                cursor.execute(
                    """
                    SELECT version_number, document FROM mdl_versions
                    WHERE project_id = %s::uuid
                    ORDER BY version_number DESC LIMIT 1
                    """,
                    (project_id,),
                )
            else:
                cursor.execute(
                    """
                    SELECT version_number, document FROM mdl_versions
                    WHERE project_id = %s::uuid AND version_number = %s
                    """,
                    (project_id, version),
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

    def set_project_sample(self, project_id: str) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE projects SET is_sample = TRUE WHERE id = %s::uuid",
                (project_id,),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Project {project_id} does not exist")

    def get_active_mdl_version(self, project_id: str) -> int | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT version_number FROM mdl_versions
                WHERE project_id = %s::uuid AND status = 'active'
                """,
                (project_id,),
            )
            row = cursor.fetchone()
        return int(cast(int, row["version_number"])) if row else None

    def activate_mdl_version(
        self,
        project_id: str,
        version: int,
        *,
        deployed_by: str,
        expected_previous_version: int | None,
    ) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM projects WHERE id = %s::uuid FOR UPDATE",
                (project_id,),
            )
            if cursor.fetchone() is None:
                raise KeyError(f"Project {project_id} does not exist")
            cursor.execute(
                """
                SELECT version_number FROM mdl_versions
                WHERE project_id = %s::uuid AND status = 'active'
                FOR UPDATE
                """,
                (project_id,),
            )
            active_row = cursor.fetchone()
            active_version = (
                int(cast(int, active_row["version_number"])) if active_row else None
            )
            if active_version != expected_previous_version:
                raise RuntimeError(
                    "Active MDL changed during deployment; retry with the latest revision"
                )
            cursor.execute(
                """
                SELECT id FROM mdl_versions
                WHERE project_id = %s::uuid AND version_number = %s
                FOR UPDATE
                """,
                (project_id, version),
            )
            mdl_version = cursor.fetchone()
            if mdl_version is None:
                raise KeyError(f"MDL revision {version} does not exist")
            cursor.execute(
                """
                UPDATE mdl_versions SET status = 'pending'
                WHERE project_id = %s::uuid AND status = 'active'
                """,
                (project_id,),
            )
            cursor.execute(
                """
                UPDATE mdl_versions
                SET status = 'active', deployed_at = now(), deployed_by = %s::uuid
                WHERE id = %s::uuid
                """,
                (deployed_by, mdl_version["id"]),
            )
            cursor.execute(
                """
                INSERT INTO audit_events
                    (actor_id, action, resource_type, resource_id, details)
                VALUES (%s::uuid, 'mdl_deployed', 'project', %s,
                        jsonb_build_object('revision', %s))
                """,
                (deployed_by, project_id, version),
            )

    def restore_active_mdl_version(
        self,
        project_id: str,
        new_version: int,
        previous_version: int,
    ) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM projects WHERE id = %s::uuid FOR UPDATE",
                (project_id,),
            )
            if cursor.fetchone() is None:
                raise KeyError(f"Project {project_id} does not exist")
            cursor.execute(
                """
                UPDATE mdl_versions SET status = 'pending'
                WHERE project_id = %s::uuid AND version_number = %s AND status = 'active'
                """,
                (project_id, new_version),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "Active MDL changed before deployment rollback could be applied"
                )
            cursor.execute(
                """
                UPDATE mdl_versions SET status = 'active'
                WHERE project_id = %s::uuid AND version_number = %s
                """,
                (project_id, previous_version),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"Previous active MDL revision {previous_version} is missing"
                )

    def list_projects(self, user_id: str) -> list[dict[str, object]]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT projects.id, projects.name, projects.host, projects.database_name,
                       projects.setup_step
                FROM projects
                WHERE EXISTS (
                    SELECT 1 FROM users JOIN roles ON roles.id = users.role_id
                    WHERE users.id = %s::uuid AND roles.name = 'DBA'
                ) OR EXISTS (
                    SELECT 1 FROM project_members
                    WHERE project_members.project_id = projects.id
                      AND project_members.user_id = %s::uuid
                ) OR EXISTS (
                    SELECT 1
                    FROM project_role_members
                    JOIN users ON users.role_id = project_role_members.role_id
                    WHERE project_role_members.project_id = projects.id
                      AND users.id = %s::uuid
                )
                ORDER BY projects.created_at
                """,
                (user_id, user_id, user_id),
            )
            return [dict(row) for row in cursor.fetchall()]

    def list_roles(self) -> list[dict[str, object]]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT roles.id, roles.name, roles.description, roles.is_system,
                       COALESCE(array_agg(role_capabilities.capability)
                           FILTER (WHERE role_capabilities.capability IS NOT NULL), '{}')
                           AS capabilities
                FROM roles LEFT JOIN role_capabilities ON role_capabilities.role_id = roles.id
                GROUP BY roles.id ORDER BY roles.is_system DESC, roles.name
                """
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_role_capabilities(self, role_name: str) -> frozenset[str] | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT role_capabilities.capability
                FROM roles
                LEFT JOIN role_capabilities ON role_capabilities.role_id = roles.id
                WHERE roles.name = %s
                """,
                (role_name,),
            )
            rows = cursor.fetchall()
            if not rows:
                return None
            return frozenset(
                str(row["capability"])
                for row in rows
                if row["capability"] is not None
            )

    def create_role(
        self, name: str, description: str, capabilities: list[str]
    ) -> str:
        role_id = str(uuid4())
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO roles (id, name, description, is_system) "
                "VALUES (%s::uuid, %s, %s, FALSE)",
                (role_id, name, description),
            )
            cursor.executemany(
                "INSERT INTO role_capabilities (role_id, capability) "
                "VALUES (%s::uuid, %s)",
                [(role_id, capability) for capability in capabilities],
            )
        return role_id

    def update_role(
        self,
        role_id: str,
        *,
        name: str,
        description: str,
        capabilities: list[str],
    ) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE roles SET name = %s, description = %s "
                "WHERE id = %s::uuid AND is_system = FALSE",
                (name, description, role_id),
            )
            if cursor.rowcount == 0:
                raise KeyError("Custom role does not exist")
            cursor.execute("DELETE FROM role_capabilities WHERE role_id = %s::uuid", (role_id,))
            cursor.executemany(
                "INSERT INTO role_capabilities (role_id, capability) "
                "VALUES (%s::uuid, %s)",
                [(role_id, capability) for capability in capabilities],
            )

    def delete_role(self, role_id: str) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT roles.is_system,
                       EXISTS (SELECT 1 FROM users WHERE users.role_id = roles.id)
                       OR EXISTS (
                           SELECT 1 FROM project_role_members
                           WHERE project_role_members.role_id = roles.id
                       ) AS in_use
                FROM roles WHERE id = %s::uuid FOR UPDATE
                """,
                (role_id,),
            )
            row = cursor.fetchone()
            if row is None or bool(row["is_system"]):
                raise KeyError("Custom role does not exist")
            if bool(row["in_use"]):
                raise ValueError("Role is assigned to users or projects")
            cursor.execute("DELETE FROM roles WHERE id = %s::uuid", (role_id,))

    def list_users(self) -> list[dict[str, object]]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT users.id, users.username, users.status, users.auth_source,
                       roles.name AS role
                FROM users JOIN roles ON roles.id = users.role_id
                ORDER BY users.username
                """
            )
            return [dict(row) for row in cursor.fetchall()]

    def set_user_role(self, user_id: str, role_name: str) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE users SET role_id = (SELECT id FROM roles WHERE name = %s)
                WHERE id = %s::uuid AND EXISTS (SELECT 1 FROM roles WHERE name = %s)
                """,
                (role_name, user_id, role_name),
            )
            if cursor.rowcount == 0:
                raise KeyError("User or role does not exist")

    def replace_user_project_access(self, user_id: str, project_ids: list[str]) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM users WHERE id = %s::uuid", (user_id,))
            if cursor.fetchone() is None:
                raise KeyError("User does not exist")
            self._validate_project_ids(cursor, project_ids)
            cursor.execute("DELETE FROM project_members WHERE user_id = %s::uuid", (user_id,))
            cursor.executemany(
                "INSERT INTO project_members (project_id, user_id) "
                "VALUES (%s::uuid, %s::uuid)",
                [(project_id, user_id) for project_id in project_ids],
            )

    def replace_role_project_access(self, role_id: str, project_ids: list[str]) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM roles WHERE id = %s::uuid", (role_id,))
            if cursor.fetchone() is None:
                raise KeyError("Role does not exist")
            self._validate_project_ids(cursor, project_ids)
            cursor.execute(
                "DELETE FROM project_role_members WHERE role_id = %s::uuid", (role_id,)
            )
            cursor.executemany(
                "INSERT INTO project_role_members (project_id, role_id) "
                "VALUES (%s::uuid, %s::uuid)",
                [(project_id, role_id) for project_id in project_ids],
            )

    def get_user_project_access(self, user_id: str) -> list[str]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT project_id FROM project_members WHERE user_id = %s::uuid",
                (user_id,),
            )
            return [str(row["project_id"]) for row in cursor.fetchall()]

    def get_role_project_access(self, role_id: str) -> list[str]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT project_id FROM project_role_members WHERE role_id = %s::uuid",
                (role_id,),
            )
            return [str(row["project_id"]) for row in cursor.fetchall()]

    def get_user_attributes(self, user_id: str) -> dict[str, object]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT key, value FROM user_attributes WHERE user_id = %s::uuid",
                (user_id,),
            )
            return {str(row["key"]): row["value"] for row in cursor.fetchall()}

    def replace_user_attributes(self, user_id: str, attributes: dict[str, object]) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM users WHERE id = %s::uuid", (user_id,))
            if cursor.fetchone() is None:
                raise KeyError("User does not exist")
            cursor.execute("DELETE FROM user_attributes WHERE user_id = %s::uuid", (user_id,))
            cursor.executemany(
                """
                INSERT INTO user_attributes (user_id, key, value, source)
                VALUES (%s::uuid, %s, %s, 'manual')
                """,
                [
                    (user_id, key, Jsonb(value))
                    for key, value in attributes.items()
                ],
            )

    # ------------------------------------------------------------------ identity

    def list_identity_mappings(self) -> list[dict[str, object]]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT m.id, m.source, m.match, r.name AS role
                FROM identity_mappings m JOIN roles r ON r.id = m.role_id
                ORDER BY m.source, m.created_at, m.match
                """
            )
            return [
                {"id": str(row["id"]), "source": row["source"], "match": row["match"], "role": row["role"]}
                for row in cursor.fetchall()
            ]

    def get_identity_mappings(self, source: str) -> list[tuple[str, str]]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT m.match, r.name AS role
                FROM identity_mappings m JOIN roles r ON r.id = m.role_id
                WHERE m.source = %s ORDER BY m.created_at, m.match
                """,
                (source,),
            )
            return [(str(row["match"]), str(row["role"])) for row in cursor.fetchall()]

    def replace_identity_mappings(
        self, source: str, mappings: list[tuple[str, str]], *, actor_id: str
    ) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM identity_mappings WHERE source = %s", (source,))
            for match, role_name in mappings:
                cursor.execute("SELECT id FROM roles WHERE name = %s", (role_name,))
                role = cursor.fetchone()
                if role is None:
                    raise KeyError(f"Role {role_name!r} does not exist")
                cursor.execute(
                    "INSERT INTO identity_mappings (source, match, role_id) VALUES (%s, %s, %s)",
                    (source, match, role["id"]),
                )
            cursor.execute(
                """
                INSERT INTO audit_events (actor_id, action, resource_type, resource_id, details)
                VALUES (%s::uuid, 'identity_mappings_replaced', 'identity', %s, %s)
                """,
                (actor_id, source, Jsonb({"mappings": [{"match": m, "role": r} for m, r in mappings]})),
            )

    def provision_external_user(
        self,
        username: str,
        source: str,
        role_name: str,
        attributes: Mapping[str, JsonValue],
    ) -> str:
        """Create the account on first login; on later logins re-apply role and attributes."""
        name = username.strip().casefold()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM roles WHERE name = %s", (role_name,))
            role = cursor.fetchone()
            if role is None:
                raise ValueError(f"Role {role_name!r} does not exist")
            cursor.execute(
                "SELECT id, auth_source FROM users WHERE lower(username) = %s FOR UPDATE", (name,)
            )
            existing = cursor.fetchone()
            if existing is not None and existing["auth_source"] != source:
                raise ValueError("That username belongs to an account from another source")
            if existing is None:
                cursor.execute(
                    """
                    INSERT INTO users (id, username, password_hash, role_id, auth_source,
                                       must_change_password)
                    VALUES (gen_random_uuid(), %s, NULL, %s, %s, FALSE) RETURNING id
                    """,
                    (name, role["id"], source),
                )
                user_id = str(cursor.fetchone()["id"])  # type: ignore[index]
                action = "external_user_created"
            else:
                user_id = str(existing["id"])
                cursor.execute(
                    "UPDATE users SET role_id = %s WHERE id = %s::uuid AND role_id <> %s",
                    (role["id"], user_id, role["id"]),
                )
                action = "external_user_refreshed"
            cursor.execute(
                "DELETE FROM user_attributes WHERE user_id = %s::uuid AND source = %s",
                (user_id, source),
            )
            for key, value in attributes.items():
                cursor.execute(
                    """
                    INSERT INTO user_attributes (user_id, key, value, source)
                    VALUES (%s::uuid, %s, %s, %s)
                    ON CONFLICT (user_id, key) DO NOTHING
                    """,
                    (user_id, key, Jsonb(value), source),
                )
            cursor.execute(
                """
                INSERT INTO audit_events (actor_id, action, resource_type, resource_id, details)
                VALUES (%s::uuid, %s, 'user', %s, %s)
                """,
                (user_id, action, name, Jsonb({"source": source, "role": role_name})),
            )
        return user_id

    @staticmethod
    def _validate_project_ids(cursor: psycopg.Cursor[Row], project_ids: list[str]) -> None:
        if len(set(project_ids)) != len(project_ids):
            raise ValueError("Project IDs must be unique")
        if not project_ids:
            return
        cursor.execute(
            "SELECT id FROM projects WHERE id = ANY(%s::uuid[])",
            (project_ids,),
        )
        found = {str(row["id"]) for row in cursor.fetchall()}
        if found != set(project_ids):
            raise KeyError("One or more projects do not exist")

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
        auth_source=str(row.get("auth_source", "local")),
    )
