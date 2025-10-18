from __future__ import annotations

import math
import random
import time
from collections import deque
from typing import TYPE_CHECKING

import gi
import numpy as np

from fitness_tracker.ui_free_run import FreeRunView
from fitness_tracker.ui_mode import ModeSelectView
from fitness_tracker.ui_workout import WorkoutView
from fitness_tracker.workouts import Workout, load_workout, pretty_workout_name

gi.require_versions({"Gtk": "4.0", "Adw": "1"})
from gi.repository import Adw, GLib

if TYPE_CHECKING:
    from pathlib import Path

# Pebble bridge workout constants
TGT_NONE, TGT_POWER, TGT_PACE = 0, 1, 2

class TrackerPageUI:
    def __init__(self, app: "FitnessAppUI") -> None:
        self.app = app

        # buffers (ms + values)
        self.window_sec = 60
        self.window_ms = self.window_sec * 1000.0
        self._times = deque()
        self._bpms = deque()
        self._powers = deque()
        self._last_ms = None

        # lifecycle flags
        self._running = False  # true ONLY after Start is pressed
        self._armed = False  # page is visible (preview) but not running yet

        # stats
        self._bpm_sum = 0.0
        self._bpm_n = 0
        self._bpm_max = 0
        self._last_bpm = 0

        # test-mode tick
        self._test_source = None
        self._start_monotonic = 0.0

        # live sensor cache
        self._rt_mph = 0.0
        self._rt_cadence = 0
        self._rt_dist_mi = 0.0
        self._rt_pace_str = "0:00"
        self._rt_watts = 0.0

        # status updater
        self._status_timer_id: int | None = None

        # workout state
        self._workout: Workout | None = None
        self._workout_path: Path | None = None
        self._active_step_index: int = -1
        self._manual_offset_s: float = 0.0
        self._sim_target_mph: float | None = None

        # UI pages
        self.nav: Adw.NavigationView | None = None
        self.mode_view: ModeSelectView | None = None
        self.free_view: FreeRunView | None = None
        self.workout_view: WorkoutView | None = None

    # ---- build
    def build_page(self):
        self.nav = Adw.NavigationView()

        self.mode_view = ModeSelectView(
            workouts_running_dir=self.app.workouts_running_dir,
            on_start_free_run=self._show_free_run_page,
            on_start_workout=self._start_workout_from_path,
        )
        self.mode_page = Adw.NavigationPage.new(self.mode_view, "Choose Activity")
        self.nav.add(self.mode_page)

        if self._status_timer_id is None:
            self._status_timer_id = GLib.timeout_add_seconds(1, self._tick_status)

        return self.nav

    # ---- mode callbacks
    def _start_workout_from_path(self, path: Path) -> None:
        w = load_workout(path)
        self._workout = w if w.steps else None
        self._workout_path = path
        self._manual_offset_s = 0.0
        self._show_workout_page()

    # -------------------------
    #  Page show / run control
    # -------------------------
    def _show_free_run_page(self) -> None:
        if self.app.pebble_bridge:
            self.app.pebble_bridge.update(tgt_kind=TGT_NONE)

        # Build page but DO NOT start timers/recording yet
        self._armed = True
        self._running = False
        self._reset_buffers()

        self.free_view = FreeRunView(self.app)
        # Stop always works
        self.free_view.btn_stop.connect("clicked", lambda *_: self._stop_run_and_back())
        # Start may or may not exist depending on your FreeRunView version
        btn_start = getattr(self.free_view, "btn_start", None)
        if btn_start:
            btn_start.connect("clicked", lambda *_: self._begin_run_now())

        self._push(self.free_view, "Free Run")
        # initial statuses & preview values
        self._update_metric_statuses()
        self._update_free_preview_timer_and_cards()

        # In preview we DO NOT call recorder.start_recording()

        # If test mode, we still feed preview samples, but we gate timers/progress
        if self.app.test_mode and self._test_source is None:
            self._hrsim_last_ms = None
            self._hrsim_bpm = float(self.app.resting_hr)
            self._test_source = GLib.timeout_add(1000, self._tick_test)

    def _show_workout_page(self) -> None:
        self._armed = True
        self._running = False
        self._reset_buffers()

        raw = self._workout_path.stem if self._workout_path else "Workout"
        nice = pretty_workout_name(raw)
        self.workout_view = WorkoutView(
            title=nice,
            on_prev=lambda: self._skip_step(-1),
            on_next=lambda: self._skip_step(+1),
            on_stop=self._stop_run_and_back,
            on_start_record=self._begin_run_now,
        )
        self._push(self.workout_view, nice)
        self._active_step_index = -1

        # Prime UI in preview (t=0)
        self._update_workout_guidance(elapsed_s=0)
        self.workout_view.set_progress(0.0)
        self._update_metric_statuses()
        self._update_workout_preview_timers()

        if self.app.test_mode and self._test_source is None:
            self._hrsim_last_ms = None
            self._hrsim_bpm = float(self.app.resting_hr)
            self._test_source = GLib.timeout_add(1000, self._tick_test)

    def _begin_run_now(self) -> None:
        """Called when Start is pressed."""
        if self._running:
            return
        self._running = True
        self._armed = True
        self._start_monotonic = time.monotonic()
        self._last_ms = 0
        if self.app.recorder:
            self.app.recorder.start_recording()

        # flip Start/Stop sensitivity on workout page if present
        if self.workout_view:
            self.workout_view.set_recording(True)
        if self.free_view and hasattr(self.free_view, "set_recording"):
            self.free_view.set_recording(True)

    def _stop_run_and_back(self) -> None:
        # Always stop timers/recording
        self._running = False
        self._armed = False
        if self.app.recorder:
            self.app.recorder.stop_recording()
        if self._test_source:
            GLib.source_remove(self._test_source)
            self._test_source = None

        # release page refs and go home
        self.free_view = None
        self.workout_view = None
        self._pop_to_mode()

    # ---- nav helpers
    def _push(self, child, title: str):
        page = Adw.NavigationPage.new(child, title)
        self.nav.push(page)

    def _pop_to_mode(self):
        self.nav.pop_to_page(self.mode_page)

    # ---- recorder callbacks (public)
    def on_bpm(self, delta_ms: float, bpm: int) -> None:
        self._last_bpm = bpm
        self._last_ms = delta_ms

        # If not running yet, keep preview values/cards updated but do NOT progress timers
        if not self._running:
            self._preview_cards_only()
            # workout preview: keep guidance at t=0
            if self.workout_view and self._workout:
                self._update_workout_guidance(elapsed_s=0)
                self.workout_view.set_progress(0.0)
            return

        # Running → feed sample
        watts = self._current_power_for_time(delta_ms)
        self._push_sample(delta_ms, bpm, watts)

    def on_running(self, delta_ms, speed_mps, cadence_spm, distance_m, power_watts):
        mph = speed_mps * 2.23693629
        dist_mi = (distance_m or 0.0) * 0.00062137119
        pace_str = self._pace_from_mph(mph) if mph > 0.01 else "0:00"
        watts = power_watts if power_watts is not None else 0.0

        self._rt_mph = mph
        self._rt_cadence = int(cadence_spm)
        self._rt_dist_mi = dist_mi
        self._rt_pace_str = pace_str
        self._rt_watts = float(watts)

        if not self._running:
            # live preview to cards
            self._preview_cards_only()
            # keep workout targeting visible at t=0
            if self.workout_view and self._workout:
                self._update_workout_guidance(elapsed_s=0)
                self.workout_view.set_progress(0.0)
            return

        # Running: use last bpm + current watts
        self._push_sample(delta_ms, self._last_bpm, self._rt_watts)

    # ---- core update
    def _push_sample(self, t_ms: float, bpm: int, watts: float) -> None:
        self._bpm_sum += bpm
        self._bpm_n += 1
        self._bpm_max = max(self._bpm_max, bpm)

        cutoff = t_ms - self.window_ms
        self._times.append(t_ms)
        self._bpms.append(bpm)
        self._powers.append(watts)
        while self._times and self._times[0] < cutoff:
            self._times.popleft()
            self._bpms.popleft()
            self._powers.popleft()

        # Build arrays for free-view chart (if visible)
        x = (np.array(self._times) - cutoff) / 1000.0 if self._times else np.array([])
        hr = np.array(self._bpms) if self._bpms else np.array([])
        pw = np.array(self._powers) if self._powers else np.array([])
        _, _, _, rgb = self._zone_info(self._bpms[-1]) if self._bpms else ("", 0, 0, (1, 1, 1))

        if self.free_view and hasattr(self.free_view, "update_chart"):
            self.free_view.update_chart(x, hr, pw, hr_rgb=rgb)

        # workout guidance (and completion detection) — only when running
        elapsed_s = max(0, int(t_ms // 1_000))
        if self._running and self.workout_view and self._workout:
            self._update_workout_guidance(elapsed_s)
            self._update_workout_running_timers(elapsed_s)
            self._maybe_complete_workout(elapsed_s)

        # cards/timer
        mph = self._rt_mph if not self.app.test_mode else getattr(self, "_last_mph", 0.0)
        cadence = self._rt_cadence if not self.app.test_mode else getattr(self, "_last_cadence", 0)
        dist_mi = (
            self._rt_dist_mi
            if not self.app.test_mode
            else getattr(self, "_integrated_distance_miles", 0.0)
        )
        pace_str = self._rt_pace_str if not self.app.test_mode else self._pace_from_mph(mph)
        watts_val = round(self._rt_watts if not self.app.test_mode else watts)

        self._set_cards(dist_mi, pace_str, cadence, mph, bpm, watts_val)

        # free run timer
        if self.free_view:
            hh, rem = divmod(elapsed_s, 3600)
            mm, ss = divmod(rem, 60)
            self.free_view.set_timer(f"{hh:02d}:{mm:02d}:{ss:02d}")

        # pebble bridge
        if self.app.pebble_bridge:
            speed_mps = float(mph) * 0.44704
            dist_m = float(dist_mi) / 0.00062137119
            self.app.pebble_bridge.update(
                hr=int(bpm),
                speed_mps=speed_mps,
                cadence=int(cadence),
                dist_m=dist_m,
                status=1 if self._running else 0,
                power_w=int(watts_val),
            )

    # ---- workout guidance
    def _update_workout_guidance(self, elapsed_s: int) -> None:
        """Compute target for a given elapsed_s (caller decides whether running or preview)."""
        if not self.workout_view or not self._workout:
            return

        t_s = max(0.0, float(elapsed_s) + self._manual_offset_s)

        (
            w_mid,
            w_lo,
            w_hi,
            v_mid,
            v_lo,
            v_hi,
            idx,
        ) = self._workout.target_band_at(t_s, self.app.ftp_watts)
        self._active_step_index = idx

        # target text
        tgt_txt = "Target: —"
        if w_mid is not None:
            tgt_txt = (
                f"Target: {int(round(w_lo or w_mid * 0.95))}–{int(round(w_hi or w_mid * 1.05))} W"
            )
        elif v_mid is not None:
            tgt_txt = f"Target: {self._pace_from_mps(v_mid)} /mi"
            if v_lo is not None and v_hi is not None:
                # order pace strings from faster to slower
                lo_s = 1609.344 / max(1e-6, v_lo)
                hi_s = 1609.344 / max(1e-6, v_hi)
                a = self._pace_from_mps(v_lo)
                b = self._pace_from_mps(v_hi)
                pace_a, pace_b = (a, b) if lo_s <= hi_s else (b, a)
                tgt_txt = f"Target: {pace_a}–{pace_b} /mi"

        # next preview
        nxt_text = "Next: —"
        if 0 <= idx + 1 < len(self._workout.steps):
            ns = self._workout.steps[idx + 1]
            if ns.watts is not None or ns.percent_ftp is not None:
                nxt_w = ns.target_watts(self.app.ftp_watts)
                nxt_text = f"Next: {int(round(nxt_w))} W for {int(ns.duration_s)} s"
            elif ns.speed_mps is not None:
                nxt_text = (
                    f"Next: {self._pace_from_mps(ns.speed_mps)} /mi for {int(ns.duration_s)} s"
                )

        self.workout_view.set_target_text(tgt_txt)
        self.workout_view.set_next_text(nxt_text)

        # gauge update (choose power vs pace)
        if w_mid is not None:
            cur_w = float(
                self._rt_watts if not self.app.test_mode else getattr(self, "_last_power", 0.0)
            )
            self.workout_view.set_gauge_power(
                current_w=cur_w,
                target_w=float(w_mid),
                target_w_lo=w_lo,
                target_w_hi=w_hi,
            )
            if self.app.pebble_bridge:
                lo = float(w_lo if w_lo is not None else w_mid * 0.95)
                hi = float(w_hi if w_hi is not None else w_mid * 1.05)
                self.app.pebble_bridge.update(
                    tgt_kind=TGT_POWER,
                    tgt_lo=w_lo,
                    tgt_hi=w_hi,
                )
        elif v_mid is not None:
            mph = self._rt_mph if not self.app.test_mode else getattr(self, "_last_mph", 0.0)
            cur_mps = float(mph) * 0.44704
            self.workout_view.set_gauge_pace(
                current_mps=cur_mps,
                target_mps=float(v_mid),
                current_pace_text=self._pace_from_mps(cur_mps),
                target_pace_text=self._pace_from_mps(v_mid),
                target_mps_lo=v_lo,
                target_mps_hi=v_hi,
            )
            if self.app.pebble_bridge:
                lo = float(v_lo if v_lo is not None else v_mid * 0.97)
                hi = float(v_hi if v_hi is not None else v_mid * 1.03)
                self.app.pebble_bridge.update(
                    tgt_kind=TGT_PACE,
                    tgt_lo=lo,   # m/s
                    tgt_hi=hi,   # m/s
                )
        else:
            if self.app.pebble_bridge:
                self.app.pebble_bridge.update(tgt_kind=TGT_NONE)

        # step progress — ONLY advance when running
        if self._running:
            acc = 0.0
            step_start = 0.0
            step_dur = 1.0
            for i, s in enumerate(self._workout.steps):
                nxt = acc + s.duration_s
                if i == idx:
                    step_start = acc
                    step_dur = max(1.0, s.duration_s)
                    break
                acc = nxt
            step_elapsed = min(max(0.0, t_s - step_start), step_dur)
            frac = step_elapsed / step_dur
            self.workout_view.set_progress(float(frac))

    def _maybe_complete_workout(self, elapsed_s: int) -> None:
        """When workout time is up, switch to Free Run and carry on (only if running)."""
        if not (self._running and self._workout):
            return
        t_s = max(0.0, float(elapsed_s) + self._manual_offset_s)
        total = float(self._workout.total_seconds)
        if t_s >= total - 1e-6:
            self._workout = None
            self._workout_path = None
            self._active_step_index = -1
            self._manual_offset_s = 0.0
            # Swap UI to free-run (keep recording, timer, charts alive)
            self.free_view = FreeRunView(self.app)
            self.free_view.btn_stop.connect("clicked", lambda *_: self._stop_run_and_back())
            # Replace the top page: pop workout, push free-run
            self.nav.pop()
            self._push(self.free_view, "Free Run")
            self.free_view.set_recording(True)
            self._update_metric_statuses()
            self.app.show_toast("✅ Workout complete. Continuing in Free Run…")

            if self.app.pebble_bridge:
                self.app.pebble_bridge.update(tgt_kind=TGT_NONE)


    def _skip_step(self, direction: int) -> None:
        if not self._workout or self._active_step_index < 0:
            return
        starts = [0.0]
        for s in self._workout.steps:
            starts.append(starts[-1] + s.duration_s)

        current_elapsed = max(0.0, (self._last_ms or 0.0) / 1000.0)
        t_s = max(0.0, current_elapsed + self._manual_offset_s)

        target_idx = min(
            max(0, self._active_step_index + (1 if direction > 0 else -1)),
            len(self._workout.steps) - 1,
        )
        target_start = starts[target_idx]
        # Immediate jump (preview and running both supported)
        self._manual_offset_s += (target_start - t_s) + 0.001
        # refresh guidance with either running time or preview time 0
        self._update_workout_guidance(int(current_elapsed if self._running else 0))

    # ---- helpers
    def _set_cards(self, dist_mi, pace_str, cadence, mph, bpm, watts):
        # Free-run cards
        if self.free_view:
            self.free_view.set_metrics(dist_mi, pace_str, cadence, mph, bpm, watts)
        # Workout metric strip
        if self.workout_view:
            # pick center card domain based on whether there is an active power target
            is_power = False
            if self._workout and self._active_step_index >= 0:
                s = self._workout.steps[self._active_step_index]
                is_power = (
                    (s.watts is not None)
                    or (s.percent_ftp is not None)
                    or (s.watts_hi is not None)
                    or (s.percent_ftp_hi is not None)
                )
            self.workout_view.set_metrics(
                bpm=int(bpm),
                pace=pace_str,
                cadence_spm=int(cadence),
                speed_mph=float(mph),
                dist_mi=float(dist_mi),
                power_watts=int(watts),
                is_power=is_power,
            )

    def _preview_cards_only(self) -> None:
        """Update the visible cards/labels without progressing time/progress."""
        mph = self._rt_mph if not self.app.test_mode else getattr(self, "_last_mph", 0.0)
        cadence = self._rt_cadence if not self.app.test_mode else getattr(self, "_last_cadence", 0)
        dist_mi = (
            self._rt_dist_mi
            if not self.app.test_mode
            else getattr(self, "_integrated_distance_miles", 0.0)
        )
        pace_str = self._rt_pace_str if not self.app.test_mode else self._pace_from_mph(mph)
        watts_val = round(
            self._rt_watts if not self.app.test_mode else getattr(self, "_last_power", 0.0)
        )
        self._set_cards(dist_mi, pace_str, cadence, mph, int(self._last_bpm or 0), watts_val)

        # keep timers frozen in preview
        self._update_free_preview_timer_and_cards()
        self._update_workout_preview_timers()

        # pebble preview (status=0)
        if self.app.pebble_bridge:
            self.app.pebble_bridge.update(
                hr=int(self._last_bpm or 0),
                speed_mps=float(mph) * 0.44704,
                cadence=int(cadence),
                dist_m=float(dist_mi) / 0.00062137119,
                status=0,
                power_w=int(watts_val),
            )

    def _update_free_preview_timer_and_cards(self) -> None:
        if self.free_view:
            self.free_view.set_timer("00:00:00")

    def _update_workout_preview_timers(self) -> None:
        if not self.workout_view:
            return
        self.workout_view.set_elapsed_text("00:00")
        # remaining = current step full duration
        if self._workout and self._workout.steps:
            dur = int(self._workout.steps[0].duration_s)
            self.workout_view.set_step_remaining_text(self._fmt_mmss(dur))
        else:
            self.workout_view.set_step_remaining_text("00:00")

    def _update_workout_running_timers(self, elapsed_s: int) -> None:
        if not (self.workout_view and self._workout):
            return
        # elapsed
        self.workout_view.set_elapsed_text(
            self._fmt_hhmmss(elapsed_s)[-5:]
        )  # show mm:ss for legibility

        # remaining in current step
        t_s = max(0.0, float(elapsed_s) + self._manual_offset_s)
        acc = 0.0
        step_start = 0.0
        step_dur = 0.0
        for i, s in enumerate(self._workout.steps):
            nxt = acc + s.duration_s
            if t_s < nxt + 1e-6:
                step_start = acc
                step_dur = s.duration_s
                break
            acc = nxt
        step_elapsed = min(max(0.0, t_s - step_start), max(1.0, step_dur))
        remaining = int(max(0.0, step_dur - step_elapsed))
        self.workout_view.set_step_remaining_text(self._fmt_mmss(remaining))

    # ---- test-mode generator
    def _tick_test(self) -> bool:
        if not (self._running or self._armed):
            return False
        t_now = time.monotonic()
        t_ms = int((t_now - getattr(self, "_start_monotonic", t_now)) * 1000)
        # In preview (armed but not running), keep t_ms at 0 so nothing progresses
        if not self._running:
            t_ms = 0
        self._last_ms = t_ms
        t_min = max(0.0, t_ms / 60000.0)
        dt_s = 1.0
        if getattr(self, "_hrsim_last_ms", None) is not None:
            dt_s = max(0.001, (t_ms - self._hrsim_last_ms) / 1000.0)
        self._hrsim_last_ms = t_ms

        # Target power profile
        target_power = getattr(self, "_last_power", 250.0)
        self._sim_target_mph = None
        if self._workout:
            # lock to t=0 when not running
            t_s = (t_ms / 1000.0 if self._running else 0.0) + self._manual_offset_s
            w, v_mps, _ = self._workout.target_at(t_s, self.app.ftp_watts)
            if w is not None:
                target_power = float(w)
            elif v_mps is not None:
                 # Pace-targeted step: drive simulated power from speed
                self._sim_target_mph = float(v_mps) * 2.23693629
                mph = self._sim_target_mph
                # very simple running-power proxy (keeps values sensible in 5–10 mph)
                target_power = max(80.0, 18.0 * mph)  # e.g., 5 mph ≈ 90 W, 10 mph ≈ 180 W
        else:
            # free-run waves
            cycle_len = 120.0
            phase = (t_min * 60) % cycle_len
            if phase < 30:
                target_power = random.uniform(400, 600)
            elif phase < 60:
                target_power = random.uniform(180, 250)
            elif phase < 90:
                target_power = random.uniform(350, 550)
            else:
                target_power = random.uniform(180, 240)

        last_power = getattr(self, "_last_power", 250.0)
        self._last_power = last_power + 0.35 * (target_power - last_power)

        zones = self.app.calculate_hr_zones()
        z2_lo, _ = zones["Zone 3"]
        if target_power > 300:
            hr_target = self.app.max_hr - random.uniform(0, 3)
            tau = 10.0
        else:
            hr_target = z2_lo - random.uniform(0, 5)
            tau = 45.0
        alpha = 1.0 - math.exp(-dt_s / tau)
        self._hrsim_bpm += alpha * (hr_target - self._hrsim_bpm)
        self._hrsim_bpm += random.uniform(-0.8, 0.8)
        self._hrsim_bpm = float(max(self.app.resting_hr, min(self.app.max_hr, self._hrsim_bpm)))
        bpm = int(round(self._hrsim_bpm))

        target_mph = getattr(self, "_sim_target_mph", None)
        cur_mph = getattr(self, "_last_mph", 6.8)
        if target_mph is not None:
            cur_mph = cur_mph + 0.25 * (target_mph - cur_mph)
            cur_mph += random.uniform(-0.05, 0.05)
        else:
            cur_mph = max(2.0, min(10.0, cur_mph + random.uniform(-0.3, 0.3)))
        self._last_mph = cur_mph
        self._last_cadence = max(
            75, min(95, getattr(self, "_last_cadence", 86) + random.uniform(-2, 2))
        )

        if self._running:
            dmiles = self._last_mph / 3600.0
            self._integrated_distance_miles = (
                getattr(self, "_integrated_distance_miles", 0.0) + dmiles
            )

        # Push or preview
        if self._running:
            self._push_sample(t_ms, bpm, self._last_power)
        else:
            self._last_bpm = bpm
            self._preview_cards_only()
            if self.workout_view and self._workout:
                self._update_workout_guidance(elapsed_s=0)
                self.workout_view.set_progress(0.0)
        return True

    # ---- connection dots
    def _update_metric_statuses(self) -> None:
        rec = getattr(self.app, "recorder", None)
        if not rec:
            hr_ok = speed_ok = cad_ok = pow_ok = True
        else:
            hr_ok = bool(rec.hr_connected)
            speed_ok = bool(rec.speed_connected)
            cad_ok = bool(rec.cadence_connected)
            pow_ok = bool(rec.power_connected)

        if self.free_view and hasattr(self.free_view, "set_statuses"):
            self.free_view.set_statuses(hr_ok, speed_ok, cad_ok, pow_ok)
        if self.workout_view:
            self.workout_view.set_statuses(
                hr_ok=hr_ok, cad_ok=cad_ok, spd_ok=speed_ok, pow_ok=pow_ok
            )

    def _tick_status(self) -> bool:
        self._update_metric_statuses()
        return True

    # ---- resets & utils
    def _reset_buffers(self) -> None:
        self._times.clear()
        self._bpms.clear()
        self._powers.clear()
        self._last_ms = None
        self._bpm_sum = 0.0
        self._bpm_n = 0
        self._bpm_max = 0
        # freeze integrated distance until running
        if hasattr(self, "_integrated_distance_miles"):
            self._integrated_distance_miles = getattr(self, "_integrated_distance_miles", 0.0)

    def _zone_info(self, hr: float):
        zones = self.app.calculate_hr_zones()
        order = list(zones.keys())
        for name, color in zip(order, self.app.ZONE_COLORS):
            lo, hi = zones[name]
            if lo <= hr < hi:
                return name, lo, hi, self._rgb(color)
        first, last = order[0], order[-1]
        if hr < zones[first][0]:
            return first, *zones[first], self._rgb(self.app.ZONE_COLORS[0])
        return last, *zones[last], self._rgb(self.app.ZONE_COLORS[-1])

    def _rgb(self, hex_color: str) -> tuple[float, float, float]:
        hex_color = hex_color.lstrip("#")
        r = int(hex_color[0:2], 16) / 255.0
        g = int(hex_color[2:4], 16) / 255.0
        b = int(hex_color[4:6], 16) / 255.0
        return (r, g, b)

    @staticmethod
    def _pace_from_mph(mph: float) -> str:
        if mph <= 0.01:
            return "0:00"
        mins_per_mile = 60.0 / mph
        m = int(mins_per_mile)
        s = round((mins_per_mile - m) * 60)
        if s == 60:
            m += 1
            s = 0
        return f"{m}:{s:02d}"

    @staticmethod
    def _pace_from_mps(mps: float) -> str:
        if mps <= 0.001:
            return "0:00"
        mph = mps * 2.23693629
        return TrackerPageUI._pace_from_mph(mph)

    def _current_power_for_time(self, _t_ms: float) -> float:
        if not self.app.test_mode:
            return float(self._rt_watts or 0.0)
        last = getattr(self, "_last_power", 250.0)
        target_watts = last
        self._sim_target_mph = None
        if self._workout and self._last_ms is not None:
            t_s = (self._last_ms / 1000.0 if self._running else 0.0) + self._manual_offset_s
            w, v_mps, _ = self._workout.target_at(t_s, self.app.ftp_watts)
            if w is not None:
                target_watts = float(w)
            elif v_mps is not None:
                self._sim_target_mph = float(v_mps) * 2.23693629
        self._last_power = last + 0.25 * (target_watts - last)
        return self._last_power

    @staticmethod
    def _fmt_mmss(total_s: int) -> str:
        m, s = divmod(int(total_s), 60)
        return f"{m:02d}:{s:02d}"

    @staticmethod
    def _fmt_hhmmss(total_s: int) -> str:
        h, r = divmod(int(total_s), 3600)
        m, s = divmod(r, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    def redraw(self) -> None:
        if self.free_view and getattr(self.free_view, "fig", None):
            self.free_view.fig.canvas.draw_idle()
