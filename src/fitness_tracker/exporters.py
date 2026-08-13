from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, Protocol, cast
from xml.etree.ElementTree import Element, SubElement, tostring

from loguru import logger

from fitness_tracker.core.sports import SportTypesEnum

_TCX_SPORT_NAMES = {
    SportTypesEnum.running: "Running",
    SportTypesEnum.biking: "Biking",
    SportTypesEnum.unknown: "Other",
}


class _PrimarySample(Protocol):
    timestamp_ms: int
    speed_mps: float | None
    total_distance_m: float | None
    cadence_rpm: float | None
    cadence_spm: float | None
    power_watts: float | None
    altitude_m: float | None


if TYPE_CHECKING:
    from fitness_tracker.data.models import Activity, CyclingMetrics, HeartRate, RunningMetrics
# ---------- Helpers ----------


def _iso_with_local_offset(dt: datetime) -> str:
    """Render a datetime as ISO-8601 with its local timezone offset."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    # Convert to the machine's local timezone for human-friendly wall time
    local_dt = dt.astimezone()  # system local tz with correct DST
    return local_dt.isoformat(timespec="seconds")


def _sec_str(
    act: Activity,
    primary_samples: list[_PrimarySample],
    heart_rates: list[HeartRate],
) -> str:
    """
    Total time in seconds for the <Lap>. Prefer the DB end_time if present,
    otherwise fall back to the last sample (primary timeline or HR) timestamp.
    """
    if act.end_time:
        dur = max(0.0, (act.end_time - act.start_time).total_seconds())
        return f"{dur:.1f}"

    tmax = 0
    if primary_samples:
        tmax = max(tmax, int(primary_samples[-1].timestamp_ms))
    if heart_rates:
        tmax = max(tmax, int(heart_rates[-1].timestamp_ms))
    return f"{max(0.0, tmax / 1000.0):.1f}"


def _lap_distance_m_str(primary_samples: list[_PrimarySample]) -> str:
    """
    Distance for the lap in meters (string). Prefer the final total_distance_m
    if present; otherwise integrate speed over time as a fallback.
    """
    if not primary_samples:
        return "0.0"

    # Prefer device-reported total distance (already meters)
    dists = [s.total_distance_m for s in primary_samples if s.total_distance_m is not None]
    if dists:
        return f"{float(dists[-1]):.3f}"

    # Fallback: integrate v * dt
    total = 0.0
    last_ms = int(primary_samples[0].timestamp_ms)
    for s in primary_samples[1:]:
        ts = int(s.timestamp_ms)
        dt = max(0.0, (ts - last_ms) / 1000.0)  # seconds
        v = float(s.speed_mps or 0.0)
        total += max(0.0, v * dt)  # meters
        last_ms = ts
    return f"{total:.3f}"


_TimelineKind = Literal["running", "cycling", "hr"]
_ACTIVITY_EXTENSION_NS = "http://www.garmin.com/xmlschemas/ActivityExtension/v2"


def _build_tcx_lap(
    *,
    act: Activity,
    heart_rates: list[HeartRate],
    primary: list[_PrimarySample],
    sport_type: SportTypesEnum,
) -> tuple[Element, Element]:
    """Build the TCX root and the activity track container."""
    tcx = Element(
        "TrainingCenterDatabase",
        {
            "xmlns": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2",
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xsi:schemaLocation": (
                "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2 "
                "http://www.garmin.com/xmlschemas/TrainingCenterDatabasev2.xsd "
                "http://www.garmin.com/xmlschemas/ActivityExtension/v2 "
                "http://www.garmin.com/xmlschemas/ActivityExtensionv2.xsd"
            ),
            "xmlns:ns3": _ACTIVITY_EXTENSION_NS,
        },
    )
    activities = SubElement(tcx, "Activities")
    act_node = SubElement(activities, "Activity", {"Sport": _TCX_SPORT_NAMES[sport_type]})
    SubElement(act_node, "Id").text = _iso_with_local_offset(act.start_time)

    lap = SubElement(act_node, "Lap", {"StartTime": _iso_with_local_offset(act.start_time)})
    SubElement(lap, "TotalTimeSeconds").text = _sec_str(act, primary, heart_rates)
    SubElement(lap, "DistanceMeters").text = _lap_distance_m_str(primary)
    # Energy expenditure is intentionally not tracked; TCX still requires Calories.
    SubElement(lap, "Calories").text = "0"
    SubElement(lap, "Intensity").text = "Active"
    SubElement(lap, "TriggerMethod").text = "Manual"
    return tcx, SubElement(lap, "Track")


def _sample_time(act: Activity, timestamp_ms: int) -> str:
    """Render a sample timestamp using the activity's local timezone."""
    start = act.start_time if act.start_time.tzinfo else act.start_time.replace(tzinfo=UTC)
    return _iso_with_local_offset(start.astimezone() + timedelta(milliseconds=timestamp_ms))


def _append_heart_rate(
    trackpoint: Element,
    heart_rates: list[HeartRate],
    hr_idx: int,
    current_ms: int,
) -> int:
    """Append the latest heart rate at or before a trackpoint."""
    if not heart_rates or heart_rates[0].timestamp_ms > current_ms:
        return hr_idx
    while hr_idx + 1 < len(heart_rates) and heart_rates[hr_idx + 1].timestamp_ms <= current_ms:
        hr_idx += 1
    hr = SubElement(trackpoint, "HeartRateBpm")
    SubElement(hr, "Value").text = str(int(heart_rates[hr_idx].bpm))
    return hr_idx


def _distance_for_sample(
    sample: _PrimarySample,
    *,
    timestamp_ms: int,
    last_timestamp_ms: int,
    last_distance_m: float,
) -> float:
    """Return a non-decreasing total distance for a primary sample."""
    if sample.total_distance_m is not None:
        distance_m = float(sample.total_distance_m)
    else:
        dt_s = max(0.0, (timestamp_ms - last_timestamp_ms) / 1000.0)
        distance_m = last_distance_m + max(0.0, float(sample.speed_mps or 0.0) * dt_s)
    return max(distance_m, last_distance_m)


def _append_extensions(
    trackpoint: Element,
    sample: _PrimarySample,
    *,
    timeline_kind: _TimelineKind,
) -> None:
    """Append optional speed, cadence, and power extension values."""
    cadence_spm = sample.cadence_spm if timeline_kind == "running" else None
    if sample.speed_mps is None and sample.power_watts is None and cadence_spm is None:
        return
    ext = SubElement(trackpoint, "Extensions")
    tpx = SubElement(ext, f"{{{_ACTIVITY_EXTENSION_NS}}}TPX")
    if sample.speed_mps is not None:
        SubElement(
            tpx,
            f"{{{_ACTIVITY_EXTENSION_NS}}}Speed",
        ).text = f"{float(sample.speed_mps):.6f}"
    if cadence_spm is not None:
        SubElement(tpx, f"{{{_ACTIVITY_EXTENSION_NS}}}RunCadence").text = str(
            round(float(cadence_spm)),
        )
    if sample.power_watts is not None:
        SubElement(tpx, f"{{{_ACTIVITY_EXTENSION_NS}}}Watts").text = str(
            round(float(sample.power_watts)),
        )


def _append_primary_trackpoints(
    *,
    track: Element,
    act: Activity,
    primary: list[_PrimarySample],
    heart_rates: list[HeartRate],
    timeline_kind: Literal["running", "cycling"],
) -> None:
    """Append running or cycling samples to a TCX track."""
    last_timestamp_ms = int(primary[0].timestamp_ms)
    last_distance_m = 0.0
    hr_idx = 0
    for sample in primary:
        timestamp_ms = int(sample.timestamp_ms)
        trackpoint = SubElement(track, "Trackpoint")
        SubElement(trackpoint, "Time").text = _sample_time(act, timestamp_ms)

        distance_m = _distance_for_sample(
            sample,
            timestamp_ms=timestamp_ms,
            last_timestamp_ms=last_timestamp_ms,
            last_distance_m=last_distance_m,
        )
        if sample.altitude_m is not None:
            SubElement(trackpoint, "AltitudeMeters").text = f"{float(sample.altitude_m):.3f}"
        SubElement(trackpoint, "DistanceMeters").text = f"{distance_m:.3f}"
        last_timestamp_ms = timestamp_ms
        last_distance_m = distance_m

        hr_idx = _append_heart_rate(trackpoint, heart_rates, hr_idx, timestamp_ms)
        if timeline_kind == "cycling" and sample.cadence_rpm is not None:
            SubElement(trackpoint, "Cadence").text = str(round(float(sample.cadence_rpm)))
        _append_extensions(trackpoint, sample, timeline_kind=timeline_kind)


def _append_heart_rate_trackpoints(
    *,
    track: Element,
    act: Activity,
    heart_rates: list[HeartRate],
) -> None:
    """Append heart-rate samples when no sport-specific timeline exists."""
    for heart_rate in heart_rates:
        trackpoint = SubElement(track, "Trackpoint")
        SubElement(trackpoint, "Time").text = _sample_time(act, heart_rate.timestamp_ms)
        SubElement(trackpoint, "DistanceMeters").text = "0.000"
        hr = SubElement(trackpoint, "HeartRateBpm")
        SubElement(hr, "Value").text = str(int(heart_rate.bpm))


# ---------- TCX exporter ----------
def activity_to_tcx(
    *,
    act: Activity,
    heart_rates: list[HeartRate],
    running: list[RunningMetrics] | None = None,
    cycling: list[CyclingMetrics] | None = None,
    sport_type: SportTypesEnum,
) -> bytes:
    """
    Build a TCX (Garmin Training Center XML) for an activity.

    Trackpoint timeline preference:
      1) RunningMetrics  | CyclingMetrics (primary — contains speed/cadence/power/distance)
      2) HeartRate (fallback when no running metrics exist)

    Units:
      - DistanceMeters: meters
      - TPX Speed: m/s
      - RunCadence: steps/min (spm)
      - Cadence: rpm (for cycling)
      - Watts: instantaneous power (W)

    Heart rate samples are aligned as the nearest sample with timestamp <= current t.
    """
    # Sort inputs by their relative timestamps (ms from session start)
    heart_rates = sorted(heart_rates, key=lambda h: h.timestamp_ms)
    running = sorted((running or []), key=lambda r: r.timestamp_ms)
    cycling = sorted((cycling or []), key=lambda c: c.timestamp_ms)

    # Choose timeline
    timeline_kind: str
    primary: list[_PrimarySample]
    if sport_type == SportTypesEnum.running and running:
        timeline_kind = "running"
        primary = cast("list[_PrimarySample]", running)
    elif sport_type == SportTypesEnum.biking and cycling:
        timeline_kind = "cycling"
        primary = cast("list[_PrimarySample]", cycling)
    else:
        timeline_kind = "hr"
        primary = []

    tcx, track = _build_tcx_lap(
        act=act,
        heart_rates=heart_rates,
        primary=primary,
        sport_type=sport_type,
    )
    if timeline_kind in ("running", "cycling"):
        _append_primary_trackpoints(
            track=track,
            act=act,
            primary=primary,
            heart_rates=heart_rates,
            timeline_kind=timeline_kind,
        )
    else:
        _append_heart_rate_trackpoints(track=track, act=act, heart_rates=heart_rates)

    return tostring(tcx, encoding="utf-8", xml_declaration=True)


def infer_sport(
    hrs: list[HeartRate],
    runs: list[RunningMetrics],
    cycles: list[CyclingMetrics],
    activity_id: int,
) -> SportTypesEnum:
    """
    Infer sport type from available metrics.

    Priority:
        1) Running if any running metrics and no cycling metrics
        2) Biking if any cycling metrics and no running metrics
        3) Running if HR-only (most common case for HR-only)
        4) Unknown if conflicting or no data (logs a warning; caller can decide how to handle)
    """
    if runs and not cycles:
        return SportTypesEnum.running

    if cycles and not runs:
        return SportTypesEnum.biking

    if hrs and not runs and not cycles:
        return SportTypesEnum.running  # HR-only, default to running (most common case for HR-only)

    logger.warning(
        f"Unable to infer sport for activity {activity_id}. runs={len(runs)} cycles={len(cycles)}",
    )
    return SportTypesEnum.unknown
