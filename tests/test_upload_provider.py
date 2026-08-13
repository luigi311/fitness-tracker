"""Tests for upload-provider persistence boundaries."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from fitness_tracker.core.sports import SportTypesEnum
from fitness_tracker.data.models import Activity, RunningMetrics
from fitness_tracker.upload_providers.intervals_icu import IntervalsICUUploader

EXPECTED_LOCAL_PERSISTENCE_ATTEMPTS = 2


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
    repository.mark_upload_failed.assert_not_called()
    assert all(
        call.kwargs["provider_activity_id"] == "remote-activity"
        for call in repository.mark_upload_ok.call_args_list
    )
