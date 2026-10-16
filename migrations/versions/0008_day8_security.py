"""Day 8: pin an OIDC account to the provider's stable subject id."""

from alembic import op

revision = "0008_day8"
down_revision = "0007_day7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN external_subject TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN external_subject")
