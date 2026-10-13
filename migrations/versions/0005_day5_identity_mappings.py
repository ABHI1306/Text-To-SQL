"""Add LDAP/OIDC group-to-role mappings."""

from alembic import op

revision = "0005_day5"
down_revision = "0004_day4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE identity_mappings (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            source TEXT NOT NULL CHECK (source IN ('ldap', 'oidc')),
            match TEXT NOT NULL CHECK (length(btrim(match)) > 0),
            role_id UUID NOT NULL REFERENCES roles(id) ON DELETE RESTRICT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (source, match)
        );
        GRANT SELECT, INSERT, UPDATE, DELETE ON identity_mappings TO text2sql_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        REVOKE ALL ON identity_mappings FROM text2sql_app;
        DROP TABLE identity_mappings;
        """
    )
