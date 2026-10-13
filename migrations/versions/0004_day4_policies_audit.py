"""Add Day 4 row policies and audit browser indexes."""

from alembic import op

revision = "0004_day4"
down_revision = "0003_day3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE row_policies (
            id UUID PRIMARY KEY,
            project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            model TEXT NOT NULL,
            name TEXT NOT NULL CHECK (name ~ '^[a-z][a-z0-9_]*$'),
            predicate TEXT NOT NULL CHECK (length(btrim(predicate)) > 0),
            applies_to JSONB NOT NULL DEFAULT '{}'::jsonb,
            on_missing TEXT NOT NULL DEFAULT 'deny' CHECK (on_missing IN ('deny')),
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            updated_by UUID REFERENCES users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (project_id, name)
        );
        CREATE INDEX row_policies_project_model_idx ON row_policies (project_id, model);
        GRANT SELECT, INSERT, UPDATE, DELETE ON row_policies TO text2sql_app;

        ALTER TABLE audit_events ADD COLUMN ip TEXT;
        CREATE INDEX audit_events_created_idx ON audit_events (created_at DESC, id DESC);
        CREATE INDEX audit_events_actor_idx ON audit_events (actor_id, id DESC);
        CREATE INDEX audit_events_action_idx ON audit_events (action, id DESC);
        CREATE INDEX audit_events_resource_idx
            ON audit_events (resource_type, resource_id, id DESC);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX audit_events_resource_idx;
        DROP INDEX audit_events_action_idx;
        DROP INDEX audit_events_actor_idx;
        DROP INDEX audit_events_created_idx;
        ALTER TABLE audit_events DROP COLUMN ip;
        REVOKE ALL ON row_policies FROM text2sql_app;
        DROP TABLE row_policies;
        """
    )
