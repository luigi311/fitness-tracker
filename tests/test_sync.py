from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from fitness_tracker import database as database_module
from fitness_tracker.activity_stats import StatsCalculator
from fitness_tracker.core.environment import Environment
from fitness_tracker.core.errors import UserActionableError
from fitness_tracker.core.file_permissions import PRIVATE_FILE_MODE
from fitness_tracker.core.sports import SportTypesEnum
from fitness_tracker.data.models import (
    Activity,
    ActivitySport,
    ActivityStats,
    ActivityUpload,
    HeartRate,
    LocationPoint,
)
from fitness_tracker.data.sqlite_files import prepare_private_sqlite_database, secure_sqlite_files
from fitness_tracker.data.sync import DatabaseSynchronizer
from fitness_tracker.database import (
    DatabaseConnectionError,
    DatabaseManager,
    DatabaseMigrationError,
)
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

PROVIDER = "intervals_icu"
START_TIME = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
EXPECTED_ACTIVITY_COUNT = 2
EXPECTED_HEART_RATE_BPM = 140
EXPECTED_HARDEN_CALL_COUNT = 2
EXPECTED_POINTS_AFTER_LOCAL_SYNC = 3
EXPECTED_POINTS_AFTER_BIDIRECTIONAL_SYNC = 4
EXPECTED_MERGED_ROUTE_POINTS = 2


def _manager(path: Path) -> DatabaseManager:
    return DatabaseManager(f"sqlite:///{path}")


def _insert_activity(
    db: DatabaseManager,
    public_id: UUID,
    *,
    start_time: datetime = START_TIME,
    end_time: datetime | None = None,
    environment: Environment = Environment.INDOOR,
) -> int:
    with db.Session() as session:
        activity = Activity(
            public_id=public_id,
            start_time=start_time,
            end_time=end_time,
            environment=environment.value,
        )
        session.add(activity)
        session.commit()
        return int(activity.id)


def _activity(db: DatabaseManager, public_id: UUID) -> Activity:
    with db.Session() as session:
        return session.query(Activity).filter_by(public_id=public_id).one()


def _create_legacy_remote(path: Path) -> None:
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE activities ("
                "id INTEGER PRIMARY KEY, start_time DATETIME NOT NULL, "
                "end_time DATETIME, UNIQUE (start_time))",
            ),
        )
        connection.execute(
            text(
                "CREATE TABLE heart_rate ("
                "id INTEGER PRIMARY KEY, activity_id INTEGER NOT NULL, "
                "timestamp_ms BIGINT NOT NULL, bpm INTEGER NOT NULL, "
                "rr_interval REAL)",
            ),
        )
        connection.execute(
            text(
                "INSERT INTO activities (id, start_time, end_time) "
                "VALUES (1, '2025-01-01 00:00:00', '2025-01-01 00:30:00')",
            ),
        )
        connection.execute(
            text(
                "INSERT INTO heart_rate (activity_id, timestamp_ms, bpm) VALUES (1, 1000, 140)",
            ),
        )


def test_sync_migrates_legacy_sqlite_remote_with_backup(tmp_path: Path) -> None:
    local = _manager(tmp_path / "local.db")
    remote_path = tmp_path / "legacy-remote.db"
    _create_legacy_remote(remote_path)
    public_id = uuid4()
    _insert_activity(local, public_id)
    activity_id = _activity(local, public_id).id
    with local.Session() as session:
        session.add(
            LocationPoint(
                activity_id=activity_id,
                timestamp_ms=1_000,
                latitude_deg=39.7392,
                longitude_deg=-104.9903,
            ),
        )
        session.commit()

    local.sync_to_database(f"sqlite:///{remote_path}")

    remote_engine = create_engine(f"sqlite:///{remote_path}")
    assert "public_id" in {
        column["name"] for column in inspect(remote_engine).get_columns("activities")
    }
    assert remote_path.with_name(
        f"{remote_path.name}.pre-{database_module._alembic_head_revision()}",  # noqa: SLF001
    ).is_file()
    remote = _manager(remote_path)
    assert _activity(remote, public_id).public_id == public_id
    with remote.Session() as session:
        assert session.query(Activity).count() == EXPECTED_ACTIVITY_COUNT
        assert (
            session.query(HeartRate).filter_by(timestamp_ms=1000).one().bpm
            == EXPECTED_HEART_RATE_BPM
        )
        assert session.query(LocationPoint).count() == 1


def test_sync_transfers_environment_for_new_activity(tmp_path: Path) -> None:
    local = _manager(tmp_path / "local.db")
    remote = _manager(tmp_path / "remote.db")
    public_id = uuid4()
    _insert_activity(local, public_id, environment=Environment.OUTDOOR)

    local.sync_to_database(f"sqlite:///{tmp_path / 'remote.db'}")

    assert _activity(remote, public_id).environment == Environment.OUTDOOR.value


def test_sync_preserves_location_duplicates_and_converges_both_directions(
    tmp_path: Path,
) -> None:
    local = _manager(tmp_path / "local.db")
    remote = _manager(tmp_path / "remote.db")
    public_id = uuid4()
    local_id = _insert_activity(local, public_id, environment=Environment.OUTDOOR)
    remote_id = _insert_activity(remote, public_id, environment=Environment.OUTDOOR)
    first = LocationPoint(
        activity_id=local_id,
        timestamp_ms=1_000,
        latitude_deg=39.7392,
        longitude_deg=-104.9903,
    )
    duplicate = LocationPoint(
        activity_id=local_id,
        timestamp_ms=1_000,
        latitude_deg=39.7392,
        longitude_deg=-104.9903,
    )
    second = LocationPoint(
        activity_id=local_id,
        timestamp_ms=1_000,
        latitude_deg=39.7393,
        longitude_deg=-104.9903,
    )
    with local.Session() as session:
        session.add_all([first, duplicate, second])
        session.commit()

    remote_url = f"sqlite:///{tmp_path / 'remote.db'}"
    local.sync_to_database(remote_url)

    with remote.Session() as session:
        remote_points = (
            session.query(LocationPoint)
            .filter_by(activity_id=remote_id)
            .order_by(LocationPoint.timestamp_ms, LocationPoint.id)
            .all()
        )
    assert len(remote_points) == EXPECTED_POINTS_AFTER_LOCAL_SYNC
    assert [(point.timestamp_ms, point.latitude_deg) for point in remote_points] == [
        (1_000, 39.7392),
        (1_000, 39.7392),
        (1_000, 39.7393),
    ]

    with remote.Session() as session:
        session.add(
            LocationPoint(
                activity_id=remote_id,
                timestamp_ms=2_000,
                latitude_deg=39.7394,
                longitude_deg=-104.9903,
            ),
        )
        session.commit()

    local.sync_to_database(remote_url)
    # A repeated pass verifies that the merged location multiset is idempotent.
    local.sync_to_database(remote_url)

    with local.Session() as session:
        local_points = (
            session.query(LocationPoint)
            .filter_by(activity_id=local_id)
            .order_by(LocationPoint.timestamp_ms, LocationPoint.id)
            .all()
        )
    assert len(local_points) == EXPECTED_POINTS_AFTER_BIDIRECTIONAL_SYNC
    assert [(point.timestamp_ms, point.latitude_deg) for point in local_points] == [
        (1_000, 39.7392),
        (1_000, 39.7392),
        (1_000, 39.7393),
        (2_000, 39.7394),
    ]


def test_syncing_new_measurements_invalidates_destination_upload(
    tmp_path: Path,
) -> None:
    local = _manager(tmp_path / "local.db")
    remote = _manager(tmp_path / "remote.db")
    public_id = uuid4()
    local_id = _insert_activity(
        local,
        public_id,
        end_time=START_TIME + timedelta(minutes=30),
        environment=Environment.OUTDOOR,
    )
    remote_id = _insert_activity(
        remote,
        public_id,
        end_time=START_TIME + timedelta(minutes=30),
        environment=Environment.OUTDOOR,
    )
    remote.repository.mark_upload_ok(
        remote_id,
        PROVIDER,
        provider_activity_id="remote-activity",
        payload_hash="old-route-hash",
    )
    with local.Session() as session:
        session.add(
            LocationPoint(
                activity_id=local_id,
                timestamp_ms=1_000,
                latitude_deg=39.7392,
                longitude_deg=-104.9903,
            ),
        )
        session.commit()

    local.sync_to_database(f"sqlite:///{tmp_path / 'remote.db'}")

    with remote.Session() as session:
        upload = session.query(ActivityUpload).filter_by(activity_id=remote_id).one()
        assert upload.status == "pending"
        assert upload.payload_hash is None
        assert upload.provider_activity_id == "remote-activity"


def test_sync_preserves_source_successful_upload_on_fresh_peer(tmp_path: Path) -> None:
    local = _manager(tmp_path / "local.db")
    remote = _manager(tmp_path / "remote.db")
    public_id = uuid4()
    local_id = _insert_activity(
        local,
        public_id,
        end_time=START_TIME + timedelta(minutes=30),
        environment=Environment.OUTDOOR,
    )
    _insert_activity(
        remote,
        public_id,
        end_time=START_TIME + timedelta(minutes=30),
        environment=Environment.OUTDOOR,
    )
    with local.Session() as session:
        session.add(
            LocationPoint(
                activity_id=local_id,
                timestamp_ms=1_000,
                latitude_deg=39.7392,
                longitude_deg=-104.9903,
            ),
        )
        session.commit()
    local.repository.mark_upload_ok(
        local_id,
        PROVIDER,
        provider_activity_id="already-uploaded",
        payload_hash="route-hash",
    )

    local.sync_to_database(f"sqlite:///{tmp_path / 'remote.db'}")

    with remote.Session() as session:
        upload = session.query(ActivityUpload).one()
        assert upload.status == "ok"
        assert upload.payload_hash == "route-hash"
        assert upload.provider_activity_id == "already-uploaded"
    with local.Session() as session:
        upload = session.query(ActivityUpload).one()
        assert upload.status == "ok"
        assert upload.payload_hash == "route-hash"


def test_sync_invalidates_uploads_for_divergent_routes(tmp_path: Path) -> None:
    local = _manager(tmp_path / "local.db")
    remote = _manager(tmp_path / "remote.db")
    public_id = uuid4()
    local_id = _insert_activity(
        local,
        public_id,
        end_time=START_TIME + timedelta(minutes=30),
        environment=Environment.OUTDOOR,
    )
    remote_id = _insert_activity(
        remote,
        public_id,
        end_time=START_TIME + timedelta(minutes=30),
        environment=Environment.OUTDOOR,
    )
    with local.Session() as session:
        session.add(
            LocationPoint(
                activity_id=local_id,
                timestamp_ms=1_000,
                latitude_deg=39.7392,
                longitude_deg=-104.9903,
            ),
        )
        session.commit()
    with remote.Session() as session:
        session.add(
            LocationPoint(
                activity_id=remote_id,
                timestamp_ms=1_000,
                latitude_deg=39.7402,
                longitude_deg=-104.9903,
            ),
        )
        session.commit()
    local.repository.mark_upload_ok(
        local_id,
        PROVIDER,
        provider_activity_id="local-upload",
        payload_hash="hash-a",
    )
    remote.repository.mark_upload_ok(
        remote_id,
        PROVIDER,
        provider_activity_id="remote-upload",
        payload_hash="hash-b",
    )

    local.sync_to_database(f"sqlite:///{tmp_path / 'remote.db'}")

    with local.Session() as session:
        upload = session.query(ActivityUpload).one()
        assert upload.status == "pending"
        assert upload.payload_hash is None
        assert session.query(LocationPoint).count() == EXPECTED_MERGED_ROUTE_POINTS
    with remote.Session() as session:
        upload = session.query(ActivityUpload).one()
        assert upload.status == "pending"
        assert upload.payload_hash is None
        assert session.query(LocationPoint).count() == EXPECTED_MERGED_ROUTE_POINTS


def test_sync_invalidates_when_newer_destination_upload_wins(tmp_path: Path) -> None:
    local = _manager(tmp_path / "local.db")
    remote = _manager(tmp_path / "remote.db")
    public_id = uuid4()
    local_id = _insert_activity(
        local,
        public_id,
        end_time=START_TIME + timedelta(minutes=30),
        environment=Environment.OUTDOOR,
    )
    remote_id = _insert_activity(
        remote,
        public_id,
        end_time=START_TIME + timedelta(minutes=30),
        environment=Environment.OUTDOOR,
    )
    with local.Session() as session:
        session.add(
            LocationPoint(
                activity_id=local_id,
                timestamp_ms=1_000,
                latitude_deg=39.7392,
                longitude_deg=-104.9903,
            ),
        )
        session.add(
            ActivityUpload(
                activity_id=local_id,
                provider=PROVIDER,
                status="ok",
                uploaded_at=START_TIME + timedelta(seconds=1),
                updated_at=START_TIME + timedelta(seconds=1),
                provider_activity_id="full-upload",
                payload_hash="full-hash",
            ),
        )
        session.commit()
    with remote.Session() as session:
        session.add(
            ActivityUpload(
                activity_id=remote_id,
                provider=PROVIDER,
                status="ok",
                uploaded_at=START_TIME + timedelta(seconds=2),
                updated_at=START_TIME + timedelta(seconds=2),
                provider_activity_id="empty-upload",
                payload_hash="empty-hash",
            ),
        )
        session.commit()

    local.sync_to_database(f"sqlite:///{tmp_path / 'remote.db'}")

    for manager in (local, remote):
        with manager.Session() as session:
            upload = session.query(ActivityUpload).one()
            assert upload.status == "pending"
            assert upload.payload_hash is None


def test_sync_invalidates_uploads_when_tcx_metadata_changes(tmp_path: Path) -> None:
    local = _manager(tmp_path / "local.db")
    remote = _manager(tmp_path / "remote.db")
    public_id = uuid4()
    local_id = _insert_activity(
        local,
        public_id,
        end_time=START_TIME + timedelta(minutes=30),
        environment=Environment.OUTDOOR,
    )
    remote_id = _insert_activity(
        remote,
        public_id,
        end_time=START_TIME + timedelta(minutes=20),
        environment=Environment.OUTDOOR,
    )
    with local.Session() as session:
        session.add_all(
            [
                ActivitySport(activity_id=local_id, sport_type_id=SportTypesEnum.running.value),
                ActivityUpload(
                    activity_id=local_id,
                    provider=PROVIDER,
                    status="ok",
                    uploaded_at=START_TIME + timedelta(seconds=1),
                    updated_at=START_TIME + timedelta(seconds=1),
                    provider_activity_id="running-upload",
                    payload_hash="running-hash",
                ),
            ],
        )
        session.commit()
    with remote.Session() as session:
        session.add_all(
            [
                ActivitySport(activity_id=remote_id, sport_type_id=SportTypesEnum.biking.value),
                ActivityUpload(
                    activity_id=remote_id,
                    provider=PROVIDER,
                    status="ok",
                    uploaded_at=START_TIME + timedelta(seconds=2),
                    updated_at=START_TIME + timedelta(seconds=2),
                    provider_activity_id="biking-upload",
                    payload_hash="biking-hash",
                ),
            ],
        )
        session.commit()
    StatsCalculator(local).compute_for_activity(local_id)
    StatsCalculator(remote).compute_for_activity(remote_id)

    local.sync_to_database(f"sqlite:///{tmp_path / 'remote.db'}")

    for manager in (local, remote):
        with manager.Session() as session:
            activity = session.query(Activity).one()
            sport = session.query(ActivitySport).one()
            stats = session.query(ActivityStats).one()
            upload = session.query(ActivityUpload).one()
            assert activity.end_time is not None
            assert activity.end_time.replace(tzinfo=UTC) == START_TIME + timedelta(minutes=30)
            assert sport.sport_type_id == SportTypesEnum.running.value
            assert stats.sport_type_id == SportTypesEnum.running.value
            assert stats.duration_s == 30 * 60
            assert stats.end_time is not None
            assert stats.end_time.replace(tzinfo=UTC) == START_TIME + timedelta(minutes=30)
            assert upload.status == "pending"
            assert upload.payload_hash is None


def test_sync_replaces_migration_default_environment_on_conflict(tmp_path: Path) -> None:
    local = _manager(tmp_path / "local.db")
    remote = _manager(tmp_path / "remote.db")
    public_id = uuid4()
    _insert_activity(local, public_id, environment=Environment.OUTDOOR)
    _insert_activity(remote, public_id, environment=Environment.INDOOR)

    local.sync_to_database(f"sqlite:///{tmp_path / 'remote.db'}")

    assert _activity(local, public_id).environment == Environment.OUTDOOR.value
    assert _activity(remote, public_id).environment == Environment.OUTDOOR.value


def test_reverse_sync_does_not_replay_forward_upload_invalidation(tmp_path: Path) -> None:
    local = _manager(tmp_path / "local.db")
    remote = _manager(tmp_path / "remote.db")
    public_id = uuid4()
    local_id = _insert_activity(
        local,
        public_id,
        end_time=START_TIME + timedelta(minutes=30),
        environment=Environment.OUTDOOR,
    )
    remote_id = _insert_activity(
        remote,
        public_id,
        end_time=START_TIME + timedelta(minutes=30),
        environment=Environment.OUTDOOR,
    )
    uploaded_at = START_TIME + timedelta(minutes=31)
    for manager, activity_id in ((local, local_id), (remote, remote_id)):
        with manager.Session() as session:
            session.add(
                ActivityUpload(
                    activity_id=activity_id,
                    provider=PROVIDER,
                    status="ok",
                    uploaded_at=uploaded_at,
                    updated_at=uploaded_at,
                    provider_activity_id="shared-upload",
                    payload_hash="complete-route-hash",
                ),
            )
            session.commit()
    with local.Session() as session:
        session.add(
            LocationPoint(
                activity_id=local_id,
                timestamp_ms=1_000,
                latitude_deg=39.7392,
                longitude_deg=-104.9903,
            ),
        )
        session.commit()

    local.sync_to_database(f"sqlite:///{tmp_path / 'remote.db'}")

    with local.Session() as session:
        upload = session.query(ActivityUpload).one()
        assert upload.status == "ok"
        assert upload.payload_hash == "complete-route-hash"
        assert upload.provider_activity_id == "shared-upload"
    assert local.repository.list_not_uploaded(PROVIDER) == []
    with remote.Session() as session:
        upload = session.query(ActivityUpload).one()
        assert upload.status == "pending"
        assert upload.payload_hash is None


def test_sync_normalizes_backend_timestamp_representation(tmp_path: Path) -> None:
    source = _manager(tmp_path / "source.db")
    destination = _manager(tmp_path / "destination.db")
    public_id = uuid4()
    source_id = _insert_activity(source, public_id, environment=Environment.OUTDOOR)
    destination_id = _insert_activity(destination, public_id, environment=Environment.OUTDOOR)
    source_time = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    # Both databases are SQLite; the explicit aware/naive values below simulate the
    # representation mismatch that occurs when synchronizing SQLite with PostgreSQL.
    with source.Session() as session:
        session.add(
            LocationPoint(
                activity_id=source_id,
                timestamp_ms=1_000,
                latitude_deg=39.7392,
                longitude_deg=-104.9903,
                source_time_utc=source_time,
            ),
        )
        session.commit()
    with destination.Session() as session:
        session.add(
            LocationPoint(
                activity_id=destination_id,
                timestamp_ms=1_000,
                latitude_deg=39.7392,
                longitude_deg=-104.9903,
                source_time_utc=source_time.replace(tzinfo=None),
            ),
        )
        session.commit()

    with source.Session() as source_session, destination.Session() as destination_session:
        source_point = source_session.query(LocationPoint).one()
        source_point.source_time_utc = source_time
        with source_session.no_autoflush, destination_session.no_autoflush:
            DatabaseSynchronizer.reconcile_sessions(
                source_session,
                destination_session,
                Mock(),
            )
        destination_session.commit()

    with destination.Session() as session:
        points = session.query(LocationPoint).filter_by(activity_id=destination_id).all()
    assert len(points) == 1


def test_sync_hardens_remote_sqlite_database(tmp_path: Path) -> None:
    local = _manager(tmp_path / "local.db")
    remote_path = tmp_path / "remote.db"
    public_id = uuid4()
    _insert_activity(local, public_id)

    local.sync_to_database(f"sqlite:///{remote_path}")
    assert remote_path.stat().st_mode & 0o777 == PRIVATE_FILE_MODE

    remote_path.chmod(0o644)
    local.sync_to_database(f"sqlite:///{remote_path}")
    assert remote_path.stat().st_mode & 0o777 == PRIVATE_FILE_MODE


def test_sqlite_hardening_rejects_non_regular_paths(tmp_path: Path) -> None:
    database_path = tmp_path / "database.db"
    directory_mode = 0o755
    database_path.mkdir()
    database_path.chmod(directory_mode)

    with pytest.raises(OSError, match="not a regular file"):
        prepare_private_sqlite_database(database_path)
    assert database_path.stat().st_mode & 0o777 == directory_mode

    unrelated_path = tmp_path / "unrelated.txt"
    unrelated_mode = 0o644
    unrelated_path.write_text("private")
    unrelated_path.chmod(unrelated_mode)
    backup_path = tmp_path / "remote.db.pre-attacker"
    backup_path.symlink_to(unrelated_path)

    with pytest.raises(OSError, match="symbolic links"):
        secure_sqlite_files(tmp_path / "remote.db")
    assert unrelated_path.stat().st_mode & 0o777 == unrelated_mode


def test_missing_sqlite_parent_errors_are_user_actionable(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing" / "database.db"

    with pytest.raises(DatabaseMigrationError, match="prepared securely"):
        DatabaseManager(f"sqlite:///{missing_path}")

    local = _manager(tmp_path / "local.db")
    with pytest.raises(DatabaseConnectionError, match="prepare remote SQLite"):
        local.sync_to_database(f"sqlite:///{missing_path}")


def test_upload_candidates_require_finalized_activities(tmp_path: Path) -> None:
    db = _manager(tmp_path / "database.db")
    activity_id = db.start_activity(SportTypesEnum.running, Environment.INDOOR)
    db.repository.mark_upload_ok(activity_id, PROVIDER, provider_activity_id="partial-upload")

    assert db.repository.list_not_uploaded(PROVIDER) == []

    db.stop_activity(activity_id)

    with db.Session() as session:
        upload = session.query(ActivityUpload).filter_by(activity_id=activity_id).one()
        assert upload.status == "pending"
    assert [activity.id for activity in db.repository.list_not_uploaded(PROVIDER)] == [activity_id]


def test_stop_activity_preserves_end_time_when_retrying(tmp_path: Path) -> None:
    db = _manager(tmp_path / "database.db")
    activity_id = db.start_activity(SportTypesEnum.running, Environment.INDOOR)
    stopped_at = START_TIME + timedelta(minutes=30)

    with db.Session() as session:
        activity = session.get(Activity, activity_id)
        assert activity is not None
        activity.end_time = stopped_at
        session.commit()

    db.stop_activity(activity_id)

    with db.Session() as session:
        activity = session.get(Activity, activity_id)
        assert activity is not None
        assert activity.end_time is not None
        assert activity.end_time.replace(tzinfo=UTC) == stopped_at


def test_stop_activity_retry_preserves_successful_upload(tmp_path: Path) -> None:
    db = _manager(tmp_path / "database.db")
    activity_id = db.start_activity(SportTypesEnum.running, Environment.INDOOR)
    db.stop_activity(activity_id)
    db.repository.mark_upload_ok(activity_id, PROVIDER, provider_activity_id="remote-1")

    db.stop_activity(activity_id)

    with db.Session() as session:
        upload = session.query(ActivityUpload).filter_by(activity_id=activity_id).one()
        assert upload.status == "ok"


def test_new_samples_invalidate_successful_uploads(tmp_path: Path) -> None:
    db = _manager(tmp_path / "database.db")
    activity_id = db.start_activity(SportTypesEnum.running, Environment.INDOOR)
    db.stop_activity(activity_id)
    db.repository.mark_upload_ok(
        activity_id,
        PROVIDER,
        provider_activity_id="remote-1",
        payload_hash="old-hash",
    )

    db.insert_heart_rate(activity_id, 1_000, 140, None)
    db._flush_pending()  # noqa: SLF001

    with db.Session() as session:
        upload = session.query(ActivityUpload).filter_by(activity_id=activity_id).one()
        assert upload.status == "pending"
        assert upload.payload_hash is None
        assert upload.provider_activity_id == "remote-1"
    assert [activity.id for activity in db.repository.list_not_uploaded(PROVIDER)] == [activity_id]


def test_accepted_upload_state_is_recoverable_and_invalidated_by_new_samples(
    tmp_path: Path,
) -> None:
    db = _manager(tmp_path / "database.db")
    activity_id = db.start_activity(SportTypesEnum.running, Environment.INDOOR)
    db.stop_activity(activity_id)
    db.repository.mark_upload_accepted(
        activity_id,
        PROVIDER,
        provider_activity_id="remote-1",
        payload_hash="accepted-hash",
        error_message="local success update failed",
    )

    upload = db.repository.get_activity_upload(activity_id, PROVIDER)
    assert upload is not None
    assert upload.status == "accepted"
    assert upload.provider_activity_id == "remote-1"
    assert [activity.id for activity in db.repository.list_not_uploaded(PROVIDER)] == [activity_id]

    db.insert_heart_rate(activity_id, 1_000, 140, None)
    db._flush_pending()  # noqa: SLF001

    upload = db.repository.get_activity_upload(activity_id, PROVIDER)
    assert upload is not None
    assert upload.status == "pending"
    assert upload.payload_hash is None


@pytest.mark.parametrize(
    ("payload_case", "expected_status"),
    [("matching", "accepted"), ("different", "pending")],
)
def test_reconcile_sessions_preserves_accepted_upload_only_for_matching_payloads(
    tmp_path: Path,
    payload_case: str,
    expected_status: str,
) -> None:
    source = _manager(tmp_path / "source.db")
    destination = _manager(tmp_path / "destination.db")
    public_id = uuid4()
    source_id = _insert_activity(source, public_id)
    destination_id = _insert_activity(destination, public_id)
    with source.Session() as session:
        session.add(
            ActivityUpload(
                activity_id=source_id,
                provider=PROVIDER,
                status="accepted",
                uploaded_at=START_TIME,
                updated_at=START_TIME,
                provider_activity_id="remote-1",
                payload_hash="accepted-hash",
                last_error="local success update failed",
            ),
        )
        session.commit()
    if payload_case == "different":
        with destination.Session() as session:
            session.add(HeartRate(activity_id=destination_id, timestamp_ms=1_000, bpm=140))
            session.commit()

    with source.Session() as source_session, destination.Session() as destination_session:
        DatabaseSynchronizer.reconcile_sessions(
            source_session,
            destination_session,
            Mock(),
        )
        destination_session.commit()

    with destination.Session() as session:
        upload = session.query(ActivityUpload).filter_by(activity_id=destination_id).one()
    assert upload.status == expected_status


def test_concurrent_upload_updates_share_one_provider_row(tmp_path: Path) -> None:
    db = _manager(tmp_path / "database.db")
    activity_id = db.start_activity(SportTypesEnum.running, Environment.INDOOR)
    workers = 6
    barrier = Barrier(workers, timeout=5.0)

    def update_upload(index: int) -> None:
        barrier.wait()
        if index % 2:
            db.repository.mark_upload_failed(
                activity_id,
                PROVIDER,
                f"failure-{index}",
                payload_hash=f"hash-{index}",
            )
        else:
            db.repository.mark_upload_ok(
                activity_id,
                PROVIDER,
                provider_activity_id=f"remote-{index}",
                payload_hash=f"hash-{index}",
            )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        list(executor.map(update_upload, range(workers)))

    with db.Session() as session:
        uploads = session.query(ActivityUpload).filter_by(activity_id=activity_id).all()
    assert len(uploads) == 1
    assert uploads[0].status in {"ok", "failed"}
    assert uploads[0].payload_hash is not None


def test_failed_activity_stats_excludes_pending_and_successful_uploads(tmp_path: Path) -> None:
    db = _manager(tmp_path / "database.db")
    failed_id = db.start_activity(SportTypesEnum.running, Environment.INDOOR)
    successful_id = db.start_activity(SportTypesEnum.running, Environment.INDOOR)
    pending_id = db.start_activity(SportTypesEnum.running, Environment.INDOOR)
    for activity_id in (failed_id, successful_id, pending_id):
        db.finalize_activity(activity_id)

    db.repository.mark_upload_failed(failed_id, PROVIDER, "network unavailable")
    db.repository.mark_upload_failed(failed_id, "another_provider", "service unavailable")
    db.repository.mark_upload_ok(successful_id, PROVIDER)

    rows = db.repository.list_failed_activity_stats()

    assert [row.activity_id for row in rows] == [failed_id]


def test_failure_does_not_overwrite_successful_upload(tmp_path: Path) -> None:
    db = _manager(tmp_path / "database.db")
    activity_id = db.start_activity(SportTypesEnum.running, Environment.INDOOR)
    db.repository.mark_upload_ok(
        activity_id,
        PROVIDER,
        provider_activity_id="remote-success",
        payload_hash="success-hash",
    )

    db.repository.mark_upload_failed(
        activity_id,
        PROVIDER,
        "late worker failure",
        payload_hash="failed-hash",
    )

    with db.Session() as session:
        upload = session.query(ActivityUpload).filter_by(activity_id=activity_id).one()
    assert upload.status == "ok"
    assert upload.provider_activity_id == "remote-success"
    assert upload.payload_hash == "success-hash"
    assert upload.last_error is None


def test_sync_translates_remote_artifact_hardening_errors(tmp_path: Path) -> None:
    local = _manager(tmp_path / "local.db")
    victim_path = tmp_path / "victim.txt"
    victim_mode = 0o644
    victim_path.write_text("private")
    victim_path.chmod(victim_mode)
    backup_path = tmp_path / "remote.db.pre-attacker"
    backup_path.symlink_to(victim_path)

    with pytest.raises(DatabaseConnectionError, match="secure remote SQLite"):
        local.sync_to_database(f"sqlite:///{tmp_path / 'remote.db'}")
    assert victim_path.stat().st_mode & 0o777 == victim_mode


def test_remote_cleanup_error_does_not_mask_sync_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local = _manager(tmp_path / "local.db")
    harden = Mock(side_effect=[None, OSError("cleanup failed")])
    monkeypatch.setattr("fitness_tracker.data.sync.secure_sqlite_files", harden)

    def fail_sync(
        _source: Session,
        _destination: Session,
        *,
        rebuild_stats: object,
    ) -> None:
        del rebuild_stats
        message = "sync failed"
        raise RuntimeError(message)

    monkeypatch.setattr(
        DatabaseSynchronizer,
        "reconcile_sessions",
        staticmethod(fail_sync),
    )

    with pytest.raises(RuntimeError, match="sync failed"):
        local.sync_to_database(f"sqlite:///{tmp_path / 'remote.db'}")
    assert harden.call_count == EXPECTED_HARDEN_CALL_COUNT


def test_sync_disposes_remote_engine_when_preparation_fails() -> None:
    engine = create_engine("sqlite:///:memory:")
    dispose = Mock(wraps=engine.dispose)
    engine.dispose = dispose

    def fail_prepare(_engine) -> None:
        message = "prepare failed"
        raise RuntimeError(message)

    synchronizer = DatabaseSynchronizer(
        prepare_remote_database=fail_prepare,
        local_session_factory=Session,
        sync_direction=lambda _source, _destination: None,
        engine_factory=lambda _dsn: engine,
    )

    with pytest.raises(RuntimeError, match="prepare failed"):
        synchronizer.sync("sqlite:///:memory:")

    dispose.assert_called_once_with()


def test_sync_rejects_legacy_postgresql_without_explicit_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local = _manager(tmp_path / "local.db")

    class FakeUrl:
        def get_backend_name(self) -> str:
            return "postgresql"

    class FakeEngine:
        url = FakeUrl()

        def dispose(self):
            return None

        def connect(self):
            class Connection:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return None

            return Connection()

    def fake_create_engine(*_args, **_kwargs):
        return FakeEngine()

    def schema_needs_migration(_engine) -> bool:
        return True

    monkeypatch.setattr("fitness_tracker.database.create_engine", fake_create_engine)
    monkeypatch.setattr(
        DatabaseManager,
        "_schema_needs_migration",
        staticmethod(schema_needs_migration),
    )

    with pytest.raises(DatabaseMigrationError, match="verified PostgreSQL backup"):
        local.sync_to_database("postgresql://example.invalid/fitness")


def test_database_sync_errors_are_user_actionable() -> None:
    assert issubclass(DatabaseConnectionError, UserActionableError)
    assert issubclass(DatabaseMigrationError, UserActionableError)


def test_sync_updates_activity_after_it_stops(tmp_path: Path) -> None:
    local = _manager(tmp_path / "local.db")
    remote = _manager(tmp_path / "remote.db")
    public_id = uuid4()
    activity_id = _insert_activity(local, public_id)

    local.sync_to_database(f"sqlite:///{tmp_path / 'remote.db'}")

    stopped_at = START_TIME + timedelta(minutes=45)
    with local.Session() as session:
        session.get(Activity, activity_id).end_time = stopped_at
        session.commit()

    local.sync_to_database(f"sqlite:///{tmp_path / 'remote.db'}")

    copied = _activity(remote, public_id)
    assert copied.end_time is not None
    assert copied.end_time.replace(tzinfo=UTC) == stopped_at


def test_sync_transfers_new_upload_state(tmp_path: Path) -> None:
    local = _manager(tmp_path / "local.db")
    remote = _manager(tmp_path / "remote.db")
    public_id = uuid4()
    activity_id = _insert_activity(local, public_id)

    local.sync_to_database(f"sqlite:///{tmp_path / 'remote.db'}")
    local.repository.mark_upload_ok(
        activity_id,
        PROVIDER,
        provider_activity_id="remote-1",
        payload_hash="hash",
    )
    local.sync_to_database(f"sqlite:///{tmp_path / 'remote.db'}")

    copied = _activity(remote, public_id)
    with remote.Session() as session:
        upload = (
            session.query(ActivityUpload).filter_by(activity_id=copied.id, provider=PROVIDER).one()
        )
        assert upload.status == "ok"
        assert upload.provider_activity_id == "remote-1"
        assert upload.payload_hash == "hash"


def test_sync_transfers_sport_and_rebuilds_derived_stats(tmp_path: Path) -> None:
    local = _manager(tmp_path / "local.db")
    remote = _manager(tmp_path / "remote.db")
    activity_id = local.start_activity(SportTypesEnum.biking, Environment.INDOOR)
    with local.Session() as session:
        session.add(
            HeartRate(
                activity_id=activity_id,
                timestamp_ms=1_000,
                bpm=130,
            ),
        )
        session.commit()
    StatsCalculator(local).compute_for_activity(activity_id)

    local.sync_to_database(f"sqlite:///{tmp_path / 'remote.db'}")

    with remote.Session() as session:
        activity = session.query(Activity).one()
        sport = session.query(ActivitySport).filter_by(activity_id=activity.id).one()
        stats = session.query(ActivityStats).filter_by(activity_id=activity.id).one()
        assert sport.sport_type_id == SportTypesEnum.biking.value
        assert stats.sport_type_id == SportTypesEnum.biking.value


def test_reconcile_rebuilds_stats_only_when_metrics_are_copied(tmp_path: Path) -> None:
    source = _manager(tmp_path / "source.db")
    destination = _manager(tmp_path / "destination.db")
    public_id = uuid4()
    source_id = _insert_activity(source, public_id)
    _insert_activity(destination, public_id)
    rebuild_stats = Mock()

    with source.Session() as source_session, destination.Session() as destination_session:
        DatabaseSynchronizer.reconcile_sessions(
            source_session,
            destination_session,
            rebuild_stats,
        )
        destination_session.commit()
    rebuild_stats.assert_not_called()

    with source.Session() as session:
        session.add(HeartRate(activity_id=source_id, timestamp_ms=1_000, bpm=140))
        session.commit()
    with source.Session() as source_session, destination.Session() as destination_session:
        DatabaseSynchronizer.reconcile_sessions(
            source_session,
            destination_session,
            rebuild_stats,
        )
        destination_session.commit()

    rebuild_stats.assert_called_once()

    rebuild_stats.reset_mock()
    with source.Session() as session:
        session.add(
            LocationPoint(
                activity_id=source_id,
                timestamp_ms=2_000,
                latitude_deg=39.7392,
                longitude_deg=-104.9903,
            ),
        )
        session.commit()
    with source.Session() as source_session, destination.Session() as destination_session:
        DatabaseSynchronizer.reconcile_sessions(
            source_session,
            destination_session,
            rebuild_stats,
        )
        destination_session.commit()

    rebuild_stats.assert_not_called()


def test_sync_preserves_measurements_sharing_timestamp(tmp_path: Path) -> None:
    local = _manager(tmp_path / "local.db")
    remote = _manager(tmp_path / "remote.db")
    public_id = uuid4()
    local_id = _insert_activity(local, public_id)
    remote_id = _insert_activity(remote, public_id)

    with local.Session() as session:
        session.add_all(
            [
                HeartRate(activity_id=local_id, timestamp_ms=1_000, bpm=100),
                HeartRate(activity_id=local_id, timestamp_ms=1_000, bpm=150),
            ],
        )
        session.commit()
    with remote.Session() as session:
        session.add(HeartRate(activity_id=remote_id, timestamp_ms=1_000, bpm=100))
        session.commit()

    remote_url = f"sqlite:///{tmp_path / 'remote.db'}"
    local.sync_to_database(remote_url)
    local.sync_to_database(remote_url)

    with remote.Session() as session:
        rows = (
            session.query(HeartRate).filter_by(activity_id=remote_id).order_by(HeartRate.id).all()
        )
        assert [(row.timestamp_ms, row.bpm) for row in rows] == [
            (1_000, 100),
            (1_000, 150),
        ]


def test_repeated_bidirectional_sync_is_idempotent(tmp_path: Path) -> None:
    local = _manager(tmp_path / "local.db")
    remote = _manager(tmp_path / "remote.db")
    _insert_activity(local, uuid4())
    local_url = f"sqlite:///{tmp_path / 'local.db'}"
    remote_url = f"sqlite:///{tmp_path / 'remote.db'}"

    local.sync_to_database(remote_url)
    remote.sync_to_database(local_url)
    local.sync_to_database(remote_url)
    remote.sync_to_database(local_url)

    with local.Session() as session:
        assert session.query(Activity).count() == 1
    with remote.Session() as session:
        assert session.query(Activity).count() == 1


def test_activities_with_colliding_start_times_stay_distinct_by_uuid(tmp_path: Path) -> None:
    local = _manager(tmp_path / "local.db")
    remote = _manager(tmp_path / "remote.db")
    public_ids = {uuid4(), uuid4()}
    for public_id in public_ids:
        _insert_activity(local, public_id)

    local.sync_to_database(f"sqlite:///{tmp_path / 'remote.db'}")

    with remote.Session() as session:
        copied_ids = {row.public_id for row in session.query(Activity).all()}
    assert copied_ids == public_ids


def test_older_failed_upload_cannot_overwrite_newer_ok(tmp_path: Path) -> None:
    local = _manager(tmp_path / "local.db")
    remote = _manager(tmp_path / "remote.db")
    public_id = uuid4()
    local_id = _insert_activity(local, public_id)
    remote_id = _insert_activity(remote, public_id)
    older = START_TIME + timedelta(minutes=1)
    newer = START_TIME + timedelta(minutes=2)

    with local.Session() as session:
        session.add(
            ActivityUpload(
                activity_id=local_id,
                provider=PROVIDER,
                status="failed",
                last_error="temporary failure",
                updated_at=older,
            ),
        )
        session.commit()
    with remote.Session() as session:
        session.add(
            ActivityUpload(
                activity_id=remote_id,
                provider=PROVIDER,
                status="ok",
                uploaded_at=newer,
                updated_at=newer,
            ),
        )
        session.commit()

    local.sync_to_database(f"sqlite:///{tmp_path / 'remote.db'}")

    with remote.Session() as session:
        upload = (
            session.query(ActivityUpload).filter_by(activity_id=remote_id, provider=PROVIDER).one()
        )
        assert upload.status == "ok"


def test_equal_timestamp_uploads_converge_to_ok(tmp_path: Path) -> None:
    local = _manager(tmp_path / "local.db")
    remote = _manager(tmp_path / "remote.db")
    public_id = uuid4()
    local_id = _insert_activity(local, public_id)
    remote_id = _insert_activity(remote, public_id)
    timestamp = START_TIME + timedelta(minutes=1)
    local_url = f"sqlite:///{tmp_path / 'local.db'}"
    remote_url = f"sqlite:///{tmp_path / 'remote.db'}"

    with local.Session() as session:
        session.add(
            ActivityUpload(
                activity_id=local_id,
                provider=PROVIDER,
                status="failed",
                last_error="temporary failure",
                updated_at=timestamp,
            ),
        )
        session.commit()
    with remote.Session() as session:
        session.add(
            ActivityUpload(
                activity_id=remote_id,
                provider=PROVIDER,
                status="ok",
                uploaded_at=timestamp,
                updated_at=timestamp,
                provider_activity_id="remote-1",
                payload_hash="hash",
            ),
        )
        session.commit()

    local.sync_to_database(remote_url)
    remote.sync_to_database(local_url)

    with local.Session() as session:
        local_upload = (
            session.query(ActivityUpload).filter_by(activity_id=local_id, provider=PROVIDER).one()
        )
        assert local_upload.status == "ok"
        assert local_upload.provider_activity_id == "remote-1"
        assert local_upload.payload_hash == "hash"
        assert local_upload.last_error is None
    with remote.Session() as session:
        remote_upload = (
            session.query(ActivityUpload).filter_by(activity_id=remote_id, provider=PROVIDER).one()
        )
        assert remote_upload.status == "ok"
        assert remote_upload.provider_activity_id == "remote-1"
        assert remote_upload.payload_hash == "hash"
        assert remote_upload.last_error is None


def test_partial_failure_rolls_back_the_aggregate(tmp_path: Path, monkeypatch) -> None:
    local = _manager(tmp_path / "local.db")
    remote_url = f"sqlite:///{tmp_path / 'remote.db'}"
    public_id = uuid4()
    activity_id = _insert_activity(local, public_id)
    with local.Session() as session:
        session.add(
            HeartRate(
                activity_id=activity_id,
                timestamp_ms=1_000,
                bpm=140,
                rr_interval=None,
            ),
        )
        session.commit()

    def fail_child_copy(*_args, **_kwargs):
        message = "child copy failed"
        raise RuntimeError(message)

    monkeypatch.setattr(Session, "bulk_insert_mappings", fail_child_copy)
    with pytest.raises(RuntimeError, match="child copy failed"):
        local.sync_to_database(remote_url)

    remote = _manager(tmp_path / "remote.db")
    with remote.Session() as session:
        assert session.query(Activity).count() == 0
