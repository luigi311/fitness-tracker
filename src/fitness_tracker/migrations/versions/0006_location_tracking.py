"""Persist activity environments and accepted location points."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_DEFAULT_ENVIRONMENT = "indoor"
_LOCATION_INDEXES = (
    ("ix_location_activity_id", ("activity_id",)),
    ("ix_location_activity_time", ("activity_id", "timestamp_ms")),
)


def _make_environment_non_nullable() -> None:
    """Require the backfilled activity environment on both supported databases."""
    connection = op.get_bind()
    environment = next(
        column
        for column in inspect(connection).get_columns("activities")
        if column["name"] == "environment"
    )
    if environment["nullable"] is False:
        return
    if connection.dialect.name == "sqlite":
        with op.batch_alter_table("activities") as batch:
            batch.alter_column(
                "environment",
                existing_type=sa.String(length=16),
                nullable=False,
            )
    else:
        op.alter_column(
            "activities",
            "environment",
            existing_type=sa.String(length=16),
            nullable=False,
        )


def _drop_environment() -> None:
    """Remove the activity environment using SQLite-compatible alteration."""
    connection = op.get_bind()
    if "activities" not in inspect(connection).get_table_names():
        return
    if "environment" not in {
        column["name"] for column in inspect(connection).get_columns("activities")
    }:
        return
    if connection.dialect.name == "sqlite":
        with op.batch_alter_table("activities") as batch:
            batch.drop_column("environment")
    else:
        op.drop_column("activities", "environment")


def upgrade() -> None:
    """Add the required environment column and location-point series."""
    connection = op.get_bind()
    tables = set(inspect(connection).get_table_names())

    if "activities" in tables:
        activity_columns = {
            column["name"] for column in inspect(connection).get_columns("activities")
        }
        if "environment" not in activity_columns:
            op.add_column(
                "activities",
                sa.Column("environment", sa.String(length=16), nullable=True),
            )
        connection.execute(
            sa.text("UPDATE activities SET environment = :environment WHERE environment IS NULL"),
            {"environment": _DEFAULT_ENVIRONMENT},
        )
        _make_environment_non_nullable()

    if "location_points" not in tables:
        op.create_table(
            "location_points",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column(
                "activity_id",
                sa.Integer(),
                sa.ForeignKey("activities.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("timestamp_ms", sa.BigInteger(), nullable=False),
            sa.Column("latitude_deg", sa.Float(), nullable=False),
            sa.Column("longitude_deg", sa.Float(), nullable=False),
            sa.Column("accuracy_m", sa.Float()),
            sa.Column("altitude_m", sa.Float()),
            sa.Column("speed_mps", sa.Float()),
            sa.Column("heading_deg", sa.Float()),
            sa.Column("source_time_utc", sa.DateTime(timezone=True)),
            sa.PrimaryKeyConstraint("id"),
        )

    existing_indexes = {
        index["name"] for index in inspect(connection).get_indexes("location_points")
    }
    for name, columns in _LOCATION_INDEXES:
        if name not in existing_indexes:
            op.create_index(name, "location_points", columns)


def downgrade() -> None:
    """Drop location points and the persisted activity environment."""
    connection = op.get_bind()
    if "location_points" in inspect(connection).get_table_names():
        existing_indexes = {
            index["name"] for index in inspect(connection).get_indexes("location_points")
        }
        for name, _columns in reversed(_LOCATION_INDEXES):
            if name in existing_indexes:
                op.drop_index(name, table_name="location_points")
        op.drop_table("location_points")
    _drop_environment()
