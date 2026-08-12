"""Add deterministic upload-state update timestamps."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add, backfill, and require activity upload update timestamps."""
    connection = op.get_bind()
    tables = set(inspect(connection).get_table_names())
    if "activity_uploads" not in tables:
        return

    columns = {item["name"] for item in inspect(connection).get_columns("activity_uploads")}
    if "updated_at" not in columns:
        op.add_column(
            "activity_uploads",
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )
    connection.execute(
        sa.text(
            "UPDATE activity_uploads "
            "SET updated_at = COALESCE(updated_at, uploaded_at, CURRENT_TIMESTAMP) "
            "WHERE updated_at IS NULL",
        ),
    )
    updated_at = next(
        item
        for item in inspect(connection).get_columns("activity_uploads")
        if item["name"] == "updated_at"
    )
    if not updated_at["nullable"]:
        return

    if connection.dialect.name == "sqlite":
        with op.batch_alter_table("activity_uploads") as batch:
            batch.alter_column(
                "updated_at",
                existing_type=sa.DateTime(timezone=True),
                nullable=False,
            )
    else:
        op.alter_column(
            "activity_uploads",
            "updated_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )


def downgrade() -> None:
    """Remove activity upload update timestamps."""
    connection = op.get_bind()
    if "activity_uploads" not in inspect(connection).get_table_names():
        return
    if "updated_at" not in {
        item["name"] for item in inspect(connection).get_columns("activity_uploads")
    }:
        return
    if connection.dialect.name == "sqlite":
        with op.batch_alter_table("activity_uploads") as batch:
            batch.drop_column("updated_at")
    else:
        op.drop_column("activity_uploads", "updated_at")
