"""Bidirectional synchronization for activity databases."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from loguru import logger
from sqlalchemy import Engine, exc, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from fitness_tracker.core.errors import DatabaseConnectionError
from fitness_tracker.data.models import (
    Activity,
    ActivitySport,
    ActivityUpload,
    CyclingMetrics,
    HeartRate,
    RunningMetrics,
)
from fitness_tracker.data.sqlite_files import (
    prepare_private_sqlite_database,
    secure_sqlite_files,
    sqlite_database_path,
)

if TYPE_CHECKING:
    from collections.abc import Callable

DATABASE_MODEL = type[HeartRate] | type[RunningMetrics] | type[CyclingMetrics]
_UPLOAD_STATUS_PRIORITY = {
    "failed": 0,
    "pending": 1,
    "ok": 2,
}
_MIN_UPLOAD_TIME = datetime.min.replace(tzinfo=UTC)


def _as_utc(value: datetime) -> datetime:
    """Return a datetime with an explicit UTC timezone without changing its instant."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class DatabaseSynchronizer:
    """Prepare a remote database and reconcile activity aggregates both ways."""

    def __init__(
        self,
        *,
        prepare_remote_database: Callable[[Engine], None],
        local_session_factory: sessionmaker[Session],
        sync_direction: Callable[[Session, Session], None],
        engine_factory: Callable[[str], Engine],
    ) -> None:
        self.prepare_remote_database = prepare_remote_database
        self.local_session_factory = local_session_factory
        self.sync_direction = sync_direction
        self.engine_factory = engine_factory

    def sync(self, database_dsn: str) -> None:
        """Synchronize activity aggregates in both directions without deletions."""
        remote_engine: Engine | None = None
        remote_sqlite_path = None
        synchronization_completed = False
        try:
            try:
                remote_engine = self.engine_factory(database_dsn)
                remote_sqlite_path = sqlite_database_path(remote_engine)
                if remote_sqlite_path is not None:
                    prepare_private_sqlite_database(remote_sqlite_path)
                with remote_engine.connect() as _:
                    pass
            except OSError as error:
                message = f"❌  Could not prepare remote SQLite database: {error}"
                raise DatabaseConnectionError(message) from error
            except exc.SQLAlchemyError as error:
                message = f"❌  Could not connect to remote database: {error}"
                raise DatabaseConnectionError(message) from error

            try:
                self.prepare_remote_database(remote_engine)
                if remote_sqlite_path is not None:
                    secure_sqlite_files(remote_sqlite_path)
                remote_session_factory: sessionmaker[Session] = sessionmaker(
                    bind=remote_engine,
                    expire_on_commit=False,
                )

                with self.local_session_factory() as local, remote_session_factory() as remote:
                    self.sync_direction(local, remote)
                    remote.commit()

                    self.sync_direction(remote, local)
                    local.commit()
            except OSError as error:
                message = "❌  Could not secure remote SQLite database artifacts"
                raise DatabaseConnectionError(message) from error
            synchronization_completed = True
        finally:
            try:
                if remote_sqlite_path is not None:
                    try:
                        secure_sqlite_files(remote_sqlite_path)
                    except OSError as error:
                        if synchronization_completed:
                            message = "❌  Could not secure remote SQLite database artifacts"
                            raise DatabaseConnectionError(message) from error
                        logger.warning(
                            "Could not harden remote SQLite artifacts after an earlier "
                            "synchronization error: {}",
                            error,
                        )
            finally:
                if remote_engine is not None:
                    remote_engine.dispose()

    @staticmethod
    def reconcile_sessions(
        source: Session,
        destination: Session,
        rebuild_stats: Callable[[Session, int], object],
    ) -> None:
        """Reconcile every source aggregate into the destination session."""
        destination_activities = {
            activity.public_id: activity for activity in destination.scalars(select(Activity)).all()
        }
        source_activities = source.scalars(
            select(Activity).order_by(Activity.start_time, Activity.id),
        ).all()
        for source_activity in source_activities:
            destination_activity = destination_activities.get(source_activity.public_id)
            if destination_activity is None:
                destination_activity = Activity(
                    public_id=source_activity.public_id,
                    start_time=source_activity.start_time,
                    end_time=source_activity.end_time,
                )
                destination.add(destination_activity)
                destination.flush()
                destination_activities[source_activity.public_id] = destination_activity
            else:
                DatabaseSynchronizer._reconcile_activity(
                    source_activity,
                    destination_activity,
                )

            copied_metrics = False
            for model in (HeartRate, RunningMetrics, CyclingMetrics):
                copied_metrics |= DatabaseSynchronizer._copy_metric_rows(
                    source,
                    destination,
                    source_activity,
                    destination_activity,
                    model,
                )
            DatabaseSynchronizer._reconcile_uploads(
                source,
                destination,
                source_activity,
                destination_activity,
            )
            DatabaseSynchronizer._reconcile_sport(
                source,
                destination,
                source_activity,
                destination_activity,
            )
            if copied_metrics:
                destination.flush()
                rebuild_stats(destination, destination_activity.id)

    @staticmethod
    def _reconcile_activity(source: Activity, destination: Activity) -> None:
        """Apply monotonic activity closure without propagating deletions."""
        if source.end_time is not None and (
            destination.end_time is None or _as_utc(source.end_time) > _as_utc(destination.end_time)
        ):
            destination.end_time = source.end_time

    @staticmethod
    def _reconcile_sport(
        source_session: Session,
        destination_session: Session,
        source_activity: Activity,
        destination_activity: Activity,
    ) -> None:
        source_row = source_session.scalar(
            select(ActivitySport).where(ActivitySport.activity_id == source_activity.id),
        )
        if source_row is None:
            return

        destination_row = destination_session.scalar(
            select(ActivitySport).where(
                ActivitySport.activity_id == destination_activity.id,
            ),
        )
        if destination_row is None:
            destination_session.add(
                ActivitySport(
                    activity_id=destination_activity.id,
                    sport_type_id=source_row.sport_type_id,
                ),
            )
        else:
            destination_row.sport_type_id = source_row.sport_type_id

    @staticmethod
    def _copy_metric_rows(
        source_session: Session,
        destination_session: Session,
        source_activity: Activity,
        destination_activity: Activity,
        model: DATABASE_MODEL,
    ) -> bool:
        source_rows = source_session.scalars(
            select(model)
            .where(model.activity_id == source_activity.id)
            .order_by(model.timestamp_ms),
        ).all()
        if not source_rows:
            return False
        mapped_attributes = DatabaseSynchronizer._mapped_metric_attributes(model)
        destination_rows = destination_session.scalars(
            select(model).where(model.activity_id == destination_activity.id),
        ).all()
        destination_counts = Counter(
            tuple(getattr(row, attribute) for attribute in mapped_attributes)
            for row in destination_rows
        )
        source_counts: Counter[tuple[Any, ...]] = Counter()
        rows = []
        for row in source_rows:
            key = tuple(getattr(row, attribute) for attribute in mapped_attributes)
            source_counts[key] += 1
            if source_counts[key] <= destination_counts[key]:
                continue
            rows.append(
                {
                    "activity_id": destination_activity.id,
                    **{attribute: getattr(row, attribute) for attribute in mapped_attributes},
                },
            )
        if rows:
            destination_session.bulk_insert_mappings(model, rows)
        return bool(rows)

    @staticmethod
    def _mapped_metric_attributes(model: DATABASE_MODEL) -> tuple[str, ...]:
        """Return mapped column attributes, excluding database identity fields."""
        return tuple(
            attribute.key
            for attribute in inspect(model).column_attrs
            if attribute.key not in {"id", "activity_id"}
        )

    @staticmethod
    def _reconcile_uploads(
        source_session: Session,
        destination_session: Session,
        source_activity: Activity,
        destination_activity: Activity,
    ) -> None:
        source_rows = source_session.scalars(
            select(ActivityUpload).where(ActivityUpload.activity_id == source_activity.id),
        ).all()
        if not source_rows:
            return
        destination_rows = {
            row.provider: row
            for row in destination_session.scalars(
                select(ActivityUpload).where(
                    ActivityUpload.activity_id == destination_activity.id,
                ),
            ).all()
        }
        for source_row in source_rows:
            destination_row = destination_rows.get(source_row.provider)
            if destination_row is None:
                destination_session.add(
                    ActivityUpload(
                        activity_id=destination_activity.id,
                        provider=source_row.provider,
                        status=source_row.status,
                        uploaded_at=source_row.uploaded_at,
                        updated_at=source_row.updated_at,
                        provider_activity_id=source_row.provider_activity_id,
                        payload_hash=source_row.payload_hash,
                        last_error=source_row.last_error,
                    ),
                )
            elif DatabaseSynchronizer._upload_is_newer(source_row, destination_row):
                destination_row.status = source_row.status
                destination_row.uploaded_at = source_row.uploaded_at
                destination_row.updated_at = source_row.updated_at
                destination_row.provider_activity_id = source_row.provider_activity_id
                destination_row.payload_hash = source_row.payload_hash
                destination_row.last_error = source_row.last_error

    @staticmethod
    def _upload_is_newer(source: ActivityUpload, destination: ActivityUpload) -> bool:
        """Compare upload rows using deterministic last-write-wins ordering."""
        return DatabaseSynchronizer._upload_ordering(
            source,
        ) > DatabaseSynchronizer._upload_ordering(destination)

    @staticmethod
    def _upload_ordering(
        row: ActivityUpload,
    ) -> tuple[datetime, int, str, str, str, str, datetime]:
        """Return the stable ordering used to reconcile equal-time upload rows."""
        updated = row.updated_at or row.uploaded_at
        updated_key = _as_utc(updated) if updated is not None else _MIN_UPLOAD_TIME
        uploaded_key = _as_utc(row.uploaded_at) if row.uploaded_at is not None else _MIN_UPLOAD_TIME
        return (
            updated_key,
            _UPLOAD_STATUS_PRIORITY.get(row.status, -1),
            row.status,
            row.provider_activity_id or "",
            row.payload_hash or "",
            row.last_error or "",
            uploaded_key,
        )
