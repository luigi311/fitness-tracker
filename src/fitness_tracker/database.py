import contextlib
import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from functools import cache, partial
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from bleaksport.models import CyclingSample, RunningSample, TrainerSample
from loguru import logger
from sqlalchemy import (
    Engine,
    create_engine,
    event,
    exc,
    inspect,
    select,
)
from sqlalchemy.engine import Connection
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy.orm import Session, sessionmaker

from fitness_tracker.activity_stats import StatsCalculator
from fitness_tracker.core.errors import (
    DatabaseConnectionError as DatabaseConnectionError,  # noqa: PLC0414
)
from fitness_tracker.core.errors import DatabaseMigrationError
from fitness_tracker.core.file_permissions import secure_file
from fitness_tracker.core.sports import SportTypesEnum
from fitness_tracker.data.models import (
    Activity,
    ActivitySport,
    ActivityUpload,
    CyclingMetrics,
    HeartRate,
    RunningMetrics,
)
from fitness_tracker.data.repositories import ActivityRepository, SqlAlchemyActivityRepository
from fitness_tracker.data.sqlite_files import (
    prepare_private_sqlite_database,
    secure_sqlite_files,
    sqlite_database_path,
)
from fitness_tracker.data.sync import DatabaseSynchronizer

MIGRATION_ATTEMPT_RETENTION = 3
_REQUIRED_COLUMNS = (
    ("running_metrics", "incline_percent"),
    ("cycling_metrics", "incline_percent"),
    ("running_metrics", "altitude_m"),
    ("cycling_metrics", "altitude_m"),
    ("activity_uploads", "updated_at"),
)


def _build_alembic_config() -> Config:
    config = Config()
    config.set_main_option("script_location", "fitness_tracker:migrations")
    return config


@cache
def _alembic_head_revision() -> str:
    revision = ScriptDirectory.from_config(_build_alembic_config()).get_current_head()
    if revision is None:
        message = "Alembic migration history has no head revision"
        raise RuntimeError(message)
    return revision


def _sqlite_pragmas(dbapi_con: sqlite3.Connection, _con_record: object) -> None:
    cur = dbapi_con.cursor()
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("PRAGMA synchronous=NORMAL;")
    cur.execute("PRAGMA foreign_keys=ON;")
    cur.close()


class DatabaseManager:
    """Coordinate database migrations, activity persistence, and synchronization."""

    BATCH_SIZE = 25

    def __init__(self, database_url: str) -> None:
        connect_args: dict[str, Any] = {}
        if database_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False

        self.engine = create_engine(
            database_url,
            echo=False,
            future=True,
            connect_args=connect_args,
            pool_pre_ping=True,
        )
        sqlite_database = sqlite_database_path(self.engine)
        if database_url.startswith("sqlite"):
            event.listen(self.engine, "connect", _sqlite_pragmas)

        self._initialize_schema(sqlite_database)
        self.Session: sessionmaker[Session] = sessionmaker(
            bind=self.engine,
            future=True,
            expire_on_commit=False,
        )
        self.repository: ActivityRepository = SqlAlchemyActivityRepository(self.Session)
        self.stat_calc = StatsCalculator(self)

        # staging area for batching
        self._pending_lock = Lock()
        self._pending_hr: list[HeartRate] = []
        self._pending_run: list[RunningMetrics] = []
        self._pending_cyc: list[CyclingMetrics] = []

    def _initialize_schema(self, sqlite_database: Path | None) -> None:
        """Prepare the database file, migrate its schema, and secure artifacts."""
        migration_backup: Path | None = None
        migration_attempt_backup: Path | None = None
        try:
            if sqlite_database is not None:
                self._prepare_sqlite_database(sqlite_database)
            if self._schema_needs_migration(self.engine):
                migration_backup = self._backup_before_migration(self.engine)
                migration_attempt_backup = self._backup_for_migration_attempt(self.engine)
            self._migrate(self.engine)
            self._verify_schema(self.engine)
            self._remove_successful_migration_attempt(migration_attempt_backup)
            if sqlite_database is not None:
                secure_sqlite_files(sqlite_database)
        except DatabaseMigrationError:
            raise
        except Exception as error:
            recovery_backup = migration_attempt_backup or migration_backup
            raise self._migration_error(recovery_backup) from error

    def _prepare_sqlite_database(self, database_path: Path) -> None:
        """Create and secure a SQLite database before schema inspection."""
        try:
            prepare_private_sqlite_database(database_path)
            secure_sqlite_files(database_path)
        except OSError as error:
            message = "Database file could not be prepared securely"
            raise DatabaseMigrationError(message) from error

    @staticmethod
    def _remove_successful_migration_attempt(attempt_path: Path | None) -> None:
        """Remove a migration-attempt snapshot after schema verification succeeds."""
        if attempt_path is None:
            return
        try:
            secure_file(attempt_path)
            attempt_path.unlink()
        except OSError as error:
            logger.warning(
                "Could not remove successful migration attempt snapshot {}: {}",
                attempt_path,
                error,
            )

    @staticmethod
    def _migration_error(backup_path: Path | None) -> DatabaseMigrationError:
        """Build the user-facing error for an unsuccessful schema initialization."""
        if backup_path is not None:
            message = (
                f"Database migration failed; restore the pre-migration backup at {backup_path}"
            )
        else:
            message = "Database migration failed; no pre-migration backup is available"
        return DatabaseMigrationError(message)

    def start_activity(self, sport_type: SportTypesEnum) -> int:
        """Create an activity for ``sport_type`` and return its database ID."""
        with self.Session() as session:
            # store UTC with tzinfo
            act = Activity(start_time=datetime.now(tz=ZoneInfo("UTC")))
            session.add(act)
            session.flush()  # get act.id populated

            # link activity to sport type
            sport_activity = ActivitySport(activity_id=act.id, sport_type_id=sport_type.value)
            session.add(sport_activity)
            session.commit()

            return int(act.id)

    def stop_activity(self, activity_id: int) -> None:
        """Set an activity's end time and invalidate stale successful uploads."""
        # flush any leftover heart rates before closing
        self._flush_pending()

        with self.Session() as session:
            act = session.get(Activity, activity_id)
            if act is None:
                logger.warning(f"stop_activity: activity {activity_id} not found")
                return
            if act.end_time is None:
                act.end_time = datetime.now(tz=ZoneInfo("UTC"))
                self._invalidate_successful_uploads(session, {activity_id})
            session.commit()

    def finalize_activity(self, activity_id: int) -> None:
        """Close an activity and calculate its derived statistics."""
        self.stop_activity(activity_id)
        self.stat_calc.compute_for_activity(activity_id)

    def insert_heart_rate(
        self,
        activity_id: int,
        timestamp_ms: int,
        bpm: int,
        rr: float | None,
    ) -> None:
        """Queue a heart-rate sample for batched persistence."""
        # collect into pending list
        hr = HeartRate(
            activity_id=activity_id,
            timestamp_ms=timestamp_ms,
            bpm=bpm,
            rr_interval=rr,
        )
        with self._pending_lock:
            self._pending_hr.append(hr)

            # flush in batches
            if len(self._pending_hr) >= self.BATCH_SIZE:
                self._flush_pending_locked()

    def insert_running_metrics(
        self,
        activity_id: int,
        sample: RunningSample | TrainerSample,
        incline_percent: float | None,
    ) -> None:
        """Queue a running or trainer sample for batched persistence."""
        rm = RunningMetrics(
            activity_id=activity_id,
            timestamp_ms=sample.timestamp_ms,
            speed_mps=sample.speed_mps,
            cadence_spm=sample.cadence_spm,
            stride_length_m=sample.stride_length_m if isinstance(sample, RunningSample) else None,
            total_distance_m=sample.distance_m,
            power_watts=sample.power_watts,
            incline_percent=incline_percent,
            altitude_m=sample.altitude_m,
        )
        with self._pending_lock:
            self._pending_run.append(rm)
            if len(self._pending_run) >= self.BATCH_SIZE:
                self._flush_pending_locked()

    def insert_cycling_metrics(
        self,
        activity_id: int,
        sample: CyclingSample | TrainerSample,
        incline_percent: float | None,
    ) -> None:
        """Queue a cycling or trainer sample for batched persistence."""
        cm = CyclingMetrics(
            activity_id=activity_id,
            timestamp_ms=sample.timestamp_ms,
            speed_mps=sample.speed_mps,
            cadence_rpm=sample.cadence_rpm,
            total_distance_m=sample.distance_m,
            power_watts=sample.power_watts,
            incline_percent=incline_percent,
            altitude_m=sample.altitude_m,
        )
        with self._pending_lock:
            self._pending_cyc.append(cm)
            if len(self._pending_cyc) >= self.BATCH_SIZE:
                self._flush_pending_locked()

    def _flush_pending(self) -> None:
        with self._pending_lock:
            self._flush_pending_locked()

    def _flush_pending_locked(self) -> None:
        activity_ids = {
            item.activity_id
            for pending_items in (self._pending_hr, self._pending_run, self._pending_cyc)
            for item in pending_items
        }
        with self.Session() as session:
            if self._pending_hr:
                session.add_all(self._pending_hr)
            if self._pending_run:
                session.add_all(self._pending_run)
            if self._pending_cyc:
                session.add_all(self._pending_cyc)
            self._invalidate_successful_uploads(session, activity_ids)
            session.commit()

        self._pending_hr.clear()
        self._pending_run.clear()
        self._pending_cyc.clear()

    @staticmethod
    def _invalidate_successful_uploads(session: Session, activity_ids: set[int]) -> None:
        """Mark successful uploads stale when their activity payload changes."""
        if not activity_ids:
            return
        uploads = session.scalars(
            select(ActivityUpload).where(
                ActivityUpload.activity_id.in_(activity_ids),
                ActivityUpload.status == "ok",
            ),
        ).all()
        now = datetime.now(UTC)
        for upload in uploads:
            upload.status = "pending"
            upload.uploaded_at = None
            upload.updated_at = now
            upload.payload_hash = None
            upload.last_error = None

    @staticmethod
    def _alembic_revision(connection: Connection) -> str | None:
        heads = MigrationContext.configure(connection).get_current_heads()
        if len(heads) > 1:
            message = f"Database has multiple Alembic heads: {sorted(heads)}"
            raise RuntimeError(message)
        return heads[0] if heads else None

    def _migrate(self, engine: Engine) -> None:
        """Apply the checked-in Alembic revisions in one database transaction."""
        with engine.connect() as connection:
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
                connection.commit()
            transaction = connection.begin()
            try:
                config = _build_alembic_config()
                config.attributes["connection"] = connection
                command.upgrade(config, _alembic_head_revision())
                transaction.commit()
            except Exception:
                transaction.rollback()
                raise
            finally:
                if connection.dialect.name == "sqlite":
                    connection.exec_driver_sql("PRAGMA foreign_keys=ON")

    @staticmethod
    def _identity_schema_needs_migration(inspector: Inspector) -> bool:
        if "activities" not in inspector.get_table_names():
            return False
        columns = {column["name"] for column in inspector.get_columns("activities")}
        if "public_id" not in columns:
            return True
        return any(
            constraint.get("column_names") == ["start_time"]
            for constraint in inspector.get_unique_constraints("activities")
        )

    @staticmethod
    def _schema_invariant_errors(inspector: Inspector) -> list[str]:
        """Return required schema invariants that the database does not satisfy."""
        tables = set(inspector.get_table_names())
        missing = [
            f"{table}.{column}"
            for table, column in _REQUIRED_COLUMNS
            if table not in tables
            or column not in {item["name"] for item in inspector.get_columns(table)}
        ]
        if DatabaseManager._identity_schema_needs_migration(inspector):
            missing.append("activities.public_id/unique identity")
        if "activity_uploads" in tables:
            upload_columns = {
                item["name"]: item for item in inspector.get_columns("activity_uploads")
            }
            updated_at = upload_columns.get("updated_at")
            if updated_at is not None and updated_at["nullable"] is not False:
                missing.append("activity_uploads.updated_at NOT NULL")
        return missing

    @staticmethod
    def _schema_needs_migration(engine: Engine) -> bool:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        if not tables:
            return False

        with engine.connect() as connection:
            alembic_revision = DatabaseManager._alembic_revision(connection)

        if alembic_revision != _alembic_head_revision():
            return True

        return bool(DatabaseManager._schema_invariant_errors(inspector))

    @staticmethod
    def _migration_backup_paths(engine: Engine) -> tuple[Path, Path]:
        """Return the SQLite database and immutable target-revision backup paths."""
        if engine.url.get_backend_name() != "sqlite":
            message = (
                "Automatic database migration backups are only supported for SQLite; "
                "back up the database before upgrading"
            )
            raise RuntimeError(message)

        database_path = sqlite_database_path(engine)
        if database_path is None:
            message = "A file-backed SQLite database is required for migration backup"
            raise RuntimeError(message)
        secure_file(database_path)
        if not database_path.is_file():
            message = f"SQLite database file does not exist: {database_path}"
            raise RuntimeError(message)

        backup_path = database_path.with_name(
            f"{database_path.name}.pre-{_alembic_head_revision()}",
        )
        return database_path, backup_path

    @staticmethod
    def _copy_sqlite_backup(database_path: Path, backup_path: Path) -> Path:
        """Copy a SQLite database to a new immutable backup path."""
        if backup_path.exists():
            if not backup_path.is_file():
                message = f"SQLite migration backup path is not a file: {backup_path}"
                raise RuntimeError(message)
            secure_file(backup_path)
            return backup_path

        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{backup_path.name}.",
            suffix=".tmp",
            dir=database_path.parent,
        )
        os.close(fd)
        temporary_path = Path(temporary_name)
        source: sqlite3.Connection | None = None
        destination: sqlite3.Connection | None = None
        try:
            source = sqlite3.connect(database_path)
            destination = sqlite3.connect(temporary_path)
            source.backup(destination)
            destination.commit()
            destination.close()
            destination = None
            temporary_path.replace(backup_path)
            secure_file(backup_path)
            return backup_path
        finally:
            if source is not None:
                source.close()
            if destination is not None:
                destination.close()
            if temporary_path.exists():
                with contextlib.suppress(OSError):
                    temporary_path.unlink()

    @staticmethod
    def _backup_before_migration(engine: Engine) -> Path:
        """Create or reuse an immutable SQLite backup from before migration."""
        database_path, backup_path = DatabaseManager._migration_backup_paths(engine)
        return DatabaseManager._copy_sqlite_backup(database_path, backup_path)

    @staticmethod
    def _backup_for_migration_attempt(engine: Engine) -> Path:
        """Create a fresh, timestamped SQLite snapshot for this migration attempt."""
        database_path, immutable_path = DatabaseManager._migration_backup_paths(engine)
        if not immutable_path.is_file():
            DatabaseManager._backup_before_migration(engine)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        attempt_path = database_path.with_name(
            f"{immutable_path.name}.attempt-{timestamp}-{uuid4().hex}",
        )
        attempt_path = DatabaseManager._copy_sqlite_backup(database_path, attempt_path)
        DatabaseManager._prune_migration_attempts(database_path, immutable_path)
        return attempt_path

    @staticmethod
    def _prune_migration_attempts(database_path: Path, immutable_path: Path) -> None:
        """Keep a bounded number of recent migration-attempt snapshots."""
        attempt_paths = sorted(
            database_path.parent.glob(f"{immutable_path.name}.attempt-*"),
        )
        for path in attempt_paths[:-MIGRATION_ATTEMPT_RETENTION]:
            if path.is_symlink() or not path.is_file():
                message = f"SQLite migration attempt path is not a regular file: {path}"
                raise RuntimeError(message)
            secure_file(path)
            path.unlink()

    @staticmethod
    def _verify_schema(engine: Engine) -> None:
        inspector = inspect(engine)
        missing = DatabaseManager._schema_invariant_errors(inspector)
        if missing:
            message = f"Database migration left required columns missing: {missing}"
            raise RuntimeError(message)

        with engine.connect() as conn:
            alembic_revision = DatabaseManager._alembic_revision(conn)
        head_revision = _alembic_head_revision()
        if alembic_revision != head_revision:
            message = (
                f"Database migration ended at Alembic revision {alembic_revision}; "
                f"expected {head_revision}"
            )
            raise RuntimeError(message)

    def _prepare_remote_database(self, engine: Engine) -> None:
        """Migrate and verify a remote database before synchronization."""
        try:
            needs_migration = self._schema_needs_migration(engine)
        except exc.SQLAlchemyError as error:
            message = "Could not inspect the remote database schema before synchronization"
            raise DatabaseMigrationError(message) from error
        except RuntimeError as error:
            message = f"Remote database schema cannot be used: {error}"
            raise DatabaseMigrationError(message) from error

        backup_path: Path | None = None
        if needs_migration:
            if engine.url.get_backend_name() != "sqlite":
                message = (
                    "Remote database migration is required, but automatic migration is "
                    "supported only for SQLite. Take a verified PostgreSQL backup and "
                    "migrate its schema before retrying synchronization."
                )
                raise DatabaseMigrationError(message)
            try:
                backup_path = self._backup_before_migration(engine)
            except Exception as error:
                message = (
                    "Remote SQLite migration requires a pre-migration backup; "
                    "no remote changes were made"
                )
                raise DatabaseMigrationError(message) from error

        try:
            self._migrate(engine)
            self._verify_schema(engine)
        except Exception as error:
            if backup_path is not None:
                message = (
                    "Remote database migration failed; restore the pre-migration backup at "
                    f"{backup_path}"
                )
            else:
                message = (
                    "Remote database schema preparation failed; synchronization was not performed"
                )
            raise DatabaseMigrationError(message) from error

    def sync_to_database(self, database_dsn: str) -> None:
        """Synchronize activity aggregates in both directions without deletions."""
        self._flush_pending()
        DatabaseSynchronizer(
            prepare_remote_database=self._prepare_remote_database,
            local_session_factory=self.Session,
            sync_direction=partial(
                DatabaseSynchronizer.reconcile_sessions,
                rebuild_stats=StatsCalculator.rebuild_in_session,
            ),
            engine_factory=create_engine,
        ).sync(database_dsn)
