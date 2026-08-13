"""Batch and transform history chart samples outside the GTK thread."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003  # Pydantic resolves this field at runtime.
from math import floor, isfinite
from typing import TYPE_CHECKING, Final, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict

from fitness_tracker.core.sports import SportTypesEnum
from fitness_tracker.core.units import (
    UnitSystem,
    display_cadence,
    pace_minutes_per_unit,
    speed_in_units,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fitness_tracker.data.repositories import (
        ActivityRepository,
        CyclingSeriesPoint,
        RunningSeriesPoint,
    )
    from fitness_tracker.services.jobs import CancellationToken

CompareMetric = Literal["hr", "pace", "speed", "power", "cadence"]
ChartPoint = tuple[float, float | None]

MAX_COMPARE_POINTS: Final = 1_200
_LTTB_ENDPOINT_COUNT: Final = 2
_MIN_SMOOTHING_SAMPLES: Final = 2
_MIN_SMOOTHING_WINDOW: Final = 3
_SMOOTHING_SECONDS: Final = 15
_MILLISECONDS_PER_SECOND: Final = 1_000.0


class CompareActivity(BaseModel):
    """Immutable activity metadata captured before a chart query starts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    activity_id: int
    sport: SportTypesEnum
    start_time: datetime


class CompareChartRequest(BaseModel):
    """Immutable input for one compare-chart generation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    generation: int
    metric: CompareMetric
    unit_system: UnitSystem
    activities: tuple[CompareActivity, ...]
    max_points: int = MAX_COMPARE_POINTS


class ChartSeries(BaseModel):
    """One display-ready, smoothed and downsampled activity series."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    activity_id: int
    start_time: datetime
    xs: tuple[float, ...]
    ys: tuple[float | None, ...]


class CompareChartData(BaseModel):
    """Immutable result delivered to the GTK renderer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    generation: int
    metric: CompareMetric
    series: tuple[ChartSeries, ...]
    max_time_s: float


def _rolling(
    ys: list[float | None],
    window: int,
    *,
    use_median: bool = False,
) -> list[float | None]:
    """Return a vectorized rolling mean or median, ignoring missing values."""
    if window <= 1 or not ys:
        return ys
    arr = np.array([np.nan if value is None else float(value) for value in ys], dtype=float)
    n = len(arr)
    half = window // 2
    left = np.maximum(0, np.arange(n) - half)
    right = np.minimum(n, np.arange(n) + half + 1)

    if use_median:
        padded = np.pad(arr, (half, half), constant_values=np.nan)
        with np.errstate(all="ignore"):
            smoothed = np.nanmedian(
                np.lib.stride_tricks.sliding_window_view(padded, 2 * half + 1),
                axis=1,
            )
    else:
        valid = ~np.isnan(arr)
        values = np.nan_to_num(arr, nan=0.0)
        sums = np.concatenate(([0.0], np.cumsum(values)))
        counts = np.concatenate(([0], np.cumsum(valid, dtype=int)))
        totals = sums[right] - sums[left]
        count = counts[right] - counts[left]
        smoothed = np.full(n, np.nan)
        valid_bins = count > 0
        smoothed[valid_bins] = totals[valid_bins] / count[valid_bins]

    return [None if np.isnan(value) else float(value) for value in smoothed]


def downsample_lttb(
    points: Sequence[ChartPoint],
    max_points: int,
) -> tuple[ChartPoint, ...]:
    """Downsample a series with Largest-Triangle-Three-Buckets.

    Missing values are omitted from the display series. The raw samples remain
    available through the repository for exports and other consumers.
    """
    if max_points <= 0:
        message = f"max_points must be positive, got {max_points}"
        raise ValueError(message)
    valid = tuple((float(x), float(y)) for x, y in points if y is not None and isfinite(float(y)))
    if len(valid) <= max_points:
        return valid
    if max_points == 1:
        return (valid[0],)
    if max_points == _LTTB_ENDPOINT_COUNT:
        return (valid[0], valid[-1])

    every = (len(valid) - _LTTB_ENDPOINT_COUNT) / (max_points - _LTTB_ENDPOINT_COUNT)
    sampled: list[ChartPoint] = [valid[0]]
    a_index = 0

    for bucket_index in range(max_points - _LTTB_ENDPOINT_COUNT):
        average_start = floor((bucket_index + 1) * every) + 1
        average_end = floor((bucket_index + 2) * every) + 1
        average_end = min(average_end, len(valid))
        average_bucket = valid[average_start:average_end]
        if average_bucket:
            average_x = sum(point[0] for point in average_bucket) / len(average_bucket)
            average_y = sum(point[1] for point in average_bucket) / len(average_bucket)
        else:
            average_x, average_y = valid[-1]

        candidate_start = floor(bucket_index * every) + 1
        candidate_end = min(floor((bucket_index + 1) * every) + 1, len(valid) - 1)
        if candidate_end <= candidate_start:
            candidate_end = min(candidate_start + 1, len(valid) - 1)

        point_a = valid[a_index]
        ax, ay = point_a
        best_index = candidate_start
        best_area = -1.0
        for candidate_index in range(candidate_start, candidate_end):
            px, py = valid[candidate_index]
            area = abs(
                (ax - average_x) * (py - ay) - (ax - px) * (average_y - ay),
            )
            if area > best_area:
                best_area = area
                best_index = candidate_index

        sampled.append(valid[best_index])
        a_index = best_index

    sampled.append(valid[-1])
    return tuple(sampled)


def build_compare_chart_data(
    request: CompareChartRequest,
    repository: ActivityRepository,
    token: CancellationToken,
) -> CompareChartData:
    """Query, smooth, and downsample one compare request off the UI thread."""
    token.raise_if_cancelled()
    activity_ids = tuple(activity.activity_id for activity in request.activities)
    heart_rate_series: dict[int, list[tuple[int, int]]] = {}
    running_series: dict[int, list[RunningSeriesPoint]] = {}
    cycling_series: dict[int, list[CyclingSeriesPoint]] = {}

    if request.metric == "hr":
        heart_rate_series = repository.list_heart_rate_series(activity_ids)
    else:
        running_ids = tuple(
            activity.activity_id
            for activity in request.activities
            if activity.sport == SportTypesEnum.running
        )
        cycling_ids = tuple(
            activity.activity_id
            for activity in request.activities
            if activity.sport == SportTypesEnum.biking
        )
        if running_ids:
            running_series = repository.list_running_metric_series(running_ids)
        if cycling_ids:
            cycling_series = repository.list_cycling_metric_series(cycling_ids)

    chart_series: list[ChartSeries] = []
    max_time_s = 0.0
    for activity in request.activities:
        token.raise_if_cancelled()
        values = _activity_values(
            activity,
            request,
            heart_rate_series,
            running_series,
            cycling_series,
        )
        if not values:
            continue

        t0 = values[0][0]
        xs = [(timestamp_ms - t0) / _MILLISECONDS_PER_SECOND for timestamp_ms, _value in values]
        ys = [value for _timestamp_ms, value in values]
        if len(xs) >= _MIN_SMOOTHING_SAMPLES:
            dt = (xs[-1] - xs[0]) / max(1, len(xs) - 1)
            sample_hz = 1.0 / dt if dt > 0 else 1.0
        else:
            sample_hz = 1.0
        window = max(_MIN_SMOOTHING_WINDOW, round(_SMOOTHING_SECONDS * sample_hz))
        smoothed = _rolling(ys, window)
        sampled = downsample_lttb(
            tuple(zip(xs, smoothed, strict=True)),
            request.max_points,
        )
        if not sampled:
            continue

        sampled_xs = tuple(point[0] for point in sampled)
        sampled_ys = tuple(point[1] for point in sampled)
        chart_series.append(
            ChartSeries(
                activity_id=activity.activity_id,
                start_time=activity.start_time,
                xs=sampled_xs,
                ys=sampled_ys,
            ),
        )
        max_time_s = max(max_time_s, sampled_xs[-1])

    return CompareChartData(
        generation=request.generation,
        metric=request.metric,
        series=tuple(chart_series),
        max_time_s=max_time_s,
    )


def _activity_values(
    activity: CompareActivity,
    request: CompareChartRequest,
    heart_rate_series: dict[int, list[tuple[int, int]]],
    running_series: dict[int, list[RunningSeriesPoint]],
    cycling_series: dict[int, list[CyclingSeriesPoint]],
) -> list[tuple[int, float | None]]:
    if request.metric == "hr":
        return [
            (timestamp_ms, float(bpm))
            for timestamp_ms, bpm in heart_rate_series.get(activity.activity_id, [])
        ]

    if activity.sport == SportTypesEnum.running:
        return _running_values(
            running_series.get(activity.activity_id, []),
            request.metric,
            request.unit_system,
        )
    if activity.sport == SportTypesEnum.biking:
        return _cycling_values(
            cycling_series.get(activity.activity_id, []),
            request.metric,
            request.unit_system,
        )
    return []


def _running_values(
    points: Sequence[RunningSeriesPoint],
    metric: CompareMetric,
    unit_system: UnitSystem,
) -> list[tuple[int, float | None]]:
    values: list[tuple[int, float | None]] = []
    for timestamp_ms, speed_mps, cadence_spm, power_watts in points:
        if metric == "pace":
            pace = pace_minutes_per_unit(speed_mps, unit_system)
            value = pace if isfinite(pace) else None
        elif metric == "speed":
            value = speed_in_units(speed_mps, unit_system)
        elif metric == "power":
            value = None if power_watts is None else float(power_watts)
        elif metric == "cadence":
            value = float(display_cadence(cadence_spm, SportTypesEnum.running))
        else:
            value = None
        values.append((timestamp_ms, value))
    return values


def _cycling_values(
    points: Sequence[CyclingSeriesPoint],
    metric: CompareMetric,
    unit_system: UnitSystem,
) -> list[tuple[int, float | None]]:
    values: list[tuple[int, float | None]] = []
    for timestamp_ms, speed_mps, cadence_rpm, power_watts in points:
        if metric == "speed":
            value = speed_in_units(speed_mps, unit_system)
        elif metric == "power":
            value = None if power_watts is None else float(power_watts)
        elif metric == "cadence":
            value = None if cadence_rpm is None else float(cadence_rpm)
        else:
            value = None
        values.append((timestamp_ms, value))
    return values
