"""Cascade activity deletion to all measurement tables."""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op
from sqlalchemy import inspect

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_METRIC_TABLES = ("heart_rate", "running_metrics", "cycling_metrics")
_SQLITE_COLUMNS = {
    "heart_rate": (
        "id INTEGER NOT NULL PRIMARY KEY, activity_id INTEGER NOT NULL, "
        "timestamp_ms BIGINT NOT NULL, bpm INTEGER NOT NULL, rr_interval FLOAT, energy_kj FLOAT"
    ),
    "running_metrics": (
        "id INTEGER NOT NULL PRIMARY KEY, activity_id INTEGER NOT NULL, "
        "timestamp_ms BIGINT NOT NULL, speed_mps FLOAT NOT NULL, cadence_spm INTEGER NOT NULL, "
        "stride_length_m FLOAT, total_distance_m FLOAT, power_watts FLOAT, "
        "incline_percent FLOAT, altitude_m FLOAT"
    ),
    "cycling_metrics": (
        "id INTEGER NOT NULL PRIMARY KEY, activity_id INTEGER NOT NULL, "
        "timestamp_ms BIGINT NOT NULL, speed_mps FLOAT NOT NULL, cadence_rpm INTEGER, "
        "total_distance_m FLOAT, power_watts FLOAT, incline_percent FLOAT, altitude_m FLOAT"
    ),
}


def _foreign_key_name(connection: Connection, table: str) -> str | None:
    return next(
        (
            constraint.get("name")
            for constraint in inspect(connection).get_foreign_keys(table)
            if constraint.get("constrained_columns") == ["activity_id"]
        ),
        None,
    )


def _sqlite_rebuild(table: str, *, ondelete: str | None) -> None:
    connection = op.get_bind()
    old_table = f"{table}_0005_old"
    columns = [column["name"] for column in inspect(connection).get_columns(table)]
    index_statements = [
        statement
        for (statement,) in connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL",
            (table,),
        )
    ]
    column_list = ", ".join(columns)
    delete_clause = f" ON DELETE {ondelete}" if ondelete else ""

    connection.exec_driver_sql(f"ALTER TABLE {table} RENAME TO {old_table}")
    connection.exec_driver_sql(
        f"CREATE TABLE {table} ({_SQLITE_COLUMNS[table]}, "
        f"FOREIGN KEY(activity_id) REFERENCES activities(id){delete_clause})",
    )
    connection.exec_driver_sql(
        f"INSERT INTO {table} ({column_list}) SELECT {column_list} FROM {old_table}",  # noqa: S608 - identifiers come from fixed migration tables and reflected columns
    )
    connection.exec_driver_sql(f"DROP TABLE {old_table}")
    for statement in index_statements:
        connection.exec_driver_sql(statement)


def _replace_foreign_key(table: str, *, ondelete: str | None) -> None:
    connection = op.get_bind()
    if table not in inspect(connection).get_table_names():
        return
    if connection.dialect.name == "sqlite":
        _sqlite_rebuild(table, ondelete=ondelete)
        return

    foreign_key_name = _foreign_key_name(connection, table)
    if foreign_key_name is None:
        message = f"Could not identify {table}.activity_id foreign key"
        raise RuntimeError(message)
    op.drop_constraint(foreign_key_name, table, type_="foreignkey")
    op.create_foreign_key(
        foreign_key_name,
        table,
        "activities",
        ["activity_id"],
        ["id"],
        ondelete=ondelete,
    )


def upgrade() -> None:
    """Apply database-level cascade deletion to measurement rows."""
    for table in _METRIC_TABLES:
        _replace_foreign_key(table, ondelete="CASCADE")


def downgrade() -> None:
    """Restore measurement foreign keys without delete actions."""
    for table in reversed(_METRIC_TABLES):
        _replace_foreign_key(table, ondelete=None)
