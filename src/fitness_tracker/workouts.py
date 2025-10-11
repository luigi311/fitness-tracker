from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class WorkoutStep:
    duration_s: float
    # One of:
    watts: float | None = None
    percent_ftp: float | None = None

    # For pace-based targets (canonical internal unit = meters/second)
    speed_mps: float | None = None

    def target_watts(self, ftp_watts: int) -> float:
        if self.watts is not None:
            return float(self.watts)
        if self.percent_ftp is not None:
            return float(ftp_watts) * float(self.percent_ftp) / 100.0
        return 0.0

    def target_speed_mps(self) -> float | None:
        return float(self.speed_mps) if self.speed_mps is not None else None


@dataclass
class Workout:
    name: str
    steps: list[WorkoutStep]

    @property
    def total_seconds(self) -> float:
        return sum(s.duration_s for s in self.steps)

    def target_at(self, t_s: float, ftp_watts: int) -> tuple[float | None, float | None, int]:
        """
        Return (target_watts_or_None, target_speed_mps_or_None, step_index)
        for elapsed seconds t_s. If past end, returns last step.
        Exactly one of watts or speed_mps will be non-None for a given step.
        """
        acc = 0.0
        for i, s in enumerate(self.steps):
            if t_s < acc + s.duration_s:
                w = s.watts or (s.percent_ftp is not None and s.target_watts(ftp_watts)) or None
                w = float(w) if w not in (False, None) else None
                v = s.target_speed_mps()
                return (w, v, i)
            acc += s.duration_s
        # after last step
        if self.steps:
            s = self.steps[-1]
            w = s.watts or (s.percent_ftp is not None and s.target_watts(ftp_watts)) or None
            w = float(w) if w not in (False, None) else None
            v = s.target_speed_mps()
            return (w, v, len(self.steps) - 1)
        return (None, None, -1)


# -----------------------
# Parsers
# -----------------------


def _parse_float(x: str, default: float = 0.0) -> float:
    try:
        return float(x.strip())
    except Exception:
        return default


def parse_mrc(path: Path) -> Workout:
    """
    Minimal MRC parser.
    We support the classic [COURSE DATA] section:
      Seconds [tab] %FTP  (or) Seconds [tab] Watts
    We auto-detect whether values look like percentages (>0..200) or watts (>50..2000).
    """
    name = path.stem
    in_data = False
    rows: list[tuple[float, float]] = []
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            if line.upper().startswith("[COURSE DATA]"):
                in_data = True
                continue
            if line.startswith("[") and line.endswith("]") and in_data:
                # reached another section
                break
            if in_data:
                parts = line.replace(",", "\t").split()
                if len(parts) < 2:
                    continue
                t = _parse_float(parts[0])
                v = _parse_float(parts[1])
                rows.append((t, v))

    steps: list[WorkoutStep] = []
    if not rows:
        return Workout(name=name, steps=steps)

    # Heuristic: if median between 20..200 -> %FTP, else assume watts
    vals = [v for _, v in rows]
    vals_sorted = sorted(vals)
    mid = vals_sorted[len(vals_sorted) // 2]
    as_pct = 20.0 <= mid <= 200.0

    # Convert point data into steps (delta of successive times)
    for i in range(1, len(rows)):
        dt = max(0.0, rows[i][0] - rows[i - 1][0])
        v = rows[i - 1][1]
        # MRC: treat values as *power only* (either %FTP or watts)
        if as_pct:
            steps.append(WorkoutStep(duration_s=dt, percent_ftp=v, speed_mps=None))
        else:
            steps.append(WorkoutStep(duration_s=dt, watts=v, speed_mps=None))

    # Last point has no duration; ignore or give a tiny tail
    return Workout(name=name, steps=steps)


def parse_erg(path: Path) -> Workout:
    """
    Minimal ERG parser.
    Typical format:
      [COURSE DATA]
      time(sec)    watts_or_%ftp
    If we see values <= 200 (most of the time) we treat as %FTP; else watts.
    """
    # ERG is often identical-enough for our minimal handling.
    return parse_mrc(path)


def parse_fit(path: Path) -> Workout:
    """
    FIT workout parser using fitparse (optional dependency).
    Falls back to an empty workout if fitparse is missing.
    """
    name = path.stem
    try:
        from fitparse import FitFile  # type: ignore
    except Exception:
        return Workout(name=name, steps=[])

    ff = FitFile(str(path))
    steps: list[WorkoutStep] = []
    # FIT "workout_step" messages can include duration and target in various units
    for m in ff.get_messages("workout_step"):
        fields = {f.name: f.value for f in m}
        dur_type = fields.get("duration_type")
        tgt_type = fields.get("target_type")
        duration_s = 0.0

        # Duration (we support simple "time" steps)
        if dur_type == "time" or "duration_time" in fields:
            duration_s = float(fields.get("duration_time", 0.0))
        elif "duration_value" in fields:
            duration_s = float(fields["duration_value"])

        if duration_s <= 0:
            continue

        watts = None
        pct = None
        speed_mps = None

        # Power target
        if tgt_type in ("power", "power_3s", "power_lap") or "target_power_low" in fields:
            # Use mid of low/high if present
            lo = fields.get("target_power_low")
            hi = fields.get("target_power_high")
            if lo is not None and hi is not None:
                watts = 0.5 * (float(lo) + float(hi))
            elif lo is not None:
                watts = float(lo)
        # %FTP (power) target
        elif (
            tgt_type in ("power_percent_ftp", "power_3s_percent_ftp")
            or "target_power_percent_low" in fields
        ):
            lo = fields.get("target_power_percent_low")
            hi = fields.get("target_power_percent_high")
            if lo is not None and hi is not None:
                pct = 0.5 * (float(lo) + float(hi))
            elif lo is not None:
                pct = float(lo)
        # Pace/Speed target (running); FIT generally stores *speed in m/s*
        if tgt_type in ("pace", "speed") or "target_speed_low" in fields:
            slo = fields.get("target_speed_low")
            shi = fields.get("target_speed_high")
            if slo is not None and shi is not None:
                speed_mps = 0.5 * (float(slo) + float(shi))
            elif slo is not None:
                speed_mps = float(slo)

        # For a step, we keep *either* power (watts/%FTP) or pace (speed_mps)
        # If both are present, prefer power and ignore speed (common on bike).
        if watts is not None or pct is not None:
            steps.append(
                WorkoutStep(duration_s=duration_s, watts=watts, percent_ftp=pct, speed_mps=None)
            )
        else:
            steps.append(
                WorkoutStep(
                    duration_s=duration_s, watts=None, percent_ftp=None, speed_mps=speed_mps
                )
            )

    return Workout(name=name, steps=steps)


# -----------------------
# Intervals.icu JSON parser
# -----------------------


def _coerce_float(v, default: float | None = None) -> float | None:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def _flatten_icu_steps(steps: list[dict], threshold_pace_mps: float | None) -> list[WorkoutStep]:
    """
    Convert Intervals.icu 'steps' (which may include nested sets with 'reps')
    into a flat list of WorkoutStep, using speed targets when pace is provided.
    Priority for pace target per step:
      1) `_pace.value` or midpoint of `_pace.start/end` (already m/s in JSON)
      2) `pace` with units '%pace' -> threshold_pace_mps * percentage/100
      3) midpoint of `_pace.start/end` if available
    If power fields are ever present in their JSON (rare here), we can extend similarly.
    """
    flat: list[WorkoutStep] = []

    def handle_step(node: dict):
        # If it's a repeated block with 'reps'
        if "reps" in node and isinstance(node.get("steps"), list):
            reps = int(node.get("reps", 1) or 1)
            for _ in range(max(1, reps)):
                for sub in node["steps"]:
                    handle_step(sub)
            return

        dur = _coerce_float(node.get("duration"), 0.0) or 0.0
        if dur <= 0:
            return

        # Targets we might parse
        speed_mps: float | None = None
        watts: float | None = None
        percent_ftp: float | None = None

        # -------- Pace (speed) parsing (as before) --------
        pmeta = node.get("_pace")
        if isinstance(pmeta, dict):
            if pmeta.get("value") is not None:
                speed_mps = _coerce_float(pmeta.get("value"))
            else:
                s0 = _coerce_float(pmeta.get("start"))
                s1 = _coerce_float(pmeta.get("end"))
                if s0 is not None and s1 is not None:
                    speed_mps = 0.5 * (s0 + s1)

        if speed_mps is None and isinstance(node.get("pace"), dict):
            p = node["pace"]
            units = (p.get("units") or "").casefold()
            if "%pace" in units and threshold_pace_mps:
                vals = []
                for key in ("start", "end", "value"):
                    v = _coerce_float(p.get(key))
                    if v is not None:
                        vals.append(v)
                pct = None
                if len(vals) == 1:
                    pct = vals[0]
                elif len(vals) >= 2:
                    pct = 0.5 * (vals[0] + vals[1])
                if pct is not None:
                    speed_mps = threshold_pace_mps * (pct / 100.0)

        # -------- Power parsing (NEW) --------
        # Prefer absolute watts from _power when present
        pwmeta = node.get("_power")
        if isinstance(pwmeta, dict):
            if pwmeta.get("value") is not None:
                watts = _coerce_float(pwmeta.get("value"))
            else:
                w0 = _coerce_float(pwmeta.get("start"))
                w1 = _coerce_float(pwmeta.get("end"))
                if w0 is not None and w1 is not None:
                    watts = 0.5 * (w0 + w1)

        # Or parse %FTP
        if watts is None and isinstance(node.get("power"), dict):
            p = node["power"]
            units = (p.get("units") or "").casefold()
            vals = []
            for key in ("start", "end", "value"):
                v = _coerce_float(p.get(key))
                if v is not None:
                    vals.append(v)

            if "%ftp" in units:
                # Keep as percent_ftp; we'll scale by the user's FTP at runtime
                if len(vals) == 1:
                    percent_ftp = vals[0]
                elif len(vals) >= 2:
                    percent_ftp = 0.5 * (vals[0] + vals[1])
            # If units look like absolute watts, take value/midpoint
            elif len(vals) == 1:
                watts = vals[0]
            elif len(vals) >= 2:
                watts = 0.5 * (vals[0] + vals[1])

        # If power is present for this step, prefer it over pace (typical for bike)
        if watts is not None or percent_ftp is not None:
            speed_mps = None

        flat.append(
            WorkoutStep(
                duration_s=dur,
                watts=watts,
                percent_ftp=percent_ftp,
                speed_mps=speed_mps,
            )
        )

    for s in steps:
        handle_step(s)
    return flat


def parse_intervals_icu_json(path: Path) -> Workout:
    """
    Parse Intervals.icu exported workout JSON (running/cycling).
    We normalize to power or speed steps; this file example is PACE-based.
    - We treat `_pace.*` as meters/second (as exported by Intervals.icu).
    - If only %pace is present, we scale by `threshold_pace` (expected m/s).
    """
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    name = data.get("description") or path.stem

    # threshold_pace is typically present and (empirically) in m/s in exports.
    # If missing, we'll still try to use `_pace` values per step.
    threshold_pace_mps = _coerce_float(data.get("threshold_pace"))

    steps_in = data.get("steps") or []
    steps = _flatten_icu_steps(steps_in, threshold_pace_mps)

    return Workout(name=name, steps=steps)


# -----------------------
# Discovery
# -----------------------

SUPPORTED_EXTS = (".mrc", ".erg", ".fit", ".json")


def discover_workouts(folder: Path) -> list[Path]:
    return sorted([p for p in folder.glob("*") if p.suffix.lower() in SUPPORTED_EXTS])


def load_workout(path: Path) -> Workout:
    ext = path.suffix.lower()
    if ext == ".mrc":
        return parse_mrc(path)
    if ext == ".erg":
        return parse_erg(path)
    if ext == ".fit":
        return parse_fit(path)
    if ext == ".json":
        return parse_intervals_icu_json(path)
    return Workout(name=path.stem, steps=[])
