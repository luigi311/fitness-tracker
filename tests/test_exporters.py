import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from fitness_tracker.core.sports import SportTypesEnum
from fitness_tracker.data.models import Activity, HeartRate, RunningMetrics
from fitness_tracker.exporters import activity_to_tcx


@pytest.fixture(autouse=True)
def _use_utc_local_timezone(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    previous_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    if previous_tz is None:
        monkeypatch.delenv("TZ", raising=False)
    else:
        monkeypatch.setenv("TZ", previous_tz)
    time.tzset()


def test_running_activity_matches_tcx_golden_file() -> None:
    start = datetime(2026, 1, 2, 8, 0, tzinfo=UTC)
    activity = Activity(id=7, start_time=start, end_time=start + timedelta(seconds=3))
    heart_rates = [
        HeartRate(timestamp_ms=0, bpm=140),
        HeartRate(timestamp_ms=1_500, bpm=142),
    ]
    running = [
        RunningMetrics(
            timestamp_ms=0,
            speed_mps=2.5,
            cadence_spm=80,
            total_distance_m=0.0,
            power_watts=200,
            altitude_m=100.0,
        ),
        RunningMetrics(
            timestamp_ms=2_000,
            speed_mps=3.0,
            cadence_spm=82,
            total_distance_m=6.0,
            power_watts=210,
            altitude_m=100.5,
        ),
    ]

    generated = activity_to_tcx(
        act=activity,
        heart_rates=heart_rates,
        running=running,
        sport_type=SportTypesEnum.running,
    )
    golden = Path(__file__).parent / "fixtures" / "activity_running.tcx"

    assert generated == golden.read_bytes().removesuffix(b"\n")


def test_tcx_does_not_attach_future_heart_rate_to_primary_sample() -> None:
    start = datetime(2026, 1, 2, 8, 0, tzinfo=UTC)
    activity = Activity(id=7, start_time=start, end_time=start + timedelta(seconds=2))
    generated = activity_to_tcx(
        act=activity,
        heart_rates=[HeartRate(timestamp_ms=1_000, bpm=140)],
        running=[
            RunningMetrics(timestamp_ms=0, speed_mps=2.5, cadence_spm=80),
            RunningMetrics(timestamp_ms=2_000, speed_mps=3.0, cadence_spm=82),
        ],
        sport_type=SportTypesEnum.running,
    )

    root = ET.fromstring(generated)  # noqa: S314 - parsing exporter output in-memory
    namespace = {"tcx": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"}
    trackpoints = root.findall(
        "tcx:Activities/tcx:Activity/tcx:Lap/tcx:Track/tcx:Trackpoint",
        namespace,
    )
    assert trackpoints[0].find("tcx:HeartRateBpm", namespace) is None
    assert trackpoints[1].findtext("tcx:HeartRateBpm/tcx:Value", namespaces=namespace) == "140"
