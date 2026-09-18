# ruff: noqa: SLF001, PLR2004
"""GPS sensor calculations and their recording/persistence boundary."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from bleaksport import CyclingSample, RunningSample
from fitness_tracker.core.environment import Environment
from fitness_tracker.core.sensor_profile import SensorProfile
from fitness_tracker.core.settings import AppSettings, SensorSettings
from fitness_tracker.core.sports import SportTypesEnum
from fitness_tracker.data.models import ActivityStats, CyclingMetrics, LocationPoint, RunningMetrics
from fitness_tracker.database import DatabaseManager
from fitness_tracker.hardware import recorder as recorder_module
from fitness_tracker.hardware.gps_metrics import GpsMetricProcessor
from fitness_tracker.hardware.location import LocationFix, LocationState, haversine_distance_m
from fitness_tracker.hardware.recorder import Recorder, RecorderSensorKind


def _fix(latitude=0.0, **kwargs):
    return LocationFix(latitude_deg=latitude, longitude_deg=0.0, accuracy_m=5.0, **kwargs)


def test_provider_speed_and_derived_speed_share_cumulative_distance():
    processor = GpsMetricProcessor(12.0)
    first = _fix(speed_mps=3.0, altitude_m=100.0)
    second = _fix(0.00002, altitude_m=101.0)
    initial = processor.process(first, 1000)
    moving = processor.process(second, 2000)
    assert initial.speed_mps == 3.0
    assert initial.distance_m == 0
    assert moving.speed_mps == pytest.approx(haversine_distance_m(first, second))
    assert moving.distance_m == pytest.approx(moving.speed_mps)
    assert moving.altitude_m == 101.0


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
    assert metrics.speed_mps == pytest.approx(metrics.distance_m / 2)
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
    assert metrics.speed_mps == pytest.approx(metrics.distance_m / 2)


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
    ):
        location = _Source()
        samples = []
        recorder = Recorder(
            profile=SensorProfile(speed_source=source, speed_name="Old BLE", speed_address="AA"),
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


def test_gps_selection_round_trips_and_legacy_settings_default_to_bluetooth(tmp_path):
    settings = AppSettings.load(tmp_path, create_if_missing=True)
    settings.running_sensors = SensorSettings(speed_source="gps")
    settings.save()
    reloaded = AppSettings.load(tmp_path)
    assert SensorProfile.from_sensor_settings(reloaded.running_sensors).speed_source == "gps"
    assert SensorSettings(speed_name="Footpod", speed_address="AA").speed_source == "ble"
