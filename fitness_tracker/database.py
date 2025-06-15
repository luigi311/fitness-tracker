from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy import (
    Column,
    BigInteger,
    Integer,
    Float,
    DateTime,
    ForeignKey,
    create_engine,
    UniqueConstraint,
    Index,
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
    __table_args__ = (UniqueConstraint('start_time', name='uq_activities_start_time'),)

    heart_rates = relationship("HeartRate", back_populates="activity")


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
        Index('ix_hr_activity_id', 'activity_id'),
        Index('ix_hr_activity_time', 'activity_id', 'timestamp_ms'),
    )


class DatabaseManager:
    BATCH_SIZE = 25

    def __init__(self, database_url: str):
        # Create engine and tables locally
        self.engine = create_engine(database_url, echo=False)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        # staging area for batching
        self._pending_hr: list[HeartRate] = []

    def start_activity(self) -> int:
        with self.Session() as session:
            # store UTC with tzinfo
            act = Activity(start_time=datetime.now(tz=ZoneInfo("UTC")))
            session.add(act)
            session.commit()
            return int(act.id)

    def stop_activity(self, activity_id: int):
        # flush any leftover heart rates before closing
        self._flush_pending()

        with self.Session() as session:
            act = session.get(Activity, activity_id)
            act.end_time = datetime.now(tz=ZoneInfo("UTC"))
            session.commit()

    def insert_heart_rate(
        self,
        activity_id: int,
        timestamp_ms: int,
        bpm: int,
        rr: float | None,
        energy: float | None,
    ):
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

    def _flush_pending(self):
        if not self._pending_hr:
            return

        with self.Session() as session:
            session.add_all(self._pending_hr)
            session.commit()

        # clear the staging area
        self._pending_hr.clear()

    def sync_to_database(self, database_dsn: str):
        """
        Two-way sync between local SQLite and remote Database:
        1) push new local activities → remote
        2) pull new remote activities → local
        """
        # 0) Flush any pending local heart-rate samples
        self._flush_pending()

        # 1) Setup engines & sessions
        remote_engine = create_engine(database_dsn, echo=False)
        Base.metadata.create_all(remote_engine)
        LocalSession = self.Session
        RemoteSession = sessionmaker(bind=remote_engine)

        # how many activities we process per batch
        SYNC_BATCH_SIZE = 100

        with LocalSession() as local, RemoteSession() as remote:
            # ── Phase 1: Local → Remote ──────────────────────────────
            def _sync_batch(batch: list[Activity]):
                # find which of these start_times already exist remotely
                existing = {
                    t for (t,) in remote
                        .query(Activity.start_time)
                        .all()
                }

                # convert to UTC for comparison
                existing = {t.astimezone(ZoneInfo("UTC")) for t in existing}
                # filter out any activities that already exist remotely
                new_batch = [
                    act for act in batch
                    if act.start_time.replace(tzinfo=ZoneInfo("UTC")) not in existing
                ]

                for act in new_batch:
                    if act.start_time in existing:
                        continue

                    # create the remote activity
                    new_act = Activity(
                        start_time=act.start_time,
                        end_time=act.end_time,
                    )
                    remote.add(new_act)
                    # flush to get new_act.id
                    remote.flush()

                    # pull local heart-rate rows
                    hrs = (
                        local.query(HeartRate)
                             .filter_by(activity_id=act.id)
                             .order_by(HeartRate.timestamp_ms)
                             .all()
                    )
                    # bulk-insert into remote
                    hr_maps = [
                        {
                            "activity_id": new_act.id,
                            "timestamp_ms": hr.timestamp_ms,
                            "bpm": hr.bpm,
                            "rr_interval": hr.rr_interval,
                            "energy_kj": hr.energy_kj,
                        }
                        for hr in hrs
                    ]
                    if hr_maps:
                        remote.bulk_insert_mappings(HeartRate, hr_maps)

            # page through local activities
            batch = []
            for act in local.query(Activity)\
                             .order_by(Activity.start_time)\
                             .yield_per(SYNC_BATCH_SIZE):
                batch.append(act)
                if len(batch) >= SYNC_BATCH_SIZE:
                    _sync_batch(batch)
                    batch.clear()
            if batch:
                _sync_batch(batch)

            # commit everything local → remote
            remote.commit()

            
            # ── Phase 2: Remote → Local ──────────────────────────────
            def _sync_remote_batch(batch: list[Activity]):
                # find which of these start_times already exist locally
                existing = {
                    t for (t,) in local
                        .query(Activity.start_time)
                        .all()
                }

                # convert to UTC for comparison
                existing = {t.replace(tzinfo=ZoneInfo("UTC")) for t in existing}
                # filter out any activities that already exist locally
                new_batch = [
                    act for act in batch
                    if act.start_time.astimezone(ZoneInfo("UTC")) not in existing
                ]

                for act in new_batch:
                    if act.start_time in existing:
                        continue

                    # create the local activity
                    new_act = Activity(
                        start_time=act.start_time,
                        end_time=act.end_time,
                    )
                    local.add(new_act)
                    # flush to get new_act.id
                    local.flush()

                    # pull remote heart-rate rows
                    hrs = (
                        remote.query(HeartRate)
                              .filter_by(activity_id=act.id)
                              .order_by(HeartRate.timestamp_ms)
                              .all()
                    )
                    # bulk-insert into local
                    hr_maps = [
                        {
                            "activity_id": new_act.id,
                            "timestamp_ms": hr.timestamp_ms,
                            "bpm": hr.bpm,
                            "rr_interval": hr.rr_interval,
                            "energy_kj": hr.energy_kj,
                        }
                        for hr in hrs
                    ]
                    if hr_maps:
                        local.bulk_insert_mappings(HeartRate, hr_maps)

            # page through remote activities
            batch = []
            for act in remote.query(Activity)\
                             .order_by(Activity.start_time)\
                             .yield_per(SYNC_BATCH_SIZE):
                batch.append(act)
                if len(batch) >= SYNC_BATCH_SIZE:
                    _sync_remote_batch(batch)
                    batch.clear()
            if batch:
                _sync_remote_batch(batch)
            # commit everything remote → local
            local.commit()
