"""Bootstrap the legacy table set for fresh and pre-Alembic databases.

This first revision is intentionally idempotent: databases created by older
releases may already contain any subset of these tables, so the upgrade checks
the live catalog and creates only missing tables. It is a compatibility bridge
to the numbered Alembic revisions, not a strictly reversible snapshot of one
historical schema; downgrades through this bootstrap are therefore not a
supported recovery path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create missing legacy tables without consulting current ORM metadata."""
    connection = op.get_bind()
    tables = set(inspect(connection).get_table_names())

    if "activities" not in tables:
        op.create_table(
            "activities",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("end_time", sa.DateTime(timezone=True)),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("start_time", name="uq_activities_start_time"),
        )
        tables.add("activities")
    if "activity_sport" not in tables:
        op.create_table(
            "activity_sport",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column(
                "activity_id",
                sa.Integer(),
                sa.ForeignKey("activities.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("sport_type_id", sa.Integer(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("activity_id", name="uq_activity_sport_activity_id"),
        )
        tables.add("activity_sport")
    if "heart_rate" not in tables:
        op.create_table(
            "heart_rate",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("activity_id", sa.Integer(), nullable=False),
            sa.Column("timestamp_ms", sa.BigInteger(), nullable=False),
            sa.Column("bpm", sa.Integer(), nullable=False),
            sa.Column("rr_interval", sa.Float()),
            sa.ForeignKeyConstraint(["activity_id"], ["activities.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        tables.add("heart_rate")
    if "running_metrics" not in tables:
        op.create_table(
            "running_metrics",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("activity_id", sa.Integer(), nullable=False),
            sa.Column("timestamp_ms", sa.BigInteger(), nullable=False),
            sa.Column("speed_mps", sa.Float(), nullable=False),
            sa.Column("cadence_spm", sa.Integer(), nullable=False),
            sa.Column("stride_length_m", sa.Float()),
            sa.Column("total_distance_m", sa.Float()),
            sa.Column("power_watts", sa.Float()),
            sa.ForeignKeyConstraint(["activity_id"], ["activities.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        tables.add("running_metrics")
    if "cycling_metrics" not in tables:
        op.create_table(
            "cycling_metrics",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("activity_id", sa.Integer(), nullable=False),
            sa.Column("timestamp_ms", sa.BigInteger(), nullable=False),
            sa.Column("speed_mps", sa.Float(), nullable=False),
            sa.Column("cadence_rpm", sa.Integer()),
            sa.Column("total_distance_m", sa.Float()),
            sa.Column("power_watts", sa.Float()),
            sa.ForeignKeyConstraint(["activity_id"], ["activities.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        tables.add("cycling_metrics")
    if "activity_uploads" not in tables:
        op.create_table(
            "activity_uploads",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column(
                "activity_id",
                sa.Integer(),
                sa.ForeignKey("activities.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("provider", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("uploaded_at", sa.DateTime(timezone=True)),
            sa.Column("provider_activity_id", sa.String(length=128)),
            sa.Column("payload_hash", sa.String(length=64)),
            sa.Column("last_error", sa.Text()),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("activity_id", "provider", name="uq_activity_provider"),
        )
        tables.add("activity_uploads")
    if "activity_stats" not in tables:
        op.create_table(
            "activity_stats",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column(
                "activity_id",
                sa.Integer(),
                sa.ForeignKey("activities.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("sport_type_id", sa.Integer(), nullable=False),
            sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("end_time", sa.DateTime(timezone=True)),
            sa.Column("duration_s", sa.Integer(), nullable=False),
            sa.Column("distance_m", sa.Float()),
            sa.Column("avg_speed_mps", sa.Float()),
            sa.Column("avg_bpm", sa.Float()),
            sa.Column("max_bpm", sa.Integer()),
            sa.Column("total_energy_kj", sa.Float(), nullable=False),
            sa.Column("avg_cadence", sa.Float()),
            sa.Column("avg_power_watts", sa.Float()),
            sa.Column("total_ascent_m", sa.Float()),
            sa.Column("total_descent_m", sa.Float()),
            sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("activity_id", name="uq_activity_stats_activity_id"),
        )
        tables.add("activity_stats")

    _create_indexes(connection)


def downgrade() -> None:
    """Drop the fixed baseline table set."""
    connection = op.get_bind()
    for table in reversed(
        (
            "activity_stats",
            "activity_uploads",
            "cycling_metrics",
            "running_metrics",
            "heart_rate",
            "activity_sport",
            "activities",
        ),
    ):
        if table in inspect(connection).get_table_names():
            op.drop_table(table)


def _create_indexes(connection: Connection) -> None:
    indexes = {
        index["name"]
        for table in inspect(connection).get_table_names()
        for index in inspect(connection).get_indexes(table)
    }
    definitions = (
        ("ix_hr_activity_id", "heart_rate", ("activity_id",)),
        ("ix_hr_activity_time", "heart_rate", ("activity_id", "timestamp_ms")),
        ("ix_run_activity_id", "running_metrics", ("activity_id",)),
        ("ix_run_activity_time", "running_metrics", ("activity_id", "timestamp_ms")),
        ("ix_cyc_activity_id", "cycling_metrics", ("activity_id",)),
        ("ix_cyc_activity_time", "cycling_metrics", ("activity_id", "timestamp_ms")),
        ("ix_upload_provider_status", "activity_uploads", ("provider", "status")),
        ("ix_upload_activity", "activity_uploads", ("activity_id",)),
        ("ix_stats_activity_id", "activity_stats", ("activity_id",)),
        ("ix_stats_start_time", "activity_stats", ("start_time",)),
        ("ix_stats_sport", "activity_stats", ("sport_type_id",)),
    )
    tables = set(inspect(connection).get_table_names())
    for name, table, columns in definitions:
        if table in tables and name not in indexes:
            op.create_index(name, table, columns)
