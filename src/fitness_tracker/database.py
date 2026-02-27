from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    exc,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

Base = declarative_base()


class Activity(Base):
    __tablename__ = "activities"
    id = Column(Integer, primary_key=True)
    # timezone-aware UTC
    start_time = Column(DateTime(timezone=True), nullable=False)
    end_time = Column(DateTime(timezone=True))

    # ensure we never create two activities with the same start_time
    __table_args__ = (UniqueConstraint("start_time", name="uq_activities_start_time"),)

    heart_rates = relationship("HeartRate", back_populates="activity")
    running_metrics = relationship("RunningMetrics", backref="activity")
    cycling_metrics = relationship("CyclingMetrics", backref="activity")


class HeartRate(Base):
    __tablename__ = "heart_rate"

    id = Column(Integer, primary_key=True)
    activity_id = Column(Integer, ForeignKey("activities.id"), nullable=False)
    timestamp_ms = Column(BigInteger, nullable=False)
    bpm = Column(Integer, nullable=False)
    rr_interval = Column(Float)
    energy_kj = Column(Float)

    activity = relationship("Activity", back_populates="heart_rates")

    # index for quick lookups by activity, and by activity+time
    __table_args__ = (
        Index("ix_hr_activity_id", "activity_id"),
        Index("ix_hr_activity_time", "activity_id", "timestamp_ms"),
    )


class RunningMetrics(Base):
    __tablename__ = "running_metrics"

    id = Column(Integer, primary_key=True)
    activity_id = Column(Integer, ForeignKey("activities.id"), nullable=False)
    timestamp_ms = Column(BigInteger, nullable=False)

    # core metrics
    speed_mps = Column(Float, nullable=False)  # RSCS speed (m/s)
    cadence_spm = Column(Integer, nullable=False)  # steps per minute
    stride_length_m = Column(Float)  # optional
    total_distance_m = Column(Float)  # optional
    power_watts = Column(Float)  # optional (Stryd CPS if present)

    # indexes to query by activity and time
    __table_args__ = (
        Index("ix_run_activity_id", "activity_id"),
        Index("ix_run_activity_time", "activity_id", "timestamp_ms"),
    )


class CyclingMetrics(Base):
    __tablename__ = "cycling_metrics"

    id = Column(Integer, primary_key=True)
    activity_id = Column(Integer, ForeignKey("activities.id"), nullable=False)
    timestamp_ms = Column(BigInteger, nullable=False)

    speed_mps = Column(Float, nullable=False)
    cadence_rpm = Column(Integer)
    total_distance_m = Column(Float)
    power_watts = Column(Float)

    __table_args__ = (
        Index("ix_cyc_activity_id", "activity_id"),
        Index("ix_cyc_activity_time", "activity_id", "timestamp_ms"),
    )


class ActivityUpload(Base):
    __tablename__ = "activity_uploads"

    id = Column(Integer, primary_key=True)
    activity_id = Column(Integer, ForeignKey("activities.id", ondelete="CASCADE"), nullable=False)
    provider = Column(String(64), nullable=False)  # e.g. "intervals_icu"
    status = Column(String(16), nullable=False, default="pending")  # "pending"|"ok"|"failed"
    uploaded_at = Column(DateTime(timezone=True))  # when status->ok
    provider_activity_id = Column(String(128))  # remote id if returned
    payload_hash = Column(String(64))  # optional content hash (dedupe)
    last_error = Column(Text)

    activity = relationship("Activity", backref="uploads")

    __table_args__ = (
        UniqueConstraint("activity_id", "provider", name="uq_activity_provider"),
        Index("ix_upload_provider_status", "provider", "status"),
        Index("ix_upload_activity", "activity_id"),
    )


def _sqlite_pragmas(dbapi_con, _con_record) -> None:
    cur = dbapi_con.cursor()
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("PRAGMA synchronous=NORMAL;")
    cur.execute("PRAGMA foreign_keys=ON;")
    cur.close()


class DatabaseManager:
    BATCH_SIZE = 25

    def __init__(self, database_url: str) -> None:
        connect_args = {}
        if database_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False

        self.engine = create_engine(
            database_url,
            echo=False,
            future=True,
            connect_args=connect_args,
            pool_pre_ping=True,
        )
        if database_url.startswith("sqlite"):
            event.listen(self.engine, "connect", _sqlite_pragmas)

        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, future=True, expire_on_commit=False)

        # staging area for batching
        self._pending_hr: list[HeartRate] = []
        self._pending_run: list[RunningMetrics] = []
        self._pending_cyc: list[CyclingMetrics] = []

    def start_activity(self) -> int:
        with self.Session() as session:
            # store UTC with tzinfo
            act = Activity(start_time=datetime.now(tz=ZoneInfo("UTC")))
            session.add(act)
            session.commit()
            return int(act.id)

    def stop_activity(self, activity_id: int) -> None:
        # flush any leftover heart rates before closing
        self._flush_pending()

        with self.Session() as session:
            act = session.get(Activity, activity_id)
            act.end_time = datetime.now(tz=ZoneInfo("UTC"))
            session.commit()

    def list_not_uploaded(self, provider: str) -> list[Activity]:
        """All activities that have no successful upload row for this provider."""
        with self.Session() as session:
            # activities that EITHER:
            #  - have no row at all for this provider, OR
            #  - have a row but not status 'ok' (so we can retry failures)
            return (
                session.query(Activity)
                .outerjoin(
                    ActivityUpload,
                    (Activity.id == ActivityUpload.activity_id)
                    & (ActivityUpload.provider == provider),
                )
                .filter(
                    (ActivityUpload.id.is_(None)) | (ActivityUpload.status != "ok"),
                )
                .order_by(Activity.start_time)
                .all()
            )

    def mark_upload_ok(
        self,
        activity_id: int,
        provider: str,
        provider_activity_id: str | None = None,
        payload_hash: str | None = None,
    ) -> None:
        with self.Session() as session:
            row = (
                session.query(ActivityUpload)
                .filter_by(activity_id=activity_id, provider=provider)
                .one_or_none()
            )
            now = datetime.now(UTC)
            if row is None:
                row = ActivityUpload(
                    activity_id=activity_id,
                    provider=provider,
                )
                session.add(row)
            row.status = "ok"
            row.uploaded_at = now
            row.provider_activity_id = provider_activity_id or row.provider_activity_id
            row.payload_hash = payload_hash or row.payload_hash
            row.last_error = None
            session.commit()

    def mark_upload_failed(
        self,
        activity_id: int,
        provider: str,
        error_message: str,
        payload_hash: str | None = None,
    ) -> None:
        with self.Session() as session:
            row = (
                session.query(ActivityUpload)
                .filter_by(activity_id=activity_id, provider=provider)
                .one_or_none()
            )
            if row is None:
                row = ActivityUpload(
                    activity_id=activity_id,
                    provider=provider,
                )
                session.add(row)
            row.status = "failed"
            row.uploaded_at = None
            row.last_error = error_message[:1000]  # keep it bounded
            if payload_hash:
                row.payload_hash = payload_hash
            session.commit()

    def insert_heart_rate(
        self,
        activity_id: int,
        timestamp_ms: int,
        bpm: int,
        rr: float | None,
        energy: float | None,
    ) -> None:
        # collect into pending list
        hr = HeartRate(
            activity_id=activity_id,
            timestamp_ms=timestamp_ms,
            bpm=bpm,
            rr_interval=rr,
            energy_kj=energy,
        )
        self._pending_hr.append(hr)

        # flush in batches
        if len(self._pending_hr) >= self.BATCH_SIZE:
            self._flush_pending()

    def insert_running_metrics(
        self,
        activity_id: int,
        timestamp_ms: int,
        speed_mps: float,
        cadence_spm: int,
        stride_length_m: float | None,
        total_distance_m: float | None,
        power_watts: float | None,
    ) -> None:
        rm = RunningMetrics(
            activity_id=activity_id,
            timestamp_ms=timestamp_ms,
            speed_mps=speed_mps,
            cadence_spm=cadence_spm,
            stride_length_m=stride_length_m,
            total_distance_m=total_distance_m,
            power_watts=power_watts,
        )
        self._pending_run.append(rm)
        if len(self._pending_run) >= self.BATCH_SIZE:
            self._flush_pending()

    def insert_cycling_metrics(
        self,
        activity_id: int,
        timestamp_ms: int,
        speed_mps: float,
        cadence_rpm: int | None,
        total_distance_m: float | None,
        power_watts: float | None,
    ) -> None:
        cm = CyclingMetrics(
            activity_id=activity_id,
            timestamp_ms=timestamp_ms,
            speed_mps=speed_mps,
            cadence_rpm=cadence_rpm,
            total_distance_m=total_distance_m,
            power_watts=power_watts,
        )
        self._pending_cyc.append(cm)
        if len(self._pending_cyc) >= self.BATCH_SIZE:
            self._flush_pending()

    def _flush_pending(self):
        with self.Session() as session:
            if self._pending_hr:
                session.add_all(self._pending_hr)
            if self._pending_run:
                session.add_all(self._pending_run)
            if self._pending_cyc:
                session.add_all(self._pending_cyc)
            session.commit()

        self._pending_hr.clear()
        self._pending_run.clear()
        self._pending_cyc.clear()

    def sync_to_database(self, database_dsn: str):
        self._flush_pending()

        try:
            remote_engine = create_engine(database_dsn, echo=False)
            with remote_engine.connect() as _:
                pass
        except exc.SQLAlchemyError as e:
            msg = f"❌  Could not connect to remote database: {e}"
            raise ConnectionError(msg)

        remote_engine = create_engine(database_dsn, echo=False)
        Base.metadata.create_all(remote_engine)
        LocalSession = self.Session
        RemoteSession = sessionmaker(bind=remote_engine)

        SYNC_BATCH_SIZE = 100

        with LocalSession() as local, RemoteSession() as remote:
            # ---------- Local → Remote ----------
            def _sync_batch_l2r(batch: list[Activity]):
                existing = {t for (t,) in remote.query(Activity.start_time).all()}
                existing = {t.astimezone(ZoneInfo("UTC")) for t in existing}
                new_batch = [
                    act
                    for act in batch
                    if act.start_time.replace(tzinfo=ZoneInfo("UTC")) not in existing
                ]
                for act in new_batch:
                    if act.start_time in existing:
                        continue
                    new_act = Activity(start_time=act.start_time, end_time=act.end_time)
                    remote.add(new_act)
                    remote.flush()

                    # HR rows
                    hrs = (
                        local.query(HeartRate)
                        .filter_by(activity_id=act.id)
                        .order_by(HeartRate.timestamp_ms)
                        .all()
                    )
                    if hrs:
                        remote.bulk_insert_mappings(
                            HeartRate,
                            [
                                {
                                    "activity_id": new_act.id,
                                    "timestamp_ms": hr.timestamp_ms,
                                    "bpm": hr.bpm,
                                    "rr_interval": hr.rr_interval,
                                    "energy_kj": hr.energy_kj,
                                }
                                for hr in hrs
                            ],
                        )

                    # Running rows
                    runs = (
                        local.query(RunningMetrics)
                        .filter_by(activity_id=act.id)
                        .order_by(RunningMetrics.timestamp_ms)
                        .all()
                    )
                    if runs:
                        remote.bulk_insert_mappings(
                            RunningMetrics,
                            [
                                {
                                    "activity_id": new_act.id,
                                    "timestamp_ms": r.timestamp_ms,
                                    "speed_mps": r.speed_mps,
                                    "cadence_spm": r.cadence_spm,
                                    "stride_length_m": r.stride_length_m,
                                    "total_distance_m": r.total_distance_m,
                                    "power_watts": r.power_watts,
                                }
                                for r in runs
                            ],
                        )

                    # Cycling rows
                    cycls = (
                        local.query(CyclingMetrics)
                        .filter_by(activity_id=act.id)
                        .order_by(CyclingMetrics.timestamp_ms)
                        .all()
                    )
                    if cycls:
                        remote.bulk_insert_mappings(
                            CyclingMetrics,
                            [
                                {
                                    "activity_id": new_act.id,
                                    "timestamp_ms": c.timestamp_ms,
                                    "speed_mps": c.speed_mps,
                                    "cadence_rpm": c.cadence_rpm,
                                    "total_distance_m": c.total_distance_m,
                                    "power_watts": c.power_watts,
                                }
                                for c in cycls
                            ],
                        )

            batch = []
            for act in (
                local.query(Activity).order_by(Activity.start_time).yield_per(SYNC_BATCH_SIZE)
            ):
                batch.append(act)
                if len(batch) >= SYNC_BATCH_SIZE:
                    _sync_batch_l2r(batch)
                    batch.clear()
            if batch:
                _sync_batch_l2r(batch)
            remote.commit()

            # ---------- Remote → Local ----------
            def _sync_batch_r2l(batch: list[Activity]):
                existing = {t for (t,) in local.query(Activity.start_time).all()}
                existing = {t.replace(tzinfo=ZoneInfo("UTC")) for t in existing}
                new_batch = [
                    act
                    for act in batch
                    if act.start_time.astimezone(ZoneInfo("UTC")) not in existing
                ]
                for act in new_batch:
                    if act.start_time in existing:
                        continue
                    new_act = Activity(start_time=act.start_time, end_time=act.end_time)
                    local.add(new_act)
                    local.flush()

                    hrs = (
                        remote.query(HeartRate)
                        .filter_by(activity_id=act.id)
                        .order_by(HeartRate.timestamp_ms)
                        .all()
                    )
                    if hrs:
                        local.bulk_insert_mappings(
                            HeartRate,
                            [
                                {
                                    "activity_id": new_act.id,
                                    "timestamp_ms": hr.timestamp_ms,
                                    "bpm": hr.bpm,
                                    "rr_interval": hr.rr_interval,
                                    "energy_kj": hr.energy_kj,
                                }
                                for hr in hrs
                            ],
                        )

                    runs = (
                        remote.query(RunningMetrics)
                        .filter_by(activity_id=act.id)
                        .order_by(RunningMetrics.timestamp_ms)
                        .all()
                    )
                    if runs:
                        local.bulk_insert_mappings(
                            RunningMetrics,
                            [
                                {
                                    "activity_id": new_act.id,
                                    "timestamp_ms": r.timestamp_ms,
                                    "speed_mps": r.speed_mps,
                                    "cadence_spm": r.cadence_spm,
                                    "stride_length_m": r.stride_length_m,
                                    "total_distance_m": r.total_distance_m,
                                    "power_watts": r.power_watts,
                                }
                                for r in runs
                            ],
                        )

                    cycls = (
                        remote.query(CyclingMetrics)
                        .filter_by(activity_id=act.id)
                        .order_by(CyclingMetrics.timestamp_ms)
                        .all()
                    )
                    if cycls:
                        local.bulk_insert_mappings(
                            CyclingMetrics,
                            [
                                {
                                    "activity_id": new_act.id,
                                    "timestamp_ms": c.timestamp_ms,
                                    "speed_mps": c.speed_mps,
                                    "cadence_rpm": c.cadence_rpm,
                                    "total_distance_m": c.total_distance_m,
                                    "power_watts": c.power_watts,
                                }
                                for c in cycls
                            ],
                        )

            batch = []
            for act in (
                remote.query(Activity).order_by(Activity.start_time).yield_per(SYNC_BATCH_SIZE)
            ):
                batch.append(act)
                if len(batch) >= SYNC_BATCH_SIZE:
                    _sync_batch_r2l(batch)
                    batch.clear()
            if batch:
                _sync_batch_r2l(batch)
            local.commit()
