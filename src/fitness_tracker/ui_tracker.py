from __future__ import annotations

import math
import random
import time
from collections import deque
from typing import TYPE_CHECKING

import gi
import numpy as np
from bleaksport import CyclingSample, HeartRateSample, RunningSample, TrainerSample
from workout_parser.main import load_workout, pretty_workout_name

from fitness_tracker.database import SportTypesEnum
from fitness_tracker.ui_free_run import FreeRunView
from fitness_tracker.ui_mode import IndoorOutdoorEnum, ModeSelectView
from fitness_tracker.ui_workout import WorkoutView

gi.require_versions({"Gtk": "4.0", "Adw": "1"})
from gi.repository import Adw, GLib  # noqa: E402  # ty:ignore[unresolved-import]

if TYPE_CHECKING:
    from pathlib import Path

    from workout_parser.models import Workout

# Pebble bridge workout constants
TGT_NONE, TGT_POWER, TGT_PACE = 0, 1, 2


class TrackerPageUI:
    def __init__(self, app) -> None:
        self.app = app

        # buffers (ms + values)
        self.window_sec = 60
        self.window_ms = self.window_sec * 1000.0
        self._times = deque()
        self._bpms = deque()
        self._powers = deque()
        self._last_ms: int | None = None
        self._erg_last_set_watts: int | None = None
        self._erg_last_set_ts: float = 0.0

        # lifecycle flags
        self._running = False  # true ONLY after Start is pressed
        self._armed = False  # page is visible (preview) but not running yet

        # stats
        self._bpm_max = 0
        self._last_bpm = 0

        # test-mode tick
        self._test_source = None
        self._start_monotonic = 0.0

        # live sensor cache
        self._rt_mph: float = 0.0
        self._rt_mps: float = 0.0
        self._rt_cadence: int = 0
        self._rt_dist_mi: float = 0.0
        self._rt_dist_m: float = 0.0
        self._rt_pace_str: str = "0:00"
        self._rt_watts: int = 0

        # status updater
        self._status_timer_id: int | None = None

        # workout state
        self._workout: Workout | None = None
        self._active_step_index: int = -1
        self._manual_offset_s: float = 0.0
        self._sim_target_mph: float | None = None
        self._hrsim_last_ms: int | None = None
        self._hrsim_bpm: float | None = None

        # UI timer state
        self._timer_source_id: int | None = None
        self._elapsed_display_s: int = 0

        # workout pause state
        self._workout_paused: bool = False
        self._workout_pause_started_monotonic: float | None = None

        # UI pages
        self.nav: Adw.NavigationView | None = None
        self.mode_view: ModeSelectView | None = None
        self.free_view: FreeRunView | None = None
        self.workout_view: WorkoutView | None = None

    # ---- build
    def build_page(self) -> Adw.NavigationView:
        nav = Adw.NavigationView()
        # We want to force users to use the Stop button to exit free-run or workout pages
        # so disable the back gesture on all pages
        nav.set_pop_on_escape(False)

        self.mode_view = ModeSelectView(
            workouts_running_dir=self.app.workouts_running_dir,
            workouts_cycling_dir=self.app.workouts_cycling_dir,
            on_start_free=self._show_free_from_mode,
            on_start_workout=self._start_workout,
        )
        self.mode_page = Adw.NavigationPage.new(self.mode_view, "Choose Activity")
        nav.add(self.mode_page)

        if self._status_timer_id is None:
            self._status_timer_id = GLib.timeout_add_seconds(1, self._tick_status)

        self.nav = nav
        return self.nav

    # ---- mode callbacks
    def _start_workout(
        self,
        workout: Workout,
        sport_type: SportTypesEnum,
        in_outdoor: IndoorOutdoorEnum,
        trainer: bool = False,
    ) -> None:
        self._workout: Workout | None = workout if workout.steps else None
        self._manual_offset_s = 0.0
        self._show_workout_page(sport_type=sport_type, in_outdoor=in_outdoor, trainer=trainer)

    def _show_free_from_mode(
        self,
        sport_type: SportTypesEnum,
        in_outdoor: IndoorOutdoorEnum,
        trainer: bool = False,
    ) -> None:
        self._show_free_run_page(sport_type=sport_type, in_outdoor=in_outdoor, trainer=trainer)

    def _on_workout_start_pause_clicked(self) -> None:
        if not self._running:
            # First press: start the workout
            self._begin_run_now()
        elif self._workout_paused:
            # Currently paused: resume
            if self._workout_pause_started_monotonic is not None:
                paused_for = time.monotonic() - self._workout_pause_started_monotonic
                self._manual_offset_s -= paused_for
            self._workout_pause_started_monotonic = None
            self._workout_paused = False
            # Reset erg throttle so target power re-applies immediately
            self._erg_last_set_watts = None
            self._erg_last_set_ts = 0.0
            if self.workout_view:
                self.workout_view.set_paused(False)
        else:
            # Currently running: pause
            self._workout_pause_started_monotonic = time.monotonic()
            self._workout_paused = True
            # Drop ERG target so user isn't fighting the trainer
            if self.app.recorder and self.app.recorder.trainer_mux:
                self.app.recorder.set_target_power(0)
                self._erg_last_set_watts = None
            if self.workout_view:
                self.workout_view.set_paused(True)

    def _tick_timer(self) -> bool:
        """
        1 Hz UI timer:
        - updates the free-run timer label
        - updates workout timers / guidance / completion
        Independent of sensor sample timing.
        """
        if not self._running:
            # stop the timeout
            self._timer_source_id = None
            return False

        self._elapsed_display_s = int(time.monotonic() - self._start_monotonic)
        elapsed_s = self._elapsed_display_s

        # Free-run timer
        if self.free_view:
            hh, rem = divmod(elapsed_s, 3600)
            mm, ss = divmod(rem, 60)
            self.free_view.set_timer(f"{hh:02d}:{mm:02d}:{ss:02d}")

        # Workout timers / guidance
        if self.workout_view and self._workout:
            # Elapsed always updates, even while paused
            self.workout_view.set_elapsed_text(self._fmt_hhmmss(elapsed_s))

            if not self._workout_paused:
                self._update_workout_guidance(elapsed_s)
                self._update_workout_running_timers(elapsed_s)
                self._maybe_complete_workout(elapsed_s)

        return True

    # -------------------------
    #  Page show / run control
    # -------------------------
    def _make_free_view(
        self,
        sport_type: SportTypesEnum,
        in_outdoor: IndoorOutdoorEnum,
        trainer: bool,
    ) -> FreeRunView:
        """Construct a FreeRunView with stop/start buttons and incline wired up."""
        view = FreeRunView(
            self.app,
            sport_type=sport_type,
            in_outdoor=in_outdoor,
            trainer=trainer,
        )
        view.btn_stop.connect("clicked", lambda *_: self._stop_run_and_back())
        view.btn_start.connect("clicked", lambda *_: self._begin_run_now())
        view.set_incline_callback(self._on_incline_changed)
        if self.app.recorder and self.app.recorder.incline_percent is not None:
            view.incline_control.set_value(self.app.recorder.incline_percent)
        return view

    def _show_free_run_page(
        self,
        sport_type: SportTypesEnum = SportTypesEnum.running,
        in_outdoor: IndoorOutdoorEnum = IndoorOutdoorEnum.indoor,
        trainer: bool = False,
    ) -> None:
        # Reconfigure recorder for this activity *before* preview/status updates.
        self.app.apply_sensor_settings(sport_type=sport_type, trainer=trainer)
        if self.app.pebble_bridge:
            self.app.pebble_bridge.update(tgt_kind=TGT_NONE)

        # Build page but DO NOT start timers/recording yet
        self._armed = True
        self._running = False
        self._reset_buffers()

        self.free_view = self._make_free_view(sport_type, in_outdoor, trainer)
        title = "Free Ride" if sport_type == SportTypesEnum.biking else "Free Run"
        self._push(self.free_view, title)

        # initial statuses & preview values
        self.update_metric_statuses()
        self._update_free_preview_timer_and_cards()

        # In preview we DO NOT call recorder.start_recording()

        # If test mode, we still feed preview samples, but we gate timers/progress
        if self.app.test_mode and self._test_source is None:
            self._hrsim_last_ms = None
            self._hrsim_bpm = float(self.app.app_settings.personal.resting_hr or 60)
            self._test_source = GLib.timeout_add(1000, self._tick_test)

    def _show_workout_page(
        self,
        sport_type: SportTypesEnum = SportTypesEnum.running,
        in_outdoor: IndoorOutdoorEnum = IndoorOutdoorEnum.indoor,
        trainer: bool = False,
    ) -> None:
        # Reconfigure recorder for this activity *before* preview/status updates.
        self.app.apply_sensor_settings(sport_type=sport_type, trainer=trainer)

        self._armed = True
        self._running = False
        self._reset_buffers()

        raw = self._workout.name if self._workout else "Workout"
        nice = pretty_workout_name(raw)
        self.workout_view = WorkoutView(
            app=self.app,
            sport_type=sport_type,
            title=nice,
            on_prev=lambda: self._skip_step(-1),
            on_next=lambda: self._skip_step(+1),
            on_stop=self._stop_run_and_back,
            on_start_record=self._on_workout_start_pause_clicked,
            in_outdoor=in_outdoor,
            trainer=trainer,
        )
        self._workout_paused = False
        self._workout_pause_started_monotonic = None
        self._push(self.workout_view, nice)
        self._active_step_index = -1

        self.workout_view.set_incline_callback(self._on_incline_changed)
        if self.app.recorder and self.app.recorder.incline_percent is not None:
            self.workout_view.incline_control.set_value(self.app.recorder.incline_percent)

        # Prime UI in preview (t=0)
        self._update_workout_guidance(elapsed_s=0)
        self.workout_view.set_progress(0.0)
        self.update_metric_statuses()
        self._update_workout_preview_timers()

        if self.app.test_mode and self._test_source is None:
            self._hrsim_last_ms = None
            self._hrsim_bpm = float(self.app.app_settings.personal.resting_hr or 60)
            self._test_source = GLib.timeout_add(1000, self._tick_test)

    def _begin_run_now(self) -> None:
        """Called when Start is pressed."""
        if self._running:
            return
        self._running = True
        self._armed = True
        self._start_monotonic = time.monotonic()
        self._elapsed_display_s = 0
        self._last_ms = 0
        self._erg_last_set_watts = None
        self._erg_last_set_ts = 0.0

        if self.app.recorder:
            self.app.recorder.start_recording()

        # start 1 Hz UI timer (decoupled from sensors)
        if self._timer_source_id is None:
            self._timer_source_id = GLib.timeout_add_seconds(1, self._tick_timer)

        # flip Start/Stop sensitivity on workout page if present
        if self.workout_view:
            self.workout_view.set_recording(True)
        if self.free_view and hasattr(self.free_view, "set_recording"):
            self.free_view.set_recording(True)

    def _stop_run_and_back(self) -> None:
        # Always stop timers/recording
        self._running = False
        self._armed = False

        if self._timer_source_id is not None:
            GLib.source_remove(self._timer_source_id)
            self._timer_source_id = None

        if self.app.recorder:
            self.app.recorder.stop_recording()
            if self.app.recorder.activity_id:
                GLib.idle_add(self.app.history.append_activity, self.app.recorder.activity_id)
        if self._test_source:
            GLib.source_remove(self._test_source)
            self._test_source = None

        # release page refs and go home
        self.free_view = None
        self.workout_view = None

        self._erg_last_set_watts = None
        self._erg_last_set_ts = 0.0
        if self.app.recorder and self.app.recorder.trainer_mux:
            self.app.recorder.set_target_power(0)

        self._pop_to_mode()

    # ---- nav helpers
    def _push(self, child, title: str):
        page = Adw.NavigationPage.new(child, title)
        # Prevent back gesture on all pages to avoid accidental pops during activity.
        page.set_can_pop(False)
        if self.nav:
            self.nav.push(page)
        else:
            msg = "NavigationView not initialized"
            raise RuntimeError(msg)

    def _pop_to_mode(self):
        if self.nav:
            self.nav.pop_to_page(self.mode_page)
        else:
            msg = "NavigationView not initialized"
            raise RuntimeError(msg)

    # ---- recorder callbacks (public)
    def on_sample(
        self,
        sample: HeartRateSample | RunningSample | CyclingSample | TrainerSample,
    ) -> None:
        if isinstance(sample, HeartRateSample):
            if sample.heart_rate_bpm is None:
                return
            self._last_bpm = sample.heart_rate_bpm
            self._last_ms = sample.timestamp_ms
        else:
            self._last_ms = sample.timestamp_ms
            self._rt_mph = sample.speed_mph if sample.speed_mph is not None else 0.0
            self._rt_mps = sample.speed_mps if sample.speed_mps is not None else 0.0
            cadence_spm = (
                sample.cadence_spm
                if isinstance(sample, RunningSample) and sample.cadence_spm is not None
                else 0
            )
            cadence_rpm = (
                sample.cadence_rpm
                if isinstance(sample, (CyclingSample, TrainerSample))
                and sample.cadence_rpm is not None
                else 0
            )
            cadence = cadence_spm or cadence_rpm
            self._rt_cadence = int(cadence)
            self._rt_dist_mi = float(sample.distance_miles or 0.0)
            self._rt_dist_m = float(sample.distance_m or 0.0)
            self._rt_pace_str = self._pace_from_mph(self._rt_mph)
            self._rt_watts = int(sample.power_watts or 0)

        if not self._running:
            self._preview_cards_only()
            if self.workout_view and self._workout:
                self._update_workout_guidance(elapsed_s=0)
                self.workout_view.set_progress(0.0)
            return

        self._push_sample()

    # ---- core update
    def _push_sample(self) -> None:
        bpm = self._last_bpm
        t_ms = self._last_ms if self._last_ms is not None else int(time.monotonic() * 1000)
        watts = self._rt_watts

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

        # cards/timer
        speed_mph = self._rt_mph
        speed_mps = self._rt_mps
        cadence = self._rt_cadence
        dist_mi = self._rt_dist_mi
        dist_m = self._rt_dist_m
        pace_str = self._rt_pace_str
        watts_val = round(self._rt_watts)

        self._set_cards(dist_mi, pace_str, cadence, speed_mph, bpm, watts_val)

        # pebble bridge
        if self.app.pebble_bridge:
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
        idx, step = self._workout.get_step_at(t_s)
        if not step or idx is None:
            return

        if step.watts_mid is None and step.speed_mps_mid is None:
            # Only generate absolute targets if they are not already defined
            # to avoid overwriting any watt/pace targets set by the workout author
            step.generate_absolute_power_targets_from_percent(
                self.app.app_settings.personal.ftp_watts
            )
            # step.generate_pace_targets_from_percent(self.app.threshold_speed_mps)

        self._active_step_index = idx

        # target text
        tgt_txt = "Target: —"
        # lo and hi are guaranteed if any are present due to pydantic validators.
        if step.watts_lo is not None and step.watts_hi is not None:
            a = round(step.watts_lo)
            b = round(step.watts_hi)
            tgt_txt = f"Target: {a} - {b} W"
        elif step.speed_mph_lo is not None and step.speed_mph_hi is not None:
            a = self._pace_from_mph(step.speed_mph_lo)
            b = self._pace_from_mph(step.speed_mph_hi)
            tgt_txt = f"Target: {b} - {a} /mi"

        # next preview
        nxt_text = "Next: —"
        if 0 <= idx + 1 < len(self._workout.steps):
            next_step = self._workout.steps[idx + 1]

            if next_step.watts_mid is None and next_step.speed_mps_mid is None:
                next_step.generate_absolute_power_targets_from_percent(
                    self.app.app_settings.personal.ftp_watts
                )
                # next_step.generate_pace_targets_from_percent(self.app.threshold_speed_mps)

            if next_step.watts_lo is not None and next_step.watts_hi is not None:
                a = round(next_step.watts_lo)
                b = round(next_step.watts_hi)
                nxt_text = f"Next: {a} - {b} W for {int(next_step.duration_s)} s"
            elif next_step.speed_mph_lo is not None and next_step.speed_mph_hi is not None:
                a = self._pace_from_mph(next_step.speed_mph_lo)
                b = self._pace_from_mph(next_step.speed_mph_hi)
                nxt_text = f"Next: {b} - {a} /mi for {int(next_step.duration_s)} s"

        self.workout_view.set_target_text(tgt_txt)
        self.workout_view.set_next_text(nxt_text)

        # gauge update (choose power vs pace)
        if step.watts_lo is not None and step.watts_hi is not None and step.watts_mid is not None:
            self.workout_view.set_gauge_power(
                current_w=self._rt_watts,
                target_w_mid=step.watts_mid,
                target_w_lo=step.watts_lo,
                target_w_hi=step.watts_hi,
            )
            if self.app.pebble_bridge:
                self.app.pebble_bridge.update(
                    tgt_kind=TGT_POWER,
                    tgt_lo=step.watts_lo,
                    tgt_hi=step.watts_hi,
                )

            # ERG Mode controls
            if self.app.recorder and self.app.recorder.trainer_mux:
                now = time.monotonic()
                # Limit erg commands to 1 every 2 seconds to avoid overwhelming the trainer or BLE connection
                # Require a change of at least 3 watts to avoid sending redundant commands
                watt_diff = 3
                time_diff = 2.0
                if self._erg_last_set_watts is None or (
                    abs(self._erg_last_set_watts - step.watts_mid) >= watt_diff
                    and now - self._erg_last_set_ts > time_diff
                ):
                    self.app.recorder.set_target_power(step.watts_mid)
                    self._erg_last_set_watts = step.watts_mid
                    self._erg_last_set_ts = now

        # If a single speed target is defined
        # all of them are guaranteed to be defined due to pydantic validators
        elif (
            step.speed_mps_mid is not None
            and step.speed_mph_mid is not None
            and step.speed_mps_lo is not None
            and step.speed_mps_hi is not None
        ):
            self.workout_view.set_gauge_pace(
                current_mps=self._rt_mps,
                current_pace_text=self._pace_from_mph(self._rt_mph),
                target_pace_text=self._pace_from_mph(step.speed_mph_mid),
                target_mps_lo=step.speed_mps_lo,
                target_mps_mid=step.speed_mps_mid,
                target_mps_hi=step.speed_mps_hi,
            )
            if self.app.pebble_bridge:
                self.app.pebble_bridge.update(
                    tgt_kind=TGT_PACE,
                    tgt_lo=step.speed_mps_lo,
                    tgt_hi=step.speed_mps_hi,
                )
        elif self.app.pebble_bridge:
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

        if t_s >= total:
            self._workout = None
            self._active_step_index = -1
            self._manual_offset_s = 0.0

            # Swap UI to free-run (keep recording, timer, charts alive)
            sport_type = (
                self.workout_view.sport_type if self.workout_view else SportTypesEnum.running
            )
            in_outdoor = (
                self.workout_view.in_outdoor if self.workout_view else IndoorOutdoorEnum.indoor
            )
            trainer = self.workout_view.trainer if self.workout_view else False

            self.free_view = self._make_free_view(sport_type, in_outdoor, trainer)

            # Replace the top page: pop workout, push free-run
            if self.nav:
                self.nav.pop()
                self._push(self.free_view, "Free Run")
                self.free_view.set_recording(True)
                self.update_metric_statuses()
            # Clear reference to the workout view so it is not updated off-screen
            self.workout_view = None
            self.app.show_toast("✅ Workout complete. Continuing in Free Run…")

            if self.app.pebble_bridge:
                self.app.pebble_bridge.update(tgt_kind=TGT_NONE)

    def _skip_step(self, direction: int) -> None:
        if not self._workout or self._active_step_index < 0:
            return

        starts = [0.0]
        for s in self._workout.steps:
            starts.append(starts[-1] + s.duration_s)

        current_elapsed = float(self._elapsed_display_s if self._running else 0.0)
        t_s = max(0.0, current_elapsed + self._manual_offset_s)

        target_idx = min(
            max(0, self._active_step_index + (1 if direction > 0 else -1)),
            len(self._workout.steps) - 1,
        )
        target_start = starts[target_idx]

        # Immediate jump (preview and running both supported)
        self._manual_offset_s += (target_start - t_s) + 0.001

        # Reset erg values so it sets immediately on step change if in an erg step
        self._erg_last_set_watts = None
        self._erg_last_set_ts = 0.0

        # refresh guidance with either running time or preview time 0
        self._update_workout_guidance(int(current_elapsed if self._running else 0))

    # ---- helpers
    def _set_cards(
        self,
        dist_mi: float,
        pace_str: str,
        cadence: int,
        mph: float,
        bpm: int,
        watts: int,
    ) -> None:
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
                    (s.watts_mid is not None)
                    or (s.percent_watts_mid is not None)
                    or (s.watts_hi is not None)
                    or (s.percent_watts_hi is not None)
                )
            self.workout_view.set_metrics(
                bpm=bpm,
                pace=pace_str,
                cadence_spm=cadence,
                speed_mph=mph,
                dist_mi=dist_mi,
                power_watts=watts,
                is_power=is_power,
            )

    def _preview_cards_only(self) -> None:
        """Update the visible cards/labels without progressing time/progress."""
        speed_mph = self._rt_mph
        speed_mps = self._rt_mps
        cadence = self._rt_cadence
        dist_mi = self._rt_dist_mi
        dist_m = self._rt_dist_m
        pace_str = self._rt_pace_str
        watts_val = round(self._rt_watts)
        self._set_cards(dist_mi, pace_str, cadence, speed_mph, int(self._last_bpm or 0), watts_val)

        # keep timers frozen in preview
        self._update_free_preview_timer_and_cards()
        self._update_workout_preview_timers()

        # pebble preview (status=0)
        if self.app.pebble_bridge:
            self.app.pebble_bridge.update(
                hr=int(self._last_bpm or 0),
                speed_mps=speed_mps,
                cadence=cadence,
                dist_m=dist_m,
                status=0,
                power_w=watts_val,
            )

    def _update_free_preview_timer_and_cards(self) -> None:
        if self.free_view:
            self.free_view.set_timer("00:00")

    def _update_workout_preview_timers(self) -> None:
        if not self.workout_view:
            return
        self.workout_view.set_elapsed_text("00:00")

        # remaining = current step full duration
        if self._workout and self._workout.steps:
            dur = int(self._workout.steps[0].duration_s)
            # Keep the step remaining timer as mm:ss
            self.workout_view.set_step_remaining_text(self._fmt_mmss(dur))
        else:
            self.workout_view.set_step_remaining_text("00:00")

    def _update_workout_running_timers(self, elapsed_s: int) -> None:
        if not (self.workout_view and self._workout):
            return

        # remaining in current step
        t_s = max(0.0, float(elapsed_s) + self._manual_offset_s)
        acc = 0.0
        step_start = 0.0
        step_dur = 0.0
        for s in self._workout.steps:
            nxt = acc + s.duration_s
            if t_s < nxt + 1e-6:
                step_start = acc
                step_dur = s.duration_s
                break
            acc = nxt
        step_elapsed = min(max(0.0, t_s - step_start), max(1.0, step_dur))
        remaining = int(max(0.0, step_dur - step_elapsed))
        # Keep the step remaining timer as mm:ss
        self.workout_view.set_step_remaining_text(self._fmt_mmss(remaining))

    # ---- test-mode generator
    def _tick_test(self) -> bool:
        if not (self._running or self._armed):
            return False

        t_now = time.monotonic()
        # In preview (armed but not running), hold t_ms at 0 so nothing progresses
        t_ms = int((t_now - self._start_monotonic) * 1000) if self._running else 0
        self._last_ms = t_ms

        dt_s = 1.0
        if self._hrsim_last_ms is not None:
            dt_s = max(0.001, (t_ms - self._hrsim_last_ms) / 1000.0)
        self._hrsim_last_ms = t_ms

        # ---- power / speed target resolution ----
        target_power = getattr(self, "_last_power", 250.0)
        self._sim_target_mph = None

        if self._workout:
            t_s = (t_ms / 1000.0 if self._running else 0.0) + self._manual_offset_s
            idx, step = self._workout.get_step_at(t_s)
            if not step or idx is None:
                return True

            w, v_mph = step.watts_mid, step.speed_mph_mid
            if w is not None:
                target_power = float(w)
            elif v_mph is not None:
                self._sim_target_mph = v_mph
                target_power = max(80.0, 18.0 * v_mph)
        else:
            # free-run sinusoidal power wave
            t_min = max(0.0, t_ms / 60000.0)
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

        # ---- HR simulation ----
        zones = self.app.calculate_hr_zones()
        z2_lo, _ = zones["Zone 3"]
        if target_power > 300:
            hr_target = self.app.app_settings.personal.max_hr - random.uniform(0, 3)
            tau = 10.0
        else:
            hr_target = z2_lo - random.uniform(0, 5)
            tau = 45.0
        alpha = 1.0 - math.exp(-dt_s / tau)
        if self._hrsim_bpm is None:
            self._hrsim_bpm = float(self.app.app_settings.personal.resting_hr or 60)
        self._hrsim_bpm += alpha * (hr_target - self._hrsim_bpm)
        self._hrsim_bpm += random.uniform(-0.8, 0.8)
        self._hrsim_bpm = float(
            max(
                self.app.app_settings.personal.resting_hr or 60,
                min(self.app.app_settings.personal.max_hr or 190, self._hrsim_bpm),
            )
        )
        bpm = round(self._hrsim_bpm)

        # ---- speed / cadence / distance simulation ----
        target_mph = self._sim_target_mph
        cur_mph = getattr(self, "_last_mph", 6.8)
        if target_mph is not None:
            cur_mph = cur_mph + 0.25 * (target_mph - cur_mph)
            cur_mph += random.uniform(-0.05, 0.05)
        else:
            cur_mph = max(2.0, min(10.0, cur_mph + random.uniform(-0.3, 0.3)))
        self._last_mph = cur_mph

        self._last_cadence = int(
            max(75, min(95, getattr(self, "_last_cadence", 86) + random.uniform(-2, 2))),
        )

        if self._running:
            dmiles = self._last_mph / 3600.0
            self._integrated_distance_miles = (
                getattr(self, "_integrated_distance_miles", 0.0) + dmiles
            )
        dist_m = getattr(self, "_integrated_distance_miles", 0.0) / 0.00062137119

        # ---- inject through recorder pipeline ----
        rec = self.app.recorder
        if rec:
            # Wall-clock timestamp that the recorder handlers expect (seconds since epoch)
            wall_ts = time.time() if self._running else (time.time() - t_now)
            wall_ts_ms = int(wall_ts * 1000)

            # HR sample
            sample = HeartRateSample(
                timestamp_ms=wall_ts_ms,
                heart_rate_bpm=bpm,
                rr_interval_ms=None,
                energy_expended_kcal=None,
            )
            rec.inject_test_sample(sample)

            # Speed/power sample — choose the right sample type based on recorder config
            use_trainer = bool(rec.trainer_address)
            if use_trainer:
                sample = TrainerSample(
                    timestamp_ms=wall_ts_ms,
                    speed_kmh=float(self._last_mph) * 1.60934,
                    cadence_rpm=self._last_cadence,
                    distance_m=dist_m,
                    power_watts=int(self._last_power),
                    target_power=None,
                )
            elif self.free_view and self.free_view.sport_type == SportTypesEnum.biking:
                sample = CyclingSample(
                    timestamp_ms=wall_ts_ms,
                    speed_mps=float(self._last_mph) * 0.44704,
                    cadence_rpm=self._last_cadence,
                    distance_m=dist_m,
                    power_watts=int(self._last_power),
                )
            else:
                sample = RunningSample(
                    timestamp_ms=wall_ts_ms,
                    speed_mps=float(self._last_mph) * 0.44704,
                    cadence_spm=self._last_cadence,
                    distance_m=dist_m,
                    power_watts=int(self._last_power),
                    stride_length_m=None,
                )
            rec.inject_test_sample(sample)

        return True

    # ---- connection dots
    def update_metric_statuses(self) -> None:
        rec = self.app.recorder or None
        if self.app.test_mode or not rec:
            hr_ok = speed_ok = cad_ok = pow_ok = dist_ok = True
        else:
            hr_ok = bool(rec.hr_connected)
            speed_ok = bool(rec.speed_connected)
            cad_ok = bool(rec.cadence_connected)
            pow_ok = bool(rec.power_connected)
            dist_ok = bool(rec.distance_connected)

        if self.free_view and hasattr(self.free_view, "set_statuses"):
            self.free_view.set_statuses(hr_ok, speed_ok, cad_ok, pow_ok, dist_ok)
        if self.workout_view:
            self.workout_view.set_statuses(
                hr_ok=hr_ok,
                cad_ok=cad_ok,
                spd_ok=speed_ok,
                pow_ok=pow_ok,
                dist_ok=dist_ok,
            )

    def _tick_status(self) -> bool:
        self.update_metric_statuses()
        return True

    # ---- resets & utils
    def _reset_buffers(self) -> None:
        self._times.clear()
        self._bpms.clear()
        self._powers.clear()
        self._last_ms = None
        self._bpm_max = 0
        # freeze integrated distance until running
        if hasattr(self, "_integrated_distance_miles"):
            self._integrated_distance_miles = getattr(self, "_integrated_distance_miles", 0.0)

    def _zone_info(self, hr: float):
        zones = self.app.calculate_hr_zones()
        order = list(zones.keys())
        for name, color in zip(order, self.app.ZONE_COLORS, strict=True):
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
    def _fmt_mmss(total_s: int) -> str:
        m, s = divmod(int(total_s), 60)
        return f"{m:02d}:{s:02d}"

    @staticmethod
    def _fmt_hhmmss(total_s: int) -> str:
        h, r = divmod(int(total_s), 3600)
        m, s = divmod(r, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"

    def redraw(self) -> None:
        if self.free_view and getattr(self.free_view, "fig", None):
            self.free_view.fig.canvas.draw_idle()

    def _on_incline_changed(self, percent: float) -> None:
        """Called by either FreeRunView or WorkoutView incline controls."""
        if self.app.recorder:
            self.app.recorder.set_incline(percent)
