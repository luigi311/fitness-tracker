"""Preparation and permission hardening for private SQLite files."""

import os
from pathlib import Path

from sqlalchemy import Engine

from fitness_tracker.core.file_permissions import secure_file


def sqlite_database_path(engine: Engine) -> Path | None:
    """Return the filesystem path for a file-backed SQLite engine."""
    if engine.url.get_backend_name() != "sqlite":
        return None
    database = engine.url.database
    if database is None or database == ":memory:" or database.startswith("file:"):
        return None
    return Path(database).expanduser().absolute()


def prepare_private_sqlite_database(database_path: Path) -> None:
    """Create a SQLite target with user-only permissions before connecting."""
    try:
        descriptor = os.open(
            database_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
    except FileExistsError:
        pass
    else:
        os.close(descriptor)
    secure_file(database_path)


def secure_sqlite_files(database_path: Path) -> None:
    """Restrict a SQLite database and its sidecars and migration backups."""
    paths = (
        database_path,
        database_path.with_name(f"{database_path.name}-wal"),
        database_path.with_name(f"{database_path.name}-shm"),
        *database_path.parent.glob(f"{database_path.name}.pre-*"),
    )
    for path in paths:
        secure_file(path)
