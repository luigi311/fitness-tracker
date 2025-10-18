from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

SMALL_WORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "but",
    "by",
    "for",
    "in",
    "of",
    "on",
    "or",
    "the",
    "to",
    "vs",
}
ACRONYM_MAP = {
    "ftp": "FTP",
    "hr": "HR",
    "bpm": "BPM",
    "vo2": "VO2",
    "vo2max": "VO2max",
}

_TIME_RE = re.compile(r"^(?P<m>\d+):(?P<s>\d{1,2})(?:\.(?P<ms>\d+))?$")


def pretty_workout_name(raw: str) -> str:
    """
    Turn a file-ish workout name into a human-friendly title:
      - replace underscores/dashes with spaces
      - collapse spaces
      - Title Case with small-words lowercased (except if first)
      - preserve common acronyms (FTP, HR, BPM, VO2, VO2max).
    """
    s = (raw or "").strip()
    s = re.sub(r"[_\-]+", " ", s)  # underscores/dashes -> spaces
    s = re.sub(r"\s+", " ", s)  # collapse whitespace
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

def _decode_power_value(x: float | None) -> float | None:
    """
    Some FIT exports (Garmin/Intervals) encode absolute power targets as value+1000
    in the generic custom target fields. Decode that to real watts.

    Examples:
      1078 → 78 W, 1105 → 105 W.  Values outside 1000..2000 are returned unchanged.
    """
    if x is None:
        return None
    if 1000.0 <= x < 2000.0:
        return x - 1000.0
    return x


def parse_fit(path: Path) -> Workout:
    """
    Parse Intervals.icu-style FIT workouts including pace and repeat blocks.

    Handles:
      - Power (watts or %FTP), including files that store targets in generic
        custom_target_value_low/high depending on target_type.
      - Pace/Speed (m/s; auto-converts sec/km, sec/mi, cs/km, packed mmss)
      - Repeat markers: duration_type == 'repeat_until_steps_cmplt'
        with fields: 'duration_step' (start message_index) and 'repeat_steps' (total reps).
    """
    from fitparse import FitFile

    name = path.stem
    ff = FitFile(str(path))

    # ---------- helpers ----------
    def fnum(x) -> float | None:
        try:
            return float(x)
        except Exception:
            return None

    def first_non_none(d: dict, *keys):
        for k in keys:
            if k is None:
                continue
            if k in d and d[k] is not None:
                return d[k]
        return None

    def pace_like_to_mps(v):
        """
        Convert common pace encodings → m/s:
          - m/s (0.5..12) → as-is
          - sec/km (120..1200) → 1000 / s_per_km
          - sec/mi (200..2400) → 1609.344 / s_per_mi
          - centiseconds/km (8000..200000) → (cs/100) s/km → 1000 / s_per_km
          - packed mmss integer (e.g., 430 = 4:30 /km) → 1000 / (m*60 + s).
        """
        x = fnum(v)
        if x is None or x <= 0:
            return None
        # looks like m/s
        if 0.5 <= x <= 12.0:
            return x
        xi = int(x)
        # packed mmss (integers only, seconds < 60) → sec/km
        if 200 <= xi <= 1200 and (xi % 100) < 60 and abs(x - xi) < 1e-6:
            mins, secs = xi // 100, xi % 100
            s_per_km = mins * 60 + secs
            return 1000.0 / s_per_km if s_per_km > 0 else None
        # centiseconds/km → sec/km
        if 8000 <= x <= 200000:
            s_per_km = x / 100.0
            if 120 <= s_per_km <= 1200:
                return 1000.0 / s_per_km
        # sec/km
        if 120 <= x <= 1200:
            return 1000.0 / x
        # sec/mi
        if 200 <= x <= 2400:
            return 1609.344 / x
        return None

    # ---------- first pass: collect steps & repeat markers ----------
    entries: list[dict] = []
    for msg in ff.get_messages("workout_step"):
        fields = {f.name: f.value for f in msg}
        msg_idx = int(fields.get("message_index") or len(entries))

        # Duration
        duration_s = None
        dt_time = fnum(fields.get("duration_time"))
        dt_val = fnum(fields.get("duration_value"))
        if dt_time is not None and dt_time > 0:
            duration_s = dt_time
        elif dt_val is not None and dt_val > 0:
            duration_s = dt_val

        dur_type = str(fields.get("duration_type") or "").lower()

        # Repeat marker?
        if "repeat_until_steps_cmplt" in dur_type:
            try:
                start_index = int(fields.get("duration_step"))
                reps = int(fields.get("repeat_steps"))
            except Exception:
                continue
            entries.append(
                {
                    "type": "repeat",
                    "message_index": msg_idx,
                    "start_index": start_index,
                    "reps": reps,
                },
            )
            continue

        # Skip non-time steps
        if duration_s is None or duration_s <= 0:
            continue

        tgt_type = str(fields.get("target_type") or "").lower()

        # ---------- targets ----------
        watts = pct = speed = None
        w_lo = w_hi = pct_lo = pct_hi = v_lo = v_hi = None

        # PACE / SPEED
        if ("pace" in tgt_type) or ("speed" in tgt_type):
            # Intervals.icu uses custom_target_speed_low/high for pace targets
            lo_raw = first_non_none(
                fields,
                "custom_target_speed_low",
                "target_speed_low",
                # some files abuse generic value fields; allow if labeled as pace/speed
                "custom_target_value_low" if ("pace" in tgt_type or "speed" in tgt_type) else None,
            )
            hi_raw = first_non_none(
                fields,
                "custom_target_speed_high",
                "target_speed_high",
                "custom_target_value_high" if ("pace" in tgt_type or "speed" in tgt_type) else None,
            )
            mid_raw = fields.get("target_value")

            lo_mps = pace_like_to_mps(lo_raw)
            hi_mps = pace_like_to_mps(hi_raw)
            mid_mps = pace_like_to_mps(mid_raw)

            # Accept plausible raw m/s as a fallback
            lf, hf, mf = fnum(lo_raw), fnum(hi_raw), fnum(mid_raw)
            if lo_mps is None and lf is not None and 0.5 <= lf <= 12.0:
                lo_mps = lf
            if hi_mps is None and hf is not None and 0.5 <= hf <= 12.0:
                hi_mps = hf
            if mid_mps is None and mf is not None and 0.5 <= mf <= 12.0:
                mid_mps = mf

            if lo_mps is not None and hi_mps is not None:
                v_lo, v_hi = lo_mps, hi_mps
                speed = 0.5 * (v_lo + v_hi)
            elif lo_mps is not None:
                speed = lo_mps
            elif mid_mps is not None:
                speed = mid_mps

        # POWER – %FTP
        if ("percent" in tgt_type) or ("ftp" in tgt_type):
            lo_raw = first_non_none(
                fields,
                "target_power_percent_low",
                # some exports put % in generic value fields when target_type mentions percent
                "custom_target_value_low",
            )
            hi_raw = first_non_none(
                fields,
                "target_power_percent_high",
                "custom_target_value_high",
            )
            mid_raw = fields.get("target_value")
            lo_f, hi_f, mid_f = fnum(lo_raw), fnum(hi_raw), fnum(mid_raw)
            if lo_f is not None and hi_f is not None:
                pct_lo, pct_hi = lo_f, hi_f
                pct = 0.5 * (pct_lo + pct_hi)
            elif lo_f is not None:
                pct = lo_f
            elif mid_f is not None and 0 < mid_f <= 300:
                pct = mid_f  # plausible %FTP

        # POWER – absolute watts
        elif "power" in tgt_type:
            lo_raw = first_non_none(
                fields,
                "custom_target_power_low",
                "target_power_low",
                # allow generic value fields if type is power (but not percent)
                "custom_target_value_low"
                if ("percent" not in tgt_type and "ftp" not in tgt_type)
                else None,
            )
            hi_raw = first_non_none(
                fields,
                "custom_target_power_high",
                "target_power_high",
                "custom_target_value_high"
                if ("percent" not in tgt_type and "ftp" not in tgt_type)
                else None,
            )
            mid_raw = fields.get("target_value")
            lo_f, hi_f, mid_f = fnum(lo_raw), fnum(hi_raw), fnum(mid_raw)
            lo_w = _decode_power_value(lo_f)
            hi_w = _decode_power_value(hi_f)
            mid_w = _decode_power_value(mid_f)

            if lo_w is not None and hi_w is not None:
                w_lo, w_hi = lo_w, hi_w
                watts = 0.5 * (w_lo + w_hi)
            elif lo_w is not None:
                watts = lo_w
            elif mid_w is not None:
                watts = mid_w

        # ---------- build step (prefer power, then pace; else duration-only) ----------
        if (watts is not None) or (pct is not None) or (w_lo is not None) or (pct_lo is not None):
            step = WorkoutStep(
                duration_s=float(duration_s),
                watts=watts,
                watts_lo=w_lo,
                watts_hi=w_hi,
                percent_ftp=pct,
                percent_ftp_lo=pct_lo,
                percent_ftp_hi=pct_hi,
            )
        elif (speed is not None) or (v_lo is not None):
            step = WorkoutStep(
                duration_s=float(duration_s),
                speed_mps=speed,
                speed_mps_lo=v_lo,
                speed_mps_hi=v_hi,
            )
        else:
            step = WorkoutStep(duration_s=float(duration_s))

        entries.append({"type": "step", "message_index": msg_idx, "step": step})

    # ---------- second pass: expand repeats ----------
    entries.sort(key=lambda e: e["message_index"])
    final_steps: list[WorkoutStep] = []

    for e in entries:
        if e["type"] == "step":
            final_steps.append(e["step"])
        else:
            start = int(e["start_index"])
            end = int(e["message_index"])
            reps = int(e["reps"])
            # Collect the block of steps between start..end (message_index)
            block: list[WorkoutStep] = []
            for e2 in entries:
                if e2["type"] != "step":
                    continue
                mi = int(e2["message_index"])
                if start <= mi < end:
                    block.append(e2["step"])
            if not block or reps <= 1:
                continue
            # Append (reps-1) more copies
            for _ in range(reps - 1):
                for s in block:
                    final_steps.append(WorkoutStep(**s.__dict__))

    return Workout(name=name, steps=final_steps)


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

SUPPORTED_EXTS = (".fit", ".json")
AUTO_SUBDIRS = ("intervals_icu",)

def _date_from_filename(p: Path) -> date | None:
    # YYYY-MM-DD Title.ext
    try:
        return date.fromisoformat(p.stem.split(" ", 1)[0])
    except Exception:
        return None

def discover_workouts(running_dir: Path) -> list[Path]:
    """
    Return workout files in the order:
      1) Today's dated auto files
      2) Other dated auto files later this week (ascending date)
      3) Manual files in the root 'running' directory
    """
    today = date.today()

    # Collect auto files from provider subfolders
    auto_files: list[Path] = []
    for sub in AUTO_SUBDIRS:
        d = running_dir / sub
        if d.is_dir():
            auto_files.extend([p for p in d.glob("*.*") if p.is_file()])

    # Partition autos by date
    todays: list[tuple[date, Path]] = []
    weeks: list[tuple[date, Path]]  = []
    for p in auto_files:
        d = _date_from_filename(p)
        if not d:
            continue
        if d == today:
            todays.append((d, p))
        elif 0 <= (d - today).days <= 6:
            weeks.append((d, p))

    todays.sort(key=lambda t: t[0])   # single day but deterministic
    weeks.sort(key=lambda t: t[0])    # ascending date

    # Manual files live in running_dir root (ignore provider subdirs)
    manual = sorted(
        [p for p in running_dir.glob("*.*") if p.is_file()],
        key=lambda p: p.stem.lower(),
    )

    # Stitch in order
    ordered = [p for _, p in todays] + [p for _, p in weeks] + manual
    return ordered


def load_workout(path: Path) -> Workout:
    ext = path.suffix.lower()
    if ext == ".fit":
        return parse_fit(path)
    if ext == ".json":
        return parse_intervals_icu_json(path)
    return Workout(name=path.stem, steps=[])


def normalize_workout(workout: Workout, ftp_watts: int = 250) -> dict:
    """
    Produce a stable, source-agnostic structure for tests/snapshots.
    IMPORTANT: We only emit bands (lo/hi) when the source provided them.
               If a step had a single target, we emit mid and set lo/hi = None.
    """
    def _emit_power(step: WorkoutStep) -> dict | None:
        # Prefer absolute watts fields when present
        if (step.watts_lo is not None) or (step.watts_hi is not None) or (step.watts is not None):
            # Explicit band?
            if (step.watts_lo is not None) and (step.watts_hi is not None):
                mid = step.watts if step.watts is not None else 0.5 * (step.watts_lo + step.watts_hi)
                return {
                    "mid": float(mid),
                    "lo": float(step.watts_lo),
                    "hi": float(step.watts_hi),
                }
            # Single absolute target
            if step.watts is not None:
                return {"mid": float(step.watts), "lo": None, "hi": None}

        # Percent FTP path
        if (
            (step.percent_ftp_lo is not None) or (step.percent_ftp_hi is not None) or
            (step.percent_ftp is not None)
        ):
            if (step.percent_ftp_lo is not None) and (step.percent_ftp_hi is not None):
                mid_pct = step.percent_ftp if step.percent_ftp is not None else 0.5 * (
                    step.percent_ftp_lo + step.percent_ftp_hi
                )
                return {
                    "mid": float(ftp_watts) * float(mid_pct) / 100.0,
                    "lo": float(ftp_watts) * float(step.percent_ftp_lo) / 100.0,
                    "hi": float(ftp_watts) * float(step.percent_ftp_hi) / 100.0,
                }
            if step.percent_ftp is not None:
                return {"mid": float(ftp_watts) * float(step.percent_ftp) / 100.0, "lo": None, "hi": None}

        return None

    def _emit_pace(step: WorkoutStep) -> dict | None:
        # Explicit band?
        if (step.speed_mps_lo is not None) and (step.speed_mps_hi is not None):
            mid = step.speed_mps if step.speed_mps is not None else 0.5 * (
                float(step.speed_mps_lo) + float(step.speed_mps_hi)
            )
            return {
                "mid_mps": float(mid),
                "lo_mps": float(step.speed_mps_lo),
                "hi_mps": float(step.speed_mps_hi),
            }
        # Single target
        if step.speed_mps is not None:
            return {"mid_mps": float(step.speed_mps), "lo_mps": None, "hi_mps": None}
        return None

    out_steps: list[dict] = []
    for s in workout.steps:
        p = _emit_power(s)
        if p is not None:
            out_steps.append({"duration_s": float(s.duration_s), "kind": "power", "power": p})
            continue
        v = _emit_pace(s)
        if v is not None:
            out_steps.append({"duration_s": float(s.duration_s), "kind": "pace", "pace": v})
            continue
        out_steps.append({"duration_s": float(s.duration_s), "kind": "none"})

    return {
        "name": workout.name,
        "total_seconds": float(workout.total_seconds),
        "steps": out_steps,
    }
