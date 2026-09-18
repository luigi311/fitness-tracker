"""Allow independent GPS and Bluetooth metric readings."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Preserve unknown metrics as NULL rather than fabricated zero readings."""
    with op.batch_alter_table("running_metrics") as batch:
        batch.alter_column("speed_mps", existing_type=sa.Float(), nullable=True)
        batch.alter_column("cadence_spm", existing_type=sa.Integer(), nullable=True)
    with op.batch_alter_table("cycling_metrics") as batch:
        batch.alter_column("speed_mps", existing_type=sa.Float(), nullable=True)


def downgrade() -> None:
    """Restore the old required measurements, filling missing values with zero."""
    connection = op.get_bind()
    connection.execute(sa.text("UPDATE running_metrics SET speed_mps = 0 WHERE speed_mps IS NULL"))
    connection.execute(
        sa.text("UPDATE running_metrics SET cadence_spm = 0 WHERE cadence_spm IS NULL"),
    )
    connection.execute(sa.text("UPDATE cycling_metrics SET speed_mps = 0 WHERE speed_mps IS NULL"))
    with op.batch_alter_table("running_metrics") as batch:
        batch.alter_column("speed_mps", existing_type=sa.Float(), nullable=False)
        batch.alter_column("cadence_spm", existing_type=sa.Integer(), nullable=False)
    with op.batch_alter_table("cycling_metrics") as batch:
        batch.alter_column("speed_mps", existing_type=sa.Float(), nullable=False)
