from __future__ import annotations

from datetime import UTC, timedelta, timezone
from typing import TYPE_CHECKING
from xml.etree.ElementTree import Element, SubElement, tostring

from loguru import logger

from fitness_tracker.database import SportTypesEnum

if TYPE_CHECKING:
    from typing import Literal

    from fitness_tracker.database import Activity, CyclingMetrics, HeartRate, RunningMetrics
# ---------- Helpers ----------


def _iso_with_local_offset(dt):
    """
    Render a datetime as ISO-8601 with its timezone offset
    - If naive, assume it's UTC (how we store in DB) and then convert to local.
    - Intervals.icu respects the embedded offset for display.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    # Convert to the machine's local timezone for human-friendly wall time
    local_dt = dt.astimezone()  # system local tz with correct DST
    return local_dt.isoformat(timespec="seconds")


def _sec_str(act: Activity, primary_samples: list[object], heart_rates: list[HeartRate]) -> str:
    """
    Total time in seconds for the <Lap>. Prefer the DB end_time if present,
    otherwise fall back to the last sample (primary timeline or HR) timestamp.
    """
    if act.end_time:
        dur = max(0.0, (act.end_time - act.start_time).total_seconds())
        return f"{dur:.1f}"

    tmax = 0
    if primary_samples:
        tmax = max(tmax, int(getattr(primary_samples[-1], "timestamp_ms")))
    if heart_rates:
        tmax = max(tmax, int(heart_rates[-1].timestamp_ms))
    return f"{max(0.0, tmax / 1000.0):.1f}"


def _lap_distance_m_str(primary_samples: list[object]) -> str:
    """
    Distance for the lap in meters (string). Prefer the final total_distance_m
    if present; otherwise integrate speed over time as a fallback.
    """
    if not primary_samples:
        return "0.0"

    # Prefer device-reported total distance (already meters)
    dists = [
        getattr(s, "total_distance_m")
        for s in primary_samples
        if getattr(s, "total_distance_m", None) is not None
    ]
    if dists:
        return f"{float(dists[-1]):.3f}"

    # Fallback: integrate v * dt
    total = 0.0
    last_ms = int(getattr(primary_samples[0], "timestamp_ms"))
    for s in primary_samples[1:]:
        ts = int(getattr(s, "timestamp_ms"))
        dt = max(0.0, (ts - last_ms) / 1000.0)  # seconds
        v = float(getattr(s, "speed_mps", 0.0) or 0.0)
        total += max(0.0, v * dt)  # meters
        last_ms = ts
    return f"{total:.3f}"


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
    if sport_type == SportTypesEnum.running and running:
        timeline_kind = "running"
        primary = running
    elif sport_type == SportTypesEnum.biking and cycling:
        timeline_kind = "cycling"
        primary = cycling
    else:
        timeline_kind = "hr"
        primary = []

    # Root
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
            "xmlns:ns3": "http://www.garmin.com/xmlschemas/ActivityExtension/v2",
        },
    )
    activities = SubElement(tcx, "Activities")
    act_node = SubElement(activities, "Activity", {"Sport": sport_type.name})
    SubElement(act_node, "Id").text = _iso_with_local_offset(act.start_time)

    lap = SubElement(act_node, "Lap", {"StartTime": _iso_with_local_offset(act.start_time)})
    SubElement(lap, "TotalTimeSeconds").text = _sec_str(act, primary, heart_rates)
    SubElement(lap, "DistanceMeters").text = _lap_distance_m_str(primary)
    SubElement(lap, "Intensity").text = "Active"
    SubElement(lap, "TriggerMethod").text = "Manual"

    track = SubElement(lap, "Track")

    # Heart-rate pointer (nearest <= current t)
    hr_idx = 0
    last_dist_m = 0.0

    if timeline_kind in ("running", "cycling"):
        last_ts_ms = int(getattr(primary[0], "timestamp_ms"))
        for s in primary:
            ts = int(getattr(s, "timestamp_ms"))
            t_local = (
                act.start_time
                if act.start_time.tzinfo
                else act.start_time.replace(tzinfo=timezone.utc)
            )
            t_local = t_local.astimezone() + timedelta(milliseconds=int(getattr(s, "timestamp_ms")))

            tp = SubElement(track, "Trackpoint")
            SubElement(tp, "Time").text = _iso_with_local_offset(t_local)

            # Distance: prefer total_distance_m; else integrate speed
            total_distance_m = getattr(s, "total_distance_m", None)
            if total_distance_m is not None:
                dist_m = float(total_distance_m)
            else:
                dt_s = max(0.0, (ts - last_ts_ms) / 1000.0)
                v = float(getattr(s, "speed_mps", 0.0) or 0.0)
                dist_m = last_dist_m + max(0.0, v * dt_s)

            last_ts_ms = ts

            # Ensure non-decreasing distance
            dist_m = max(dist_m, last_dist_m)
            SubElement(tp, "DistanceMeters").text = f"{dist_m:.3f}"
            last_dist_m = dist_m

            # Heart rate (nearest <= r.timestamp_ms)
            cur_ms = int(getattr(s, "timestamp_ms"))
            while hr_idx + 1 < len(heart_rates) and heart_rates[hr_idx + 1].timestamp_ms <= cur_ms:
                hr_idx += 1
            if heart_rates:
                hr_bpm = int(heart_rates[min(hr_idx, len(heart_rates) - 1)].bpm)
                hr = SubElement(tp, "HeartRateBpm")
                SubElement(hr, "Value").text = str(hr_bpm)

            # Cadence:
            # - running cadence stays in TPX RunCadence (as before)
            # - cycling cadence uses core TCX <Cadence>
            if timeline_kind == "cycling":
                cad = getattr(s, "cadence_rpm", None)
                if cad is not None:
                    SubElement(tp, "Cadence").text = str(int(round(float(cad))))

            # Extensions (Speed m/s, Watts, RunCadence spm)
            speed_mps = getattr(s, "speed_mps", None)
            power_watts = getattr(s, "power_watts", None)
            cadence_spm = getattr(s, "cadence_spm", None) if timeline_kind == "running" else None

            if (speed_mps is not None) or (power_watts is not None) or (cadence_spm is not None):
                ext = SubElement(tp, "Extensions")
                tpx = SubElement(ext, "{http://www.garmin.com/xmlschemas/ActivityExtension/v2}TPX")
                if speed_mps is not None:
                    SubElement(
                        tpx,
                        "{http://www.garmin.com/xmlschemas/ActivityExtension/v2}Speed",
                    ).text = f"{float(speed_mps):.6f}"  # m/s
                if power_watts is not None:
                    SubElement(
                        tpx,
                        "{http://www.garmin.com/xmlschemas/ActivityExtension/v2}Watts",
                    ).text = str(round(float(power_watts)))
                if cadence_spm is not None:
                    SubElement(
                        tpx,
                        "{http://www.garmin.com/xmlschemas/ActivityExtension/v2}RunCadence",
                    ).text = str(round(float(cadence_spm)))
    else:
        # HR-only fallback timeline
        for h in heart_rates:
            t_local = (
                act.start_time if act.start_time.tzinfo else act.start_time.replace(tzinfo=UTC)
            )
            t_local = t_local.astimezone() + timedelta(milliseconds=h.timestamp_ms)
            tp = SubElement(track, "Trackpoint")
            SubElement(tp, "Time").text = _iso_with_local_offset(t_local)
            # Distance unknown -> keep last (0 unless set elsewhere)
            SubElement(tp, "DistanceMeters").text = f"{last_dist_m:.3f}"
            hr = SubElement(tp, "HeartRateBpm")
            SubElement(hr, "Value").text = str(int(h.bpm))

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
