from __future__ import annotations

import time
from collections import deque
from typing import TYPE_CHECKING

import gi
import numpy as np
from bleaksport import CyclingSample, HeartRateSample, RunningSample, TrainerSample
from workout_parser import (
    DistanceDuration,
    OpenDuration,
    PointTarget,
    RampTarget,
    RangeTarget,
    WorkoutStep,
)
from workout_parser.main import pretty_workout_name

from fitness_tracker.core.environment import Environment
from fitness_tracker.core.guidance import (
    format_step_duration,
    resolve_heart_rate_target,
    resolve_step_guidance,
    resolve_target_values,
)
from fitness_tracker.core.live_metrics import LiveMetrics
from fitness_tracker.core.sensor_status import SensorStatus
from fitness_tracker.core.session_capabilities import SessionCapabilities
from fitness_tracker.core.session_state import SessionState
from fitness_tracker.core.simulator import SensorSimulator, SimulatedReading, SimulationTarget
from fitness_tracker.core.sports import SportTypesEnum
from fitness_tracker.core.throttle import TrainerTargetThrottle
from fitness_tracker.core.trainer_mode import TrainerMode
from fitness_tracker.core.units import (
    DurationStyle,
    UnitSystem,
    format_duration,
    kph_from_mph,
    speed_in_units,
)
from fitness_tracker.core.workout_session import WorkoutSession
from fitness_tracker.services.pebble import apply_pebble_guidance
from fitness_tracker.services.trainer import apply_trainer_guidance
from fitness_tracker.ui.pages.mode import ModeSelectView
from fitness_tracker.ui.pages.session import SessionView
from fitness_tracker.workout_execution import (
    WorkoutDistanceAccumulator,
    WorkoutExecution,
    WorkoutExecutionSnapshot,
)
from fitness_tracker.workouts import format_step_remaining

gi.require_versions({"Gtk": "4.0", "Adw": "1"})
from gi.repository import Adw, GLib  # noqa: E402  # ty:ignore[unresolved-import]

if TYPE_CHECKING:
    from gi.repository import Gtk  # ty:ignore[unresolved-import]
    from workout_parser.models import Workout

    from fitness_tracker.hardware.recorder import Recorder
    from fitness_tracker.ui.app import FitnessAppUI

# Pebble bridge workout constants
TGT_NONE = 0


class TrackerPageUI:
    """Coordinate the active session page, sensors, guidance, and navigation."""

    def __init__(self, app: FitnessAppUI) -> None:
        self.app = app

        # buffers (ms + values)
        self.window_sec = 60
        self.window_ms = self.window_sec * 1000.0
        self._times = deque()
        self._bpms = deque()
        self._powers = deque()
        self._last_ms: int | None = None
        self._trainer_target_throttle = TrainerTargetThrottle()

        # lifecycle state
        self._session_state: SessionState | None = None
        self._start_requested = False

        # stats
        self._bpm_max = 0

        # test-mode tick
        self._test_source = None
        self._start_monotonic = 0.0
        self._test_simulator = self._new_test_simulator()

        # live sensor cache
        self._live_metrics = LiveMetrics()
        self._metrics_dirty = False

        # status updater
        self._status_timer_id: int | None = None

        # workout state
        self._workout_session: (
            WorkoutSession[
                Workout,
                WorkoutStep,
                WorkoutExecution,
                WorkoutExecutionSnapshot,
                WorkoutDistanceAccumulator,
            ]
            | None
        ) = None

        # UI timer state
        self._timer_source_id: int | None = None
        self._elapsed_display_s: int = 0

        # UI pages
        self.nav: Adw.NavigationView | None = None
        self.mode_view: ModeSelectView | None = None
        self.session_view: SessionView | None = None

    # ---- build
    def build_page(self) -> Adw.NavigationView:
        """Build and return the tracker navigation view."""
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
    @property
    def session_open(self) -> bool:
        """Return whether a session page currently owns the recorder profile."""
        return self.session_view is not None and self._session_state is not None

    def _start_workout(
        self,
        workout: Workout,
        sport_type: SportTypesEnum,
        environment: Environment,
    ) -> None:
        steps = workout.expanded_steps()
        if not steps:
            self.app.show_toast("Workout has no executable steps")
            return
        if any(isinstance(step.duration, OpenDuration) for step in steps):
            self.app.show_toast("Open-ended workout steps are not supported yet")
            return

        ftp_watts = self.app.app_settings.personal.ftp_watts
        resolved_steps = tuple(step.resolve_power_targets(ftp_watts) for step in steps)
        self._workout_session = WorkoutSession(
            workout=workout,
            steps=resolved_steps,
            execution=WorkoutExecution(resolved_steps),
            distance_accumulator=WorkoutDistanceAccumulator(),
        )
        self._show_workout_page(sport_type=sport_type, environment=environment)

    def _show_free_from_mode(
        self,
        sport_type: SportTypesEnum,
        environment: Environment,
    ) -> None:
        self._show_free_run_page(sport_type=sport_type, environment=environment)

    def _on_workout_start_pause_clicked(self) -> None:
        session = self._workout_session
        if session is None:
            return
        if self._session_state in (None, SessionState.PREVIEW):
            # First press: start the workout
            self._begin_run_now()
        elif self._session_state is SessionState.PAUSED:
            # Currently paused: resume
            if session.pause_started_monotonic is not None:
                paused_for = time.monotonic() - session.pause_started_monotonic
                session.manual_offset_s -= paused_for
            session.distance_accumulator.reset_raw_baseline()
            session.pause_started_monotonic = None
            self._session_state = SessionState.RUNNING
            # Reset throttling so the active target re-applies immediately.
            self._trainer_target_throttle.reset()
            if (
                session.trainer_control_mode is TrainerMode.SPEED
                and session.manual_speed_kmh is not None
                and self.app.recorder
            ):
                self.app.recorder.set_target_speed(session.manual_speed_kmh)
            if self.session_view:
                self.session_view.set_state(SessionState.RUNNING)
        else:
            # Currently running: pause
            session.pause_started_monotonic = time.monotonic()
            self._session_state = SessionState.PAUSED
            # Drop ERG target so user isn't fighting the trainer
            if self.app.recorder and self.app.recorder.trainer_connected:
                if self.session_view and self.session_view.sport_type == SportTypesEnum.running:
                    self.app.recorder.set_target_speed(0)
                else:
                    self.app.recorder.set_target_power(0)
            self._trainer_target_throttle.reset()
            if self.session_view:
                self.session_view.set_state(SessionState.PAUSED)

    def _tick_timer(self) -> bool:
        """
        1 Hz UI timer:
        - updates the free-run timer label
        - updates workout timers / guidance / completion
        Independent of sensor sample timing.
        """
        if self._session_state not in (SessionState.RUNNING, SessionState.PAUSED):
            # stop the timeout
            self._timer_source_id = None
            return False

        elapsed_s = max(0.0, time.monotonic() - self._start_monotonic)
        self._elapsed_display_s = int(elapsed_s)
        display_elapsed_s = self._elapsed_display_s

        if self.session_view and self._workout_session:
            # Elapsed always updates, even while paused
            self.session_view.set_elapsed_text(
                format_duration(
                    display_elapsed_s,
                    DurationStyle.CLOCK,
                    always_hours=False,
                ),
            )

            if self._session_state is not SessionState.PAUSED:
                snapshot = self._update_workout_execution(elapsed_s)
                if snapshot and not snapshot.completed:
                    self._update_workout_running_timers(snapshot)
                self._maybe_complete_workout(snapshot)
        elif self.session_view:
            self.session_view.set_timer(format_duration(display_elapsed_s, DurationStyle.CLOCK))
        self._render_live_metrics()

        return True

    # -------------------------
    #  Page show / run control
    # -------------------------
    def _session_capabilities(
        self,
        sport_type: SportTypesEnum,
        environment: Environment,
    ) -> SessionCapabilities:
        """Resolve optional session controls once for the selected view."""
        trainer = environment.uses_trainer
        return SessionCapabilities(
            incline=(sport_type == SportTypesEnum.running and environment is Environment.INDOOR),
            trainer_targets=trainer,
        )

    def _make_session_view(
        self,
        sport_type: SportTypesEnum,
        environment: Environment,
        *,
        title: str,
        workout: bool,
    ) -> SessionView:
        """Construct the shared session page with its optional workout panel."""
        capabilities = self._session_capabilities(
            sport_type,
            environment,
        )
        return SessionView(
            app=self.app,
            sport_type=sport_type,
            title=title,
            on_prev=lambda: self._skip_step(-1),
            on_next=lambda: self._skip_step(+1),
            on_stop=self._stop_run_and_back,
            on_start_record=(
                self._on_workout_start_pause_clicked if workout else self._begin_run_now
            ),
            on_incline=self._on_incline_changed,
            on_trainer_target=self._on_trainer_target_changed,
            capabilities=capabilities,
            workout=workout,
        )

    def _show_free_run_page(
        self,
        sport_type: SportTypesEnum = SportTypesEnum.running,
        environment: Environment = Environment.INDOOR,
    ) -> None:
        self._workout_session = None
        self._start_requested = False

        if self.app.pebble_bridge:
            self.app.pebble_bridge.update(tgt_kind=TGT_NONE)

        # Build page but DO NOT start timers/recording yet
        self._session_state = SessionState.PREVIEW
        self._reset_buffers()

        title = "Free Ride" if sport_type == SportTypesEnum.biking else "Free Run"
        view = self._make_session_view(
            sport_type,
            environment,
            title=title,
            workout=False,
        )
        self.session_view = view
        self._push(self.session_view, title)

        self.app.apply_sensor_settings(
            sport_type=sport_type,
            trainer=environment.uses_trainer,
            on_ready=lambda: self._on_sensor_profile_ready(view),
            allow_during_session=True,
        )

        # initial statuses & preview values
        self.update_metric_statuses()
        self._update_free_preview_timer_and_cards()

        # In preview we DO NOT call recorder.start_recording()

        # If test mode, we still feed preview samples, but we gate timers/progress
        if self.app.test_mode and self._test_source is None:
            self._test_simulator = self._new_test_simulator()
            self._test_source = GLib.timeout_add(1000, self._tick_test)

    def _show_workout_page(
        self,
        sport_type: SportTypesEnum = SportTypesEnum.running,
        environment: Environment = Environment.INDOOR,
    ) -> None:
        session = self._workout_session
        if session is None:
            return
        self._session_state = SessionState.PREVIEW
        self._start_requested = False
        self._reset_buffers()

        raw = session.workout.name if session.workout else "Workout"
        nice = pretty_workout_name(raw)
        session.trainer_control_mode = (
            TrainerMode.BIAS
            if sport_type == SportTypesEnum.running or environment.uses_trainer
            else TrainerMode.POWER
        )
        view = self._make_session_view(
            sport_type=sport_type,
            environment=environment,
            title=nice,
            workout=True,
        )
        self.session_view = view
        if self.app.pebble_bridge:
            self.app.pebble_bridge.update(
                workout_outdoor=environment is Environment.OUTDOOR,
            )
        session.pause_started_monotonic = None
        self._push(self.session_view, nice)

        self.app.apply_sensor_settings(
            sport_type=sport_type,
            trainer=environment.uses_trainer,
            on_ready=lambda: self._on_sensor_profile_ready(view),
            allow_during_session=True,
        )

        # Prime UI in preview (t=0)
        snapshot = self._update_workout_execution(elapsed_s=0)
        if snapshot and not snapshot.completed:
            self.session_view.set_progress(snapshot.progress)
        self.update_metric_statuses()
        self._update_workout_preview_timers()

        if self.app.test_mode and self._test_source is None:
            self._test_simulator = self._new_test_simulator()
            self._test_source = GLib.timeout_add(1000, self._tick_test)

    def _on_sensor_profile_ready(self, view: SessionView) -> None:
        """Release the session start gate when its exact recorder is installed."""
        if self.session_view is not view or self._session_state is None:
            return
        view.set_profile_ready(ready=True)
        if self._start_requested:
            self._start_requested = False
            self._begin_run_now()

    def _maybe_notify_distance_waiting(self) -> None:
        """Tell the user once when a distance workout starts without a source."""
        session = self._workout_session
        if (
            session is None
            or self.app.test_mode
            or session.distance_waiting_notified
            or not any(isinstance(step.duration, DistanceDuration) for step in session.steps)
        ):
            return

        recorder = self.app.recorder
        if recorder and recorder.distance_connected:
            return

        self.app.show_toast("Waiting for distance sensor")
        session.distance_waiting_notified = True

    def _defer_start_for_finalization(self, view: SessionView, recorder: Recorder) -> bool:
        """Hold Start until the recorder's previous activity is finalized."""
        if not (recorder.finalization_in_progress or recorder.finalization_pending):
            return False

        self._start_requested = True

        def on_finalization_complete(activity_id: int | None) -> None:
            if activity_id is None:
                self._start_requested = False
                view.set_profile_ready(ready=True)
                return
            self._on_sensor_profile_ready(view)

        wait_result = self.app.wait_for_finalization(
            recorder,
            on_finalization_complete,
            owner=view,
        )
        if not wait_result.waiting:
            view.set_profile_ready(ready=True)
            return False
        if recorder.finalization_pending and not recorder.finalization_in_progress:
            self.app.show_finalization_pending(recorder)
        else:
            self.app.show_toast("Waiting for activity finalization to complete")
        return True

    def _begin_run_now(self) -> None:
        """Called when Start is pressed."""
        if self._session_state in (SessionState.RUNNING, SessionState.PAUSED):
            return
        view = self.session_view
        recorder = self.app.recorder
        if view is None or not view.profile_ready or recorder is None:
            self._start_requested = True
            if view is not None:
                view.set_profile_ready(ready=False)
            return

        if self._defer_start_for_finalization(view, recorder):
            return

        self._start_requested = False
        try:
            recorder.start_recording()
        except Exception as error:
            self.app.show_toast(f"Unable to start recording: {error}")
            return

        if self._workout_session:
            self._workout_session.distance_accumulator.reset()
        self._session_state = SessionState.RUNNING
        self._start_monotonic = time.monotonic()
        self._elapsed_display_s = 0
        self._last_ms = 0
        self._trainer_target_throttle.reset()

        self._maybe_notify_distance_waiting()

        # start 1 Hz UI timer (decoupled from sensors)
        if self._timer_source_id is None:
            self._timer_source_id = GLib.timeout_add_seconds(1, self._tick_timer)

        if self.session_view:
            self.session_view.set_state(SessionState.RUNNING)

    def _stop_run_and_back(self) -> None:
        recorder = self.app.recorder
        trainer_session = (
            self.session_view is not None and self.session_view.capabilities.trainer_targets
        )
        neutralization_error: Exception | None = None
        try:
            if trainer_session and recorder:
                recorder.neutralize_trainer()
        except Exception as error:
            neutralization_error = error
        finally:
            # Always tear down UI session state, even if the trainer rejects zero load.
            self._session_state = None
            self._start_requested = False

            if self._timer_source_id is not None:
                GLib.source_remove(self._timer_source_id)
                self._timer_source_id = None

        # Always stop recording after attempting trainer neutralization.
        if recorder:
            self.app.schedule_finalization(recorder)

        if neutralization_error is not None:
            self.app.show_toast(f"Unable to neutralize trainer: {neutralization_error}")
        if self._test_source:
            GLib.source_remove(self._test_source)
            self._test_source = None

        # release page refs and go home
        self.session_view = None
        self._workout_session = None

        self._trainer_target_throttle.reset()

        self._pop_to_mode()

    # ---- nav helpers
    def _push(self, child: Gtk.Widget, title: str) -> None:
        page = Adw.NavigationPage.new(child, title)
        # Prevent back gesture on all pages to avoid accidental pops during activity.
        page.set_can_pop(False)
        if self.nav:
            self.nav.push(page)
        else:
            msg = "NavigationView not initialized"
            raise RuntimeError(msg)

    def _pop_to_mode(self) -> None:
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
        """Process one sensor sample and refresh the active session state."""
        session = self._workout_session
        distance_changed = False
        if isinstance(sample, HeartRateSample):
            if sample.heart_rate_bpm is None:
                return
            self._live_metrics.heart_rate_bpm = sample.heart_rate_bpm
            self._last_ms = sample.timestamp_ms
        else:
            self._last_ms = sample.timestamp_ms
            self._live_metrics.speed_mps = sample.speed_mps if sample.speed_mps is not None else 0.0
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
            self._live_metrics.cadence_spm = int(cadence)
            self._live_metrics.distance_m = float(sample.distance_m or 0.0)
            if session is not None:
                self._sync_workout_distance_source(
                    connected=self._distance_source_is_connected(),
                )
                previous_distance_m = session.distance_accumulator.distance_m
                session.distance_accumulator.observe(
                    sample.distance_m,
                    running=self._session_state in (SessionState.RUNNING, SessionState.PAUSED),
                    paused=self._session_state is SessionState.PAUSED,
                )
                distance_changed = session.distance_accumulator.distance_m != previous_distance_m
            self._live_metrics.power_watts = int(sample.power_watts or 0)

        self._metrics_dirty = True

        if self._session_state not in (SessionState.RUNNING, SessionState.PAUSED):
            if self.session_view and session is not None:
                self._update_workout_execution(elapsed_s=0, render_guidance=False)
            return

        self._append_chart_sample()
        if session is not None and self._session_state is SessionState.RUNNING and distance_changed:
            snapshot = self._update_workout_execution(
                elapsed_s=self._elapsed_display_s,
                render_guidance=False,
            )
            self._maybe_complete_workout(snapshot)

    # ---- core update
    def _append_chart_sample(self) -> None:
        metrics = self._live_metrics
        t_ms = self._last_ms if self._last_ms is not None else int(time.monotonic() * 1000)

        cutoff = t_ms - self.window_ms
        self._times.append(t_ms)
        self._bpms.append(metrics.heart_rate_bpm)
        self._powers.append(metrics.power_watts)
        while self._times and self._times[0] < cutoff:
            self._times.popleft()
            self._bpms.popleft()
            self._powers.popleft()

        self._bpm_max = max(self._bpm_max, metrics.heart_rate_bpm)

    def _render_live_metrics(self) -> None:
        if not self._metrics_dirty:
            return

        metrics = self._live_metrics
        bpm = metrics.heart_rate_bpm
        t_ms = self._last_ms if self._last_ms is not None else int(time.monotonic() * 1000)
        cutoff = t_ms - self.window_ms

        # Build arrays for free-view chart (if visible)
        x = (np.array(self._times) - cutoff) / 1000.0 if self._times else np.array([])
        hr = np.array(self._bpms) if self._bpms else np.array([])
        pw = np.array(self._powers) if self._powers else np.array([])
        _, _, _, rgb = self._zone_info(self._bpms[-1]) if self._bpms else ("", 0, 0, (1, 1, 1))

        if self.session_view:
            self.session_view.update_chart(x, hr, pw, hr_rgb=rgb)

        # cards/timer
        speed_mps = metrics.speed_mps
        cadence = metrics.cadence_spm
        dist_m = metrics.distance_m
        watts_val = round(metrics.power_watts)

        self._set_cards(
            distance_m=dist_m,
            speed_mps=speed_mps,
            cadence=cadence,
            bpm=bpm,
            watts=watts_val,
        )

        # pebble bridge
        if self.app.pebble_bridge:
            self.app.pebble_bridge.update(
                hr=int(bpm),
                speed_mps=speed_mps,
                cadence=int(cadence),
                dist_m=dist_m,
                power_w=int(watts_val),
                units=int(self.app.unit_system == UnitSystem.IMPERIAL),
            )

        self._metrics_dirty = False

    # ---- workout guidance
    def _biased_target_values(
        self,
        target: PointTarget | RangeTarget | RampTarget | None,
        progress: float = 0.0,
        *,
        decimal_places: int,
    ) -> tuple[float, float, float] | None:
        session = self._workout_session
        return resolve_target_values(
            target,
            progress,
            bias_pct=session.bias_percent if session else 0,
            decimal_places=decimal_places,
        )

    def _hr_target_values(
        self,
        step: WorkoutStep,
        progress: float = 0.0,
    ) -> tuple[float, float, float] | None:
        """Resolve a step's preferred HR target to an absolute BPM band."""
        session = self._workout_session
        return resolve_heart_rate_target(
            step,
            progress,
            bias_pct=session.bias_percent if session else 0,
            personal=self.app.app_settings.personal,
            zones=self.app.calculate_hr_zones(),
        )

    def _update_workout_execution(
        self,
        elapsed_s: float,
        *,
        render_guidance: bool = True,
    ) -> WorkoutExecutionSnapshot | None:
        """Advance the workout executor and apply any active-step guidance."""
        session = self._workout_session
        if session is None:
            return None

        workout_elapsed_s = self._current_workout_elapsed_s(elapsed_s)
        snapshot = session.execution.update(
            workout_elapsed_s,
            session.distance_accumulator.distance_m,
        )
        if snapshot.step_changed and not snapshot.completed and not render_guidance:
            session.defer_step_change()
        if not snapshot.completed and render_guidance:
            self._update_workout_guidance(snapshot)
        session.snapshot = snapshot.model_copy(update={"step_changed": False})
        return snapshot

    def _current_workout_elapsed_s(self, elapsed_s: float | None = None) -> float:
        """Return elapsed workout time with pauses excluded."""
        session = self._workout_session
        if session is None:
            return 0.0
        current_elapsed_s = float(self._elapsed_display_s if elapsed_s is None else elapsed_s)
        if (
            self._session_state is SessionState.PAUSED
            and session.pause_started_monotonic is not None
        ):
            current_elapsed_s -= time.monotonic() - session.pause_started_monotonic
        return max(0.0, current_elapsed_s + session.manual_offset_s)

    def _update_workout_guidance(
        self,
        snapshot: WorkoutExecutionSnapshot | None = None,
    ) -> None:
        """Resolve and distribute target guidance for one workout snapshot."""
        session = self._workout_session
        session_view = self.session_view
        if session_view is None or session is None:
            return
        metrics = self._live_metrics

        if snapshot is None:
            snapshot = session.snapshot
        if snapshot is None:
            snapshot = session.execution.snapshot()
        if snapshot is None or snapshot.completed:
            return

        idx = snapshot.active_index
        step = snapshot.step
        if idx is None or step is None:
            return

        next_step = session.steps[idx + 1] if idx + 1 < len(session.steps) else None
        guidance = resolve_step_guidance(
            step,
            next_step,
            progress=snapshot.progress,
            bias_pct=session.bias_percent,
            personal=self.app.app_settings.personal,
            zones=self.app.calculate_hr_zones(),
            unit_system=self.app.unit_system,
        )

        pending_step_change = session.consume_pending_step_change()
        if snapshot.step_changed or pending_step_change:
            self.app.show_workout_step_notification(
                idx + 1,
                len(session.steps),
                guidance.target_text,
                announce=self._session_state in (SessionState.RUNNING, SessionState.PAUSED),
            )

        session_view.apply_guidance(
            guidance,
            metrics,
            progress=(
                snapshot.progress
                if self._session_state in (SessionState.RUNNING, SessionState.PAUSED)
                else None
            ),
        )
        apply_pebble_guidance(guidance, bridge=self.app.pebble_bridge)
        apply_trainer_guidance(
            guidance,
            target_sink=self.app.recorder,
            trainer_mode=session.trainer_control_mode,
            trainer_session=session_view.capabilities.trainer_targets,
            sport_type=session_view.sport_type,
            throttle=self._trainer_target_throttle,
        )

    def _maybe_complete_workout(self, snapshot: WorkoutExecutionSnapshot | None) -> None:
        """Reveal the continuous free-run panel after the workout completes."""
        session = self._workout_session
        session_view = self.session_view
        if not (
            self._session_state is SessionState.RUNNING
            and session is not None
            and session_view is not None
        ):
            return
        if snapshot is None or not snapshot.completed:
            return

        if session.execution.completed:
            self._workout_session = None
            title = "Free Ride" if session_view.sport_type == SportTypesEnum.biking else "Free Run"
            session_view.set_title(title)
            session_view.set_workout_visible(visible=False)
            session_view.set_state(SessionState.RUNNING)
            session_view.set_timer(
                format_duration(self._elapsed_display_s, DurationStyle.CLOCK),
            )
            self.update_metric_statuses()
            self.app.show_workout_complete_notification()

            if self.app.pebble_bridge:
                self.app.pebble_bridge.update(tgt_kind=TGT_NONE)

    def _skip_step(self, direction: int) -> None:
        session = self._workout_session
        if session is None:
            return

        current_elapsed_s = self._current_workout_elapsed_s(
            self._elapsed_display_s
            if self._session_state in (SessionState.RUNNING, SessionState.PAUSED)
            else 0.0,
        )
        if direction > 0:
            snapshot = session.execution.next_step(
                elapsed_s=current_elapsed_s,
                distance_m=session.distance_accumulator.distance_m,
            )
        else:
            snapshot = session.execution.previous_step(
                elapsed_s=current_elapsed_s,
                distance_m=session.distance_accumulator.distance_m,
            )

        # Reset target throttling so the destination step applies immediately.
        self._trainer_target_throttle.reset()

        self._update_workout_guidance(snapshot)
        session.snapshot = snapshot.model_copy(update={"step_changed": False})
        if self.session_view and not snapshot.completed:
            if self._session_state not in (SessionState.RUNNING, SessionState.PAUSED):
                self.session_view.set_progress(snapshot.progress)
            self.session_view.set_step_remaining_text(format_step_remaining(snapshot))

    # ---- helpers
    def _set_cards(
        self,
        *,
        distance_m: float,
        speed_mps: float,
        cadence: int,
        bpm: int,
        watts: int,
    ) -> None:
        if self.session_view:
            self.session_view.set_unit_system(self.app.unit_system)
            self.session_view.set_metrics(
                bpm=bpm,
                pace_mps=speed_mps,
                cadence_spm=cadence,
                speed_mps=speed_mps,
                distance_m=distance_m,
                power_watts=watts,
            )

    def _update_free_preview_timer_and_cards(self) -> None:
        if self.session_view and self._workout_session is None:
            self.session_view.set_timer(format_duration(0, DurationStyle.COUNTDOWN))

    def _update_workout_preview_timers(self) -> None:
        if not self.session_view or self._workout_session is None:
            return
        self.session_view.set_elapsed_text(format_duration(0, DurationStyle.COUNTDOWN))

        session = self._workout_session
        snapshot = session.snapshot if session else None
        if snapshot and not snapshot.completed:
            self.session_view.set_step_remaining_text(format_step_remaining(snapshot))
        elif session and session.steps:
            self.session_view.set_step_remaining_text(format_step_duration(session.steps[0]))
        else:
            self.session_view.set_step_remaining_text("—")

    def _update_workout_running_timers(self, snapshot: WorkoutExecutionSnapshot) -> None:
        if not (self.session_view and self._workout_session):
            return

        self.session_view.set_step_remaining_text(format_step_remaining(snapshot))

    def _new_test_simulator(self) -> SensorSimulator:
        """Build a simulator from the current personal settings and HR zones."""
        personal = self.app.app_settings.personal
        low_hr, _ = self.app.calculate_hr_zones()["Zone 3"]
        return SensorSimulator(
            resting_hr=float(personal.resting_hr or 60),
            max_hr=float(personal.max_hr or 190),
            low_hr=float(low_hr),
        )

    # ---- test-mode generator
    def _tick_test(self) -> bool:
        if self._session_state is None:
            return False

        t_now = time.monotonic()
        # In preview, hold elapsed time at 0 so nothing progresses.
        elapsed_s = (
            max(0.0, t_now - self._start_monotonic)
            if self._session_state in (SessionState.RUNNING, SessionState.PAUSED)
            else 0.0
        )
        t_ms = int(elapsed_s * 1000)
        self._last_ms = t_ms

        # ---- power / speed target resolution ----
        target: SimulationTarget | None = None
        step: WorkoutStep | None = None
        step_progress = 0.0

        if self._workout_session:
            snapshot = self._update_workout_execution(elapsed_s=elapsed_s)
            if snapshot and snapshot.completed:
                self._maybe_complete_workout(snapshot)
            if snapshot and not snapshot.completed:
                step = snapshot.step
                step_progress = snapshot.progress

        if step is not None:
            power = self._biased_target_values(step.power_watts, step_progress, decimal_places=0)
            speed = self._biased_target_values(step.speed_mps, step_progress, decimal_places=1)
            heart_rate = self._hr_target_values(step, step_progress)
            target_power: float | None = None
            target_speed: float | None = None
            target_hr: float | None = None
            if power:
                target_power = power[1]
            elif speed:
                target_speed = speed[1]
                target_power = max(
                    80.0,
                    18.0 * speed_in_units(speed[1], UnitSystem.IMPERIAL),
                )
            if heart_rate:
                target_hr = heart_rate[1]
            target = SimulationTarget(
                power_watts=target_power,
                speed_mps=target_speed,
                heart_rate_bpm=target_hr,
            )

        reading = self._test_simulator.tick(
            1.0,
            target,
            active=self._session_state in (SessionState.RUNNING, SessionState.PAUSED),
        )

        # ---- inject through recorder pipeline ----
        if self.app.recorder:
            # Wall-clock timestamp that the recorder handlers expect (seconds since epoch)
            wall_ts = time.time()
            wall_ts_ms = int(wall_ts * 1000)
            self._inject_test_reading(reading, wall_ts_ms)

        return True

    def _inject_test_reading(self, reading: SimulatedReading, timestamp_ms: int) -> None:
        """Adapt a pure simulated reading into the recorder's upstream sample models."""
        recorder = self.app.recorder
        if recorder is None:
            return

        recorder.inject_test_sample(
            HeartRateSample(
                timestamp_ms=timestamp_ms,
                heart_rate_bpm=reading.heart_rate_bpm,
                rr_interval_ms=None,
            ),
        )

        if recorder.trainer_configured:
            sample = TrainerSample(
                timestamp_ms=timestamp_ms,
                speed_kmh=speed_in_units(reading.speed_mps, UnitSystem.METRIC),
                cadence_rpm=reading.cadence_spm,
                distance_m=reading.distance_m,
                power_watts=reading.power_watts,
                target_power=None,
            )
        elif self.session_view and self.session_view.sport_type == SportTypesEnum.biking:
            sample = CyclingSample(
                timestamp_ms=timestamp_ms,
                speed_mps=reading.speed_mps,
                cadence_rpm=reading.cadence_spm,
                distance_m=reading.distance_m,
                power_watts=reading.power_watts,
            )
        else:
            sample = RunningSample(
                timestamp_ms=timestamp_ms,
                speed_mps=reading.speed_mps,
                cadence_spm=reading.cadence_spm,
                distance_m=reading.distance_m,
                power_watts=reading.power_watts,
                stride_length_m=None,
            )
        recorder.inject_test_sample(sample)

    # ---- connection dots
    def _distance_source_is_connected(self) -> bool:
        recorder = self.app.recorder
        if self.app.test_mode or not recorder:
            return True
        return bool(recorder.distance_connected)

    def _sync_workout_distance_source(self, *, connected: bool) -> None:
        session = self._workout_session
        if session and (not connected or session.distance_source_connected is False):
            session.distance_accumulator.reset_raw_baseline()
        if session:
            session.distance_source_connected = connected

    def update_metric_statuses(self) -> None:
        """Refresh sensor connection indicators and distance-source state."""
        rec = self.app.recorder or None
        if self.app.test_mode:
            hr_ok = speed_ok = cad_ok = pow_ok = dist_ok = True
        elif not rec:
            hr_ok = speed_ok = cad_ok = pow_ok = dist_ok = False
        else:
            hr_ok = bool(rec.hr_connected)
            speed_ok = bool(rec.speed_connected)
            cad_ok = bool(rec.cadence_connected)
            pow_ok = bool(rec.power_connected)
            dist_ok = bool(rec.distance_connected)

        self._sync_workout_distance_source(connected=dist_ok)
        status = SensorStatus(
            hr=hr_ok,
            speed=speed_ok,
            cadence=cad_ok,
            power=pow_ok,
            distance=dist_ok,
        )
        if self.session_view:
            self.session_view.set_statuses(status)

    def _tick_status(self) -> bool:
        self.update_metric_statuses()
        if self._session_state not in (SessionState.RUNNING, SessionState.PAUSED):
            session = self._workout_session
            if (
                self.session_view
                and session
                and session.snapshot
                and not session.snapshot.completed
            ):
                self._update_workout_guidance(session.snapshot)
            self._render_live_metrics()
            self._update_free_preview_timer_and_cards()
            self._update_workout_preview_timers()
        return True

    # ---- resets & utils
    def _reset_buffers(self) -> None:
        self._times.clear()
        self._bpms.clear()
        self._powers.clear()
        self._last_ms = None
        self._bpm_max = 0
        if self._workout_session:
            self._workout_session.distance_accumulator.reset()

    def _zone_info(
        self,
        hr: float,
    ) -> tuple[str, float, float, tuple[float, float, float]]:
        name, low, high, color_index = self.app.hr_zones.for_heart_rate(hr)
        return name, low, high, self.app.chart_theme.zone_rgb[color_index]

    def redraw(self) -> None:
        """Redraw the active session chart, if one is open."""
        if self.session_view:
            self.session_view.redraw()

    def refresh_units(self) -> None:
        """Refresh active metric values and labels after a unit preference change."""
        if self.session_view:
            metrics = self._live_metrics
            self._set_cards(
                distance_m=metrics.distance_m,
                speed_mps=metrics.speed_mps,
                cadence=metrics.cadence_spm,
                bpm=metrics.heart_rate_bpm,
                watts=round(metrics.power_watts),
            )
            session = self._workout_session
            if self.session_view and session and session.snapshot:
                self._update_workout_guidance(
                    session.snapshot.model_copy(update={"step_changed": False}),
                )

    def refresh_theme(self) -> None:
        """Restyle the active live chart after a desktop theme change."""
        if self.session_view:
            self.session_view.refresh_theme()

    def _on_incline_changed(self, percent: float) -> None:
        """Called by the shared session incline control."""
        if self.app.recorder:
            self.app.recorder.set_incline(percent)

    def _on_trainer_target_changed(self, mode: TrainerMode, value: float) -> None:
        session = self._workout_session
        if session:
            session.trainer_control_mode = mode
        if mode is TrainerMode.BIAS:
            if session:
                session.bias_percent = int(value)
            self._trainer_target_throttle.reset()
            self._update_workout_guidance()
            return
        if not self.app.recorder:
            return
        if mode is TrainerMode.POWER:
            self.app.recorder.set_target_power(int(value))
        elif mode is TrainerMode.RESISTANCE:
            self.app.recorder.set_target_resistance(value)
        elif mode is TrainerMode.SPEED:
            speed_kmh = round(
                value if self.app.unit_system == UnitSystem.METRIC else kph_from_mph(value),
                3,
            )
            if session:
                session.manual_speed_kmh = speed_kmh
            self.app.recorder.set_target_speed(speed_kmh)
