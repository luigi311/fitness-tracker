from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING, Literal, Protocol
from xml.etree.ElementTree import Element, SubElement, register_namespace, tostring

from loguru import logger

from fitness_tracker.core.environment import Environment
from fitness_tracker.core.sports import SportTypesEnum
from fitness_tracker.hardware.location import LocationFix, haversine_distance_m

_TCX_SPORT_NAMES = {
    SportTypesEnum.running: "Running",
    SportTypesEnum.biking: "Biking",
    SportTypesEnum.unknown: "Other",
}


class _PrimarySample(Protocol):
    @property
    def timestamp_ms(self) -> int: ...

    @property
    def speed_mps(self) -> float | None: ...

    @property
    def total_distance_m(self) -> float | None: ...

    @property
    def power_watts(self) -> float | None: ...

    @property
    def altitude_m(self) -> float | None: ...


class _RunningSample(_PrimarySample, Protocol):
    @property
    def cadence_spm(self) -> float | None: ...


class _CyclingSample(_PrimarySample, Protocol):
    @property
    def cadence_rpm(self) -> float | None: ...


if TYPE_CHECKING:
    from collections.abc import Sequence

    from fitness_tracker.data.models import (
        Activity,
        CyclingMetrics,
        HeartRate,
        LocationPoint,
        RunningMetrics,
    )
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
    primary_samples: Sequence[_PrimarySample],
    heart_rates: list[HeartRate],
    locations: Sequence[LocationPoint],
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
    if locations:
        tmax = max(tmax, int(locations[-1].timestamp_ms))
    return f"{max(0.0, tmax / 1000.0):.1f}"


def _location_fix(point: LocationPoint) -> LocationFix:
    """Convert a persisted point to the validated type used by the distance helper."""
    return LocationFix(
        latitude_deg=point.latitude_deg,
        longitude_deg=point.longitude_deg,
        accuracy_m=point.accuracy_m,
        altitude_m=point.altitude_m,
        speed_mps=point.speed_mps,
        heading_deg=point.heading_deg,
        source_time_utc=point.source_time_utc,
    )


def _gps_distance_m(locations: Sequence[LocationPoint]) -> float:
    """Return cumulative great-circle distance for a location-only timeline."""
    total = 0.0
    for previous, current in pairwise(locations):
        total += haversine_distance_m(_location_fix(previous), _location_fix(current))
    return total


def _lap_distance_m_str(
    primary_samples: Sequence[_PrimarySample],
    locations: Sequence[LocationPoint],
) -> str:
    """
    Distance for the lap in meters (string). Prefer the final total_distance_m
    if present; otherwise integrate speed over time as a fallback.
    """
    if not primary_samples:
        return f"{_gps_distance_m(locations):.3f}" if locations else "0.0"

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


@dataclass
class _TimelineEvent:
    """One emitted time with optional primary and location source data."""

    timestamp_ms: int
    primary: _PrimarySample | None = None
    sensor: _PrimarySample | None = None
    location: LocationPoint | None = None


def _build_tcx_lap(
    *,
    act: Activity,
    heart_rates: list[HeartRate],
    locations: Sequence[LocationPoint],
    primary: Sequence[_PrimarySample],
    sport_type: SportTypesEnum,
) -> tuple[Element, Element]:
    """Build the TCX root and the activity track container."""
    register_namespace("ae", _ACTIVITY_EXTENSION_NS)
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
        },
    )
    activities = SubElement(tcx, "Activities")
    act_node = SubElement(activities, "Activity", {"Sport": _TCX_SPORT_NAMES[sport_type]})
    SubElement(act_node, "Id").text = _iso_with_local_offset(act.start_time)

    lap = SubElement(act_node, "Lap", {"StartTime": _iso_with_local_offset(act.start_time)})
    SubElement(lap, "TotalTimeSeconds").text = _sec_str(
        act,
        primary,
        heart_rates,
        locations,
    )
    SubElement(lap, "DistanceMeters").text = _lap_distance_m_str(primary, locations)
    # Energy expenditure is intentionally not tracked; TCX still requires Calories.
    SubElement(lap, "Calories").text = "0"
    SubElement(lap, "Intensity").text = "Active"
    SubElement(lap, "TriggerMethod").text = "Manual"
    return tcx, SubElement(lap, "Track")


def _sample_time(act: Activity, timestamp_ms: int) -> str:
    """Render a sample timestamp using the activity's local timezone."""
    start = act.start_time if act.start_time.tzinfo else act.start_time.replace(tzinfo=UTC)
    start_utc = start.astimezone(UTC)
    return _iso_with_local_offset(start_utc + timedelta(milliseconds=timestamp_ms))


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
    cadence_spm: float | None,
) -> None:
    """Append optional speed, cadence, and power extension values."""
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
    primary: Sequence[_PrimarySample],
    heart_rates: list[HeartRate],
    timeline_kind: Literal["running", "cycling"],
    cadence_rpm_values: Sequence[float | None],
    cadence_spm_values: Sequence[float | None],
) -> None:
    """Append running or cycling samples to a TCX track."""
    last_timestamp_ms = int(primary[0].timestamp_ms)
    last_distance_m = 0.0
    hr_idx = 0
    for sample, cadence_rpm, cadence_spm in zip(
        primary,
        cadence_rpm_values,
        cadence_spm_values,
        strict=True,
    ):
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
        if timeline_kind == "cycling" and cadence_rpm is not None:
            SubElement(trackpoint, "Cadence").text = str(round(float(cadence_rpm)))
        _append_extensions(trackpoint, sample, cadence_spm=cadence_spm)


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


def _append_position(trackpoint: Element, location: LocationPoint) -> None:
    """Append a persisted location in TCX's standard position element."""
    validated = _location_fix(location)
    position = SubElement(trackpoint, "Position")
    SubElement(position, "LatitudeDegrees").text = f"{validated.latitude_deg:.8f}"
    SubElement(position, "LongitudeDegrees").text = f"{validated.longitude_deg:.8f}"


def _location_sort_key(location: LocationPoint) -> tuple[int, int]:
    """Sort location rows by timeline, then by database identity."""
    return int(location.timestamp_ms), int(location.id or 0)


def _merge_timeline_events(
    events: Sequence[_TimelineEvent],
    locations: Sequence[LocationPoint],
) -> list[_TimelineEvent]:
    """Merge sorted base events and locations in linear time."""
    merged: list[_TimelineEvent] = []
    event_idx = 0
    location_idx = 0
    while event_idx < len(events) and location_idx < len(locations):
        event_timestamp = events[event_idx].timestamp_ms
        location_timestamp = int(locations[location_idx].timestamp_ms)
        if event_timestamp < location_timestamp:
            merged.append(events[event_idx])
            event_idx += 1
            continue
        if location_timestamp < event_timestamp:
            merged.append(_TimelineEvent(location_timestamp, location=locations[location_idx]))
            location_idx += 1
            continue

        event_start = event_idx
        while event_idx < len(events) and events[event_idx].timestamp_ms == event_timestamp:
            event_idx += 1
        location_start = location_idx
        while (
            location_idx < len(locations)
            and int(locations[location_idx].timestamp_ms) == location_timestamp
        ):
            location_idx += 1

        event_count = event_idx - event_start
        location_count = location_idx - location_start
        for offset in range(event_count):
            event = events[event_start + offset]
            if offset < location_count:
                event.location = locations[location_start + offset]
            merged.append(event)
        merged.extend(
            _TimelineEvent(
                location_timestamp,
                location=locations[location_start + offset],
            )
            for offset in range(event_count, location_count)
        )

    merged.extend(events[event_idx:])
    merged.extend(
        _TimelineEvent(int(location.timestamp_ms), location=location)
        for location in locations[location_idx:]
    )
    return merged


def _align_timeline_sensors(events: Sequence[_TimelineEvent]) -> list[_TimelineEvent]:
    """Attach the latest primary sample to each already ordered event."""
    latest_primary: _PrimarySample | None = None
    for event in events:
        if event.primary is not None:
            latest_primary = event.primary
        event.sensor = latest_primary
    return list(events)


def _build_unified_timeline(
    *,
    primary: Sequence[_PrimarySample],
    heart_rates: Sequence[HeartRate],
    locations: Sequence[LocationPoint],
    indoor_anchor: bool,
) -> list[_TimelineEvent]:
    """Merge sensor and location rows into deterministic TCX events."""
    events = [_TimelineEvent(int(sample.timestamp_ms), primary=sample) for sample in primary]
    if not primary:
        events.extend(_TimelineEvent(int(sample.timestamp_ms)) for sample in heart_rates)

    selected_locations = list(locations[:1] if indoor_anchor else locations)
    if not selected_locations:
        return _align_timeline_sensors(events)
    if not events:
        return [
            _TimelineEvent(int(location.timestamp_ms), location=location)
            for location in selected_locations
        ]

    if indoor_anchor:
        events[0].location = selected_locations[0]
        return _align_timeline_sensors(events)
    return _align_timeline_sensors(_merge_timeline_events(events, selected_locations))


def _timeline_distance(
    event: _TimelineEvent,
    *,
    has_primary: bool,
    last_primary_timestamp_ms: int,
    last_distance_m: float,
    previous_location: LocationPoint | None,
) -> tuple[float, int, float]:
    """Calculate distance and state updates for one merged timeline event."""
    if event.primary is not None:
        distance_m = _distance_for_sample(
            event.primary,
            timestamp_ms=event.timestamp_ms,
            last_timestamp_ms=last_primary_timestamp_ms,
            last_distance_m=last_distance_m,
        )
        return distance_m, event.timestamp_ms, distance_m

    if not has_primary and event.location is not None and previous_location is not None:
        last_distance_m += haversine_distance_m(
            _location_fix(previous_location),
            _location_fix(event.location),
        )
    return last_distance_m, last_primary_timestamp_ms, last_distance_m


def _append_timeline_altitude(
    trackpoint: Element,
    event: _TimelineEvent,
    *,
    indoor_anchor: bool,
) -> None:
    """Append the source altitude selected for one merged event."""
    altitude_m = event.sensor.altitude_m if event.sensor is not None else None
    if event.location is not None and (not indoor_anchor or altitude_m is None):
        gps_altitude_m = _location_fix(event.location).altitude_m
        if gps_altitude_m is not None:
            altitude_m = gps_altitude_m
    if altitude_m is not None:
        SubElement(trackpoint, "AltitudeMeters").text = f"{float(altitude_m):.3f}"


def _append_timeline_metrics(
    trackpoint: Element,
    event: _TimelineEvent,
    *,
    heart_rates: list[HeartRate],
    hr_idx: int,
    timeline_kind: _TimelineKind,
) -> int:
    """Append aligned heart rate and sport-specific values."""
    hr_idx = _append_heart_rate(trackpoint, heart_rates, hr_idx, event.timestamp_ms)
    if event.sensor is None:
        return hr_idx

    if timeline_kind == "cycling":
        cadence_rpm = getattr(event.sensor, "cadence_rpm", None)
        if cadence_rpm is not None:
            SubElement(trackpoint, "Cadence").text = str(round(float(cadence_rpm)))
        cadence_spm = None
    else:
        cadence_spm = getattr(event.sensor, "cadence_spm", None)
    _append_extensions(trackpoint, event.sensor, cadence_spm=cadence_spm)
    return hr_idx


def _append_unified_trackpoints(
    *,
    track: Element,
    act: Activity,
    events: Sequence[_TimelineEvent],
    heart_rates: list[HeartRate],
    timeline_kind: _TimelineKind,
    indoor_anchor: bool,
) -> None:
    """Append trackpoints for the merged sensor/location timeline."""
    if not events:
        return

    has_primary = any(event.primary is not None for event in events)
    first_primary = next((event for event in events if event.primary is not None), None)
    last_primary_timestamp_ms = first_primary.timestamp_ms if first_primary is not None else 0
    last_distance_m = 0.0
    previous_location: LocationPoint | None = None
    hr_idx = 0

    for event in events:
        trackpoint = SubElement(track, "Trackpoint")
        SubElement(trackpoint, "Time").text = _sample_time(act, event.timestamp_ms)

        if event.location is not None:
            _append_position(trackpoint, event.location)

        distance_m, last_primary_timestamp_ms, last_distance_m = _timeline_distance(
            event,
            has_primary=has_primary,
            last_primary_timestamp_ms=last_primary_timestamp_ms,
            last_distance_m=last_distance_m,
            previous_location=previous_location,
        )
        _append_timeline_altitude(trackpoint, event, indoor_anchor=indoor_anchor)
        SubElement(trackpoint, "DistanceMeters").text = f"{distance_m:.3f}"

        hr_idx = _append_timeline_metrics(
            trackpoint,
            event,
            heart_rates=heart_rates,
            hr_idx=hr_idx,
            timeline_kind=timeline_kind,
        )

        if event.location is not None:
            previous_location = event.location


# ---------- TCX exporter ----------
def activity_to_tcx(
    *,
    act: Activity,
    heart_rates: list[HeartRate],
    running: list[RunningMetrics] | None = None,
    cycling: list[CyclingMetrics] | None = None,
    locations: list[LocationPoint] | None = None,
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
    Location points are supplied for the unified timeline exporter.
    """
    # Sort inputs by their relative timestamps (ms from session start)
    heart_rates = sorted(heart_rates, key=lambda h: h.timestamp_ms)
    running = sorted((running or []), key=lambda r: r.timestamp_ms)
    cycling = sorted((cycling or []), key=lambda c: c.timestamp_ms)

    # Choose timeline
    timeline_kind: _TimelineKind
    primary: Sequence[_PrimarySample]
    if sport_type == SportTypesEnum.running and running:
        timeline_kind = "running"
        running_samples: Sequence[_RunningSample] = running
        primary = running_samples
        cadence_rpm_values = [None] * len(running)
        cadence_spm_values = [sample.cadence_spm for sample in running_samples]
    elif sport_type == SportTypesEnum.biking and cycling:
        timeline_kind = "cycling"
        cycling_samples: Sequence[_CyclingSample] = cycling
        primary = cycling_samples
        cadence_rpm_values = [sample.cadence_rpm for sample in cycling_samples]
        cadence_spm_values = [None] * len(cycling)
    else:
        timeline_kind = "hr"
        primary = []
        cadence_rpm_values = []
        cadence_spm_values = []

    locations = sorted(locations or [], key=_location_sort_key)
    indoor_anchor = act.environment in {
        Environment.INDOOR.value,
        Environment.TRAINER.value,
    }
    selected_locations = locations[:1] if indoor_anchor else locations
    tcx, track = _build_tcx_lap(
        act=act,
        heart_rates=heart_rates,
        locations=selected_locations,
        primary=primary,
        sport_type=sport_type,
    )
    if selected_locations:
        events = _build_unified_timeline(
            primary=primary,
            heart_rates=heart_rates,
            locations=selected_locations,
            indoor_anchor=indoor_anchor,
        )
        _append_unified_trackpoints(
            track=track,
            act=act,
            events=events,
            heart_rates=heart_rates,
            timeline_kind=timeline_kind,
            indoor_anchor=indoor_anchor,
        )
    elif timeline_kind in ("running", "cycling"):
        _append_primary_trackpoints(
            track=track,
            act=act,
            primary=primary,
            heart_rates=heart_rates,
            timeline_kind=timeline_kind,
            cadence_rpm_values=cadence_rpm_values,
            cadence_spm_values=cadence_spm_values,
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
