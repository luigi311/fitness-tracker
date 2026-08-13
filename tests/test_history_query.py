"""Tests for off-thread compare-chart data preparation."""

from datetime import UTC, datetime

from fitness_tracker.core.sports import SportTypesEnum
from fitness_tracker.core.units import UnitSystem
from fitness_tracker.data.models import RunningMetrics
from fitness_tracker.database import DatabaseManager
from fitness_tracker.services.history_query import (
    MAX_COMPARE_POINTS,
    CompareActivity,
    CompareChartRequest,
    _running_values,
    build_compare_chart_data,
    downsample_lttb,
)
from fitness_tracker.services.jobs import CancellationToken
from sqlalchemy import event

PEAK_INDEX = 50
PEAK_VALUE = 100.0
TARGET_POINTS = 12
GENERATION = 4
EXPECTED_SELECTS = 1


def test_lttb_retains_endpoints_and_a_sharp_peak() -> None:
    points = tuple(
        (float(index), PEAK_VALUE if index == PEAK_INDEX else float(index % 3))
        for index in range(100)
    )

    sampled = downsample_lttb(points, TARGET_POINTS)

    assert len(sampled) == TARGET_POINTS
    assert sampled[0] == points[0]
    assert sampled[-1] == points[-1]
    assert max(y for _x, y in sampled) == PEAK_VALUE


def test_running_cadence_chart_uses_display_steps_per_minute() -> None:
    points = ((0, 2.5, 85, None),)

    values = _running_values(points, "cadence", UnitSystem.METRIC)

    assert values == [(0, 170.0)]


def test_compare_query_is_batched_and_caps_two_hour_series() -> None:
    db = DatabaseManager("sqlite:///:memory:")
    activity_id = db.start_activity(SportTypesEnum.running)
    sample_count = 7_200
    with db.Session() as session:
        session.add_all(
            RunningMetrics(
                activity_id=activity_id,
                timestamp_ms=index * 1_000,
                speed_mps=2.5 + (index % 20) / 100.0,
                cadence_spm=170 + index % 4,
                power_watts=220.0 + index % 7,
            )
            for index in range(sample_count)
        )
        session.commit()

    request = CompareChartRequest(
        generation=GENERATION,
        metric="speed",
        unit_system=UnitSystem.METRIC,
        activities=(
            CompareActivity(
                activity_id=activity_id,
                sport=SportTypesEnum.running,
                start_time=datetime(2026, 1, 1, tzinfo=UTC),
            ),
        ),
    )
    token = CancellationToken()
    select_count = 0

    def count_selects(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        nonlocal select_count
        if statement.lstrip().upper().startswith("SELECT"):
            select_count += 1

    event.listen(db.engine, "before_cursor_execute", count_selects)
    try:
        data = build_compare_chart_data(request, db.repository, token)
    finally:
        event.remove(db.engine, "before_cursor_execute", count_selects)

    assert select_count == EXPECTED_SELECTS
    assert data.generation == GENERATION
    assert len(data.series) == 1
    assert len(data.series[0].xs) <= MAX_COMPARE_POINTS
    assert data.series[0].xs[0] == 0.0
    assert data.series[0].xs[-1] == sample_count - 1
