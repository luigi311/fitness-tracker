"""Tests for upload-provider persistence boundaries."""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import Mock

from fitness_tracker.core.environment import Environment
from fitness_tracker.core.sports import SportTypesEnum
from fitness_tracker.data.models import Activity, LocationPoint, RunningMetrics
from fitness_tracker.upload_providers.intervals_icu import IntervalsICUUploader

EXPECTED_LOCAL_PERSISTENCE_ATTEMPTS = 2
EXPECTED_PAYLOAD_COUNT = 2


def test_successful_remote_upload_retries_only_local_success_persistence() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    activity = Activity(id=1, start_time=start, end_time=start + timedelta(minutes=30))
    repository = Mock()
    repository.list_not_uploaded.return_value = [activity]
    repository.list_heart_rates.return_value = []
    repository.list_running_metrics.return_value = [
        RunningMetrics(timestamp_ms=0, speed_mps=3.0, cadence_spm=170),
    ]
    repository.list_cycling_metrics.return_value = []
    repository.list_location_points.return_value = []
    repository.get_activity_sport.return_value = SimpleNamespace(
        sport_type_id=SportTypesEnum.running.value,
    )
    repository.mark_upload_ok.side_effect = [OSError("temporary database error"), None]
    client = Mock()
    client.upload_tcx.return_value = SimpleNamespace(provider_id="remote-activity")

    result = IntervalsICUUploader(client).upload_not_uploaded(repository)

    assert result == [(1, True, None)]
    client.upload_tcx.assert_called_once()
    assert repository.mark_upload_ok.call_count == EXPECTED_LOCAL_PERSISTENCE_ATTEMPTS
    repository.list_location_points.assert_called_once_with(1)
    repository.mark_upload_failed.assert_not_called()
    assert all(
        call.kwargs["provider_activity_id"] == "remote-activity"
        for call in repository.mark_upload_ok.call_args_list
    )


def test_location_changes_tcx_payload_hash() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    activity = Activity(
        id=1,
        start_time=start,
        end_time=start + timedelta(minutes=30),
        environment=Environment.OUTDOOR.value,
    )
    repository = Mock()
    repository.list_not_uploaded.side_effect = [[activity], [activity]]
    repository.list_heart_rates.return_value = []
    repository.list_running_metrics.return_value = [
        RunningMetrics(timestamp_ms=0, speed_mps=3.0, cadence_spm=170),
    ]
    repository.list_cycling_metrics.return_value = []
    repository.list_location_points.side_effect = [
        [],
        [
            LocationPoint(
                id=1,
                activity_id=1,
                timestamp_ms=1_000,
                latitude_deg=39.7392,
                longitude_deg=-104.9903,
            ),
        ],
    ]
    repository.get_activity_sport.return_value = SimpleNamespace(
        sport_type_id=SportTypesEnum.running.value,
    )
    client = Mock()
    client.upload_tcx.return_value = SimpleNamespace(provider_id="remote-activity")

    uploader = IntervalsICUUploader(client)
    assert uploader.upload_not_uploaded(repository) == [(1, True, None)]
    assert uploader.upload_not_uploaded(repository) == [(1, True, None)]

    payloads = [call.args[1] for call in client.upload_tcx.call_args_list]
    payload_hashes = [
        call.kwargs["payload_hash"] for call in repository.mark_upload_ok.call_args_list
    ]
    assert len(payloads) == EXPECTED_PAYLOAD_COUNT
    assert payloads[0] != payloads[1]
    assert b"<LatitudeDegrees>39.73920000</LatitudeDegrees>" in payloads[1]
    assert payload_hashes == [sha256(payload).hexdigest() for payload in payloads]
    assert payload_hashes[0] != payload_hashes[1]
