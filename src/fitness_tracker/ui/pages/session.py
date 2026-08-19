"""Unified free-run and workout session view."""

from __future__ import annotations

from functools import cache
from time import monotonic
from typing import TYPE_CHECKING

import gi

from fitness_tracker.core.guidance import TargetDomain
from fitness_tracker.core.sensor_status import SensorKind
from fitness_tracker.core.session_state import SessionState
from fitness_tracker.core.trainer_mode import TrainerMode, trainer_modes_for_session
from fitness_tracker.core.units import (
    UnitSystem,
    display_cadence,
    distance_in_units,
    format_pace,
    speed_in_units,
    unit_label,
)
from fitness_tracker.ui.widgets.chart import LiveChart
from fitness_tracker.ui.widgets.metric_tile import MetricTile
from fitness_tracker.ui.widgets.session_controls import InclineControl, TargetGauge
from fitness_tracker.ui.widgets.timers import SessionTimer
from fitness_tracker.ui.widgets.trainer_control import TrainerTargetControl

gi.require_versions({"Gtk": "4.0", "Adw": "1"})
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402  # ty:ignore[unresolved-import]

if TYPE_CHECKING:
    from collections.abc import Callable

    import numpy as np

    from fitness_tracker.core.guidance import StepGuidance
    from fitness_tracker.core.live_metrics import LiveMetrics
    from fitness_tracker.core.sensor_status import SensorStatus
    from fitness_tracker.core.session_capabilities import SessionCapabilities
    from fitness_tracker.core.sports import SportTypesEnum
    from fitness_tracker.ui.app import FitnessAppUI


_MIN_CHART_POINTS = 2
_STOP_HOLD_SECONDS = 1.5
_STOP_HOLD_TICK_MS = 32
_SESSION_CSS = b"""
.stop-hold-progress trough {
    min-height: 32px;
    background: transparent;
    border: none;
}
.stop-hold-progress progress {
    min-height: 32px;
    background-color: rgba(255, 255, 255, 0.28);
    border-radius: 6px;
}
"""


@cache
def _ensure_session_css() -> None:
    provider = Gtk.CssProvider()
    provider.load_from_data(_SESSION_CSS)
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(),
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )


class SessionView(Gtk.Box):
    """Render free-run metrics and an optional workout guidance panel."""

    def __init__(
        self,
        app: FitnessAppUI,
        *,
        sport_type: SportTypesEnum,
        title: str,
        on_prev: Callable[[], None],
        on_next: Callable[[], None],
        on_stop: Callable[[], None],
        on_start_record: Callable[[], None],
        on_incline: Callable[[float], None],
        on_trainer_target: Callable[[TrainerMode, int | float], None],
        capabilities: SessionCapabilities,
        workout: bool,
        location_enabled: bool,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.app = app
        self.sport_type = sport_type
        self.capabilities = capabilities
        self._workout_visible = False
        self._state = SessionState.PREVIEW
        self._profile_ready = False

        self.set_margin_top(6)
        self.set_margin_bottom(6)
        self.set_margin_start(4)
        self.set_margin_end(4)

        self._build_workout_panel(title, on_prev, on_next)
        self._build_metric_strip(location_enabled=location_enabled)
        self._build_optional_controls(on_incline, on_trainer_target)
        self._build_recording_controls(on_stop, on_start_record)
        self._build_live_chart()

        self.set_unit_system(self.app.unit_system)
        self.set_workout_visible(visible=workout)
        self.set_state(SessionState.PREVIEW)
        self._update_compliance_pill()

    def _build_workout_navigation(
        self,
        on_prev: Callable[[], None],
        on_next: Callable[[], None],
    ) -> None:
        """Build workout step navigation buttons."""
        workout_navigation = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        workout_navigation.set_homogeneous(True)
        self.btn_prev = Gtk.Button.new_with_label("◀︎ Prev")
        self.btn_prev.add_css_class("pill")
        self.btn_prev.set_size_request(90, -1)
        self.btn_next = Gtk.Button.new_with_label("Next ▶︎")
        self.btn_next.add_css_class("pill")
        self.btn_next.set_size_request(90, -1)
        self.btn_prev.connect("clicked", lambda *_: on_prev())
        self.btn_next.connect("clicked", lambda *_: on_next())
        workout_navigation.append(self.btn_prev)
        workout_navigation.append(self.btn_next)
        workout_navigation.set_hexpand(True)
        self._workout_navigation = workout_navigation

    def _build_workout_panel(
        self,
        title: str,
        on_prev: Callable[[], None],
        on_next: Callable[[], None],
    ) -> None:
        """Build the timer and workout-guidance widgets."""
        self.timer = SessionTimer(None)
        self.append(self.timer)

        self._workout_revealer = Gtk.Revealer()
        self._workout_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.workout_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._workout_revealer.set_child(self.workout_panel)
        self.append(self._workout_revealer)

        self.title_label = Gtk.Label(xalign=0.0)
        self.title_label.add_css_class("title-2")
        self.title_label.set_single_line_mode(True)
        self.title_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.title_label.set_max_width_chars(36)
        self.title_label.set_hexpand(True)
        self.title_label.set_halign(Gtk.Align.FILL)
        self.title_label.set_text(title)
        self.workout_panel.append(self.title_label)

        timers = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.timer_elapsed = SessionTimer("Elapsed")
        self.timer_remaining = SessionTimer("Remaining")
        timers.append(self.timer_elapsed)
        timers.append(self.timer_remaining)
        self.workout_panel.append(timers)

        self.gauge = TargetGauge()
        self.gauge.set_margin_top(4)
        self.workout_panel.append(self.gauge)

        pill_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        pill_bar.set_halign(Gtk.Align.CENTER)
        self.compliance = Gtk.Label()
        self.compliance.add_css_class("pill")
        self.compliance.set_name("compliance-pill")
        pill_bar.append(self.compliance)
        self.workout_panel.append(pill_bar)

        self.lbl_target = Gtk.Label(xalign=0.0, wrap=True)
        self.lbl_target.add_css_class("heading")
        self.lbl_target.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.workout_panel.append(self.lbl_target)

        self.lbl_next = Gtk.Label(xalign=0.0, wrap=True)
        self.lbl_next.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.workout_panel.append(self.lbl_next)

        self.step_progress = Gtk.ProgressBar()
        self.step_progress.set_hexpand(True)
        self.step_progress.set_margin_top(2)
        self.step_progress.set_margin_bottom(1)
        self.step_progress.set_css_classes(["osd"])
        self.workout_panel.append(self.step_progress)
        self._build_workout_navigation(on_prev, on_next)

    def _build_metric_strip(self, *, location_enabled: bool) -> None:
        """Build the shared live metric tiles."""
        metrics = Gtk.FlowBox()
        metrics.set_selection_mode(Gtk.SelectionMode.NONE)
        metrics.set_valign(Gtk.Align.FILL)
        metrics.set_halign(Gtk.Align.FILL)
        metrics.set_homogeneous(True)
        metrics.set_column_spacing(12)
        metrics.set_row_spacing(2)

        self.card_hr = MetricTile("Heart Rate", "bpm", sensor=SensorKind.HEART_RATE)
        # Pace uses speed as an approximate source hint, not an authoritative target indicator.
        self.card_pace = MetricTile("Pace", sensor=SensorKind.SPEED)
        self.card_power = MetricTile("Power", "W", sensor=SensorKind.POWER)
        self.card_cadence = MetricTile("Cadence", "spm", sensor=SensorKind.CADENCE)
        self.card_speed = MetricTile("Speed", sensor=SensorKind.SPEED)
        self.card_distance = MetricTile("Distance", sensor=SensorKind.DISTANCE)
        self._status_tiles = (
            self.card_hr,
            self.card_pace,
            self.card_cadence,
            self.card_speed,
            self.card_distance,
            self.card_power,
        )
        for tile in self._status_tiles:
            metrics.insert(tile, -1)
        self.append(metrics)

        self.location_status = Gtk.Label(label="Location off", xalign=0.0)
        self.location_status.add_css_class("dim-label")
        self.location_status.set_wrap(True)
        self.location_status.set_margin_start(4)
        self.location_status.set_margin_end(4)
        self.location_status.set_visible(location_enabled)
        self.append(self.location_status)

    def _build_optional_controls(
        self,
        on_incline: Callable[[float], None],
        on_trainer_target: Callable[[TrainerMode, int | float], None],
    ) -> None:
        """Build capability-dependent incline and trainer controls."""
        self.incline_control: InclineControl | None = None
        if self.capabilities.incline:
            initial_incline = (
                self.app.recorder.incline_percent
                if self.app.recorder and self.app.recorder.incline_percent is not None
                else 0.0
            )
            self.incline_control = InclineControl(
                on_change=on_incline,
                initial_value=initial_incline,
            )
            self.incline_control.set_hexpand(True)
            self.append(self.incline_control)

        self.trainer_target_control: TrainerTargetControl | None = None
        if self.capabilities.trainer_targets:
            self.trainer_target_control = TrainerTargetControl(
                on_trainer_target,
                available_modes=trainer_modes_for_session(self.sport_type, include_bias=True),
                unit_system=self.app.unit_system,
            )
            self.append(self.trainer_target_control)

    def _build_recording_controls(
        self,
        on_stop: Callable[[], None],
        on_start_record: Callable[[], None],
    ) -> None:
        """Build the paired workout navigation and recording controls."""
        self._on_stop = on_stop
        self._stop_hold_started_at: float | None = None
        self._stop_hold_source_id: int | None = None
        _ensure_session_css()

        action_pair = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        action_pair.set_homogeneous(True)
        action_pair.set_hexpand(True)
        self.btn_stop = Gtk.Button()
        self.btn_stop.add_css_class("destructive-action")
        self.btn_stop.set_tooltip_text(
            f"Press and hold for {_STOP_HOLD_SECONDS} seconds to stop the activity",
        )
        stop_overlay = Gtk.Overlay()
        self._stop_progress = Gtk.ProgressBar()
        self._stop_progress.add_css_class("stop-hold-progress")
        self._stop_progress.set_fraction(0.0)
        self._stop_progress.set_hexpand(True)
        stop_overlay.set_child(self._stop_progress)
        stop_overlay.add_overlay(Gtk.Label(label="Stop"))
        self.btn_stop.set_child(stop_overlay)
        self.btn_start = Gtk.Button.new_with_label("▶️  Start")
        self.btn_start.add_css_class("suggested-action")

        stop_gesture = Gtk.GestureClick.new()
        stop_gesture.set_button(1)
        stop_gesture.connect("pressed", lambda *_: self._begin_stop_hold())
        stop_gesture.connect("released", lambda *_: self._end_stop_hold())
        stop_gesture.connect("cancel", lambda *_: self._cancel_stop_hold())
        self.btn_stop.add_controller(stop_gesture)

        stop_keys = Gtk.EventControllerKey.new()
        stop_keys.connect("key-pressed", self._on_stop_key_pressed)
        stop_keys.connect("key-released", self._on_stop_key_released)
        self.btn_stop.add_controller(stop_keys)

        stop_motion = Gtk.EventControllerMotion.new()
        stop_motion.connect("leave", lambda *_: self._cancel_stop_hold())
        self.btn_stop.add_controller(stop_motion)

        stop_focus = Gtk.EventControllerFocus.new()
        stop_focus.connect("leave", self._on_stop_focus_leave)
        self.btn_stop.add_controller(stop_focus)

        self.btn_start.connect("clicked", lambda *_: on_start_record())
        action_pair.append(self.btn_stop)
        action_pair.append(self.btn_start)

        controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        controls.set_hexpand(True)
        controls.append(self._workout_navigation)
        controls.append(action_pair)
        self.append(controls)

    def _begin_stop_hold(self) -> None:
        """Start the stop countdown without allowing repeated presses to restart it."""
        if self._stop_hold_started_at is not None:
            return
        self._stop_hold_started_at = monotonic()
        self._stop_progress.set_fraction(0.0)
        self._stop_hold_source_id = GLib.timeout_add(
            _STOP_HOLD_TICK_MS,
            self._update_stop_hold,
        )

    def _update_stop_hold(self) -> bool:
        """Update the countdown and stop once the full hold duration elapses."""
        started_at = self._stop_hold_started_at
        if started_at is None:
            return GLib.SOURCE_REMOVE
        elapsed = monotonic() - started_at
        self._stop_progress.set_fraction(
            max(0.0, min(1.0, elapsed / _STOP_HOLD_SECONDS)),
        )
        if elapsed < _STOP_HOLD_SECONDS:
            return GLib.SOURCE_CONTINUE

        self._stop_hold_source_id = None
        self._complete_stop_hold()
        return GLib.SOURCE_REMOVE

    def _end_stop_hold(self) -> None:
        """Complete a sufficiently long hold or cancel an early release."""
        started_at = self._stop_hold_started_at
        if started_at is None:
            return
        if monotonic() - started_at >= _STOP_HOLD_SECONDS:
            self._complete_stop_hold()
        else:
            self._cancel_stop_hold()

    def _complete_stop_hold(self) -> None:
        """Run the destructive stop action exactly once after confirmation."""
        source_id = self._stop_hold_source_id
        self._stop_hold_source_id = None
        self._stop_hold_started_at = None
        if source_id is not None:
            GLib.source_remove(source_id)
        self._stop_progress.set_fraction(1.0)
        self.btn_stop.set_sensitive(False)
        self._on_stop()

    def _cancel_stop_hold(self) -> None:
        """Reset an incomplete stop hold without ending the activity."""
        source_id = self._stop_hold_source_id
        self._stop_hold_source_id = None
        self._stop_hold_started_at = None
        if source_id is not None:
            GLib.source_remove(source_id)
        self._stop_progress.set_fraction(0.0)

    def _on_stop_focus_leave(self, _controller: Gtk.EventControllerFocus) -> None:
        """Cancel a keyboard-initiated hold when the button loses focus."""
        self._cancel_stop_hold()

    @staticmethod
    def _is_stop_hold_key(keyval: int) -> bool:
        return keyval in (Gdk.KEY_space, Gdk.KEY_Return, Gdk.KEY_KP_Enter)

    def _on_stop_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        _state: Gdk.ModifierType,
    ) -> bool:
        if not self._is_stop_hold_key(keyval):
            return False
        self._begin_stop_hold()
        return True

    def _on_stop_key_released(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        _state: Gdk.ModifierType,
    ) -> None:
        if self._is_stop_hold_key(keyval):
            self._end_stop_hold()

    def _build_live_chart(self) -> None:
        """Build the live heart-rate and power chart."""
        self.live_chart = LiveChart(
            theme=self.app.chart_theme,
            zones=self.app.calculate_hr_zones(),
            resting_hr=self.app.app_settings.personal.resting_hr,
            max_hr=self.app.app_settings.personal.max_hr,
        )
        self._live_chart_frame = Gtk.Frame(label="Live HR / Power")
        self._live_chart_frame.set_child(self.live_chart.canvas)
        self.append(self._live_chart_frame)

    def set_workout_visible(self, *, visible: bool) -> None:
        """Reveal or hide workout guidance without replacing the session page."""
        self._workout_visible = visible
        self._cancel_stop_hold()
        self._workout_revealer.set_reveal_child(visible)
        self.timer.set_visible(not visible)
        self._live_chart_frame.set_visible(not visible)
        self._workout_navigation.set_visible(visible)
        self.title_label.set_visible(visible)
        if self.trainer_target_control:
            self.trainer_target_control.set_mode_available(
                TrainerMode.BIAS,
                available=visible,
            )
        if visible:
            self.btn_start.set_label("Start")
        else:
            self.btn_start.set_label("▶️  Start")

    def set_title(self, title: str) -> None:
        """Update the workout title shown in the guidance panel."""
        self.title_label.set_text(title or "Workout")

    @property
    def profile_ready(self) -> bool:
        """Return whether the recorder for this session is installed."""
        return self._profile_ready

    def set_profile_ready(self, *, ready: bool) -> None:
        """Enable Start only after the requested recorder profile is installed."""
        self._profile_ready = ready
        self.set_state(self._state)

    def set_timer(self, text: str) -> None:
        """Update the free-run clock."""
        self.timer.set_text(text)

    def set_elapsed_text(self, text: str) -> None:
        """Update the workout elapsed timer."""
        self.timer_elapsed.set_text(text)

    def set_step_remaining_text(self, text: str) -> None:
        """Update the workout remaining timer."""
        self.timer_remaining.set_text(text)

    def set_unit_system(self, unit_system: UnitSystem) -> None:
        """Update all distance, pace, speed, and trainer labels."""
        self.card_distance.set_unit(unit_label("distance", unit_system))
        self.card_pace.set_unit(f"min/{unit_label('pace', unit_system)}")
        self.card_speed.set_unit(unit_label("speed", unit_system))
        if self.trainer_target_control:
            self.trainer_target_control.set_unit_system(unit_system)

    def set_metrics(
        self,
        *,
        bpm: int | None = None,
        pace_mps: float | None = None,
        cadence_spm: int | None = None,
        speed_mps: float | None = None,
        distance_m: float | None = None,
        power_watts: float | None = None,
    ) -> None:
        """Update the shared metric strip."""
        unit_system = self.app.unit_system
        if bpm is not None:
            self.card_hr.set_value(str(int(bpm)))
        if pace_mps is not None:
            self.card_pace.set_value(format_pace(pace_mps, unit_system))
        if cadence_spm is not None:
            self.card_cadence.set_value(str(display_cadence(cadence_spm, self.sport_type)))
        if speed_mps is not None:
            self.card_speed.set_value(f"{speed_in_units(speed_mps, unit_system):.1f}")
        if distance_m is not None:
            self.card_distance.set_value(f"{distance_in_units(distance_m, unit_system):.2f}")
        if power_watts is not None:
            self.card_power.set_value(str(int(power_watts)))

    def set_statuses(self, status: SensorStatus) -> None:
        """Update all sensor connection indicators."""
        for tile in self._status_tiles:
            tile.apply_status(status)

    def set_location_status(self, status: str) -> None:
        """Update the user-visible location lifecycle status."""
        self.location_status.set_text(status)

    def set_state(self, state: SessionState) -> None:
        """Update common session controls and workout timer emphasis."""
        self._state = state
        self.btn_start.set_sensitive(
            self._profile_ready and (self._workout_visible or state is not SessionState.RUNNING),
        )
        self.btn_stop.set_sensitive(True)
        if not self._workout_visible:
            return
        if state is SessionState.PREVIEW:
            self.btn_start.set_label("Start")
            self.timer_remaining.set_opacity(1.0)
        elif state is SessionState.RUNNING:
            self.btn_start.set_label("Pause")
            self.timer_remaining.set_opacity(1.0)
        else:
            self.btn_start.set_label("Resume")
            self.timer_remaining.set_opacity(0.5)

    def set_progress(self, frac: float) -> None:
        """Update current workout-step progress."""
        self.step_progress.set_fraction(max(0.0, min(1.0, float(frac))))

    def set_target_text(self, text: str) -> None:
        """Update the current target label."""
        self.lbl_target.set_text(text)
        self._update_compliance_pill()

    def set_next_text(self, text: str) -> None:
        """Update the next target label."""
        self.lbl_next.set_text(text)

    def apply_guidance(
        self,
        guidance: StepGuidance,
        metrics: LiveMetrics,
        *,
        progress: float | None = None,
    ) -> None:
        """Apply resolved target text, gauge state, and optional progress."""
        self.set_target_text(guidance.target_text)
        self.set_next_text(guidance.next_text)

        if guidance.domain is TargetDomain.POWER:
            self.set_gauge_power(
                current_w=metrics.power_watts,
                target_w_mid=guidance.mid,
                target_w_lo=guidance.low,
                target_w_hi=guidance.high,
            )
        elif guidance.domain is TargetDomain.PACE:
            self.set_gauge_pace(
                current_mps=metrics.speed_mps,
                current_pace_text=format_pace(metrics.speed_mps, self.app.unit_system),
                target_pace_text=format_pace(guidance.mid, self.app.unit_system),
                target_mps_lo=guidance.low,
                target_mps_mid=guidance.mid,
                target_mps_hi=guidance.high,
            )
        elif guidance.domain is TargetDomain.HEART_RATE:
            self.set_gauge_hr(
                current_bpm=metrics.heart_rate_bpm,
                target_bpm_mid=guidance.mid,
                target_bpm_lo=guidance.low,
                target_bpm_hi=guidance.high,
            )

        if progress is not None:
            self.set_progress(progress)

    def update_chart(
        self,
        x_secs: np.ndarray,
        hr: np.ndarray,
        pw: np.ndarray,
        hr_rgb: tuple[float, float, float] = (1, 1, 1),
    ) -> None:
        """Update chart data; the tracker controls the redraw cadence."""
        self.live_chart.line_hr.set_data(x_secs, hr)
        self.live_chart.line_pw.set_data(x_secs, pw)
        self.live_chart.line_hr.set_color(hr_rgb)

        if len(pw) >= _MIN_CHART_POINTS:
            pmin, pmax = float(pw.min()), float(pw.max())
            pad = max(10.0, 0.1 * (pmax - pmin if pmax != pmin else max(1.0, pmax)))
            self.live_chart.power_axes.set_ylim(max(0.0, pmin - pad), pmax + pad)
        else:
            self.live_chart.power_axes.set_ylim(0, 500)

    def refresh_theme(self) -> None:
        """Restyle the continuous chart without discarding its data."""
        self.live_chart.refresh_theme(
            theme=self.app.chart_theme,
            zones=self.app.calculate_hr_zones(),
            resting_hr=self.app.app_settings.personal.resting_hr,
            max_hr=self.app.app_settings.personal.max_hr,
        )

    def redraw(self) -> None:
        """Redraw the continuous chart."""
        self.live_chart.canvas.draw_idle()

    def set_gauge_power(
        self,
        *,
        current_w: float,
        target_w_lo: float,
        target_w_mid: float,
        target_w_hi: float,
    ) -> None:
        """Show a power target band."""
        self.gauge.set_state(
            value=current_w,
            target_lo=target_w_lo,
            target_mid=target_w_mid,
            target_hi=target_w_hi,
            headline=f"{round(current_w)} W",
            subline=f"Target: {target_w_mid} W",
            domain_pad=0.5,
        )
        self._update_compliance_pill()

    def set_gauge_pace(
        self,
        *,
        current_mps: float,
        current_pace_text: str,
        target_pace_text: str,
        target_mps_lo: float,
        target_mps_mid: float,
        target_mps_hi: float,
    ) -> None:
        """Show a pace target band."""
        pace_unit = unit_label("pace", self.app.unit_system)
        self.gauge.set_state(
            value=current_mps,
            target_lo=target_mps_lo,
            target_mid=target_mps_mid,
            target_hi=target_mps_hi,
            headline=f"{current_pace_text} /{pace_unit}",
            subline=f"Target: {target_pace_text} /{pace_unit}",
            domain_pad=0.5,
        )
        self._update_compliance_pill()

    def set_gauge_hr(
        self,
        *,
        current_bpm: float,
        target_bpm_lo: float,
        target_bpm_mid: float,
        target_bpm_hi: float,
    ) -> None:
        """Show a heart-rate target band."""
        self.gauge.set_state(
            value=current_bpm,
            target_lo=target_bpm_lo,
            target_mid=target_bpm_mid,
            target_hi=target_bpm_hi,
            headline=f"{round(current_bpm)} bpm",
            subline=f"Target: {round(target_bpm_mid)} bpm",
            domain_pad=0.25,
        )
        self._update_compliance_pill()

    def _update_compliance_pill(self) -> None:
        status = self.gauge.band_status()
        for css_class in ("pill-in", "pill-near", "pill-low", "pill-high"):
            self.compliance.remove_css_class(css_class)

        if status == "in":
            self.compliance.add_css_class("pill-in")
            text = "In Target"
        elif status == "near":
            self.compliance.add_css_class("pill-near")
            text = "Close to Target"
        elif status == "low":
            self.compliance.add_css_class("pill-low")
            text = "Below Target"
        else:
            self.compliance.add_css_class("pill-high")
            text = "Above Target"
        self.compliance.set_text(text)
