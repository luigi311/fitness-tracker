"""C4 measurement contracts."""

# ruff: noqa: PLR2004

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from bleaksport import CyclingSample, HeartRateSample, RunningSample, TrainerSample
from fitness_tracker import database as database_module
from fitness_tracker.core.environment import Environment
from fitness_tracker.core.measurements import NormalizedHeartRate
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
from fitness_tracker.data.sqlite_files import secure_sqlite_files
from fitness_tracker.database import DatabaseManager, DatabaseMigrationError
from fitness_tracker.hardware.location import LocationFix
from fitness_tracker.hardware.processor import SampleProcessor
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

_DATABASE_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "database"


def _database() -> DatabaseManager:
    return DatabaseManager("sqlite:///:memory:")


def _trainer_sample(*, altitude_m: float | None) -> TrainerSample:
    return TrainerSample(
        timestamp_ms=1_000,
        speed_kmh=10.0,
        cadence_spm=80,
        cadence_rpm=80.0,
        altitude_m=altitude_m,
        inclination=8.0,
    )


@pytest.mark.parametrize(
    ("sport_type", "expected_table"),
    [
        (SportTypesEnum.running, RunningMetrics),
        (SportTypesEnum.biking, CyclingMetrics),
    ],
)
def test_zero_altitude_is_preserved(
    sport_type: SportTypesEnum,
    expected_table: type[RunningMetrics] | type[CyclingMetrics],
) -> None:
    db = _database()
    activity_id = db.start_activity(sport_type, Environment.INDOOR)
    sample = _trainer_sample(altitude_m=0.0)

    if sport_type == SportTypesEnum.running:
        db.insert_running_metrics(activity_id, sample, incline_percent=8.0)
    else:
        db.insert_cycling_metrics(activity_id, sample, incline_percent=8.0)
    db.stop_activity(activity_id)

    with db.Session() as session:
        row = session.query(expected_table).one()

    assert row.altitude_m == 0.0
    assert row.incline_percent == 8.0


def test_incomplete_required_metrics_are_not_queued() -> None:
    db = _database()
    activity_id = db.start_activity(SportTypesEnum.running, Environment.INDOOR)

    db.insert_running_metrics(
        activity_id,
        RunningSample(timestamp_ms=1_000, speed_mps=None, cadence_spm=170),
        incline_percent=None,
    )
    db.insert_running_metrics(
        activity_id,
        RunningSample(timestamp_ms=2_000, speed_mps=3.0, cadence_spm=None),
        incline_percent=None,
    )
    db.insert_cycling_metrics(
        activity_id,
        CyclingSample(timestamp_ms=3_000, speed_mps=None, cadence_rpm=80.0),
        incline_percent=None,
    )

    assert db._pending_run == []  # noqa: SLF001
    assert db._pending_cyc == []  # noqa: SLF001


def test_fractional_cycling_cadence_is_queued_as_an_integer() -> None:
    db = _database()
    activity_id = db.start_activity(SportTypesEnum.biking, Environment.OUTDOOR)

    db.insert_cycling_metrics(
        activity_id,
        CyclingSample(timestamp_ms=1_000, speed_mps=8.0, cadence_rpm=80.6),
        incline_percent=None,
    )
    db.stop_activity(activity_id)

    with db.Session() as session:
        row = session.query(CyclingMetrics).one()
    assert row.cadence_rpm == 81


def test_fresh_migration_matches_upload_metadata() -> None:
    db = _database()

    database_column = next(
        column
        for column in inspect(db.engine).get_columns("activity_uploads")
        if column["name"] == "updated_at"
    )

    assert database_column["nullable"] is ActivityUpload.__table__.c.updated_at.nullable is False

    activity_columns = {column["name"] for column in inspect(db.engine).get_columns("activities")}
    assert "environment" in activity_columns
    environment_column = next(
        column
        for column in inspect(db.engine).get_columns("activities")
        if column["name"] == "environment"
    )
    assert environment_column["nullable"] is False

    location_columns = {
        column["name"] for column in inspect(db.engine).get_columns("location_points")
    }
    assert location_columns == {
        "id",
        "activity_id",
        "timestamp_ms",
        "latitude_deg",
        "longitude_deg",
        "accuracy_m",
        "altitude_m",
        "speed_mps",
        "heading_deg",
        "source_time_utc",
    }
    assert {index["name"] for index in inspect(db.engine).get_indexes("location_points")} == {
        "ix_location_activity_id",
        "ix_location_activity_time",
    }


@pytest.mark.parametrize("environment", [Environment.OUTDOOR, Environment.TRAINER])
def test_start_activity_persists_environment(environment: Environment) -> None:
    db = _database()
    activity_id = db.start_activity(SportTypesEnum.running, environment)

    with db.Session() as session:
        activity = session.get(Activity, activity_id)

    assert activity is not None
    assert activity.environment == environment.value


def test_downgrade_upgrade_roundtrip_recreates_environment_and_location_schema(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "v5.db"
    database_url = f"sqlite:///{database_path}"
    db = DatabaseManager(database_url)
    db.start_activity(SportTypesEnum.running, Environment.OUTDOOR)
    db.engine.dispose()

    engine = create_engine(database_url)
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
        transaction = connection.begin()
        try:
            config = database_module._build_alembic_config()  # noqa: SLF001
            config.attributes["connection"] = connection
            command.downgrade(config, "0005")
            transaction.commit()
        except Exception:
            transaction.rollback()
            raise
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    engine.dispose()

    migrated = DatabaseManager(database_url)

    with migrated.Session() as session:
        activity = session.query(Activity).one()

    assert activity.environment == Environment.INDOOR.value
    assert "location_points" in inspect(migrated.engine).get_table_names()


def test_activity_deletion_cascades_to_measurements_and_uploads() -> None:
    db = _database()
    activity_id = db.start_activity(SportTypesEnum.running, Environment.INDOOR)
    with db.Session() as session:
        session.add_all(
            [
                HeartRate(activity_id=activity_id, timestamp_ms=1_000, bpm=140),
                RunningMetrics(
                    activity_id=activity_id,
                    timestamp_ms=1_000,
                    speed_mps=3.0,
                    cadence_spm=170,
                ),
                CyclingMetrics(
                    activity_id=activity_id,
                    timestamp_ms=1_000,
                    speed_mps=8.0,
                ),
                LocationPoint(
                    activity_id=activity_id,
                    timestamp_ms=1_000,
                    latitude_deg=39.7392,
                    longitude_deg=-104.9903,
                ),
                ActivityUpload(
                    activity_id=activity_id,
                    provider="test",
                    status="pending",
                ),
            ],
        )
        session.commit()

    foreign_keys = {
        table: inspect(db.engine).get_foreign_keys(table)[0]["options"].get("ondelete")
        for table in ("heart_rate", "running_metrics", "cycling_metrics", "location_points")
    }
    assert set(foreign_keys.values()) == {"CASCADE"}

    with db.engine.begin() as connection:
        connection.execute(text("DELETE FROM activities WHERE id = :id"), {"id": activity_id})

    with db.Session() as session:
        assert session.query(HeartRate).count() == 0
        assert session.query(RunningMetrics).count() == 0
        assert session.query(CyclingMetrics).count() == 0
        assert session.query(LocationPoint).count() == 0
        assert session.query(ActivityUpload).count() == 0


def test_location_points_are_batched_ordered_and_invalidate_uploads() -> None:
    db = _database()
    activity_id = db.start_activity(SportTypesEnum.running, Environment.OUTDOOR)
    db.repository.mark_upload_ok(activity_id, "test", payload_hash="old-hash")
    first = LocationFix(
        latitude_deg=39.7392,
        longitude_deg=-104.9903,
        accuracy_m=5.0,
    )
    second = LocationFix(
        latitude_deg=39.7393,
        longitude_deg=-104.9902,
        altitude_m=1_600.0,
        source_time_utc=first.source_time_utc,
    )

    db.insert_location_point(activity_id, 1_000, first)
    db.insert_location_point(activity_id, 1_000, second)
    db.stop_activity(activity_id)

    points = db.repository.list_location_points(activity_id)

    assert len(points) == 2
    assert [point.timestamp_ms for point in points] == [1_000, 1_000]
    assert points[0].latitude_deg == first.latitude_deg
    assert points[1].altitude_m == second.altitude_m
    with db.Session() as session:
        upload = session.query(ActivityUpload).one()
    assert upload.status == "pending"
    assert upload.payload_hash is None


def test_failed_mixed_measurement_flush_keeps_rows_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _database()
    activity_id = db.start_activity(SportTypesEnum.running, Environment.OUTDOOR)
    db.insert_heart_rate(activity_id, 1_000, 140, None)
    db.insert_location_point(
        activity_id,
        1_000,
        LocationFix(latitude_deg=39.7392, longitude_deg=-104.9903),
    )
    original_commit = Session.commit
    fail_once = True

    def commit_once(session: Session) -> None:
        nonlocal fail_once
        if fail_once:
            fail_once = False
            session.flush()
            message = "temporary commit failure"
            raise RuntimeError(message)
        original_commit(session)

    monkeypatch.setattr(Session, "commit", commit_once)

    with pytest.raises(RuntimeError, match="temporary commit failure"):
        db.finalize_activity(activity_id)
    assert len(db._pending_hr) == 1  # noqa: SLF001
    assert len(db._pending_location) == 1  # noqa: SLF001

    db.finalize_activity(activity_id)

    assert db._pending_hr == []  # noqa: SLF001
    assert db._pending_location == []  # noqa: SLF001
    with db.Session() as session:
        assert session.query(HeartRate).filter_by(activity_id=activity_id).count() == 1
        assert session.query(LocationPoint).filter_by(activity_id=activity_id).count() == 1
    assert len(db.repository.list_location_points(activity_id)) == 1


def test_migration_secures_sqlite_database_sidecars_and_backup(tmp_path: Path) -> None:
    database_path = tmp_path / "released-v4.db"
    fixture_path = _DATABASE_FIXTURE_DIR / "v4.sql"
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.executescript(fixture_path.read_text())
        connection.execute(
            "CREATE INDEX ix_hr_positive_bpm ON heart_rate (bpm) WHERE bpm > 0",
        )

    DatabaseManager(f"sqlite:///{database_path}")

    backup_path = database_path.with_name(
        f"{database_path.name}.pre-{database_module._alembic_head_revision()}",  # noqa: SLF001
    )
    assert database_path.stat().st_mode & 0o777 == 0o600
    assert backup_path.stat().st_mode & 0o777 == 0o600
    assert "ix_hr_positive_bpm" in {
        index["name"]
        for index in inspect(create_engine(f"sqlite:///{database_path}")).get_indexes(
            "heart_rate",
        )
    }
    for suffix in ("-wal", "-shm"):
        sidecar = database_path.with_name(f"{database_path.name}{suffix}")
        sidecar.touch()
        sidecar.chmod(0o644)
    secure_sqlite_files(database_path)
    assert all(
        database_path.with_name(f"{database_path.name}{suffix}").stat().st_mode & 0o777 == 0o600
        for suffix in ("-wal", "-shm")
    )


@pytest.mark.parametrize("schema_release", [1, 2, 3, 4, 5])
def test_migrations_upgrade_every_shipped_schema(
    tmp_path: Path,
    schema_release: int,
) -> None:
    database_path = tmp_path / f"v{schema_release}.db"
    fixture_path = _DATABASE_FIXTURE_DIR / f"v{schema_release}.sql"
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.executescript(fixture_path.read_text())

    db = DatabaseManager(f"sqlite:///{database_path}")

    with db.Session() as session:
        activity = session.query(Activity).one()
        heart_rate = session.query(HeartRate).one()
        assert activity.public_id is not None
        assert activity.environment == Environment.INDOOR.value
        assert heart_rate.energy_kj == 1.25
        assert session.query(RunningMetrics).count() == int(schema_release >= 2)
        assert session.query(ActivityUpload).count() == int(schema_release >= 2)
        assert session.query(CyclingMetrics).count() == int(schema_release >= 3)
        assert session.query(ActivitySport).count() == int(schema_release >= 3)
        assert session.query(ActivityStats).count() == int(schema_release >= 3)

    migrated_inspector = inspect(db.engine)
    activity_columns = {column["name"] for column in migrated_inspector.get_columns("activities")}
    assert "public_id" in activity_columns
    assert not any(
        constraint.get("column_names") == ["start_time"]
        for constraint in migrated_inspector.get_unique_constraints("activities")
    )
    assert {
        constraint["name"] for constraint in migrated_inspector.get_unique_constraints("activities")
    } >= {"uq_activities_public_id"}
    for table in (
        "heart_rate",
        "running_metrics",
        "cycling_metrics",
        "activity_uploads",
        "location_points",
    ):
        activity_foreign_key = next(
            constraint
            for constraint in migrated_inspector.get_foreign_keys(table)
            if constraint["constrained_columns"] == ["activity_id"]
        )
        assert activity_foreign_key["referred_table"] == "activities"
        assert activity_foreign_key["referred_columns"] == ["id"]
        assert activity_foreign_key["options"].get("ondelete") == "CASCADE"
    assert all(
        column in {item["name"] for item in migrated_inspector.get_columns(table)}
        for table, column in (
            ("running_metrics", "incline_percent"),
            ("running_metrics", "altitude_m"),
            ("cycling_metrics", "incline_percent"),
            ("cycling_metrics", "altitude_m"),
        )
    )
    updated_at = next(
        column
        for column in migrated_inspector.get_columns("activity_uploads")
        if column["name"] == "updated_at"
    )
    assert updated_at["nullable"] is False

    with db.engine.connect() as connection:
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == database_module._alembic_head_revision()  # noqa: SLF001
        )
        assert (
            connection.execute(text("SELECT energy_kj FROM heart_rate WHERE id = 1")).scalar_one()
            == 1.25
        )


@pytest.mark.parametrize(
    ("sport_type", "expected_table"),
    [
        (SportTypesEnum.running, RunningMetrics),
        (SportTypesEnum.biking, CyclingMetrics),
    ],
)
def test_trainer_grade_is_not_persisted_as_altitude(
    sport_type: SportTypesEnum,
    expected_table: type[RunningMetrics] | type[CyclingMetrics],
) -> None:
    db = _database()
    activity_id = db.start_activity(sport_type, Environment.INDOOR)
    sample = _trainer_sample(altitude_m=None)

    if sport_type == SportTypesEnum.running:
        db.insert_running_metrics(activity_id, sample, incline_percent=8.0)
    else:
        db.insert_cycling_metrics(activity_id, sample, incline_percent=8.0)
    db.stop_activity(activity_id)

    with db.Session() as session:
        row = session.query(expected_table).one()

    assert row.altitude_m is None
    assert row.incline_percent == 8.0


def _create_legacy_local_database(path: Path) -> None:
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE activities ("
                "id INTEGER PRIMARY KEY, start_time DATETIME NOT NULL, end_time DATETIME, "
                "CONSTRAINT uq_activities_start_time UNIQUE (start_time))",
            ),
        )
        connection.execute(
            text(
                "CREATE TABLE heart_rate ("
                "id INTEGER PRIMARY KEY, activity_id INTEGER NOT NULL, "
                "timestamp_ms BIGINT NOT NULL, bpm INTEGER NOT NULL, "
                "rr_interval REAL, FOREIGN KEY(activity_id) REFERENCES activities(id))",
            ),
        )
        connection.execute(
            text(
                "INSERT INTO activities (id, start_time, end_time) VALUES "
                "(1, '2026-01-01 08:00:00', '2026-01-01 08:30:00'), "
                "(2, '2026-01-02 08:00:00', '2026-01-02 08:45:00'), "
                "(3, '2026-01-03 08:00:00', NULL)",
            ),
        )
        connection.execute(
            text(
                "INSERT INTO heart_rate "
                "(id, activity_id, timestamp_ms, bpm, rr_interval) "
                "VALUES (1, 2, 1000, 142, 422.5)",
            ),
        )


def test_local_migration_rebuilds_identity_preserves_rows_and_creates_backup(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy.db"
    _create_legacy_local_database(database_path)

    db = DatabaseManager(f"sqlite:///{database_path}")

    with db.Session() as session:
        activities = session.query(Activity).order_by(Activity.id).all()
        heart_rates = session.query(HeartRate).all()
        alembic_revision = session.execute(
            text("SELECT version_num FROM alembic_version"),
        ).scalar_one()

    assert [activity.id for activity in activities] == [1, 2, 3]
    assert len({activity.public_id for activity in activities}) == 3
    assert [(row.activity_id, row.bpm, row.rr_interval) for row in heart_rates] == [
        (2, 142, 422.5),
    ]
    assert alembic_revision == database_module._alembic_head_revision()  # noqa: SLF001

    migrated_inspector = inspect(db.engine)
    assert "public_id" in {
        column["name"] for column in migrated_inspector.get_columns("activities")
    }
    assert not any(
        constraint.get("column_names") == ["start_time"]
        for constraint in migrated_inspector.get_unique_constraints("activities")
    )
    assert any(
        constraint.get("column_names") == ["public_id"]
        for constraint in migrated_inspector.get_unique_constraints("activities")
    )

    backup_path = database_path.with_name(
        f"{database_path.name}.pre-{database_module._alembic_head_revision()}",  # noqa: SLF001
    )
    assert backup_path.is_file()
    assert not list(
        tmp_path.glob(
            f"{backup_path.name}.attempt-*",
        ),
    )
    backup_engine = create_engine(f"sqlite:///{backup_path}")
    backup_inspector = inspect(backup_engine)
    assert "public_id" not in {
        column["name"] for column in backup_inspector.get_columns("activities")
    }
    assert any(
        constraint.get("column_names") == ["start_time"]
        for constraint in backup_inspector.get_unique_constraints("activities")
    )
    with backup_engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM activities")).scalar_one() == 3
        assert connection.execute(text("SELECT COUNT(*) FROM heart_rate")).scalar_one() == 1


def test_migration_retry_preserves_first_known_good_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "legacy.db"
    _create_legacy_local_database(database_path)
    original_migrate = DatabaseManager._migrate  # noqa: SLF001

    def fail_after_partial_ddl(_database_manager, engine) -> None:
        with engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE partial_migration (id INTEGER PRIMARY KEY)"),
            )
        message = "forced migration failure"
        raise RuntimeError(message)

    monkeypatch.setattr(DatabaseManager, "_migrate", fail_after_partial_ddl)
    with pytest.raises(DatabaseMigrationError, match="pre-migration backup"):
        DatabaseManager(f"sqlite:///{database_path}")

    backup_path = database_path.with_name(
        f"{database_path.name}.pre-{database_module._alembic_head_revision()}",  # noqa: SLF001
    )
    first_backup = backup_path.read_bytes()
    backup_inspector = inspect(create_engine(f"sqlite:///{backup_path}"))
    assert "partial_migration" not in backup_inspector.get_table_names()

    monkeypatch.setattr(DatabaseManager, "_migrate", original_migrate)
    db = DatabaseManager(f"sqlite:///{database_path}")

    assert backup_path.read_bytes() == first_backup
    assert "partial_migration" in inspect(db.engine).get_table_names()


def test_migration_retry_snapshots_database_state_for_each_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "legacy.db"
    _create_legacy_local_database(database_path)

    def fail_migration(_database_manager, _engine) -> None:
        message = "forced migration failure"
        raise RuntimeError(message)

    monkeypatch.setattr(DatabaseManager, "_migrate", fail_migration)
    with pytest.raises(DatabaseMigrationError, match="attempt-"):
        DatabaseManager(f"sqlite:///{database_path}")

    immutable_path = database_path.with_name(
        f"legacy.db.pre-{database_module._alembic_head_revision()}",  # noqa: SLF001
    )
    first_attempts = sorted(
        tmp_path.glob(
            f"legacy.db.pre-{database_module._alembic_head_revision()}.attempt-*",  # noqa: SLF001
        ),
    )
    assert len(first_attempts) == 1

    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.execute(
            "INSERT INTO activities (id, start_time, end_time) VALUES "
            "(4, '2026-01-04 08:00:00', '2026-01-04 08:30:00')",
        )

    with pytest.raises(DatabaseMigrationError, match="attempt-"):
        DatabaseManager(f"sqlite:///{database_path}")

    attempt_paths = sorted(
        tmp_path.glob(
            f"legacy.db.pre-{database_module._alembic_head_revision()}.attempt-*",  # noqa: SLF001
        ),
    )
    assert len(attempt_paths) == 2
    with create_engine(f"sqlite:///{immutable_path}").connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM activities")).scalar_one() == 3
    with create_engine(f"sqlite:///{attempt_paths[0]}").connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM activities")).scalar_one() == 3
    with create_engine(f"sqlite:///{attempt_paths[1]}").connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM activities")).scalar_one() == 4


def test_migration_attempt_snapshots_are_retained_within_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "legacy.db"
    _create_legacy_local_database(database_path)

    def fail_migration(_database_manager, _engine) -> None:
        message = "forced migration failure"
        raise RuntimeError(message)

    monkeypatch.setattr(DatabaseManager, "_migrate", fail_migration)
    for _attempt in range(database_module.MIGRATION_ATTEMPT_RETENTION + 2):
        with pytest.raises(DatabaseMigrationError, match="attempt-"):
            DatabaseManager(f"sqlite:///{database_path}")

    immutable_path = database_path.with_name(
        f"legacy.db.pre-{database_module._alembic_head_revision()}",  # noqa: SLF001
    )
    attempt_paths = sorted(
        tmp_path.glob(
            f"legacy.db.pre-{database_module._alembic_head_revision()}.attempt-*",  # noqa: SLF001
        ),
    )
    assert len(attempt_paths) == database_module.MIGRATION_ATTEMPT_RETENTION
    assert immutable_path.is_file()
    with create_engine(f"sqlite:///{immutable_path}").connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM activities")).scalar_one() == 3


def test_migration_backup_is_keyed_by_target_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "legacy.db"
    _create_legacy_local_database(database_path)
    engine = create_engine(f"sqlite:///{database_path}")

    monkeypatch.setattr(database_module, "_alembic_head_revision", lambda: "0004")
    first_backup = DatabaseManager._backup_before_migration(engine)  # noqa: SLF001

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO activities (id, start_time, end_time) "
                "VALUES (4, '2025-01-02 00:00:00', '2025-01-02 00:30:00')",
            ),
        )

    monkeypatch.setattr(database_module, "_alembic_head_revision", lambda: "next-head")
    second_backup = DatabaseManager._backup_before_migration(engine)  # noqa: SLF001

    assert first_backup.name == "legacy.db.pre-0004"
    assert second_backup.name == "legacy.db.pre-next-head"
    assert first_backup != second_backup
    with create_engine(f"sqlite:///{first_backup}").connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM activities")).scalar_one() == 3
    with create_engine(f"sqlite:///{second_backup}").connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM activities")).scalar_one() == 4


def test_measurement_migration_fails_closed_when_backup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "legacy.db"
    _create_legacy_local_database(database_path)

    def fail_backup(_engine) -> Path:
        message = "backup unavailable"
        raise OSError(message)

    monkeypatch.setattr(
        DatabaseManager,
        "_backup_before_migration",
        staticmethod(fail_backup),
    )

    with pytest.raises(DatabaseMigrationError, match="no pre-migration backup"):
        DatabaseManager(f"sqlite:///{database_path}")

    engine = create_engine(f"sqlite:///{database_path}")
    assert "alembic_version" not in inspect(engine).get_table_names()


def _process_heart_rates(samples: list[HeartRateSample]) -> list[NormalizedHeartRate]:
    """Exercise the SampleProcessor heart-rate normalization contract."""
    processor = SampleProcessor()
    normalized = [processor.process_heart_rate(sample) for sample in samples]
    assert all(isinstance(sample, NormalizedHeartRate) for sample in normalized)
    return normalized


def test_heart_rate_contract_uses_explicit_fields() -> None:
    normalized = _process_heart_rates(
        [
            HeartRateSample(
                timestamp_ms=1_000,
                heart_rate_bpm=142,
                rr_interval_ms=422.5,
            ),
        ],
    )

    sample = normalized[0]
    assert sample.timestamp_ms == 1_000
    assert sample.bpm == 142
    assert sample.rr_interval_ms == 422.5
