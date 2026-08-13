# ruff: noqa: EM101, PLR2004, TRY003

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fitness_tracker.integrations.errors import IntegrationResponseError
from fitness_tracker.integrations.intervals_icu import (
    IntervalsICUClient,
    IntervalsICUCredentials,
)
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
    monkeypatch: pytest.MonkeyPatch,
    events: Any,
    *,
    calls: list[dict[str, Any]] | None = None,
) -> IntervalsICUProvider:
    del monkeypatch
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [_event("Run", name="Run workout"), _event("Ride", name="Ride workout")]
    calls: list[dict[str, Any]] = []
    provider = _provider(monkeypatch, events, calls=calls)
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_dir = tmp_path / "workouts"
    out_dir.mkdir()
    old = out_dir / "2025-12-31 Old.json"
    old.write_text("old", encoding="utf-8")
    provider = _provider(monkeypatch, [_event("Ride")])

    _refresh_running(provider, out_dir)

    assert old.exists()
    assert old.read_text(encoding="utf-8") == "old"


def test_empty_response_preserves_last_known_good_workouts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_dir = tmp_path / "workouts"
    out_dir.mkdir()
    old = out_dir / "2025-12-31 Old.json"
    old.write_text("old", encoding="utf-8")
    provider = _provider(monkeypatch, [])

    written = _refresh_running(provider, out_dir).written

    assert written == ()
    assert old.read_text(encoding="utf-8") == "old"


def test_malformed_event_preserves_last_known_good_workouts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_dir = tmp_path / "workouts"
    out_dir.mkdir()
    old = out_dir / "2025-12-31 Old.json"
    old.write_text("old", encoding="utf-8")
    provider = _provider(monkeypatch, [_event("Run", start_date="not-a-date")])

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
    provider = _provider(monkeypatch, events)
    original_write_text = Path.write_text

    def failing_write(path: Path, content: str, *args: Any, **kwargs: Any) -> int:
        if json.loads(content).get("name") == "Broken":
            raise OSError("injected write failure")
        return original_write_text(path, content, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", failing_write)

    _refresh_running(provider, out_dir)

    assert old.exists()
    assert old.read_text(encoding="utf-8") == "old"
    assert _managed_files(out_dir) == {old}


def test_sanitized_duplicate_titles_get_distinct_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [
        _event("Run", name="Intervals / Threshold", filename="first.fit"),
        _event("Run", name="Intervals ? Threshold", filename="second.fit"),
    ]
    provider = _provider(monkeypatch, events)
    out_dir = tmp_path / "workouts"

    written = _refresh_running(provider, out_dir).written
    paths = {workout.path for workout in written}

    assert len(written) == 2
    assert len(paths) == 2
    assert all(path.exists() for path in paths)
    assert all("/" not in path.name and "?" not in path.name for path in paths)


def test_successful_replacement_removes_stale_managed_files_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_dir = tmp_path / "workouts"
    out_dir.mkdir()
    stale_json = out_dir / "2025-12-31 Stale.json"
    stale_fit = out_dir / "2025-12-30 Stale.fit"
    unrelated = out_dir / "README.txt"
    for path in (stale_json, stale_fit):
        path.write_text("stale", encoding="utf-8")
    unrelated.write_text("keep", encoding="utf-8")
    provider = _provider(monkeypatch, [_event("Run", name="Fresh")])

    _refresh_running(provider, out_dir)

    assert not stale_json.exists()
    assert not stale_fit.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert len(_managed_files(out_dir)) == 1
