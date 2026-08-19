"""Repository protocols and the SQLAlchemy-backed activity repository."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from fitness_tracker.core.sports import SportTypesEnum
from fitness_tracker.data.models import (
    Activity,
    ActivitySport,
    ActivityStats,
    ActivityUpload,
    CyclingMetrics,
    HeartRate,
    LocationPoint,
    RunningMetrics,
)

if TYPE_CHECKING:
    from collections.abc import Collection

    from sqlalchemy.dialects.postgresql.dml import Insert as PostgreSQLInsert
    from sqlalchemy.dialects.sqlite.dml import Insert as SQLiteInsert
    from sqlalchemy.orm import Session, sessionmaker


RunningSeriesPoint = tuple[int, float, int, float | None]
CyclingSeriesPoint = tuple[int, float, int | None, float | None]


class ActivityStatsRow(Protocol):
    """Activity summary fields exposed through the repository boundary."""

    activity_id: int
    sport_type_id: int
    start_time: datetime
    duration_s: int
    distance_m: float | None
    avg_speed_mps: float | None
    avg_bpm: float | None
    max_bpm: int | None
    avg_cadence: float | None
    avg_power_watts: float | None
    total_ascent_m: float | None


class ActivityRepository(Protocol):
    """Read and write operations required by history and upload consumers."""

    def get_activity(self, activity_id: int) -> Activity | None:
        """Return an activity by database ID, if it exists."""
        ...

    def get_activity_stats(self, activity_id: int) -> ActivityStatsRow | None:
        """Return the pre-computed summary for an activity, if it exists."""
        ...

    def get_activity_sport(self, activity_id: int) -> ActivitySport | None:
        """Return the sport association for an activity, if it exists."""
        ...

    def list_activity_stats(self, cutoff: datetime | None = None) -> list[ActivityStatsRow]:
        """List activity summaries, optionally restricted to a start-time cutoff."""
        ...

    def list_failed_activity_stats(self) -> list[ActivityStatsRow]:
        """List activity summaries with at least one failed upload."""
        ...

    def list_heart_rates(self, activity_id: int) -> list[HeartRate]:
        """List heart-rate samples ordered by sample time."""
        ...

    def list_heart_rate_series(
        self,
        activity_ids: Collection[int],
    ) -> dict[int, list[tuple[int, int]]]:
        """List heart-rate points grouped by activity in one query."""
        ...

    def list_running_metric_series(
        self,
        activity_ids: Collection[int],
    ) -> dict[int, list[RunningSeriesPoint]]:
        """List running chart columns grouped by activity in one query."""
        ...

    def list_cycling_metric_series(
        self,
        activity_ids: Collection[int],
    ) -> dict[int, list[CyclingSeriesPoint]]:
        """List cycling chart columns grouped by activity in one query."""
        ...

    def list_running_metrics(self, activity_id: int) -> list[RunningMetrics]:
        """List running sensor samples ordered by sample time."""
        ...

    def list_cycling_metrics(self, activity_id: int) -> list[CyclingMetrics]:
        """List cycling sensor samples ordered by sample time."""
        ...

    def list_location_points(self, activity_id: int) -> list[LocationPoint]:
        """List accepted location points ordered by time and row identity."""
        ...

    def list_not_uploaded(self, provider: str) -> list[Activity]:
        """List activities not successfully uploaded to a provider."""
        ...

    def get_activity_upload(self, activity_id: int, provider: str) -> ActivityUpload | None:
        """Return one activity's upload state for a provider."""
        ...

    def mark_upload_ok(
        self,
        activity_id: int,
        provider: str,
        provider_activity_id: str | None = None,
        payload_hash: str | None = None,
    ) -> None:
        """Record a successful upload and its provider metadata."""
        ...

    def mark_upload_failed(
        self,
        activity_id: int,
        provider: str,
        error_message: str,
        payload_hash: str | None = None,
    ) -> None:
        """Record a failed upload and its truncated error message."""
        ...

    def mark_upload_accepted(
        self,
        activity_id: int,
        provider: str,
        provider_activity_id: str,
        payload_hash: str,
        error_message: str,
    ) -> None:
        """Record remote acceptance pending local success reconciliation."""
        ...


class SqlAlchemyActivityRepository:
    """SQLAlchemy implementation of :class:`ActivityRepository`."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get_activity(self, activity_id: int) -> Activity | None:
        """Return an activity by database ID, if it exists."""
        with self._session_factory() as session:
            return session.get(Activity, activity_id)

    def get_activity_stats(self, activity_id: int) -> ActivityStatsRow | None:
        """Return the pre-computed summary for an activity, if it exists."""
        with self._session_factory() as session:
            return session.scalar(
                select(ActivityStats).where(ActivityStats.activity_id == activity_id),
            )

    def get_activity_sport(self, activity_id: int) -> ActivitySport | None:
        """Return the sport association for an activity, if it exists."""
        with self._session_factory() as session:
            return session.scalars(
                select(ActivitySport).where(ActivitySport.activity_id == activity_id),
            ).first()

    def list_activity_stats(self, cutoff: datetime | None = None) -> list[ActivityStatsRow]:
        """List activity summaries, optionally restricted to a start-time cutoff."""
        statement = select(ActivityStats).where(
            ActivityStats.sport_type_id != SportTypesEnum.unknown.value,
        )
        if cutoff is not None:
            statement = statement.where(ActivityStats.start_time >= cutoff)
        with self._session_factory() as session:
            return list(session.scalars(statement).all())

    def list_failed_activity_stats(self) -> list[ActivityStatsRow]:
        """List activity summaries with at least one failed upload."""
        failed_activity_ids = select(ActivityUpload.activity_id).where(
            ActivityUpload.status == "failed",
        )
        statement = select(ActivityStats).where(
            ActivityStats.sport_type_id != SportTypesEnum.unknown.value,
            ActivityStats.activity_id.in_(failed_activity_ids),
        )
        with self._session_factory() as session:
            return list(session.scalars(statement).all())

    def list_heart_rates(self, activity_id: int) -> list[HeartRate]:
        """List heart-rate samples ordered by sample time."""
        statement = (
            select(HeartRate)
            .where(HeartRate.activity_id == activity_id)
            .order_by(HeartRate.timestamp_ms)
        )
        with self._session_factory() as session:
            return list(session.scalars(statement).all())

    def list_heart_rate_series(
        self,
        activity_ids: Collection[int],
    ) -> dict[int, list[tuple[int, int]]]:
        """List heart-rate points grouped by activity in one query."""
        if not activity_ids:
            return {}
        statement = (
            select(HeartRate.activity_id, HeartRate.timestamp_ms, HeartRate.bpm)
            .where(HeartRate.activity_id.in_(activity_ids))
            .order_by(HeartRate.activity_id, HeartRate.timestamp_ms)
        )
        series: defaultdict[int, list[tuple[int, int]]] = defaultdict(list)
        with self._session_factory() as session:
            for activity_id, timestamp_ms, bpm in session.execute(statement):
                series[int(activity_id)].append((int(timestamp_ms), int(bpm)))
        return dict(series)

    def list_running_metric_series(
        self,
        activity_ids: Collection[int],
    ) -> dict[int, list[RunningSeriesPoint]]:
        """List running chart columns grouped by activity in one query."""
        if not activity_ids:
            return {}
        statement = (
            select(
                RunningMetrics.activity_id,
                RunningMetrics.timestamp_ms,
                RunningMetrics.speed_mps,
                RunningMetrics.cadence_spm,
                RunningMetrics.power_watts,
            )
            .where(RunningMetrics.activity_id.in_(activity_ids))
            .order_by(RunningMetrics.activity_id, RunningMetrics.timestamp_ms)
        )
        series: defaultdict[int, list[RunningSeriesPoint]] = defaultdict(list)
        with self._session_factory() as session:
            for activity_id, timestamp_ms, speed_mps, cadence_spm, power_watts in session.execute(
                statement,
            ):
                series[int(activity_id)].append(
                    (int(timestamp_ms), float(speed_mps), int(cadence_spm), power_watts),
                )
        return dict(series)

    def list_cycling_metric_series(
        self,
        activity_ids: Collection[int],
    ) -> dict[int, list[CyclingSeriesPoint]]:
        """List cycling chart columns grouped by activity in one query."""
        if not activity_ids:
            return {}
        statement = (
            select(
                CyclingMetrics.activity_id,
                CyclingMetrics.timestamp_ms,
                CyclingMetrics.speed_mps,
                CyclingMetrics.cadence_rpm,
                CyclingMetrics.power_watts,
            )
            .where(CyclingMetrics.activity_id.in_(activity_ids))
            .order_by(CyclingMetrics.activity_id, CyclingMetrics.timestamp_ms)
        )
        series: defaultdict[int, list[CyclingSeriesPoint]] = defaultdict(list)
        with self._session_factory() as session:
            for activity_id, timestamp_ms, speed_mps, cadence_rpm, power_watts in session.execute(
                statement,
            ):
                series[int(activity_id)].append(
                    (int(timestamp_ms), float(speed_mps), cadence_rpm, power_watts),
                )
        return dict(series)

    def list_running_metrics(self, activity_id: int) -> list[RunningMetrics]:
        """List running sensor samples ordered by sample time."""
        statement = (
            select(RunningMetrics)
            .where(RunningMetrics.activity_id == activity_id)
            .order_by(RunningMetrics.timestamp_ms)
        )
        with self._session_factory() as session:
            return list(session.scalars(statement).all())

    def list_cycling_metrics(self, activity_id: int) -> list[CyclingMetrics]:
        """List cycling sensor samples ordered by sample time."""
        statement = (
            select(CyclingMetrics)
            .where(CyclingMetrics.activity_id == activity_id)
            .order_by(CyclingMetrics.timestamp_ms)
        )
        with self._session_factory() as session:
            return list(session.scalars(statement).all())

    def list_location_points(self, activity_id: int) -> list[LocationPoint]:
        """List accepted location points ordered by time and row identity."""
        statement = (
            select(LocationPoint)
            .where(LocationPoint.activity_id == activity_id)
            .order_by(LocationPoint.timestamp_ms, LocationPoint.id)
        )
        with self._session_factory() as session:
            return list(session.scalars(statement).all())

    def list_not_uploaded(self, provider: str) -> list[Activity]:
        """List activities not successfully uploaded to a provider."""
        statement = (
            select(Activity)
            .outerjoin(
                ActivityUpload,
                (Activity.id == ActivityUpload.activity_id) & (ActivityUpload.provider == provider),
            )
            .where(
                Activity.end_time.is_not(None),
                (ActivityUpload.id.is_(None)) | (ActivityUpload.status != "ok"),
            )
            .order_by(Activity.start_time)
        )
        with self._session_factory() as session:
            return list(session.scalars(statement).all())

    def get_activity_upload(self, activity_id: int, provider: str) -> ActivityUpload | None:
        """Return one activity's upload state for a provider."""
        statement = select(ActivityUpload).where(
            ActivityUpload.activity_id == activity_id,
            ActivityUpload.provider == provider,
        )
        with self._session_factory() as session:
            return session.scalar(statement)

    def mark_upload_ok(
        self,
        activity_id: int,
        provider: str,
        provider_activity_id: str | None = None,
        payload_hash: str | None = None,
    ) -> None:
        """Record a successful upload and its provider metadata."""
        with self._session_factory() as session:
            now = datetime.now(UTC)
            insert = self._upload_insert(session)
            excluded = insert.excluded
            statement = insert.values(
                activity_id=activity_id,
                provider=provider,
                status="ok",
                uploaded_at=now,
                updated_at=now,
                provider_activity_id=provider_activity_id or None,
                payload_hash=payload_hash or None,
                last_error=None,
            ).on_conflict_do_update(
                index_elements=["activity_id", "provider"],
                set_={
                    "status": "ok",
                    "uploaded_at": now,
                    "updated_at": now,
                    "provider_activity_id": func.coalesce(
                        excluded.provider_activity_id,
                        ActivityUpload.provider_activity_id,
                    ),
                    "payload_hash": func.coalesce(
                        excluded.payload_hash,
                        ActivityUpload.payload_hash,
                    ),
                    "last_error": None,
                },
            )
            session.execute(statement)
            session.commit()

    def mark_upload_failed(
        self,
        activity_id: int,
        provider: str,
        error_message: str,
        payload_hash: str | None = None,
    ) -> None:
        """Record a failed upload and its truncated error message."""
        with self._session_factory() as session:
            now = datetime.now(UTC)
            insert = self._upload_insert(session)
            excluded = insert.excluded
            statement = insert.values(
                activity_id=activity_id,
                provider=provider,
                status="failed",
                uploaded_at=None,
                updated_at=now,
                payload_hash=payload_hash or None,
                last_error=error_message[:1000],
            ).on_conflict_do_update(
                index_elements=["activity_id", "provider"],
                set_={
                    "status": "failed",
                    "uploaded_at": None,
                    "updated_at": now,
                    "payload_hash": func.coalesce(
                        excluded.payload_hash,
                        ActivityUpload.payload_hash,
                    ),
                    "last_error": excluded.last_error,
                },
                where=ActivityUpload.status.not_in(("accepted", "ok")),
            )
            session.execute(statement)
            session.commit()

    def mark_upload_accepted(
        self,
        activity_id: int,
        provider: str,
        provider_activity_id: str,
        payload_hash: str,
        error_message: str,
    ) -> None:
        """Record remote acceptance pending local success reconciliation."""
        with self._session_factory() as session:
            now = datetime.now(UTC)
            truncated_error = error_message[:1000]
            insert = self._upload_insert(session)
            excluded = insert.excluded
            statement = insert.values(
                activity_id=activity_id,
                provider=provider,
                status="accepted",
                uploaded_at=now,
                updated_at=now,
                provider_activity_id=provider_activity_id,
                payload_hash=payload_hash,
                last_error=truncated_error,
            ).on_conflict_do_update(
                index_elements=["activity_id", "provider"],
                set_={
                    "status": "accepted",
                    "uploaded_at": now,
                    "updated_at": now,
                    "provider_activity_id": excluded.provider_activity_id,
                    "payload_hash": excluded.payload_hash,
                    "last_error": excluded.last_error,
                },
                where=ActivityUpload.status != "ok",
            )
            session.execute(statement)
            session.commit()

    @staticmethod
    def _upload_insert(session: Session) -> SQLiteInsert | PostgreSQLInsert:
        """Return the native upsert constructor for the configured database."""
        dialect = session.get_bind().dialect.name
        if dialect == "sqlite":
            return sqlite_insert(ActivityUpload)
        if dialect == "postgresql":
            return postgresql_insert(ActivityUpload)
        message = f"Activity upload upserts are unsupported for {dialect}"
        raise RuntimeError(message)
