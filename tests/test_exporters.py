import math
import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from sys import float_info
from xml.etree import ElementTree as ET

import pytest
from fitness_tracker.core.environment import Environment
from fitness_tracker.core.sports import SportTypesEnum
from fitness_tracker.data.models import Activity, HeartRate, LocationPoint, RunningMetrics
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
    activity = Activity(
        id=7,
        start_time=start,
        end_time=start + timedelta(seconds=3),
        environment=Environment.INDOOR.value,
    )
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


def _tcx_trackpoints(generated: bytes) -> list[ET.Element]:
    root = ET.fromstring(generated)  # noqa: S314 - parsing exporter output in-memory
    namespace = {"tcx": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"}
    return root.findall(
        "tcx:Activities/tcx:Activity/tcx:Lap/tcx:Track/tcx:Trackpoint",
        namespace,
    )


def test_outdoor_tcx_without_locations_omits_sensor_altitude() -> None:
    start = datetime(2026, 1, 2, 8, 0, tzinfo=UTC)
    generated = activity_to_tcx(
        act=Activity(
            id=7,
            start_time=start,
            environment=Environment.OUTDOOR.value,
        ),
        heart_rates=[],
        running=[RunningMetrics(timestamp_ms=0, altitude_m=100.0)],
        locations=[],
        sport_type=SportTypesEnum.running,
    )

    namespace = {"tcx": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"}
    trackpoints = _tcx_trackpoints(generated)
    assert trackpoints[0].find("tcx:AltitudeMeters", namespace) is None


def test_outdoor_tcx_merges_sensor_and_location_timelines() -> None:
    start = datetime(2026, 1, 2, 8, 0, tzinfo=UTC)
    activity = Activity(
        id=7,
        start_time=start,
        end_time=start + timedelta(seconds=3),
        environment=Environment.OUTDOOR.value,
    )
    generated = activity_to_tcx(
        act=activity,
        heart_rates=[HeartRate(timestamp_ms=0, bpm=140)],
        running=[
            RunningMetrics(
                timestamp_ms=0,
                speed_mps=2.5,
                cadence_spm=80,
                total_distance_m=0.0,
                altitude_m=100.0,
            ),
            RunningMetrics(
                timestamp_ms=2_000,
                speed_mps=3.0,
                cadence_spm=82,
                total_distance_m=6.0,
                altitude_m=100.5,
            ),
        ],
        locations=[
            LocationPoint(
                id=11,
                activity_id=7,
                timestamp_ms=1_000,
                latitude_deg=39.7392,
                longitude_deg=-104.9903,
                altitude_m=1609.0,
            ),
            LocationPoint(
                id=12,
                activity_id=7,
                timestamp_ms=2_500,
                latitude_deg=39.7393,
                longitude_deg=-104.9902,
                altitude_m=1610.0,
            ),
        ],
        sport_type=SportTypesEnum.running,
    )

    namespace = {"tcx": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"}
    trackpoints = _tcx_trackpoints(generated)
    assert [point.findtext("tcx:Time", namespaces=namespace) for point in trackpoints] == [
        "2026-01-02T08:00:00+00:00",
        "2026-01-02T08:00:01+00:00",
        "2026-01-02T08:00:02+00:00",
        "2026-01-02T08:00:02+00:00",
    ]
    assert [
        point.findtext("tcx:Position/tcx:LatitudeDegrees", namespaces=namespace)
        for point in trackpoints
    ] == [None, "39.73920000", None, "39.73930000"]
    assert trackpoints[1].findtext("tcx:HeartRateBpm/tcx:Value", namespaces=namespace) == "140"
    assert (
        trackpoints[1].findtext(
            "tcx:Extensions/ae:TPX/ae:Speed",
            namespaces={**namespace, "ae": "http://www.garmin.com/xmlschemas/ActivityExtension/v2"},
        )
        == "2.500000"
    )
    assert [
        point.findtext("tcx:AltitudeMeters", namespaces=namespace) for point in trackpoints
    ] == [None, "1609.000", None, "1610.000"]
    assert trackpoints[3].findtext("tcx:DistanceMeters", namespaces=namespace) == "6.000"


def test_outdoor_location_without_altitude_omits_altitude() -> None:
    start = datetime(2026, 1, 2, 8, 0, tzinfo=UTC)
    generated = activity_to_tcx(
        act=Activity(
            id=7,
            start_time=start,
            environment=Environment.OUTDOOR.value,
        ),
        heart_rates=[],
        running=[
            RunningMetrics(timestamp_ms=0, speed_mps=2.5, altitude_m=100.0),
            RunningMetrics(timestamp_ms=1_000, speed_mps=3.0, altitude_m=101.0),
        ],
        locations=[
            LocationPoint(
                id=1,
                timestamp_ms=0,
                latitude_deg=39.7392,
                longitude_deg=-104.9903,
                altitude_m=None,
            ),
        ],
        sport_type=SportTypesEnum.running,
    )

    namespace = {"tcx": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"}
    assert [
        point.findtext("tcx:AltitudeMeters", namespaces=namespace)
        for point in _tcx_trackpoints(generated)
    ] == [None, None]


def test_outdoor_gps_only_tcx_has_position_and_cumulative_distance() -> None:
    start = datetime(2026, 1, 2, 8, 0, tzinfo=UTC)
    generated = activity_to_tcx(
        act=Activity(
            id=7,
            start_time=start,
            environment=Environment.OUTDOOR.value,
        ),
        heart_rates=[],
        locations=[
            LocationPoint(
                id=1,
                timestamp_ms=0,
                latitude_deg=39.7392,
                longitude_deg=-104.9903,
            ),
            LocationPoint(
                id=2,
                timestamp_ms=1_000,
                latitude_deg=39.7402,
                longitude_deg=-104.9903,
            ),
        ],
        sport_type=SportTypesEnum.running,
    )

    namespace = {"tcx": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"}
    trackpoints = _tcx_trackpoints(generated)
    expected_trackpoint_count = 2
    assert len(trackpoints) == expected_trackpoint_count
    assert all(point.find("tcx:Position", namespace) is not None for point in trackpoints)
    assert float(trackpoints[-1].findtext("tcx:DistanceMeters", namespaces=namespace) or 0) > 0
    root = ET.fromstring(generated)  # noqa: S314 - parsing exporter output in-memory
    lap_distance = root.findtext(
        "tcx:Activities/tcx:Activity/tcx:Lap/tcx:DistanceMeters",
        namespaces=namespace,
    )
    assert float(lap_distance or 0) > 0


def test_indoor_tcx_adds_sensor_altitude_to_gps_anchor_altitude() -> None:
    start = datetime(2026, 1, 2, 8, 0, tzinfo=UTC)
    generated = activity_to_tcx(
        act=Activity(
            id=7,
            start_time=start,
            environment=Environment.INDOOR.value,
        ),
        heart_rates=[],
        running=[
            RunningMetrics(
                timestamp_ms=0,
                speed_mps=2.5,
                cadence_spm=80,
                altitude_m=0.0,
            ),
            RunningMetrics(
                timestamp_ms=2_000,
                speed_mps=3.0,
                cadence_spm=82,
                altitude_m=0.5,
            ),
        ],
        locations=[
            LocationPoint(
                id=1,
                timestamp_ms=500,
                latitude_deg=39.7392,
                longitude_deg=-104.9903,
                altitude_m=1609.0,
            ),
        ],
        sport_type=SportTypesEnum.running,
    )

    namespace = {"tcx": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"}
    trackpoints = _tcx_trackpoints(generated)
    assert sum(point.find("tcx:Position", namespace) is not None for point in trackpoints) == 1
    assert (
        trackpoints[0].findtext(
            "tcx:Position/tcx:LatitudeDegrees",
            namespaces=namespace,
        )
        == "39.73920000"
    )
    assert [
        point.findtext("tcx:AltitudeMeters", namespaces=namespace) for point in trackpoints
    ] == ["1609.000", "1609.500"]


def test_indoor_tcx_unknown_gps_altitude_uses_sensor_altitude() -> None:
    start = datetime(2026, 1, 2, 8, 0, tzinfo=UTC)
    generated = activity_to_tcx(
        act=Activity(
            id=7,
            start_time=start,
            environment=Environment.INDOOR.value,
        ),
        heart_rates=[],
        running=[RunningMetrics(timestamp_ms=0, altitude_m=5.0)],
        locations=[
            LocationPoint(
                id=1,
                timestamp_ms=0,
                latitude_deg=39.7392,
                longitude_deg=-104.9903,
                altitude_m=-float_info.max,
            ),
        ],
        sport_type=SportTypesEnum.running,
    )

    namespace = {"tcx": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"}
    trackpoint = _tcx_trackpoints(generated)[0]
    assert trackpoint.find("tcx:Position", namespace) is not None
    assert trackpoint.findtext("tcx:AltitudeMeters", namespaces=namespace) == "5.000"


def test_indoor_gps_only_tcx_emits_one_anchor_trackpoint() -> None:
    start = datetime(2026, 1, 2, 8, 0, tzinfo=UTC)
    generated = activity_to_tcx(
        act=Activity(
            id=7,
            start_time=start,
            environment=Environment.INDOOR.value,
        ),
        heart_rates=[],
        locations=[
            LocationPoint(
                id=1,
                timestamp_ms=500,
                latitude_deg=39.7392,
                longitude_deg=-104.9903,
                altitude_m=1609.0,
            ),
        ],
        sport_type=SportTypesEnum.running,
    )

    namespace = {"tcx": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"}
    trackpoints = _tcx_trackpoints(generated)
    assert len(trackpoints) == 1
    assert trackpoints[0].find("tcx:Position", namespace) is not None
    assert trackpoints[0].findtext("tcx:AltitudeMeters", namespaces=namespace) == "1609.000"
    assert trackpoints[0].findtext("tcx:DistanceMeters", namespaces=namespace) == "0.000"


def test_merged_tcx_distance_never_decreases() -> None:
    start = datetime(2026, 1, 2, 8, 0, tzinfo=UTC)
    generated = activity_to_tcx(
        act=Activity(
            id=7,
            start_time=start,
            environment=Environment.OUTDOOR.value,
        ),
        heart_rates=[],
        running=[
            RunningMetrics(
                timestamp_ms=0,
                speed_mps=2.5,
                cadence_spm=80,
                total_distance_m=0.0,
            ),
            RunningMetrics(
                timestamp_ms=2_000,
                speed_mps=3.0,
                cadence_spm=82,
                total_distance_m=6.0,
            ),
        ],
        locations=[
            LocationPoint(
                id=1,
                timestamp_ms=1_000,
                latitude_deg=39.7392,
                longitude_deg=-104.9903,
            ),
            LocationPoint(
                id=2,
                timestamp_ms=3_000,
                latitude_deg=39.7393,
                longitude_deg=-104.9902,
            ),
        ],
        sport_type=SportTypesEnum.running,
    )

    namespace = {"tcx": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"}
    distances = [
        float(point.findtext("tcx:DistanceMeters", namespaces=namespace) or 0)
        for point in _tcx_trackpoints(generated)
    ]
    assert distances == sorted(distances)


def test_invalid_optional_location_values_do_not_enter_tcx_xml() -> None:
    start = datetime(2026, 1, 2, 8, 0, tzinfo=UTC)
    generated = activity_to_tcx(
        act=Activity(
            id=7,
            start_time=start,
            environment=Environment.OUTDOOR.value,
        ),
        heart_rates=[],
        locations=[
            LocationPoint(
                id=1,
                timestamp_ms=0,
                latitude_deg=39.7392,
                longitude_deg=-104.9903,
                accuracy_m=math.nan,
                altitude_m=math.nan,
                speed_mps=math.inf,
                heading_deg=math.inf,
            ),
        ],
        sport_type=SportTypesEnum.running,
    )

    root = ET.fromstring(generated)  # noqa: S314 - parsing exporter-generated XML
    element_names = {element.tag.rsplit("}", 1)[-1] for element in root.iter()}
    assert element_names.isdisjoint(
        {"AccuracyMeters", "AltitudeMeters", "Speed", "HeadingDegrees"},
    )


def test_equal_timestamp_locations_use_database_id_order() -> None:
    start = datetime(2026, 1, 2, 8, 0, tzinfo=UTC)
    generated = activity_to_tcx(
        act=Activity(
            id=7,
            start_time=start,
            environment=Environment.OUTDOOR.value,
        ),
        heart_rates=[],
        locations=[
            LocationPoint(
                id=20,
                timestamp_ms=0,
                latitude_deg=39.7393,
                longitude_deg=-104.9903,
            ),
            LocationPoint(
                id=10,
                timestamp_ms=0,
                latitude_deg=39.7392,
                longitude_deg=-104.9903,
            ),
        ],
        sport_type=SportTypesEnum.running,
    )

    namespace = {"tcx": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"}
    trackpoints = _tcx_trackpoints(generated)
    assert [
        point.findtext("tcx:Position/tcx:LatitudeDegrees", namespaces=namespace)
        for point in trackpoints
    ] == ["39.73920000", "39.73930000"]
