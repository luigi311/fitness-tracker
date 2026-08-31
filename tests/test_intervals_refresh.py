# ruff: noqa: EM101, PLR2004, TRY003

import gzip
import json
import os
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
import requests
from fitness_tracker.integrations import intervals_icu as client_module
from fitness_tracker.integrations.errors import (
    IntegrationResponseError,
    IntegrationTransportError,
)
from fitness_tracker.integrations.intervals_icu import (
    IcuUploadResponse,
    IntervalsICUClient,
    IntervalsICUCredentials,
)
from fitness_tracker.workout_providers import intervals_icu as provider_module
from fitness_tracker.workout_providers.intervals_icu import IntervalsICUProvider
from fitness_tracker.workout_providers.utils import WorkoutRefreshResult

START = date(2026, 1, 1)
END = date(2026, 1, 7)


class _Response:
    def __init__(self, events: Any) -> None:
        self.headers = {"content-type": "application/json"}
        self._events = events

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._events


class _Transport:
    def __init__(self, events: Any, calls: list[dict[str, Any]] | None) -> None:
        self.events = events
        self.calls = calls

    def get(self, url: str, **kwargs: Any) -> _Response:
        if self.calls is not None:
            self.calls.append({"url": url, **kwargs})
        return _Response(self.events)

    def post(self, url: str, **kwargs: Any) -> _Response:
        if self.calls is not None:
            self.calls.append({"url": url, **kwargs})
        return _Response(self.events)


def _event(
    event_type: str,
    *,
    name: str = "Threshold",
    filename: str = "threshold.fit",
    start_date: str = "2026-01-02T08:00:00",
) -> dict[str, str]:
    return {
        "type": event_type,
        "name": name,
        "workout_filename": filename,
        "workout_file_base64": "ZmFrZQ==",
        "start_date_local": start_date,
        "provider_metadata": "preserve",
    }


def _provider(
    events: Any,
    *,
    calls: list[dict[str, Any]] | None = None,
) -> IntervalsICUProvider:
    client = IntervalsICUClient(
        IntervalsICUCredentials(athlete_id="athlete", api_key="key"),
        transport=_Transport(events, calls),
    )
    return IntervalsICUProvider(client=client)


def _refresh_running(
    provider: IntervalsICUProvider,
    out_dir: Path,
) -> WorkoutRefreshResult:
    return provider.refresh_between(
        start=START,
        end=END,
        running_dir=out_dir,
        cycling_dir=out_dir.parent / "cycling",
    )


def _managed_files(out_dir: Path) -> set[Path]:
    return {
        path
        for path in out_dir.iterdir()
        if path.is_file() and path.suffix.lower() in {".fit", ".zwo", ".erg", ".mrc", ".json"}
    }


def test_mixed_run_and_ride_response_is_filtered_by_sport(
    tmp_path: Path,
) -> None:
    events = [_event("Run", name="Run workout"), _event("Ride", name="Ride workout")]
    calls: list[dict[str, Any]] = []
    provider = _provider(events, calls=calls)
    running_dir = tmp_path / "running"
    cycling_dir = tmp_path / "cycling"

    result = provider.refresh_between(
        start=START,
        end=END,
        running_dir=running_dir,
        cycling_dir=cycling_dir,
    )
    running = [workout for workout in result.written if workout.path.parent == running_dir]
    cycling = [workout for workout in result.written if workout.path.parent == cycling_dir]

    assert len(calls) == 1
    assert len(running) == 1
    assert len(cycling) == 1
    assert running[0].title == "Run workout"
    assert cycling[0].title == "Ride workout"
    stored_event = json.loads(running[0].path.read_text(encoding="utf-8"))
    assert stored_event["provider_metadata"] == "preserve"


def test_response_without_requested_sport_preserves_last_known_good_workouts(
    tmp_path: Path,
) -> None:
    out_dir = tmp_path / "workouts"
    out_dir.mkdir()
    old = out_dir / "2025-12-31 Old.json"
    old.write_text("old", encoding="utf-8")
    provider = _provider([_event("Ride")])

    _refresh_running(provider, out_dir)

    assert old.exists()
    assert old.read_text(encoding="utf-8") == "old"


def test_empty_response_preserves_last_known_good_workouts(
    tmp_path: Path,
) -> None:
    out_dir = tmp_path / "workouts"
    out_dir.mkdir()
    old = out_dir / "2025-12-31 Old.json"
    old.write_text("old", encoding="utf-8")
    provider = _provider([])

    written = _refresh_running(provider, out_dir).written

    assert written == ()
    assert old.read_text(encoding="utf-8") == "old"


def test_malformed_event_preserves_last_known_good_workouts(
    tmp_path: Path,
) -> None:
    out_dir = tmp_path / "workouts"
    out_dir.mkdir()
    old = out_dir / "2025-12-31 Old.json"
    old.write_text("old", encoding="utf-8")
    provider = _provider([_event("Run", start_date="not-a-date")])

    with pytest.raises(IntegrationResponseError):
        _refresh_running(provider, out_dir)

    assert old.exists()
    assert old.read_text(encoding="utf-8") == "old"
    assert _managed_files(out_dir) == {old}


def test_mid_write_failure_preserves_old_set_and_writes_no_partial_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_dir = tmp_path / "workouts"
    out_dir.mkdir()
    old = out_dir / "2025-12-31 Old.json"
    old.write_text("old", encoding="utf-8")
    events = [_event("Run", name="Good"), _event("Run", name="Broken")]
    provider = _provider(events)
    original_fsync = os.fsync
    fsync_calls = 0

    def failing_fsync(descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            raise OSError("injected write failure")
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", failing_fsync)

    with pytest.raises(OSError, match="injected write failure"):
        _refresh_running(provider, out_dir)

    assert old.exists()
    assert old.read_text(encoding="utf-8") == "old"
    assert _managed_files(out_dir) == {old}


def test_sanitized_duplicate_titles_get_distinct_paths(
    tmp_path: Path,
) -> None:
    events = [
        _event("Run", name="Intervals / Threshold", filename="first.fit"),
        _event("Run", name="Intervals ? Threshold", filename="second.fit"),
    ]
    provider = _provider(events)
    out_dir = tmp_path / "workouts"

    written = _refresh_running(provider, out_dir).written
    paths = {workout.path for workout in written}

    assert len(written) == 2
    assert len(paths) == 2
    assert all(path.exists() for path in paths)
    assert all("/" not in path.name and "?" not in path.name for path in paths)


def test_successful_replacement_removes_stale_managed_files_only(
    tmp_path: Path,
) -> None:
    out_dir = tmp_path / "workouts"
    out_dir.mkdir()
    stale_json = out_dir / "2025-12-31 Stale.json"
    stale_fit = out_dir / "2025-12-30 Stale.fit"
    unrelated = out_dir / "README.txt"
    for path in (stale_json, stale_fit):
        path.write_text("stale", encoding="utf-8")
    unrelated.write_text("keep", encoding="utf-8")
    provider = _provider([_event("Run", name="Fresh")])

    _refresh_running(provider, out_dir)

    assert not stale_json.exists()
    assert not stale_fit.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert len(_managed_files(out_dir)) == 1


def test_invalid_matching_event_does_not_discard_valid_workouts(tmp_path: Path) -> None:
    invalid = _event("Run", name="Missing file")
    invalid["workout_file_base64"] = ""
    provider = _provider([_event("Run", name="Valid"), invalid])

    result = _refresh_running(provider, tmp_path / "running")

    assert result.invalid == 1
    assert len(result.written) == 1
    assert result.written[0].path.exists()


def test_unsupported_event_is_counted_once_across_sports(tmp_path: Path) -> None:
    provider = _provider([_event("Swim")])

    result = _refresh_running(provider, tmp_path / "running")

    assert result.skipped == 1


def test_fetch_logs_request_response_and_event_summary_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    athlete_id = "private-athlete-id"
    api_key = "private-api-key"
    mock_logger = Mock()
    monkeypatch.setattr(client_module, "logger", mock_logger)
    client = IntervalsICUClient(
        IntervalsICUCredentials(athlete_id=athlete_id, api_key=api_key),
        transport=_Transport([_event("Run")], None),
    )

    events = client.fetch_events(start=START, end=END, ext="fit")

    assert len(events) == 1
    debug_messages = [call.args[0] for call in mock_logger.debug.call_args_list]
    trace_messages = [call.args[0] for call in mock_logger.trace.call_args_list]
    assert any("event fetch starting" in message for message in debug_messages)
    assert any("event fetch completed" in message for message in debug_messages)
    assert any("request starting" in message for message in trace_messages)
    assert any("request completed" in message for message in trace_messages)
    assert any("event summary" in message for message in trace_messages)
    rendered_calls = repr(mock_logger.method_calls)
    assert athlete_id not in rendered_calls
    assert api_key not in rendered_calls


def test_fetch_failure_log_and_error_redact_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    athlete_id = "private-athlete-id"
    api_key = "private-api-key"
    response = requests.Response()
    response.status_code = 403
    response.raw = BytesIO(
        f'{{"athlete":"{athlete_id}","credential":"{api_key}"}}'.encode(),
    )
    response.encoding = "utf-8"

    class FailingTransport:
        @staticmethod
        def get(url: str, **_kwargs: object) -> requests.Response:
            response.url = url
            raise requests.HTTPError("forbidden", response=response)

    mock_logger = Mock()
    monkeypatch.setattr(client_module, "logger", mock_logger)
    client = IntervalsICUClient(
        IntervalsICUCredentials(athlete_id=athlete_id, api_key=api_key),
        transport=FailingTransport(),  # type: ignore[arg-type]
    )

    with pytest.raises(IntegrationTransportError) as error_info:
        client.fetch_events(start=START, end=END, ext="fit")

    assert error_info.value.debug_detail == (
        '{"athlete":"[redacted]","credential":"[redacted]"}'
    )
    rendered_calls = repr(mock_logger.method_calls)
    assert athlete_id not in rendered_calls
    assert api_key not in rendered_calls
    assert "status={}, elapsed_ms={}" in mock_logger.warning.call_args.args[0]


def test_validation_failure_logs_safe_schema_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensitive_payload = "sensitive-workout-payload"
    malformed = _event("Run", start_date="not-a-date")
    malformed["workout_file_base64"] = sensitive_payload
    mock_logger = Mock()
    monkeypatch.setattr(client_module, "logger", mock_logger)
    monkeypatch.setattr(provider_module, "logger", mock_logger)
    provider = _provider([malformed])

    with pytest.raises(IntegrationResponseError) as error_info:
        _refresh_running(provider, tmp_path / "running")

    detail = error_info.value.debug_detail or ""
    assert "payload=list(length=1)" in detail
    assert "0.start_date_local" in detail
    assert sensitive_payload not in detail
    assert sensitive_payload not in repr(mock_logger.method_calls)
    provider_error = mock_logger.error.call_args.args
    assert provider_error[1:4] == (
        "fetch events",
        "IntegrationResponseError",
        None,
    )


def test_upload_response_requires_non_empty_list() -> None:
    with pytest.raises(IntegrationResponseError, match="no upload result"):
        IcuUploadResponse.from_response(_Response([]))  # type: ignore[arg-type]


def test_upload_response_uses_first_list_item() -> None:
    result = IcuUploadResponse.from_response(  # type: ignore[arg-type]
        _Response([{"id": 42}, {"id": 99}]),
    )

    assert result.provider_id == "42"


def test_upload_response_accepts_current_activities_envelope() -> None:
    result = IcuUploadResponse.from_response(  # type: ignore[arg-type]
        _Response(
            {
                "icu_athlete_id": "i123",
                "id": "upload-batch-id",
                "activities": [{"icu_athlete_id": "i123", "id": "i456"}],
            },
        ),
    )

    assert result.provider_id == "i456"


def test_upload_response_accepts_direct_activity_object() -> None:
    result = IcuUploadResponse.from_response(  # type: ignore[arg-type]
        _Response({"icu_athlete_id": "i123", "id": "i456"}),
    )

    assert result.provider_id == "i456"


def test_upload_tcx_uses_deterministic_gzip_payload() -> None:
    calls: list[dict[str, Any]] = []
    client = IntervalsICUClient(
        IntervalsICUCredentials(athlete_id="athlete", api_key="key"),
        transport=_Transport([{"id": 42}], calls),
    )
    payload = b"<TrainingCenterDatabase>activity</TrainingCenterDatabase>"

    result = client.upload_tcx("Run_2026-01-02_08-00", payload)

    assert result.provider_id == "42"
    filename, compressed, content_type = calls[0]["files"]["file"]
    assert filename == "Run_2026-01-02_08-00.tcx.gz"
    assert content_type == "application/gzip"
    assert gzip.decompress(compressed) == payload


def test_transport_error_does_not_expose_request_url() -> None:
    response = requests.Response()
    response.status_code = 403
    athlete_id = "private-athlete-id"
    request_url = f"https://example.invalid/athlete/{athlete_id}/events"
    response.raw = BytesIO(
        (
            f'{{"request":"{request_url}",'
            f'"path":"/api/v1/athlete/{athlete_id}/events"}}'
        ).encode(),
    )
    response.encoding = "utf-8"

    def fail_request(url: str, **_kwargs: object) -> requests.Response:
        response.url = url
        message = f"403 for {url}"
        raise requests.HTTPError(message, response=response)

    with pytest.raises(IntegrationTransportError) as error_info:
        IntervalsICUClient._request(  # noqa: SLF001
            fail_request,
            request_url,
        )

    assert error_info.value.status_code == 403
    assert error_info.value.debug_detail == (
        '{"request":"[redacted URL]","path":"/api/v1/athlete/[redacted]/events"}'
    )
    assert athlete_id not in (error_info.value.debug_detail or "")
    assert athlete_id not in str(error_info.value)


def test_transport_error_redacts_json_escaped_url_and_athlete_path() -> None:
    response = requests.Response()
    response.status_code = 403
    provider_id = "private-provider.invalid"
    athlete_id = "private-athlete-id"
    escaped_url = (
        f"https:\\/\\/{provider_id}\\/api\\/v1\\/athlete\\/{athlete_id}\\/events"
    )
    escaped_path = f"\\/api\\/v1\\/athlete\\/{athlete_id}\\/events"
    response.raw = BytesIO(
        f'{{"request":"{escaped_url}","path":"{escaped_path}"}}'.encode(),
    )
    response.encoding = "utf-8"
    request_url = "https://example.invalid/status"

    def fail_request(url: str, **_kwargs: object) -> requests.Response:
        response.url = url
        message = f"403 for {url}"
        raise requests.HTTPError(message, response=response)

    with pytest.raises(IntegrationTransportError) as error_info:
        IntervalsICUClient._request(  # noqa: SLF001
            fail_request,
            request_url,
        )

    debug_detail = error_info.value.debug_detail or ""
    assert provider_id not in debug_detail
    assert athlete_id not in debug_detail
    assert "[redacted URL]" in debug_detail
    assert "/athlete/[redacted]/" in debug_detail
