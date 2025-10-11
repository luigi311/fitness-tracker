from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

SMALL_WORDS = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "of", "on", "or", "the", "to", "vs"}
ACRONYM_MAP = {
    "ftp": "FTP",
    "hr": "HR",
    "bpm": "BPM",
    "vo2": "VO2",
    "vo2max": "VO2max",
}

def pretty_workout_name(raw: str) -> str:
    """
    Turn a file-ish workout name into a human-friendly title:
      - replace underscores/dashes with spaces
      - collapse spaces
      - Title Case with small-words lowercased (except if first)
      - preserve common acronyms (FTP, HR, BPM, VO2, VO2max).
    """
    s = (raw or "").strip()
    s = re.sub(r"[_\-]+", " ", s)        # underscores/dashes -> spaces
    s = re.sub(r"\s+", " ", s)           # collapse whitespace
    if not s:
        return "Workout"

    words = s.split(" ")
    out: list[str] = []
    for i, w in enumerate(words):
        wl = w.lower()
        if wl in ACRONYM_MAP:
            out.append(ACRONYM_MAP[wl])
        elif i > 0 and wl in SMALL_WORDS:
            out.append(wl)
        else:
            # Capitalize first char, keep rest as-is (handles numbers nicely)
            out.append(w[:1].upper() + w[1:])
    return " ".join(out)

@dataclass
class WorkoutStep:
    duration_s: float

    # Power targets (absolute watts)
    watts: float | None = None
    watts_lo: float | None = None
    watts_hi: float | None = None

    # Power targets as %FTP
    percent_ftp: float | None = None
    percent_ftp_lo: float | None = None
    percent_ftp_hi: float | None = None

    # Pace targets (canonical internal unit = meters/second)
    speed_mps: float | None = None
    speed_mps_lo: float | None = None
    speed_mps_hi: float | None = None

    def target_watts(self, ftp_watts: int) -> float:
        """
        Return a single 'midpoint' target in watts for the step
        (prefers absolute watts; else %FTP scaled by ftp_watts; else 0.0).
        """
        if self.watts is not None:
            return float(self.watts)
        if self.percent_ftp is not None:
            return float(ftp_watts) * float(self.percent_ftp) / 100.0
        return 0.0

    def target_speed_mps(self) -> float | None:
        """Return a single 'midpoint' speed target in m/s (if any) for the step."""
        return float(self.speed_mps) if self.speed_mps is not None else None

    # --- Compute bands w/ fallbacks for gauge UI ---
    def power_band(
        self,
        ftp_watts: int,
        *,
        synth_rel_band: float = 0.05,  # ±5% when no explicit band is present
    ) -> tuple[float | None, float | None, float | None]:
        """
        Returns (mid_watts, lo_watts, hi_watts) or (None, None, None) if step is not power-based.
        Priority:
          1) absolute watts band (watts_lo/hi)
          2) %FTP band scaled by ftp
          3) single value (watts or %FTP) → synthesize ±synth_rel_band around mid.
        """
        # Prefer absolute watts path
        if self.watts is not None or (self.watts_lo is not None or self.watts_hi is not None):
            # midpoint
            mid = float(self.watts) if self.watts is not None else None
            if mid is None:
                # compute mid if only lo/hi available
                wlo = float(self.watts_lo) if self.watts_lo is not None else None
                whi = float(self.watts_hi) if self.watts_hi is not None else None
                if wlo is not None and whi is not None:
                    mid = 0.5 * (wlo + whi)
            if mid is None:
                # final fallback: if we only had percent_ftp, we’ll handle below
                pass
            else:
                # band
                lo = (
                    float(self.watts_lo)
                    if self.watts_lo is not None
                    else mid * (1.0 - synth_rel_band)
                )
                hi = (
                    float(self.watts_hi)
                    if self.watts_hi is not None
                    else mid * (1.0 + synth_rel_band)
                )
                return mid, lo, hi

        # Percent FTP path
        if (
            self.percent_ftp is not None
            or self.percent_ftp_lo is not None
            or self.percent_ftp_hi is not None
        ):
            mid_pct = float(self.percent_ftp) if self.percent_ftp is not None else None
            if mid_pct is None:
                plo = float(self.percent_ftp_lo) if self.percent_ftp_lo is not None else None
                phi = float(self.percent_ftp_hi) if self.percent_ftp_hi is not None else None
                if plo is not None and phi is not None:
                    mid_pct = 0.5 * (plo + phi)
            if mid_pct is None:
                return None, None, None

            mid = float(ftp_watts) * mid_pct / 100.0
            lo = (
                float(ftp_watts) * float(self.percent_ftp_lo) / 100.0
                if self.percent_ftp_lo is not None
                else mid * (1.0 - synth_rel_band)
            )
            hi = (
                float(ftp_watts) * float(self.percent_ftp_hi) / 100.0
                if self.percent_ftp_hi is not None
                else mid * (1.0 + synth_rel_band)
            )
            return mid, lo, hi

        # Not a power-based step
        return None, None, None

    def pace_band(
        self,
        *,
        synth_rel_band: float = 0.03,  # ±3% when no explicit band is present
    ) -> tuple[float | None, float | None, float | None]:
        """
        Returns (mid_speed_mps, lo_speed_mps, hi_speed_mps) or (None, None, None) if step is
        not pace-based.
        Priority:
          1) explicit speed_mps_lo/hi
          2) single speed_mps → synthesize ±synth_rel_band.
        """
        if (
            self.speed_mps is not None
            or self.speed_mps_lo is not None
            or self.speed_mps_hi is not None
        ):
            mid = float(self.speed_mps) if self.speed_mps is not None else None
            if mid is None:
                slo = float(self.speed_mps_lo) if self.speed_mps_lo is not None else None
                shi = float(self.speed_mps_hi) if self.speed_mps_hi is not None else None
                if slo is not None and shi is not None:
                    mid = 0.5 * (slo + shi)
            if mid is None:
                return None, None, None
            lo = (
                float(self.speed_mps_lo)
                if self.speed_mps_lo is not None
                else mid * (1.0 - synth_rel_band)
            )
            hi = (
                float(self.speed_mps_hi)
                if self.speed_mps_hi is not None
                else mid * (1.0 + synth_rel_band)
            )
            return mid, lo, hi

        return None, None, None


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
                # prefer power mid; else pace mid
                if (s.watts is not None) or (s.percent_ftp is not None):
                    return (s.target_watts(ftp_watts), None, i)
                return (None, s.target_speed_mps(), i)
            acc += s.duration_s
        # after last step
        if self.steps:
            s = self.steps[-1]
            if (s.watts is not None) or (s.percent_ftp is not None):
                return (s.target_watts(ftp_watts), None, len(self.steps) - 1)
            return (None, s.target_speed_mps(), len(self.steps) - 1)
        return (None, None, -1)

    def target_band_at(
        self, t_s: float, ftp_watts: int
    ) -> tuple[
        float | None,
        float | None,
        float | None,  # power mid/lo/hi
        float | None,
        float | None,
        float | None,  # pace  mid/lo/hi (m/s)
        int,  # step index
    ]:
        """
        Return (w_mid, w_lo, w_hi, v_mid, v_lo, v_hi, idx) for elapsed time t_s.
        Only one of the (power triplet) or (pace triplet) will be non-None for a step.
        """
        acc = 0.0
        for i, s in enumerate(self.steps):
            if t_s < acc + s.duration_s:
                if (s.watts is not None) or (s.percent_ftp is not None):
                    w_mid, w_lo, w_hi = s.power_band(ftp_watts)
                    return w_mid, w_lo, w_hi, None, None, None, i

                v_mid, v_lo, v_hi = s.pace_band()
                return None, None, None, v_mid, v_lo, v_hi, i
            acc += s.duration_s

        if self.steps:
            i = len(self.steps) - 1
            s = self.steps[i]
            if (s.watts is not None) or (s.percent_ftp is not None):
                w_mid, w_lo, w_hi = s.power_band(ftp_watts)
                return w_mid, w_lo, w_hi, None, None, None, i

            v_mid, v_lo, v_hi = s.pace_band()
            return None, None, None, v_mid, v_lo, v_hi, i

        return None, None, None, None, None, None, -1


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
            # power-only, %FTP; no explicit range
            steps.append(
                WorkoutStep(
                    duration_s=dt,
                    percent_ftp=v,
                    # bands will be synthesized later (±5%)
                ),
            )
        else:
            # power-only, absolute watts; no explicit range
            steps.append(
                WorkoutStep(
                    duration_s=dt,
                    watts=v,
                    # bands will be synthesized later (±5%)
                ),
            )

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

        # Prepare target containers
        watts = pct = speed = None
        w_lo = w_hi = None
        pct_lo = pct_hi = None
        v_lo = v_hi = None

        # Power (absolute watts)
        if tgt_type in ("power", "power_3s", "power_lap") or (
            "target_power_low" in fields or "target_power_high" in fields
        ):
            lo = fields.get("target_power_low")
            hi = fields.get("target_power_high")
            if lo is not None and hi is not None:
                watts = 0.5 * (float(lo) + float(hi))
                w_lo, w_hi = float(lo), float(hi)
            elif lo is not None:
                watts = float(lo)

        # Power (%FTP)
        if (
            tgt_type in ("power_percent_ftp", "power_3s_percent_ftp")
            or "target_power_percent_low" in fields
            or "target_power_percent_high" in fields
        ):
            lo = fields.get("target_power_percent_low")
            hi = fields.get("target_power_percent_high")
            if lo is not None and hi is not None:
                pct = 0.5 * (float(lo) + float(hi))
                pct_lo, pct_hi = float(lo), float(hi)
            elif lo is not None:
                pct = float(lo)

        # Pace/Speed (m/s)
        if tgt_type in ("pace", "speed") or (
            "target_speed_low" in fields or "target_speed_high" in fields
        ):
            slo = fields.get("target_speed_low")
            shi = fields.get("target_speed_high")
            if slo is not None and shi is not None:
                speed = 0.5 * (float(slo) + float(shi))
                v_lo, v_hi = float(slo), float(shi)
            elif slo is not None:
                speed = float(slo)

        # Prefer power if present; else pace
        if watts is not None or pct is not None:
            steps.append(
                WorkoutStep(
                    duration_s=duration_s,
                    watts=watts,
                    watts_lo=w_lo,
                    watts_hi=w_hi,
                    percent_ftp=pct,
                    percent_ftp_lo=pct_lo,
                    percent_ftp_hi=pct_hi,
                    # ignore pace if power is present
                )
            )
        else:
            steps.append(
                WorkoutStep(
                    duration_s=duration_s,
                    speed_mps=speed,
                    speed_mps_lo=v_lo,
                    speed_mps_hi=v_hi,
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
    into a flat list of WorkoutStep, capturing explicit bands when present.

    Pace rules:
      - Prefer `_pace.value` (m/s) or midpoint of `_pace.start/end` (m/s).
      - If only `pace` with units like '%pace', scale by `threshold_pace_mps`.
    Power rules:
      - Prefer `_power.value` or midpoint of `_power.start/end` (watts).
      - Else use `power` dict; if units == '%ftp', keep as percent range.
        Otherwise treat as absolute watts.
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
        speed = None
        s_lo = s_hi = None

        watts = None
        w_lo = w_hi = None

        pct = None
        pct_lo = pct_hi = None

        # -------- Pace (speed) parsing --------
        pmeta = node.get("_pace")
        if isinstance(pmeta, dict):
            if pmeta.get("value") is not None:
                speed = _coerce_float(pmeta.get("value"))
            # Many exports put ranges into start/end (m/s)
            s0 = _coerce_float(pmeta.get("start"))
            s1 = _coerce_float(pmeta.get("end"))
            if s0 is not None and s1 is not None:
                s_lo, s_hi = s0, s1
                if speed is None:
                    speed = 0.5 * (s0 + s1)

        if speed is None and isinstance(node.get("pace"), dict):
            p = node["pace"]
            units = (p.get("units") or "").casefold()
            if "%pace" in units and threshold_pace_mps:
                # interpret as percentage of threshold pace (in speed terms)
                vals = []
                for key in ("start", "end", "value"):
                    v = _coerce_float(p.get(key))
                    if v is not None:
                        vals.append(v)
                if len(vals) == 1:
                    pctv = vals[0]
                    speed = threshold_pace_mps * (pctv / 100.0)
                elif len(vals) >= 2:
                    p0, p1 = vals[0], vals[1]
                    speed = threshold_pace_mps * (0.5 * (p0 + p1) / 100.0)
                    s_lo = threshold_pace_mps * (p0 / 100.0)
                    s_hi = threshold_pace_mps * (p1 / 100.0)

        # -------- Power parsing --------
        pwmeta = node.get("_power")
        if isinstance(pwmeta, dict):
            if pwmeta.get("value") is not None:
                watts = _coerce_float(pwmeta.get("value"))
            w0 = _coerce_float(pwmeta.get("start"))
            w1 = _coerce_float(pwmeta.get("end"))
            if w0 is not None and w1 is not None:
                w_lo, w_hi = w0, w1
                if watts is None:
                    watts = 0.5 * (w0 + w1)

        if watts is None and isinstance(node.get("power"), dict):
            p = node["power"]
            units = (p.get("units") or "").casefold()
            vals = []
            for key in ("start", "end", "value"):
                v = _coerce_float(p.get(key))
                if v is not None:
                    vals.append(v)

            if "%ftp" in units:
                if len(vals) == 1:
                    pct = vals[0]
                elif len(vals) >= 2:
                    pct_lo, pct_hi = vals[0], vals[1]
                    pct = 0.5 * (pct_lo + pct_hi)
            # treat as absolute watts
            elif len(vals) == 1:
                watts = vals[0]
            elif len(vals) >= 2:
                w_lo, w_hi = vals[0], vals[1]
                watts = 0.5 * (w_lo + w_hi)

        # Prefer power over pace for bike-style steps
        if watts is not None or pct is not None or w_lo is not None or pct_lo is not None:
            flat.append(
                WorkoutStep(
                    duration_s=dur,
                    watts=watts,
                    watts_lo=w_lo,
                    watts_hi=w_hi,
                    percent_ftp=pct,
                    percent_ftp_lo=pct_lo,
                    percent_ftp_hi=pct_hi,
                ),
            )
        else:
            flat.append(
                WorkoutStep(
                    duration_s=dur,
                    speed_mps=speed,
                    speed_mps_lo=s_lo,
                    speed_mps_hi=s_hi,
                ),
            )

    for s in steps:
        handle_step(s)
    return flat


def parse_intervals_icu_json(path: Path) -> Workout:
    """
    Parse Intervals.icu exported workout JSON (running/cycling).
    We normalize to power or speed steps; this file often uses PACE with `_pace.start/end` in m/s.
    """
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    name = data.get("description") or data.get("name") or path.stem

    # threshold_pace is typically present and (empirically) in m/s in exports.
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
