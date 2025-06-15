from datetime import datetime
from sqlalchemy import (
    Column,
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
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime)

    # ensure we never create two activities with the same start_time
    __table_args__ = (UniqueConstraint('start_time', name='uq_activities_start_time'),)

    heart_rates = relationship("HeartRate", back_populates="activity")


class HeartRate(Base):
    __tablename__ = "heart_rate"

    id = Column(Integer, primary_key=True)
    activity_id = Column(Integer, ForeignKey("activities.id"), nullable=False)
    timestamp_ms = Column(Integer, nullable=False)
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
            # Create a new activity with the current time
            act = Activity(start_time=datetime.utcnow())
            session.add(act)
            session.commit()
            aid = act.id
            return aid

    def stop_activity(self, activity_id: int):
        # flush any leftover heart rates before closing
        self._flush_pending()

        with self.Session() as session:
            act = session.get(Activity, activity_id)
            act.end_time = datetime.utcnow()
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

    def sync_to_postgres(self, postgres_url: str):
        """
        Sync new local activities & heart rates to remote Postgres.
        Uses Activity.start_time to avoid duplicates.
        """
        # flush any pending local HR samples
        self._flush_pending()

        # Setup remote engine & tables
        remote_engine = create_engine(postgres_url, echo=False)
        Base.metadata.create_all(remote_engine)
        LocalSession = self.Session
        RemoteSession = sessionmaker(bind=remote_engine)

        with LocalSession() as local, RemoteSession() as remote:
            # 1) find which start_times already exist remotely
            existing = {a.start_time for a in remote.query(Activity).all()}

            # 2) for each new local activity, push activity + HRs
            for act in local.query(Activity).order_by(Activity.start_time):
                if act.start_time in existing:
                    continue

                # create the remote activity
                new_act = Activity(
                    start_time=act.start_time,
                    end_time=act.end_time
                )
                remote.add(new_act)
                # flush so new_act.id is populated, but don’t commit yet
                remote.flush()

                # fetch local heart rates for this activity
                hrs = (
                    local.query(HeartRate)
                         .filter_by(activity_id=act.id)
                         .order_by(HeartRate.timestamp_ms)
                         .all()
                )

                # build mappings for INSERT
                hr_mappings = [
                    {
                        "activity_id": new_act.id,
                        "timestamp_ms": hr.timestamp_ms,
                        "bpm": hr.bpm,
                        "rr_interval": hr.rr_interval,
                        "energy_kj": hr.energy_kj,
                    }
                    for hr in hrs
                ]

                if hr_mappings:
                    remote.bulk_insert_mappings(HeartRate, hr_mappings)

            # finally, commit everything in one go
            remote.commit()
