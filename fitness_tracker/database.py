from datetime import datetime
from sqlalchemy import (
    Column,
    Integer,
    Float,
    DateTime,
    ForeignKey,
    create_engine,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

Base = declarative_base()


class Activity(Base):
    __tablename__ = "activities"
    id = Column(Integer, primary_key=True)
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime)
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


class DatabaseManager:
    def __init__(self, database_url: str):
        # Create engine and tables locally
        self.engine = create_engine(database_url, echo=False)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def start_activity(self) -> int:
        session = self.Session()
        act = Activity(start_time=datetime.utcnow())
        session.add(act)
        session.commit()
        aid = act.id
        session.close()
        return aid

    def stop_activity(self, activity_id: int):
        session = self.Session()
        act = session.get(Activity, activity_id)
        act.end_time = datetime.utcnow()
        session.commit()
        session.close()

    def insert_heart_rate(
        self,
        activity_id: int,
        timestamp_ms: int,
        bpm: int,
        rr: float | None,
        energy: float | None,
    ):
        session = self.Session()
        hr = HeartRate(
            activity_id=activity_id,
            timestamp_ms=timestamp_ms,
            bpm=bpm,
            rr_interval=rr,
            energy_kj=energy,
        )
        session.add(hr)
        session.commit()
        session.close()

    def sync_to_postgres(self, postgres_url: str):
        """
        Sync new local activities & heart rates to remote Postgres.
        Uses Activity.start_time to avoid duplicates.
        """
        # Setup remote engine & tables
        remote_engine = create_engine(postgres_url, echo=False)
        Base.metadata.create_all(remote_engine)
        LocalSession = self.Session
        RemoteSession = sessionmaker(bind=remote_engine)

        local = LocalSession()
        remote = RemoteSession()

        # Cache remote start_times
        existing = {a.start_time for a in remote.query(Activity).all()}

        # Push any local activities not yet remote
        for act in local.query(Activity).order_by(Activity.start_time):
            if act.start_time in existing:
                continue
            # Create remote activity
            new_act = Activity(start_time=act.start_time, end_time=act.end_time)
            remote.add(new_act)
            remote.commit()

            # Push its samples
            hrs = local.query(HeartRate).filter_by(activity_id=act.id)
            for hr in hrs:
                new_hr = HeartRate(
                    activity_id=new_act.id,
                    timestamp_ms=hr.timestamp_ms,
                    bpm=hr.bpm,
                    rr_interval=hr.rr_interval,
                    energy_kj=hr.energy_kj,
                )
                remote.add(new_hr)
            remote.commit()

        local.close()
        remote.close()
