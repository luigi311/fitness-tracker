"""Distinguish measured GPS altitude from estimated Bluetooth altitude."""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Keep historical measurements unclassified while recording new sources."""
    for table in ("running_metrics", "cycling_metrics"):
        op.add_column(table, sa.Column("source", sa.String(length=16), nullable=True))


def downgrade() -> None:
    """Remove source provenance without changing measurement values."""
    for table in ("cycling_metrics", "running_metrics"):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("source")
