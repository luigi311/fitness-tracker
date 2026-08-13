"""Add measurement columns introduced after the legacy schema."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_MEASUREMENT_COLUMNS = (
    ("running_metrics", "incline_percent"),
    ("cycling_metrics", "incline_percent"),
    ("running_metrics", "altitude_m"),
    ("cycling_metrics", "altitude_m"),
)


def upgrade() -> None:
    """Add optional grade and altitude values where they are absent."""
    connection = op.get_bind()
    tables = set(inspect(connection).get_table_names())
    for table, column in _MEASUREMENT_COLUMNS:
        if table in tables and column not in {
            item["name"] for item in inspect(connection).get_columns(table)
        }:
            op.add_column(table, sa.Column(column, sa.Float(), nullable=True))


def downgrade() -> None:
    """Remove the optional measurement columns."""
    connection = op.get_bind()
    tables = set(inspect(connection).get_table_names())
    for table, column in reversed(_MEASUREMENT_COLUMNS):
        if table not in tables or column not in {
            item["name"] for item in inspect(connection).get_columns(table)
        }:
            continue
        if connection.dialect.name == "sqlite":
            with op.batch_alter_table(table) as batch:
                batch.drop_column(column)
        else:
            op.drop_column(table, column)
