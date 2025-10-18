from __future__ import annotations

from datetime import timedelta, timezone
from xml.etree.ElementTree import Element, SubElement, tostring

from fitness_tracker.database import Activity, HeartRate, RunningMetrics


# ---------- Helpers ----------


def _iso_with_local_offset(dt):
    """
    Render a datetime as ISO-8601 with its timezone offset
    - If naive, assume it's UTC (how we store in DB) and then convert to local.
    - Intervals.icu respects the embedded offset for display.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # Convert to the machine's local timezone for human-friendly wall time
    local_dt = dt.astimezone()  # system local tz with correct DST
    return local_dt.isoformat(timespec="seconds")


def _sec_str(act: Activity, running: list[RunningMetrics], heart_rates: list[HeartRate]) -> str:
    """
    Total time in seconds for the <Lap>. Prefer the DB end_time if present,
    otherwise fall back to the last sample (running or HR) timestamp.
    """
    if act.end_time:
        dur = max(0.0, (act.end_time - act.start_time).total_seconds())
        return f"{dur:.1f}"

    tmax = 0
    if running:
        tmax = max(tmax, int(running[-1].timestamp_ms))
    if heart_rates:
        tmax = max(tmax, int(heart_rates[-1].timestamp_ms))
    return f"{max(0.0, tmax / 1000.0):.1f}"


def _lap_distance_m_str(running: list[RunningMetrics]) -> str:
    """
    Distance for the lap in meters (string). Prefer the final total_distance_m
    if present; otherwise integrate speed over time as a fallback.
    """
    if not running:
        return "0.0"

    # Prefer device-reported total distance (already meters)
    dists = [r.total_distance_m for r in running if r.total_distance_m is not None]
    if dists:
        return f"{float(dists[-1]):.3f}"

    # Fallback: integrate v * dt
    total = 0.0
    last_ms = running[0].timestamp_ms
    for r in running[1:]:
        dt = max(0.0, (r.timestamp_ms - last_ms) / 1000.0)  # seconds
        total += max(0.0, float(r.speed_mps or 0.0) * dt)   # meters
        last_ms = r.timestamp_ms
    return f"{total:.3f}"


# ---------- TCX exporter ----------


def activity_to_tcx(
    *,
    act: Activity,
    heart_rates: list[HeartRate],
    running: list[RunningMetrics],
    sport: str = "Running",
) -> bytes:
    """
    Build a TCX (Garmin Training Center XML) for an activity.

    Trackpoint timeline preference:
      1) RunningMetrics (primary — contains speed/cadence/power/distance)
      2) HeartRate (fallback when no running metrics exist)

    Units:
      - DistanceMeters: meters
      - TPX Speed: m/s
      - RunCadence: steps/min (spm)
      - Watts: instantaneous power (W)

    Heart rate samples are aligned as the nearest sample with timestamp <= current t.
    """
    # Sort inputs by their relative timestamps (ms from session start)
    heart_rates = sorted(heart_rates, key=lambda h: h.timestamp_ms)
    running = sorted(running, key=lambda r: r.timestamp_ms)

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
    act_node = SubElement(activities, "Activity", {"Sport": sport})
    SubElement(act_node, "Id").text = _iso_with_local_offset(act.start_time)

    lap = SubElement(act_node, "Lap", {"StartTime": _iso_with_local_offset(act.start_time)})
    SubElement(lap, "TotalTimeSeconds").text = _sec_str(act, running, heart_rates)
    SubElement(lap, "DistanceMeters").text = _lap_distance_m_str(running)
    SubElement(lap, "Intensity").text = "Active"
    SubElement(lap, "TriggerMethod").text = "Manual"

    track = SubElement(lap, "Track")

    # Heart-rate pointer (nearest <= current t)
    hr_idx = 0
    last_dist_m = 0.0

    if running:
        # Use RunningMetrics timeline
        base_ms = running[0].timestamp_ms
        for r in running:
            t_local = (act.start_time if act.start_time.tzinfo else act.start_time.replace(tzinfo=timezone.utc))
            t_local = t_local.astimezone() + timedelta(milliseconds=r.timestamp_ms)

            tp = SubElement(track, "Trackpoint")
            SubElement(tp, "Time").text = _iso_with_local_offset(t_local)

            # Distance: prefer total_distance_m; else integrate speed
            if r.total_distance_m is not None:
                dist_m = float(r.total_distance_m)
            else:
                dt_s = (r.timestamp_ms - base_ms) / 1000.0
                v = float(r.speed_mps or 0.0)  # m/s
                dist_m = last_dist_m + max(0.0, v * max(0.0, dt_s))
                base_ms = r.timestamp_ms

            # Ensure non-decreasing distance
            if dist_m < last_dist_m:
                dist_m = last_dist_m
            SubElement(tp, "DistanceMeters").text = f"{dist_m:.3f}"
            last_dist_m = dist_m

            # Heart rate (nearest <= r.timestamp_ms)
            while hr_idx + 1 < len(heart_rates) and heart_rates[hr_idx + 1].timestamp_ms <= r.timestamp_ms:
                hr_idx += 1
            if heart_rates:
                hr_bpm = int(heart_rates[min(hr_idx, len(heart_rates) - 1)].bpm)
                hr = SubElement(tp, "HeartRateBpm")
                SubElement(hr, "Value").text = str(hr_bpm)

            # Extensions (Speed m/s, Watts, RunCadence spm)
            if (r.speed_mps is not None) or (r.power_watts is not None) or (r.cadence_spm is not None):
                ext = SubElement(tp, "Extensions")
                tpx = SubElement(ext, "{http://www.garmin.com/xmlschemas/ActivityExtension/v2}TPX")
                if r.speed_mps is not None:
                    SubElement(
                        tpx,
                        "{http://www.garmin.com/xmlschemas/ActivityExtension/v2}Speed",
                    ).text = f"{float(r.speed_mps):.6f}"  # m/s
                if r.power_watts is not None:
                    SubElement(
                        tpx,
                        "{http://www.garmin.com/xmlschemas/ActivityExtension/v2}Watts",
                    ).text = str(int(round(float(r.power_watts))))
                if r.cadence_spm is not None:
                    SubElement(
                        tpx,
                        "{http://www.garmin.com/xmlschemas/ActivityExtension/v2}RunCadence",
                    ).text = str(int(round(float(r.cadence_spm))))
    else:
        # HR-only fallback timeline
        for h in heart_rates:
            t_local = (act.start_time if act.start_time.tzinfo else act.start_time.replace(tzinfo=timezone.utc))
            t_local = t_local.astimezone() + timedelta(milliseconds=h.timestamp_ms)
            tp = SubElement(track, "Trackpoint")
            SubElement(tp, "Time").text = _iso_with_local_offset(t_local)
            # Distance unknown -> keep last (0 unless set elsewhere)
            SubElement(tp, "DistanceMeters").text = f"{last_dist_m:.3f}"
            hr = SubElement(tp, "HeartRateBpm")
            SubElement(hr, "Value").text = str(int(h.bpm))

    return tostring(tcx, encoding="utf-8", xml_declaration=True)
