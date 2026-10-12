"""Add Day 2 setup wizard and authentication fields."""

from alembic import op

revision = "0002_day2"
down_revision = "0001_day1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE projects
            ADD COLUMN connection_encrypted BYTEA,
            ADD COLUMN host TEXT,
            ADD COLUMN port INTEGER,
            ADD COLUMN database_name TEXT,
            ADD COLUMN ssl_enabled BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN setup_step TEXT NOT NULL DEFAULT 'connect',
            ADD COLUMN selected_tables JSONB NOT NULL DEFAULT '[]'::jsonb;

        ALTER TABLE users
            ADD COLUMN auth_source TEXT NOT NULL DEFAULT 'local'
                CHECK (auth_source IN ('local', 'ldap', 'oidc')),
            ADD COLUMN must_change_password BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN failed_login_count INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN failure_window_started_at TIMESTAMPTZ,
            ADD COLUMN locked_until TIMESTAMPTZ;

        ALTER TABLE sessions
            ADD COLUMN rotated_to UUID REFERENCES sessions(id);

        CREATE TABLE project_members (
            project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (project_id, user_id)
        );
        CREATE INDEX sessions_user_active_idx
            ON sessions (user_id, expires_at) WHERE revoked_at IS NULL;
        CREATE INDEX sessions_family_idx ON sessions (family_id);
        CREATE INDEX users_lockout_idx ON users (username, locked_until);

        INSERT INTO roles (id, name, description, is_system)
        VALUES ('00000000-0000-0000-0000-000000000001', 'DBA', 'Database administrator', TRUE)
        ON CONFLICT (name) DO NOTHING;

        INSERT INTO role_capabilities (role_id, capability)
        SELECT roles.id, capability
        FROM roles
        CROSS JOIN unnest(ARRAY[
            'ask_question', 'give_feedback', 'view_shared', 'view_all_threads',
            'rerun_sql', 'manage_connection', 'edit_mdl', 'deploy_mdl',
            'manage_knowledge', 'approve_pairs', 'manage_policies',
            'manage_profiling', 'create_dashboard', 'schedule_question',
            'share', 'run_eval', 'manage_users', 'manage_roles', 'view_audit'
        ]) AS capability
        WHERE roles.name = 'DBA'
        ON CONFLICT DO NOTHING;

        GRANT SELECT, INSERT, UPDATE, DELETE ON project_members TO text2sql_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        REVOKE ALL ON project_members FROM text2sql_app;
        DROP TABLE project_members;
        DROP INDEX users_lockout_idx;
        DROP INDEX sessions_family_idx;
        DROP INDEX sessions_user_active_idx;
        ALTER TABLE sessions DROP COLUMN rotated_to;
        ALTER TABLE users
            DROP COLUMN locked_until,
            DROP COLUMN failure_window_started_at,
            DROP COLUMN failed_login_count,
            DROP COLUMN must_change_password,
            DROP COLUMN auth_source;
        ALTER TABLE projects
            DROP COLUMN selected_tables,
            DROP COLUMN setup_step,
            DROP COLUMN ssl_enabled,
            DROP COLUMN database_name,
            DROP COLUMN port,
            DROP COLUMN host,
            DROP COLUMN connection_encrypted;
        DELETE FROM role_capabilities
        WHERE role_id = (SELECT id FROM roles WHERE name = 'DBA');
        DELETE FROM roles WHERE name = 'DBA' AND is_system;
        """
    )
