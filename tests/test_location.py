"""Pure location domain and filtering contracts."""

import asyncio
import math
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from sys import float_info

import pytest
from fitness_tracker.core.environment import Environment
from fitness_tracker.core.sports import SportTypesEnum
from fitness_tracker.hardware.location import (
    LocationFilter,
    LocationFix,
    LocationPolicy,
    LocationState,
    PortalAccuracy,
    haversine_distance_m,
    is_plausible_motion,
    location_policy_for_environment,
    max_speed_mps_for_sport,
    portal_accuracy_for_setting,
    relative_timestamp_ms,
)
from tests.conftest import FakeLocationSource, FakeLocationSourceFactory

EXPECTED_ACCEPTED_POINTS = 2
EXPECTED_LAST_TIMESTAMP_MS = 2_000


def _fix(
    latitude: float = 39.7392,
    longitude: float = -104.9903,
    *,
    accuracy_m: float | None = None,
    altitude_m: float | None = None,
    speed_mps: float | None = None,
    heading_deg: float | None = None,
    source_time_utc: datetime | None = None,
) -> LocationFix:
    return LocationFix(
        latitude,
        longitude,
        accuracy_m=accuracy_m,
        altitude_m=altitude_m,
        speed_mps=speed_mps,
        heading_deg=heading_deg,
        source_time_utc=source_time_utc,
    )


@pytest.mark.parametrize(
    ("latitude", "longitude"),
    [(-90.0, -180.0), (90.0, 180.0), (0.0, 0.0)],
)
def test_location_fix_accepts_coordinate_boundaries(latitude: float, longitude: float) -> None:
    fix = _fix(latitude, longitude)

    assert fix.latitude_deg == latitude
    assert fix.longitude_deg == longitude


@pytest.mark.parametrize(
    ("latitude", "longitude", "message"),
    [
        (math.nan, 0.0, "latitude_deg"),
        (math.inf, 0.0, "latitude_deg"),
        (0.0, -math.inf, "longitude_deg"),
        (90.001, 0.0, "latitude_deg must be between"),
        (0.0, 180.001, "longitude_deg must be between"),
    ],
)
def test_location_fix_rejects_invalid_coordinates(
    latitude: float,
    longitude: float,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _fix(latitude, longitude)


def test_location_fix_normalizes_invalid_optional_values() -> None:
    source_time = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    fix = _fix(
        accuracy_m=-1.0,
        altitude_m=math.inf,
        speed_mps=-2.0,
        heading_deg=360.0,
        source_time_utc=source_time,
    )

    assert fix.accuracy_m is None
    assert fix.altitude_m is None
    assert fix.speed_mps is None
    assert fix.heading_deg is None
    assert fix.source_time_utc == source_time


def test_location_fix_normalizes_geoclue_unknown_altitude() -> None:
    fix = _fix(altitude_m=-float_info.max)

    assert fix.altitude_m is None


def test_location_fix_normalizes_aware_source_time_to_utc() -> None:
    source_time = datetime.fromisoformat("2026-08-15T06:00:00-06:00")

    fix = _fix(source_time_utc=source_time)

    assert fix.source_time_utc == datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def test_location_policies_match_recording_decisions() -> None:
    outdoor = location_policy_for_environment(Environment.OUTDOOR)
    indoor = location_policy_for_environment(
        Environment.INDOOR,
        record_indoor_anchor=True,
        indoor_accuracy=PortalAccuracy.CITY,
    )

    assert outdoor == LocationPolicy.outdoor()
    assert indoor == LocationPolicy.anchor(PortalAccuracy.CITY)
    assert indoor.acquisition_timeout_s is not None
    assert indoor.acquisition_timeout_s > 12.5 * 60
    assert location_policy_for_environment(Environment.TRAINER) is None
    assert (
        location_policy_for_environment(
            Environment.OUTDOOR,
            record_outdoor_routes=False,
        )
        is None
    )


@pytest.mark.parametrize("field_name", ["max_accuracy_m", "acquisition_timeout_s"])
def test_location_policy_normalizes_finite_real_conversion_overflow(field_name: str) -> None:
    with pytest.raises(
        ValueError,
        match=rf"{field_name} must be a finite positive number or None",
    ):
        LocationPolicy(
            accuracy=PortalAccuracy.EXACT,
            time_threshold_s=0,
            distance_threshold_m=0,
            **{field_name: Fraction(10**10_000, 1)},
        )


@pytest.mark.parametrize(
    ("setting", "expected"),
    [
        ("city", PortalAccuracy.CITY),
        ("neighborhood", PortalAccuracy.NEIGHBORHOOD),
        ("street", PortalAccuracy.STREET),
        ("exact", PortalAccuracy.EXACT),
    ],
)
def test_portal_accuracy_setting_conversion(setting: str, expected: PortalAccuracy) -> None:
    assert portal_accuracy_for_setting(setting) is expected


def test_portal_accuracy_setting_conversion_rejects_unknown_value() -> None:
    with pytest.raises(ValueError, match="Unknown indoor location accuracy"):
        portal_accuracy_for_setting("country")


@pytest.mark.parametrize(
    ("sport_type", "expected_max_speed_mps"),
    [
        (SportTypesEnum.running, 12.0),
        (SportTypesEnum.biking, 35.0),
        (SportTypesEnum.unknown, 50.0),
    ],
)
def test_max_speed_helper_returns_conservative_sport_limits(
    sport_type: SportTypesEnum,
    expected_max_speed_mps: float,
) -> None:
    assert max_speed_mps_for_sport(sport_type) == expected_max_speed_mps


def test_outdoor_filter_accepts_ordered_points_and_suppresses_exact_duplicates() -> None:
    source_time = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    first = _fix(source_time_utc=source_time)
    duplicate = _fix(source_time_utc=source_time)
    later = _fix(latitude=39.7393, source_time_utc=source_time)
    location_filter = LocationFilter(
        LocationPolicy.outdoor(),
        max_speed_mps=max_speed_mps_for_sport(SportTypesEnum.running),
    )

    assert location_filter.accept(first, 0) == first
    assert location_filter.accept(duplicate, 1) is None
    assert location_filter.accept(later, 2_000) == later
    assert location_filter.accept(_fix(latitude=39.7394), 1) is None
    assert location_filter.accepted_count == EXPECTED_ACCEPTED_POINTS
    assert location_filter.last_timestamp_ms == EXPECTED_LAST_TIMESTAMP_MS


def test_anchor_filter_accepts_at_most_one_point() -> None:
    location_filter = LocationFilter(
        LocationPolicy.anchor(),
        max_speed_mps=max_speed_mps_for_sport(SportTypesEnum.running),
    )

    source_time = datetime.now(UTC)
    first = _fix(accuracy_m=100.0, source_time_utc=source_time)
    second = _fix(latitude=39.7393, accuracy_m=100.0, source_time_utc=source_time)

    assert location_filter.accept(first, 0) == first
    assert location_filter.accept(second, 1_000) is None
    assert location_filter.accepted_count == 1


def test_anchor_filter_waits_for_fresh_accurate_fix() -> None:
    receipt_time = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    location_filter = LocationFilter(
        LocationPolicy.anchor(PortalAccuracy.NEIGHBORHOOD),
        max_speed_mps=max_speed_mps_for_sport(SportTypesEnum.running),
    )
    coarse = _fix(
        accuracy_m=130_000.0,
        source_time_utc=receipt_time,
    )
    stale = _fix(
        latitude=39.7393,
        accuracy_m=100.0,
        source_time_utc=receipt_time - timedelta(minutes=6),
    )
    acceptable = _fix(
        latitude=39.7394,
        accuracy_m=100.0,
        source_time_utc=receipt_time,
    )

    assert (
        location_filter.accept(
            _fix(source_time_utc=receipt_time),
            0,
            receipt_time_utc=receipt_time,
        )
        is None
    )
    assert (
        location_filter.accept(
            _fix(accuracy_m=100.0),
            0,
            receipt_time_utc=receipt_time,
        )
        is None
    )
    assert location_filter.accept(coarse, 0, receipt_time_utc=receipt_time) is None
    assert location_filter.accept(stale, 1_000, receipt_time_utc=receipt_time) is None
    assert (
        location_filter.accept(acceptable, 2_000, receipt_time_utc=receipt_time)
        == acceptable
    )
    assert location_filter.accepted_count == 1


def test_filter_preserves_different_points_with_equal_timestamps() -> None:
    location_filter = LocationFilter(
        LocationPolicy.outdoor(),
        max_speed_mps=max_speed_mps_for_sport(SportTypesEnum.running),
    )

    assert location_filter.accept(_fix(), 0) is not None
    assert location_filter.accept(_fix(latitude=39.8400), 0) is not None


def test_location_filter_can_be_reset() -> None:
    location_filter = LocationFilter(
        LocationPolicy.anchor(),
        max_speed_mps=max_speed_mps_for_sport(SportTypesEnum.running),
    )
    source_time = datetime.now(UTC)
    first = _fix(accuracy_m=100.0, source_time_utc=source_time)
    second = _fix(latitude=39.7400, accuracy_m=100.0, source_time_utc=source_time)
    location_filter.accept(first, 10)

    location_filter.reset()

    assert location_filter.accept(second, 0) == second
    assert location_filter.accepted_count == 1


def test_relative_timestamps_are_nonnegative_and_monotonic_when_received_in_order() -> None:
    origin = 10_000_000_000
    timestamps = [
        relative_timestamp_ms(origin, receipt_ns)
        for receipt_ns in (origin - 1, origin, origin + 1_000_000, origin + 2_000_000)
    ]

    assert timestamps == [0, 0, 1, 2]
    assert timestamps == sorted(timestamps)


def test_haversine_distance_handles_short_and_antimeridian_segments() -> None:
    one_degree = haversine_distance_m(_fix(0.0, 0.0), _fix(1.0, 0.0))
    antimeridian = haversine_distance_m(_fix(0.0, 179.999), _fix(0.0, -179.999))

    assert one_degree == pytest.approx(111_195.080, rel=1e-5)
    assert antimeridian == pytest.approx(222.390, rel=1e-5)


def test_spike_filter_rejects_impossible_jump_without_plausible_accuracy_overlap() -> None:
    previous = _fix(0.0, 0.0, accuracy_m=5.0)
    current = _fix(0.1, 0.0, accuracy_m=5.0)

    assert not is_plausible_motion(
        previous,
        current,
        1_000,
        max_speed_mps=max_speed_mps_for_sport(SportTypesEnum.running),
    )


@pytest.mark.parametrize(
    "sport_type",
    [SportTypesEnum.running, SportTypesEnum.biking],
)
def test_spike_filter_accepts_normal_running_and_cycling_motion(
    sport_type: SportTypesEnum,
) -> None:
    previous = _fix()
    current = _fix(latitude=39.7393)

    assert is_plausible_motion(
        previous,
        current,
        1_000,
        max_speed_mps=max_speed_mps_for_sport(sport_type),
    )


@pytest.mark.parametrize(
    ("previous_accuracy_m", "current_accuracy_m"),
    [(None, 5.0), (5.0, None), (None, None)],
)
def test_spike_filter_keeps_fast_segment_when_accuracy_is_missing(
    previous_accuracy_m: float | None,
    current_accuracy_m: float | None,
) -> None:
    previous = _fix(0.0, 0.0, accuracy_m=previous_accuracy_m)
    current = _fix(0.1, 0.0, accuracy_m=current_accuracy_m)
    max_speed_mps = max_speed_mps_for_sport(SportTypesEnum.running)
    location_filter = LocationFilter(LocationPolicy.outdoor(), max_speed_mps=max_speed_mps)

    assert is_plausible_motion(
        previous,
        current,
        1_000,
        max_speed_mps=max_speed_mps,
    )
    assert location_filter.accept(previous, 0) == previous
    assert location_filter.accept(current, 1_000) == current


def test_spike_filter_keeps_high_speed_fix_when_accuracy_circles_overlap() -> None:
    previous = _fix(0.0, 0.0, accuracy_m=6_000.0)
    current = _fix(0.1, 0.0, accuracy_m=6_000.0)

    assert is_plausible_motion(
        previous,
        current,
        1_000,
        max_speed_mps=max_speed_mps_for_sport(SportTypesEnum.running),
    )


def test_fake_location_source_is_async_and_does_not_deliver_after_stop(
    fake_location_source: FakeLocationSourceFactory,
) -> None:
    source = fake_location_source()
    received: list[LocationFix] = []
    states: list[LocationState] = []

    async def exercise() -> None:
        await source.start(
            LocationPolicy.outdoor(),
            received.append,
            lambda state, _detail: states.append(state),
        )
        source.emit_fix(_fix(), timestamp_ms=0)
        await source.stop()
        source.emit_fix(_fix(latitude=39.7400), timestamp_ms=1)

    asyncio.run(exercise())

    assert received == [_fix()]
    assert states == [LocationState.ACQUIRING]
    assert source.start_count == 1
    assert source.stop_count == 1


def test_fake_location_source_delivers_multiple_outdoor_points(
    fake_location_source: FakeLocationSourceFactory,
) -> None:
    source = fake_location_source()
    received: list[LocationFix] = []

    async def exercise() -> None:
        await source.start(LocationPolicy.outdoor(), received.append, lambda _state, _detail: None)
        source.emit_fix(_fix(), timestamp_ms=0)
        source.emit_fix(_fix(latitude=39.7393), timestamp_ms=1_000)
        source.emit_fix(_fix(latitude=39.7394), timestamp_ms=2_000)
        await source.stop()

    asyncio.run(exercise())

    assert received == [_fix(), _fix(latitude=39.7393), _fix(latitude=39.7394)]


def test_fake_location_source_limits_indoor_and_trainer_anchors_to_one_point(
    fake_location_source: FakeLocationSourceFactory,
) -> None:
    policies = (
        location_policy_for_environment(Environment.INDOOR, record_indoor_anchor=True),
        location_policy_for_environment(Environment.TRAINER, record_indoor_anchor=True),
    )

    async def exercise(
        source: FakeLocationSource,
        policy: LocationPolicy,
        received: list[LocationFix],
    ) -> None:
        await source.start(policy, received.append, lambda _state, _detail: None)
        source_time = datetime.now(UTC)
        first = _fix(accuracy_m=100.0, source_time_utc=source_time)
        second = _fix(latitude=39.7393, accuracy_m=100.0, source_time_utc=source_time)
        source.emit_fix(first, timestamp_ms=0)
        source.emit_fix(second, timestamp_ms=1_000)
        await source.stop()
        assert received == [first]

    for policy in policies:
        assert policy is not None
        source = fake_location_source()
        received: list[LocationFix] = []

        asyncio.run(exercise(source, policy, received))

        assert source.start_count == 1
        assert source.stop_count == 1


def test_fake_location_source_filters_outdoor_spikes(
    fake_location_source: FakeLocationSourceFactory,
) -> None:
    source = fake_location_source()
    received: list[LocationFix] = []

    async def exercise() -> None:
        await source.start(
            LocationPolicy.outdoor(),
            received.append,
            lambda _state, _detail: None,
        )
        source.emit_fix(_fix(0.0, 0.0, accuracy_m=5.0), timestamp_ms=0)
        source.emit_fix(_fix(0.1, 0.0, accuracy_m=5.0), timestamp_ms=1_000)
        await source.stop()

    asyncio.run(exercise())

    assert received == [_fix(0.0, 0.0, accuracy_m=5.0)]
