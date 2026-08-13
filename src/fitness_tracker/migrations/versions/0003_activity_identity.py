"""Replace start-time identity with a stable public activity UUID."""

from collections.abc import Mapping
from typing import Any
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.engine import Connection

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _start_time_constraint(connection: Connection) -> Mapping[str, Any] | None:
    return next(
        (
            constraint
            for constraint in inspect(connection).get_unique_constraints("activities")
            if constraint.get("column_names") == ["start_time"]
        ),
        None,
    )


def _sqlite_upgrade(connection: Connection, columns: set[str]) -> None:
    connection.execute(
        sa.text(
            "CREATE TABLE activities_e1 ("
            "id INTEGER NOT NULL PRIMARY KEY, "
            "public_id CHAR(32) NOT NULL, "
            "start_time DATETIME NOT NULL, "
            "end_time DATETIME, "
            "CONSTRAINT uq_activities_public_id UNIQUE (public_id)"
            ")",
        ),
    )
    if "public_id" in columns:
        insert_statement = (
            "INSERT INTO activities_e1 (id, public_id, start_time, end_time) "
            "SELECT id, COALESCE(public_id, lower(hex(randomblob(16)))), "
            "start_time, end_time FROM activities"
        )
    else:
        insert_statement = (
            "INSERT INTO activities_e1 (id, public_id, start_time, end_time) "
            "SELECT id, lower(hex(randomblob(16))), start_time, end_time FROM activities"
        )
    connection.execute(
        sa.text(insert_statement),
    )
    connection.execute(sa.text("DROP TABLE activities"))
    connection.execute(sa.text("ALTER TABLE activities_e1 RENAME TO activities"))


def _postgresql_upgrade(
    connection: Connection,
    columns: set[str],
    constraint: Mapping[str, Any] | None,
) -> None:
    if "public_id" not in columns:
        op.add_column("activities", sa.Column("public_id", sa.Uuid(), nullable=True))
        for (activity_id,) in connection.execute(sa.text("SELECT id FROM activities")):
            connection.execute(
                sa.text("UPDATE activities SET public_id = :public_id WHERE id = :id"),
                {"id": activity_id, "public_id": uuid4()},
            )
        op.alter_column("activities", "public_id", nullable=False)

    if constraint is not None:
        constraint_name = constraint.get("name")
        if constraint_name is None:
            message = "Cannot identify the legacy activities.start_time constraint"
            raise RuntimeError(message)
        op.drop_constraint(constraint_name, "activities", type_="unique")
    if "public_id" not in {
        item["column_names"][0]
        for item in inspect(connection).get_unique_constraints("activities")
        if item.get("column_names")
    }:
        op.create_unique_constraint("uq_activities_public_id", "activities", ["public_id"])


def upgrade() -> None:
    """Backfill UUID identity and remove the legacy start-time uniqueness."""
    connection = op.get_bind()
    if "activities" not in inspect(connection).get_table_names():
        return

    columns = {item["name"] for item in inspect(connection).get_columns("activities")}
    constraint = _start_time_constraint(connection)
    if "public_id" in columns and constraint is None:
        return

    if connection.dialect.name == "sqlite":
        _sqlite_upgrade(connection, columns)
    else:
        _postgresql_upgrade(connection, columns, constraint)


def downgrade() -> None:
    """Restore the legacy start-time uniqueness and remove public identity."""
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        message = "SQLite activity identity downgrade is not supported"
        raise NotImplementedError(message)

    inspector = inspect(connection)
    if "activities" not in inspector.get_table_names():
        return
    columns = {item["name"] for item in inspector.get_columns("activities")}
    if "public_id" in columns:
        op.drop_constraint("uq_activities_public_id", "activities", type_="unique")
        op.drop_column("activities", "public_id")
    op.create_unique_constraint("uq_activities_start_time", "activities", ["start_time"])
