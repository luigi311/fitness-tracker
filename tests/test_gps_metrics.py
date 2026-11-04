# ruff: noqa: SLF001, PLR2004
"""GPS sensor calculations and their recording/persistence boundary."""

import asyncio
from datetime import UTC, datetime, timedelta
from math import cos, sin
from random import Random
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from bleak.exc import BleakError
from bleaksport import CyclingSample, HeartRateSample, RunningSample
from fitness_tracker.activity_stats import _calc_elevation
from fitness_tracker.core.environment import Environment
from fitness_tracker.core.metric_averages import MetricAverages
from fitness_tracker.core.sensor_profile import SensorProfile
from fitness_tracker.core.settings import AppSettings, SensorSettings
from fitness_tracker.core.sports import SportTypesEnum
from fitness_tracker.data.models import (
    Activity,
    ActivityStats,
    CyclingMetrics,
    HeartRate,
    LocationPoint,
    RunningMetrics,
)
from fitness_tracker.database import DatabaseManager
from fitness_tracker.exporters import activity_to_tcx
from fitness_tracker.hardware import recorder as recorder_module
from fitness_tracker.hardware.gps_metrics import GpsMetricProcessor
from fitness_tracker.hardware.location import (
    LocationFilter,
    LocationFix,
    LocationPolicy,
    LocationState,
    haversine_distance_m,
)
from fitness_tracker.hardware.recorder import Recorder, RecorderSensorKind


def _fix(latitude=0.0, **kwargs):
    accuracy_m = kwargs.pop("accuracy_m", 5.0)
    return LocationFix(latitude_deg=latitude, longitude_deg=0.0, accuracy_m=accuracy_m, **kwargs)


def test_provider_speed_and_derived_speed_share_cumulative_distance():
    processor = GpsMetricProcessor(12.0)
    first = _fix(speed_mps=3.0, altitude_m=100.0)
    second = _fix(0.0001, altitude_m=101.0)
    initial = processor.process(first, 1000)
    moving = processor.process(second, 11000)
    assert initial.speed_mps == 3.0
    assert initial.distance_m == 0
    assert moving.speed_mps == pytest.approx(haversine_distance_m(first, second) / 10)
    assert moving.distance_m == pytest.approx(moving.speed_mps * 10)
    assert moving.altitude_m == 101.0


@pytest.mark.parametrize("speed_mps", [None, 0.0, 0.05])
def test_stationary_jitter_does_not_accumulate_distance(speed_mps):
    processor = GpsMetricProcessor(12.0)
    location_filter = LocationFilter(LocationPolicy.outdoor(), max_speed_mps=12.0)
    for index in range(600):
        fix = _fix(0.00001 if index % 2 else -0.00001, speed_mps=speed_mps)
        accepted = location_filter.accept(fix, index * 1000)
        assert accepted is not None
        metrics = processor.process(accepted, index * 1000)
        assert metrics.distance_m == 0.0
    assert metrics.speed_mps == 0.0


def test_slow_sustained_movement_survives_the_accuracy_gate():
    processor = GpsMetricProcessor(12.0)
    first = _fix()
    processor.process(first, 0)
    for index in range(1, 601):
        metrics = processor.process(_fix(index * 0.000005), index * 1000)
    actual_distance = haversine_distance_m(first, _fix(0.003))

    assert actual_distance - 10 <= metrics.distance_m <= actual_distance
    assert metrics.speed_mps == pytest.approx(haversine_distance_m(first, _fix(0.000005)))


def test_derived_speed_reaches_zero_after_stopping_and_resets_after_a_gap():
    processor = GpsMetricProcessor(12.0)
    for index in range(41):
        moving = processor.process(_fix(index * 0.00002), index * 1000)
    for index in range(41, 51):
        stopped = processor.process(_fix(0.0008), index * 1000)

    assert stopped.speed_mps == 0.0
    assert stopped.distance_m == moving.distance_m

    resumed = processor.process(_fix(0.001), 70000)

    assert resumed.speed_mps is None
    assert resumed.distance_m == stopped.distance_m

    processor.break_segment()

    assert processor.process(_fix(0.002), 71000).speed_mps is None


def test_first_fix_does_not_invent_speed_or_cadence():
    metrics = GpsMetricProcessor(12.0).process(_fix(), 0)
    assert metrics.speed_mps is None
    assert metrics.distance_m == 0
    assert metrics.altitude_m is None


def test_bad_fixes_do_not_inflate_distance():
    processor = GpsMetricProcessor(12.0)
    processor.process(_fix(), 1000)
    assert processor.process(_fix(1.0), 2000) is None
    assert processor.process(_fix(0.00001), 1000) is None
    assert processor.process(LocationFix(0.0, 0.0, accuracy_m=500), 2000) is None
    assert (
        processor.process(_fix(source_time_utc=datetime.now(UTC) - timedelta(minutes=1)), 2000)
        is None
    )
    metrics = processor.process(_fix(0.00002, speed_mps=500), 3000)
    assert metrics.speed_mps is None
    assert metrics.distance_m < 3


def test_provider_time_rejects_repeated_and_reversed_readings():
    processor = GpsMetricProcessor(12.0)
    now = datetime.now(UTC)
    processor.process(_fix(source_time_utc=now), 1000)
    assert processor.process(_fix(0.00001, source_time_utc=now), 2000) is None
    assert (
        processor.process(_fix(0.00001, source_time_utc=now - timedelta(seconds=1)), 3000) is None
    )
    metrics = processor.process(_fix(0.00002, source_time_utc=now + timedelta(seconds=2)), 4000)
    assert metrics.speed_mps is None
    assert metrics.distance_m == 0.0


def test_stationary_reading_and_signal_gap_do_not_add_movement():
    processor = GpsMetricProcessor(12.0)
    processor.process(_fix(), 0)
    assert processor.process(_fix(0.00001, speed_mps=0), 1000).distance_m == 0
    assert processor.process(_fix(0.001), 20000).distance_m == 0
    moving = processor.process(_fix(0.00102), 21000)
    processor.break_segment()
    assert processor.process(_fix(0.1), 22000).distance_m == moving.distance_m


class _Source:
    async def start(self, policy, on_fix, on_state):
        self.policy = policy
        self.on_fix = on_fix
        self.on_state = on_state
        on_state(LocationState.ACQUIRING, None)

    async def stop(self):
        pass


@pytest.fixture
def gps_recorder(monkeypatch):
    recorders = []
    now = [1000]
    monkeypatch.setattr(recorder_module, "relative_timestamp_ms", lambda _origin: now[0])

    def build(
        sport=SportTypesEnum.running,
        *,
        route=False,
        source="gps",
        environment=Environment.OUTDOOR,
        hr_address=None,
    ):
        location = _Source()
        samples = []
        recorder = Recorder(
            profile=SensorProfile(
                speed_source=source,
                speed_name="Old BLE",
                speed_address="AA",
                hr_address=hr_address,
            ),
            weight_kg=None,
            sport_type=sport,
            database=DatabaseManager("sqlite:///:memory:"),
            on_error=Mock(),
            on_sample_update=samples.append,
            location_source=location,
            record_outdoor_routes=route,
        )
        recorders.append(recorder)
        recorder.start_recording(environment)
        recorder.loop.run_until_complete(asyncio.sleep(0))
        return recorder, location, samples, now

    yield build
    for recorder in recorders:
        recorder.stop_recording()
        recorder.loop.run_until_complete(asyncio.sleep(0))
        recorder.shutdown()


@pytest.mark.parametrize("sport", [SportTypesEnum.running, SportTypesEnum.biking])
@pytest.mark.parametrize("accuracy_m", [3.0, 5.0, 10.0])
@pytest.mark.parametrize("pattern", ["straight", "shuttle"])
def test_continuous_derived_speed_handles_turnarounds(gps_recorder, sport, accuracy_m, pattern):
    recorder, source, samples, now = gps_recorder(sport)
    meters_per_degree = haversine_distance_m(_fix(), _fix(1))
    averages = MetricAverages()
    for index in range(121):
        now[0] = index * 1000
        position_m = index * 3 if pattern == "straight" else min(index % 10, 10 - index % 10) * 3
        source.on_fix(_fix(position_m / meters_per_degree, accuracy_m=accuracy_m))
        averages.observe(speed_mps=samples[-1].speed_mps)
    assert all(sample.speed_mps is None for sample in samples[:10])
    assert [sample.speed_mps for sample in samples[10:]] == pytest.approx([3.0] * 111)
    assert averages.values()["speed_mps"] == pytest.approx(3.0)
    assert recorder.speed_connected
    recorder.stop_recording()
    model = RunningMetrics if sport == SportTypesEnum.running else CyclingMetrics
    with recorder.store.Session() as session:
        rows = session.query(model).order_by(model.timestamp_ms).all()
        assert [row.speed_mps for row in rows[10:]] == pytest.approx([3.0] * 111)


def test_derived_speed_retains_a_curved_path():
    processor = GpsMetricProcessor(12.0)
    meters_per_degree = haversine_distance_m(_fix(), _fix(1))
    speeds = []
    for index in range(61):
        angle = index * 5 / 20
        metrics = processor.process(
            LocationFix(
                latitude_deg=20 * cos(angle) / meters_per_degree,
                longitude_deg=20 * sin(angle) / meters_per_degree,
                accuracy_m=3.0,
            ),
            index * 1000,
        )
        if index >= 10:
            speeds.append(metrics.speed_mps)

    assert all(4.5 <= speed <= 5.0 for speed in speeds)


@pytest.mark.parametrize(
    ("sport", "motion_role"),
    [(SportTypesEnum.running, "rsc"), (SportTypesEnum.biking, "csc")],
)
@pytest.mark.parametrize("source", ["ble", "gps"])
@pytest.mark.parametrize(
    "events",
    ["motion_first", "power_first", "power_disconnect", "failed_reconnect", "motion_disconnect"],
)
def test_independent_bluetooth_links_preserve_other_sensor_roles(
    gps_recorder,
    sport,
    motion_role,
    source,
    events,
):
    recorder, location, _samples, _now = gps_recorder(sport, source=source)
    if source == "gps":
        location.on_fix(_fix(speed_mps=3))
    addresses = ("power", "motion") if events == "power_first" else ("motion", "power")
    for address in addresses:
        if events == "failed_reconnect" and address == "power":
            continue
        role = motion_role if address == "motion" else "cps"
        recorder._on_running_link(address, connected=True, roles={role: True})
    if events in {"power_disconnect", "failed_reconnect"}:
        recorder._on_running_link("power", connected=False, roles={})
    elif events == "motion_disconnect":
        recorder._on_running_link("motion", connected=False, roles={})

    motion = events != "motion_disconnect"
    assert recorder.speed_connected is (source == "gps" or motion)
    assert recorder.distance_connected is (source == "gps" or motion)
    assert recorder.cadence_connected is motion
    assert recorder.power_connected is (events not in {"power_disconnect", "failed_reconnect"})


@pytest.mark.parametrize("stop_fails", [False, True])
def test_speed_loop_cleanup_clears_all_link_roles(gps_recorder, monkeypatch, stop_fails):
    recorder, _location, _samples, _now = gps_recorder(source="ble")
    recorder._on_running_link("motion", connected=True, roles={"rsc": True})
    recorder._on_running_link("power", connected=True, roles={"cps": True})
    mux = SimpleNamespace(
        start=AsyncMock(side_effect=BleakError("disconnected")),
        stop=AsyncMock(side_effect=BleakError("stop failed") if stop_fails else None),
    )
    monkeypatch.setattr(recorder_module, "RunningMux", Mock(return_value=mux))

    with pytest.raises(BleakError):
        recorder.loop.run_until_complete(recorder._speed_loop())

    assert recorder._link_roles == {}
    assert not recorder.speed_connected
    assert not recorder.cadence_connected
    assert not recorder.power_connected
    assert not recorder.distance_connected


def test_unknown_speed_preserves_connection_until_signal_loss(gps_recorder):
    recorder, source, samples, now = gps_recorder()
    source.on_fix(_fix(speed_mps=3))
    now[0] += 1000
    source.on_fix(_fix(0.00002))

    assert samples[-1].speed_mps is None
    assert recorder.speed_connected

    recorder._expire_gps_signal(recorder._recording_generation, recorder._location_operation)

    assert not recorder.speed_connected


def test_bluetooth_scan_and_connection_failures_do_not_stop_gps(gps_recorder, monkeypatch):
    discover = AsyncMock(side_effect=BleakError("Bluetooth is disabled"))
    mux = SimpleNamespace(
        start=AsyncMock(side_effect=BleakError("No Bluetooth adapter")),
        stop=AsyncMock(),
    )
    monkeypatch.setattr(recorder_module.BleakScanner, "discover", discover)
    monkeypatch.setattr(recorder_module, "HeartRateMux", Mock(return_value=mux))
    recorder, source, samples, now = gps_recorder(hr_address="HR")

    async def exercise():
        workflow = asyncio.create_task(recorder._workflow())
        try:
            for _ in range(10):
                await asyncio.sleep(0)
            discover.assert_awaited_once()
            mux.start.assert_awaited_once()
            assert not workflow.done()
            assert not recorder.loop.is_closed()

            source.on_fix(_fix(speed_mps=3))
            now[0] += 1000
            source.on_fix(_fix(0.00002, speed_mps=3))

            assert recorder.speed_connected
            assert recorder.distance_connected
            assert samples[-1].distance_m > 2
        finally:
            recorder._stop_requested.set()
            recorder._stop_event.set()
            await workflow
        mux.stop.assert_awaited_once()

    recorder.loop.run_until_complete(exercise())


@pytest.mark.parametrize("sport", [SportTypesEnum.running, SportTypesEnum.biking])
@pytest.mark.parametrize("route", [False, True])
def test_gps_records_metrics_without_bluetooth_and_respects_route_privacy(
    gps_recorder,
    sport,
    route,
):
    recorder, source, samples, now = gps_recorder(sport, route=route)
    assert not recorder._configured_sensors[RecorderSensorKind.SPEED].configured
    assert source.policy.distance_threshold_m == 0
    source.on_fix(_fix(speed_mps=3.0, altitude_m=100))
    now[0] += 1000
    source.on_fix(_fix(0.00002, speed_mps=3.0, altitude_m=101))
    assert recorder.speed_connected
    assert recorder.distance_connected
    assert not recorder.cadence_connected
    assert samples[-1].timestamp_ms == 2000
    assert samples[-1].distance_m > 2
    recorder.stop_recording()
    model = RunningMetrics if sport == SportTypesEnum.running else CyclingMetrics
    with recorder.store.Session() as session:
        rows = session.query(model).order_by(model.timestamp_ms).all()
        assert len(rows) == 2
        assert rows[-1].speed_mps == 3.0
        assert rows[-1].total_distance_m == samples[-1].distance_m
        assert rows[-1].altitude_m == 101
        assert rows[-1].source == "gps"
        assert rows[-1].power_watts is None
        assert session.query(LocationPoint).count() == (2 if route else 0)
        stats = session.query(ActivityStats).one()
        assert stats.distance_m == samples[-1].distance_m
        assert stats.avg_cadence is None
    assert not recorder.speed_connected
    assert not recorder.distance_connected


@pytest.mark.parametrize("sport", [SportTypesEnum.running, SportTypesEnum.biking])
def test_bluetooth_cannot_override_selected_gps_metrics(gps_recorder, sport):
    recorder, source, samples, _now = gps_recorder(sport)
    source.on_fix(_fix(speed_mps=3.0))
    if sport == SportTypesEnum.running:
        recorder._handle_running_sample(
            RunningSample(
                timestamp_ms=9000,
                speed_mps=20,
                distance_m=999,
                cadence_spm=170,
                power_watts=250,
            ),
        )
    else:
        recorder._handle_cycling_sample(
            CyclingSample(
                timestamp_ms=9000,
                speed_mps=20,
                distance_m=999,
                cadence_rpm=90,
                power_watts=250,
            ),
        )
    ble_sample = samples[-1]
    assert ble_sample.speed_mps is None
    assert ble_sample.distance_m is None
    assert ble_sample.altitude_m is None
    assert ble_sample.power_watts == 250
    recorder._on_running_link("AA", connected=True, roles={"rsc": True, "cps": True})
    source.on_state(LocationState.UNAVAILABLE, "lost")
    assert not recorder.speed_connected
    assert not recorder.distance_connected
    assert recorder.power_connected


def test_timeout_and_next_recording_do_not_bridge_distance(gps_recorder):
    recorder, source, samples, now = gps_recorder()
    source.on_fix(_fix(speed_mps=3))
    now[0] += 1000
    source.on_fix(_fix(0.00002, speed_mps=3))
    distance = samples[-1].distance_m
    recorder._expire_gps_signal(recorder._recording_generation, recorder._location_operation)
    assert not recorder.speed_connected
    now[0] += 20000
    source.on_fix(_fix(0.001, speed_mps=3))
    assert recorder.speed_connected
    assert samples[-1].distance_m == distance
    old_callback = source.on_fix
    recorder.stop_recording()
    recorder.loop.run_until_complete(asyncio.sleep(0))
    recorder.start_recording(Environment.OUTDOOR)
    recorder.loop.run_until_complete(asyncio.sleep(0))
    old_callback(_fix(0.00101, speed_mps=3))
    assert samples[-1].distance_m == distance
    source.on_fix(_fix(0.00102, speed_mps=3))
    assert samples[-1].distance_m == 0


def test_route_only_does_not_publish_gps_sensor_data(gps_recorder):
    recorder, source, samples, _now = gps_recorder(route=True, source="ble")
    source.on_fix(_fix(speed_mps=3))
    assert samples == []
    assert not recorder.speed_connected


def test_indoor_gps_selection_does_not_start_movement_tracking(gps_recorder):
    recorder, _source, samples, _now = gps_recorder(environment=Environment.INDOOR)
    assert recorder.location_policy is None
    assert recorder._gps_metrics is None
    assert samples == []


@pytest.mark.parametrize("sport", [SportTypesEnum.running, SportTypesEnum.biking])
def test_gps_and_late_bluetooth_samples_share_the_recording_clock(gps_recorder, sport):
    recorder, source, samples, now = gps_recorder(sport)
    sample_type = RunningSample if sport == SportTypesEnum.running else CyclingSample
    handler = (
        recorder._handle_running_sample
        if sport == SportTypesEnum.running
        else recorder._handle_cycling_sample
    )
    for timestamp_ms in (30000, 31000):
        now[0] = timestamp_ms
        source.on_fix(_fix((timestamp_ms - 30000) * 0.00000002, speed_mps=3))
        recorder._handle_hr_sample(
            HeartRateSample(timestamp_ms=900000 + timestamp_ms, heart_rate_bpm=140),
        )
        handler(sample_type(timestamp_ms=800000 + timestamp_ms, power_watts=250))
        assert [sample.timestamp_ms for sample in samples[-3:]] == [timestamp_ms] * 3
    recorder.stop_recording()
    model = RunningMetrics if sport == SportTypesEnum.running else CyclingMetrics
    with recorder.store.Session() as session:
        assert [
            row.timestamp_ms for row in session.query(HeartRate).order_by(HeartRate.timestamp_ms)
        ] == [
            30000,
            31000,
        ]
        rows = session.query(model).all()
        assert sorted((row.timestamp_ms, row.source) for row in rows) == [
            (30000, "ble"),
            (30000, "gps"),
            (31000, "ble"),
            (31000, "gps"),
        ]


@pytest.mark.parametrize("sport", [SportTypesEnum.running, SportTypesEnum.biking])
def test_indoor_gps_profile_preserves_bluetooth_movement(gps_recorder, sport):
    recorder, _source, samples, _now = gps_recorder(sport, environment=Environment.INDOOR)
    recorder._on_running_link("AA", connected=True, roles={"rsc": True, "cps": True})
    recorder.set_incline(5.0)
    sample_type = RunningSample if sport == SportTypesEnum.running else CyclingSample
    handler = (
        recorder._handle_running_sample
        if sport == SportTypesEnum.running
        else recorder._handle_cycling_sample
    )
    for distance_m in (100.0, 110.0):
        handler(sample_type(timestamp_ms=1000, speed_mps=3, distance_m=distance_m))

    assert samples[-1].speed_mps == 3
    assert samples[-1].distance_m == 10
    assert samples[-1].altitude_m == 0.5
    assert recorder.speed_connected
    assert recorder.distance_connected


@pytest.mark.parametrize("sport", [SportTypesEnum.running, SportTypesEnum.biking])
def test_route_disabled_gps_export_preserves_measured_altitude(gps_recorder, sport):
    from xml.etree import ElementTree as ET  # noqa: PLC0415

    recorder, source, _samples, now = gps_recorder(sport)
    activity_id = recorder.activity_id
    source.on_fix(_fix(speed_mps=3, altitude_m=100))
    now[0] += 1000
    source.on_fix(_fix(0.00002, speed_mps=3, altitude_m=101))
    recorder.stop_recording()
    with recorder.store.Session() as session:
        assert session.query(LocationPoint).count() == 0
        generated = activity_to_tcx(
            act=session.get(Activity, activity_id),
            heart_rates=[],
            running=session.query(RunningMetrics).all(),
            cycling=session.query(CyclingMetrics).all(),
            sport_type=sport,
        )
    root = ET.fromstring(generated)  # noqa: S314 - parsing exporter-generated XML
    namespace = {"tcx": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"}
    assert [element.text for element in root.findall(".//tcx:AltitudeMeters", namespace)] == [
        "100.000",
        "101.000",
    ]
    assert root.find(".//tcx:Position", namespace) is None


def test_gps_altitude_jitter_does_not_inflate_elevation():
    samples = [
        RunningMetrics(altitude_m=100 + (3 if index % 2 else -3), source="gps")
        for index in range(3600)
    ]

    assert _calc_elevation(samples) == (0.0, 0.0)


def test_flat_gps_altitude_noise_stays_bounded_over_an_hour():
    random = Random(0)
    samples = [
        RunningMetrics(altitude_m=100 + random.gauss(0, 3), source="gps") for _ in range(3600)
    ]

    ascent, descent = _calc_elevation(samples)

    assert ascent < 10
    assert descent < 10


def test_gps_elevation_retains_a_sustained_climb_and_descent():
    samples = [
        RunningMetrics(altitude_m=altitude, source="gps")
        for altitude in (
            *[100 + index * 0.25 for index in range(401)],
            *[200 - index * 0.25 for index in range(1, 401)],
        )
    ]

    ascent, descent = _calc_elevation(samples)

    assert 90 <= ascent <= 100
    assert 90 <= descent <= 100


@pytest.mark.parametrize("source", [None, "ble"])
def test_small_bluetooth_elevation_changes_remain_precise(source):
    samples = [RunningMetrics(altitude_m=altitude, source=source) for altitude in (0, 0.1, 0.2)]

    assert _calc_elevation(samples) == pytest.approx((0.2, 0.0))


def test_gps_selection_round_trips_and_legacy_settings_default_to_bluetooth(tmp_path):
    settings = AppSettings.load(tmp_path, create_if_missing=True)
    settings.running_sensors = SensorSettings(speed_source="gps")
    settings.save()
    reloaded = AppSettings.load(tmp_path)
    assert SensorProfile.from_sensor_settings(reloaded.running_sensors).speed_source == "gps"
    assert SensorSettings(speed_name="Footpod", speed_address="AA").speed_source == "ble"
