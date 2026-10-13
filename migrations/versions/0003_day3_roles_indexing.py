"""Add Day 3 deployment, project-role access and user attributes."""

from alembic import op

revision = "0003_day3"
down_revision = "0002_day2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE projects
            ADD COLUMN is_sample BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE mdl_versions
            ADD COLUMN deployed_at TIMESTAMPTZ,
            ADD COLUMN deployed_by UUID REFERENCES users(id);
        CREATE UNIQUE INDEX mdl_versions_one_active_idx
            ON mdl_versions (project_id) WHERE status = 'active';

        CREATE TABLE project_role_members (
            project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            role_id UUID NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (project_id, role_id)
        );
        CREATE TABLE user_attributes (
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            key TEXT NOT NULL CHECK (length(key) BETWEEN 1 AND 128),
            value JSONB NOT NULL,
            source TEXT NOT NULL DEFAULT 'manual',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, key)
        );
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON project_role_members, user_attributes TO text2sql_app;

        INSERT INTO roles (id, name, description, is_system)
        VALUES ('00000000-0000-0000-0000-000000000002', 'User', 'Standard user', TRUE)
        ON CONFLICT (name) DO NOTHING;
        INSERT INTO role_capabilities (role_id, capability)
        SELECT roles.id, capability
        FROM roles
        CROSS JOIN unnest(ARRAY['ask_question', 'give_feedback', 'view_shared']) AS capability
        WHERE roles.name = 'User'
        ON CONFLICT DO NOTHING;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        REVOKE ALL ON project_role_members, user_attributes FROM text2sql_app;
        DROP TABLE user_attributes;
        DROP TABLE project_role_members;
        DROP INDEX mdl_versions_one_active_idx;
        ALTER TABLE mdl_versions DROP COLUMN deployed_by, DROP COLUMN deployed_at;
        ALTER TABLE projects DROP COLUMN is_sample;
        DELETE FROM role_capabilities
        WHERE role_id = (SELECT id FROM roles WHERE name = 'User');
        DELETE FROM roles WHERE name = 'User' AND is_system;
        """
    )
