"""Create Day 1 metadata tables and append-only audit permissions."""

from alembic import op

revision = "0001_day1"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE projects (
            id UUID PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE mdl_versions (
            id UUID PRIMARY KEY,
            project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            version_number INTEGER NOT NULL CHECK (version_number > 0),
            status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'failed')),
            document JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (project_id, version_number)
        );
        CREATE TABLE roles (
            id UUID PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL DEFAULT '',
            is_system BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE users (
            id UUID PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT,
            role_id UUID NOT NULL REFERENCES roles(id),
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'disabled', 'locked')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE role_capabilities (
            role_id UUID NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
            capability TEXT NOT NULL,
            PRIMARY KEY (role_id, capability)
        );
        CREATE TABLE sessions (
            id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            refresh_token_hash TEXT NOT NULL UNIQUE,
            family_id UUID NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            revoked_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE audit_events (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            actor_id UUID,
            action TEXT NOT NULL,
            resource_type TEXT NOT NULL,
            resource_id TEXT NOT NULL,
            details JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        """
        GRANT USAGE ON SCHEMA public TO text2sql_app;
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON projects, mdl_versions, roles, users, role_capabilities, sessions
            TO text2sql_app;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO text2sql_app;
        REVOKE ALL ON audit_events FROM PUBLIC, text2sql_app;
        GRANT SELECT, INSERT ON audit_events TO text2sql_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        REVOKE ALL ON audit_events FROM text2sql_app;
        DROP TABLE audit_events;
        DROP TABLE sessions;
        DROP TABLE role_capabilities;
        DROP TABLE users;
        DROP TABLE roles;
        DROP TABLE mdl_versions;
        DROP TABLE projects;
        """
    )
