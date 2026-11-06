"""Unified free-run and workout session view."""

from __future__ import annotations

from functools import cache
from time import monotonic
from typing import TYPE_CHECKING

import gi

from fitness_tracker.core.guidance import TargetDomain, format_target_deviation
from fitness_tracker.core.sensor_status import SensorKind, SensorStatus
from fitness_tracker.core.session_state import SessionState
from fitness_tracker.core.sports import SportTypesEnum
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
from gi.repository import Adw, Gdk, GLib, GObject, Gtk, Pango  # noqa: E402  # ty:ignore[unresolved-import]

if TYPE_CHECKING:
    from collections.abc import Callable

    import numpy as np

    from fitness_tracker.core.guidance import StepGuidance
    from fitness_tracker.core.live_metrics import LiveMetrics
    from fitness_tracker.core.session_capabilities import SessionCapabilities
    from fitness_tracker.ui.app import FitnessAppUI


_MIN_CHART_POINTS = 2
_STOP_HOLD_SECONDS = 1.5
_STOP_HOLD_TICK_MS = 32
_INLINE_ADJUSTMENTS_CLEARANCE = 8
_SESSION_CSS = b"""
.session-view .session-actions button { min-height: 48px; padding: 0 10px; }
.session-view .metric-value { font-size: 1.8em; font-weight: 700; }
.session-view .metric-status { font-size: 0.8em; }
.session-view .compact-metrics .metric-value { font-size: 1.5em; }
.session-view .compact-metrics .metric-status { font-size: 0.75em; }
.session-view .session-footer { min-height: 104px; }
.session-view .session-footer.horizontal { min-height: 48px; }
.session-view .roomy { font-size: 2em; }
.session-view .roomy button { min-height: 96px; padding: 0 20px; }
.session-view .roomy button image { -gtk-icon-size: 32px; }
.session-view .roomy .session-footer { min-height: 96px; }
.session-view .roomy .stop-hold-progress trough,
.session-view .roomy .stop-hold-progress progress { min-height: 64px; }
.session-view .reduced { font-size: 0.8em; }
.session-view .reduced button { min-height: 40px; padding: 0 8px; }
.session-view .reduced button image { -gtk-icon-size: 13px; }
.session-view .reduced .session-footer { min-height: 86px; }
.session-view .reduced .session-footer.horizontal { min-height: 40px; }
.session-view .reduced expander > title { min-height: 40px; }
.session-view .reduced .stop-hold-progress trough,
.session-view .reduced .stop-hold-progress progress { min-height: 26px; }
.session-view .mini { font-size: 0.666667em; }
.session-view .mini button { min-height: 32px; padding: 0 6px; }
.session-view .mini button image { -gtk-icon-size: 11px; }
.session-view .mini .session-footer { min-height: 69px; }
.session-view .mini .session-footer.horizontal { min-height: 32px; }
.session-view .mini expander > title { min-height: 32px; }
.session-view .mini .stop-hold-progress trough,
.session-view .mini .stop-hold-progress progress { min-height: 21px; }
.session-view .reduced flowboxchild,
.session-view .mini flowboxchild { padding: 2px; }
.session-view expander > title { min-height: 48px; }
.session-view .metric-live { color: @success_color; }
.session-view .session-timer { border: none; }
.session-view button.stop-activity {
    background-color: @destructive_bg_color;
    color: @destructive_fg_color;
}
.stop-hold-progress trough {
    min-width: 0;
    min-height: 32px;
    background: transparent;
    border: none;
}
.stop-hold-progress progress {
    min-height: 32px;
    background-color: shade(@destructive_bg_color, 0.16);
    border: none;
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
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.app = app
        self.sport_type = sport_type
        self.capabilities = capabilities
        self._workout_visible = False
        self._state = SessionState.PREVIEW
        self._profile_ready = False
        self._sensor_status = SensorStatus()
        self._target_domain = TargetDomain.NONE
        self._target_difference = ""

        _ensure_session_css()
        self.add_css_class("session-view")
        self._adaptive = Adw.BreakpointBin()
        self._adaptive.set_size_request(200, 160)
        self._adaptive.set_vexpand(True)
        self.append(self._adaptive)
        shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(shell, f"set_margin_{side}")(8)
        self._adaptive.set_child(shell)
        self._body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._body.set_valign(Gtk.Align.FILL)
        self._guidance = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._guidance.set_hexpand(True)
        self._readings = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._readings.set_hexpand(True)
        self._body.append(self._guidance)
        self._body.append(self._readings)
        self._scroller = Gtk.ScrolledWindow()
        self._scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scroller.set_vexpand(True)
        self._scroller.set_child(self._body)
        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_vexpand(True)
        self._toast_overlay.set_child(self._scroller)
        shell.append(self._toast_overlay)

        self._build_workout_panel(title, on_prev, on_next)
        self._build_metric_strip()
        self._build_optional_controls(on_incline, on_trainer_target)
        self._build_recording_controls(on_stop, on_start_record)
        shell.append(self._build_footer())
        self._build_live_chart()
        self._build_breakpoints()

        self.set_unit_system(self.app.unit_system)
        self.set_workout_visible(visible=workout)
        self.set_state(SessionState.PREVIEW)
        self._update_gauge_status()

    def _build_footer(self) -> Adw.BreakpointBin:
        """Keep action height in CSS so density and orientation cannot conflict."""
        self._footer = Adw.BreakpointBin(css_classes=["session-footer"])
        # BreakpointBin requires a positive request; CSS determines the height.
        self._footer.set_size_request(200, 1)
        self._footer.set_child(self._controls)
        for condition in (
            "min-width: 300sp",
            "min-width: 180sp and max-width: 279px",
        ):
            horizontal = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(condition))
            horizontal.add_setter(self._controls, "orientation", Gtk.Orientation.HORIZONTAL)
            horizontal.add_setter(
                self._footer,
                "css-classes",
                GObject.Value(GObject.TYPE_STRV, ["session-footer", "horizontal"]),
            )
            self._footer.add_breakpoint(horizontal)
        return self._footer

    def _build_breakpoints(self) -> None:
        """Reflow existing widgets using available logical pixels, including height."""
        compact_classes = GObject.Value(GObject.TYPE_STRV, ["compact-metrics"])
        medium = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("min-width: 480sp"))
        medium.add_setter(self.metrics, "min-children-per-line", 3)
        medium.add_setter(self.metrics, "max-children-per-line", 3)
        medium.add_setter(self._hero, "orientation", Gtk.Orientation.HORIZONTAL)
        self._adaptive.add_breakpoint(medium)

        tight = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse(
                "(min-width: 350px and max-width: 599px and max-height: 560sp) or "
                "(min-width: 350px and max-width: 479sp and max-height: 600sp) or "
                "(min-width: 350px and max-height: 400sp)",
            ),
        )
        tight.add_setter(self.metrics, "min-children-per-line", 3)
        tight.add_setter(self.metrics, "max-children-per-line", 3)
        tight.add_setter(self._hero, "orientation", Gtk.Orientation.HORIZONTAL)
        tight.add_setter(self.gauge, "content-width", 110)
        tight.add_setter(self.gauge, "content-height", 60)
        tight.add_setter(self.title_label, "visible", value=False)
        tight.add_setter(self.lbl_next, "visible", value=False)
        tight.add_setter(self._body, "css-classes", compact_classes)
        self._adaptive.add_breakpoint(tight)

        tiny = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse("max-width: 349px and max-height: 560sp"),
        )
        tiny.add_setter(self._hero, "orientation", Gtk.Orientation.HORIZONTAL)
        tiny.add_setter(self.gauge, "content-width", 100)
        tiny.add_setter(self._timers, "orientation", Gtk.Orientation.VERTICAL)
        tiny.add_setter(self.gauge, "content-height", 64)
        tiny.add_setter(self.title_label, "visible", value=False)
        tiny.add_setter(self.lbl_next, "visible", value=False)
        tiny.add_setter(self._body, "css-classes", compact_classes)
        self._adaptive.add_breakpoint(tiny)
        self._build_wide_breakpoints(compact_classes)
        self._build_small_density_breakpoints(compact_classes)
        self._build_roomy_breakpoint()
        self._adaptive.connect("notify::current-breakpoint", self._on_layout_changed)
        self.connect("unmap", lambda *_: self._cancel_stop_hold())

    def _build_wide_breakpoints(self, compact_classes: GObject.Value) -> None:
        """Keep landscape guidance beside metrics, including with enlarged text."""
        # Near-square Comet allocations keep the hero above the metric grid,
        # even at 1x scaling. Landscape places guidance beside the metrics.
        wide = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse(
                "min-width: 600sp and min-aspect-ratio: 13/10",
            ),
        )
        wide.add_setter(self._body, "orientation", Gtk.Orientation.HORIZONTAL)
        wide.add_setter(self._body, "homogeneous", value=True)
        wide.add_setter(self._guidance, "valign", Gtk.Align.START)
        wide.add_setter(self.metrics, "min-children-per-line", 2)
        wide.add_setter(self.metrics, "max-children-per-line", 2)
        wide.add_setter(self._hero, "orientation", Gtk.Orientation.HORIZONTAL)
        wide.add_setter(self.gauge, "content-width", 140)
        wide.add_setter(self.gauge, "size-scale", 1.0)
        self._adaptive.add_breakpoint(wide)

        landscape = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse(
                "min-width: 700px and min-width: 420sp and max-height: 440sp "
                "and min-aspect-ratio: 8/5",
            ),
        )
        landscape.add_setter(self._body, "orientation", Gtk.Orientation.HORIZONTAL)
        landscape.add_setter(self._body, "homogeneous", value=True)
        landscape.add_setter(self._guidance, "valign", Gtk.Align.START)
        landscape.add_setter(self.metrics, "min-children-per-line", 3)
        landscape.add_setter(self.metrics, "max-children-per-line", 3)
        landscape.add_setter(self._hero, "orientation", Gtk.Orientation.HORIZONTAL)
        landscape.add_setter(self.gauge, "content-width", 110)
        landscape.add_setter(self.gauge, "content-height", 80)
        landscape.add_setter(self.title_label, "visible", value=False)
        landscape.add_setter(self.lbl_next, "visible", value=False)
        landscape.add_setter(self._body, "css-classes", compact_classes)
        self._adaptive.add_breakpoint(landscape)

    def _set_small_density(self, layout: Adw.Breakpoint, css_class: str, scale: float) -> None:
        """Scale text, spacing, and touch targets together in small allocations."""
        shell = self._adaptive.get_child()
        layout.add_setter(shell, "css-classes", GObject.Value(GObject.TYPE_STRV, [css_class]))
        for side in ("top", "bottom", "start", "end"):
            layout.add_setter(shell, f"margin-{side}", round(8 * scale))
        for widget in (shell, self._body, self._readings, self._controls, self._workout_navigation):
            layout.add_setter(widget, "spacing", round(8 * scale))
        layout.add_setter(self._controls.get_last_child(), "spacing", round(8 * scale))
        layout.add_setter(self._hero, "spacing", round(6 * scale))
        layout.add_setter(self._timers, "spacing", round(6 * scale))
        layout.add_setter(self._step_details, "spacing", round(4 * scale))
        layout.add_setter(self.metrics, "column-spacing", round(8 * scale))
        layout.add_setter(self.metrics, "row-spacing", round(4 * scale))
        layout.add_setter(self.gauge, "label-scale", scale)
        layout.add_setter(self.live_chart.canvas, "width-request", round(200 * scale))
        layout.add_setter(self.live_chart.canvas, "height-request", round(160 * scale))
        for tile in self._status_tiles:
            layout.add_setter(tile, "width-request", round(100 * scale))
            for side, margin in (("top", 4), ("bottom", 4), ("start", 8), ("end", 8)):
                layout.add_setter(tile.get_child(), f"margin-{side}", round(margin * scale))
        buttons = [self.btn_prev, self.btn_next]
        touch_size = round(48 * scale / 8) * 8
        if self.trainer_target_control:
            control = self.trainer_target_control
            buttons.extend(control.touch_buttons)
        if self.incline_control:
            buttons.extend(self.incline_control.touch_buttons)
        for button in buttons:
            layout.add_setter(button, "height-request", touch_size)
        for button in (self.btn_prev, self.btn_next):
            layout.add_setter(button, "width-request", touch_size)
        if self.capabilities.incline or self.capabilities.trainer_targets:
            layout.add_setter(
                self._adjustments_expander.get_label_widget(),
                "height-request",
                touch_size,
            )
            layout.add_setter(self._adjustments_slot, "margin-bottom", round(8 * scale))

    def _build_small_density_breakpoints(self, compact_classes: GObject.Value) -> None:
        """Preserve the reference hierarchy at fractional and high display scales."""
        reduced = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse(
                "min-width: 420px and max-width: 519px and min-height: 300px and max-height: 560px",
            ),
        )
        self._set_small_density(reduced, "reduced", 0.8)
        reduced.add_setter(self._hero, "orientation", Gtk.Orientation.HORIZONTAL)
        reduced.add_setter(self.gauge, "content-width", 128)
        reduced.add_setter(self.gauge, "content-height", 100)
        reduced.add_setter(self.metrics, "min-children-per-line", 3)
        reduced.add_setter(self.metrics, "max-children-per-line", 3)
        self._adaptive.add_breakpoint(reduced)

        portrait = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 279px"))
        self._set_small_density(portrait, "mini", 2 / 3)
        portrait.add_setter(self.gauge, "content-width", 107)
        portrait.add_setter(self.gauge, "content-height", 107)
        self._adaptive.add_breakpoint(portrait)

        landscape = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse(
                "min-width: 400px and max-width: 599px and max-height: 280px "
                "and min-aspect-ratio: 8/5",
            ),
        )
        self._set_small_density(landscape, "mini", 2 / 3)
        landscape.add_setter(self._body, "orientation", Gtk.Orientation.HORIZONTAL)
        landscape.add_setter(self._body, "homogeneous", value=True)
        landscape.add_setter(self._guidance, "valign", Gtk.Align.START)
        landscape.add_setter(self._hero, "orientation", Gtk.Orientation.HORIZONTAL)
        landscape.add_setter(self.gauge, "content-width", 73)
        landscape.add_setter(self.gauge, "content-height", 53)
        landscape.add_setter(self.metrics, "min-children-per-line", 3)
        landscape.add_setter(self.metrics, "max-children-per-line", 3)
        landscape.add_setter(self.title_label, "visible", value=False)
        landscape.add_setter(self.lbl_next, "visible", value=False)
        landscape.add_setter(self._body, "css-classes", compact_classes)
        self._adaptive.add_breakpoint(landscape)

    def _build_roomy_breakpoint(self) -> None:
        """Give the whole dashboard the readable density of a 2x session."""
        roomy = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse(
                "min-width: 900px and min-height: 700px and max-aspect-ratio: 13/10",
            ),
        )
        roomy.add_setter(self._hero, "orientation", Gtk.Orientation.HORIZONTAL)
        roomy.add_setter(self.gauge, "content-width", 320)
        roomy.add_setter(self.gauge, "content-height", 250)
        roomy.add_setter(self.gauge, "label-scale", 2.0)
        roomy.add_setter(self.gauge, "size-scale", 1.0)
        roomy.add_setter(self.metrics, "min-children-per-line", 3)
        roomy.add_setter(self.metrics, "max-children-per-line", 3)
        roomy.add_setter(self.metrics, "vexpand", value=True)
        shell = self._adaptive.get_child()
        roomy.add_setter(shell, "css-classes", GObject.Value(GObject.TYPE_STRV, ["roomy"]))
        for side in ("top", "bottom", "start", "end"):
            roomy.add_setter(shell, f"margin-{side}", 16)
        for widget in (shell, self._body, self._readings, self._controls, self._workout_navigation):
            roomy.add_setter(widget, "spacing", 16)
        roomy.add_setter(self._controls.get_last_child(), "spacing", 16)
        roomy.add_setter(self._hero, "spacing", 12)
        roomy.add_setter(self._timers, "spacing", 12)
        roomy.add_setter(self._step_details, "spacing", 8)
        roomy.add_setter(self.metrics, "column-spacing", 16)
        roomy.add_setter(self.metrics, "row-spacing", 8)
        roomy.add_setter(self.live_chart.canvas, "height-request", 320)
        if self.capabilities.incline or self.capabilities.trainer_targets:
            roomy.add_setter(self._adjustments_expander.get_label_widget(), "height-request", 96)
            roomy.add_setter(self._adjustments_slot, "margin-bottom", 16)
        for button in (self.btn_prev, self.btn_next):
            roomy.add_setter(button, "width-request", 96)
        self._adaptive.add_breakpoint(roomy)

    def _on_layout_changed(self, *_args: object) -> None:
        """Keep the chart below portrait metrics and cancel a pending stop."""
        self._cancel_stop_hold()
        shell = self._adaptive.get_child()
        scale = next(
            (
                value
                for name, value in (("roomy", 2.0), ("reduced", 0.8), ("mini", 2 / 3))
                if shell.has_css_class(name)
            ),
            1.0,
        )
        density_changed = scale != self._layout_density
        self._layout_density = scale
        self.live_chart.set_display_scale(scale)
        if (
            not (self.capabilities.incline or self.capabilities.trainer_targets)
            or not self._adjustments_expander.get_expanded()
        ):
            self._scroller.get_vadjustment().set_value(0)
        destination = (
            self._guidance
            if self._body.get_orientation() is Gtk.Orientation.HORIZONTAL
            else self._readings
        )
        parent = self._live_chart_frame.get_parent()
        if parent is not destination:
            parent.remove(self._live_chart_frame)
            destination.append(self._live_chart_frame)
        if density_changed and self.capabilities.trainer_targets:
            self._on_adjustments_font_changed()
        else:
            self._queue_adjustments_layout()

    def _build_workout_navigation(
        self,
        on_prev: Callable[[], None],
        on_next: Callable[[], None],
    ) -> None:
        """Build workout step navigation buttons."""
        workout_navigation = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        workout_navigation.set_homogeneous(True)
        self.btn_prev = Gtk.Button.new_from_icon_name("media-skip-backward-symbolic")
        self.btn_prev.set_tooltip_text("Previous workout step")
        self.btn_prev.update_property([Gtk.AccessibleProperty.LABEL], ["Previous workout step"])
        self.btn_next = Gtk.Button.new_from_icon_name("media-skip-forward-symbolic")
        self.btn_next.set_tooltip_text("Next workout step")
        self.btn_next.update_property([Gtk.AccessibleProperty.LABEL], ["Next workout step"])
        self.btn_prev.set_size_request(48, 48)
        self.btn_next.set_size_request(48, 48)
        self.btn_prev.connect("clicked", lambda *_: on_prev())
        self.btn_next.connect("clicked", lambda *_: on_next())
        workout_navigation.append(self.btn_prev)
        workout_navigation.append(self.btn_next)
        workout_navigation.set_hexpand(False)
        self._workout_navigation = workout_navigation

    def _build_workout_panel(
        self,
        title: str,
        on_prev: Callable[[], None],
        on_next: Callable[[], None],
    ) -> None:
        """Build the timer and workout-guidance widgets."""
        self.timer = SessionTimer(None)
        self._guidance.append(self.timer)

        self._workout_revealer = Gtk.Revealer()
        self._workout_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.workout_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._workout_revealer.set_child(self.workout_panel)
        self._guidance.append(self._workout_revealer)

        self.title_label = Gtk.Label(xalign=0.0)
        self.title_label.add_css_class("heading")
        self.title_label.set_single_line_mode(True)
        self.title_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.title_label.set_max_width_chars(36)
        self.title_label.set_hexpand(True)
        self.title_label.set_halign(Gtk.Align.FILL)
        self.title_label.set_text(title)
        self.workout_panel.append(self.title_label)

        self._hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._hero.set_vexpand(True)
        self.workout_panel.append(self._hero)
        target_display = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        target_display.set_hexpand(True)
        self._hero.append(target_display)
        self._step_details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._step_details.set_hexpand(True)
        self._hero.append(self._step_details)
        self._build_step_details()

        self.gauge = TargetGauge()
        self.gauge.size_scale = 0.9
        self.gauge.set_vexpand(True)
        self.gauge.set_margin_top(4)
        target_display.append(self.gauge)

        self._build_workout_navigation(on_prev, on_next)

    def _build_step_details(self) -> None:
        """Keep timers, target instructions, and next-step context together."""
        timers = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._timers = timers
        self.timer_elapsed = SessionTimer("Elapsed")
        self.timer_remaining = SessionTimer("Remaining")
        timers.append(self.timer_elapsed)
        timers.append(self.timer_remaining)
        self._step_details.append(timers)

        self.lbl_target = Gtk.Label(xalign=0.0, wrap=True)
        self.lbl_target.add_css_class("heading")
        self.lbl_target.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self._step_details.append(self.lbl_target)

        self.lbl_next = Gtk.Label(xalign=0.0, wrap=True)
        self.lbl_next.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self._step_details.append(self.lbl_next)

        self.step_progress = Gtk.ProgressBar()
        self.step_progress.set_hexpand(True)
        self.step_progress.set_margin_top(2)
        self.step_progress.set_margin_bottom(1)
        self._step_details.append(self.step_progress)

    def _build_metric_strip(self) -> None:
        """Build the shared live metric tiles."""
        metrics = Gtk.FlowBox()
        metrics.set_selection_mode(Gtk.SelectionMode.NONE)
        metrics.set_valign(Gtk.Align.FILL)
        metrics.set_halign(Gtk.Align.FILL)
        metrics.set_homogeneous(True)
        metrics.set_column_spacing(8)
        metrics.set_row_spacing(4)
        metrics.set_min_children_per_line(2)
        metrics.set_max_children_per_line(2)
        self.metrics = metrics

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
        tiles = (
            (
                self.card_pace,
                self.card_hr,
                self.card_distance,
                self.card_cadence,
                self.card_speed,
                self.card_power,
            )
            if self.sport_type is SportTypesEnum.running
            else (
                self.card_power,
                self.card_speed,
                self.card_hr,
                self.card_cadence,
                self.card_distance,
                self.card_pace,
            )
        )
        for tile in tiles:
            metrics.insert(tile, -1)
        self._readings.append(metrics)

    def _build_optional_controls(
        self,
        on_incline: Callable[[float], None],
        on_trainer_target: Callable[[TrainerMode, int | float], None],
    ) -> None:
        """Build capability-dependent incline and trainer controls."""
        self._adjustments_inline = False
        self._layout_density = 1.0
        self._adjustments_layout_source_id: int | None = None
        adjustments = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
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
            adjustments.append(self.incline_control)

        self.trainer_target_control: TrainerTargetControl | None = None
        if self.capabilities.trainer_targets:
            self.trainer_target_control = TrainerTargetControl(
                on_trainer_target,
                available_modes=trainer_modes_for_session(self.sport_type, include_bias=True),
                unit_system=self.app.unit_system,
            )
            adjustments.append(self.trainer_target_control)

        if self.capabilities.incline or self.capabilities.trainer_targets:
            expander_label = Gtk.Label(
                label="Trainer controls"
                if self.capabilities.trainer_targets
                else "Incline controls",
                xalign=0.0,
            )
            expander_label.set_ellipsize(Pango.EllipsizeMode.END)
            expander_label.set_size_request(-1, 48)
            expander = Gtk.Expander()
            expander.set_label_widget(expander_label)
            expander.set_child(adjustments)
            control = self.trainer_target_control or self.incline_control
            if control is not None:
                control.bind_property(
                    "summary",
                    expander_label,
                    "label",
                    GObject.BindingFlags.SYNC_CREATE,
                )
            expander.connect("notify::expanded", self._on_adjustments_expanded)
            self._adjustments_expander = expander
            self._adjustments_content = adjustments
            self._adjustments_slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            self._adjustments_slot.set_margin_bottom(8)
            self._adjustments_slot.append(expander)
            self._readings.append(self._adjustments_slot)
            if self.capabilities.trainer_targets:
                for adjustment in (
                    self._scroller.get_hadjustment(),
                    self._scroller.get_vadjustment(),
                ):
                    adjustment.connect("changed", self._queue_adjustments_layout)
                self.connect("map", self._on_adjustments_font_changed)
                self.connect("unmap", self._cancel_adjustments_layout)
                settings = self.get_settings()
                for setting in ("gtk-xft-dpi", "gtk-font-name"):
                    settings.connect_object(
                        f"notify::{setting}",
                        SessionView._on_adjustments_font_changed,
                        self,
                    )

    def _on_adjustments_font_changed(self, *_args: object) -> None:
        """Refresh hidden GTK font metrics before deciding whether controls fit."""
        if self.get_mapped():
            # A collapsed expander does not refresh its children's text metrics.
            # Map the same controls for one allocation, then measure the new font.
            self._set_adjustments_inline(inline=True)
            self._cancel_adjustments_layout()
            self._adjustments_layout_source_id = GLib.timeout_add(50, self._sync_adjustments_layout)

    def _queue_adjustments_layout(self, *_args: object) -> None:
        """Reconsider direct trainer access after allocation, without frame polling."""
        if self.capabilities.trainer_targets and self._adjustments_layout_source_id is None:
            self._adjustments_layout_source_id = GLib.idle_add(self._sync_adjustments_layout)

    def _cancel_adjustments_layout(self, *_args: object) -> None:
        """Discard pending layout work when the session leaves the screen."""
        if self._adjustments_layout_source_id is not None:
            GLib.source_remove(self._adjustments_layout_source_id)
            self._adjustments_layout_source_id = None

    def _sync_adjustments_layout(self) -> bool:
        """Expose trainer controls directly when the whole dashboard can fit."""
        self._adjustments_layout_source_id = None
        if not self.get_mapped():
            return GLib.SOURCE_REMOVE
        width = self._readings.get_width()
        minimum_width = self._adjustments_content.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
        if width < minimum_width:
            inline = False
        else:
            readings_height = self._adjustments_content.measure(Gtk.Orientation.VERTICAL, width)[1]
            readings_height += self._adjustments_slot.get_margin_bottom()
            for child in self._readings:
                if child is not self._adjustments_slot and child.get_visible():
                    readings_height += child.measure(Gtk.Orientation.VERTICAL, width)[1]
                    readings_height += self._readings.get_spacing()
            guidance_height = self._guidance.measure(
                Gtk.Orientation.VERTICAL,
                self._guidance.get_width(),
            )[1]
            if self._body.get_orientation() is Gtk.Orientation.HORIZONTAL:
                required_height = max(guidance_height, readings_height)
            else:
                required_height = guidance_height + self._body.get_spacing() + readings_height
            inline = required_height + _INLINE_ADJUSTMENTS_CLEARANCE <= self._scroller.get_height()
        self._set_adjustments_inline(inline=inline)
        return GLib.SOURCE_REMOVE

    def _set_adjustments_inline(self, *, inline: bool) -> None:
        """Move the existing controls without changing the selected trainer target."""
        if inline == self._adjustments_inline:
            return
        self._cancel_stop_hold()
        if inline:
            self._adjustments_expander.set_child(None)
            self._adjustments_slot.remove(self._adjustments_expander)
            self._adjustments_slot.append(self._adjustments_content)
        else:
            self._adjustments_slot.remove(self._adjustments_content)
            self._adjustments_expander.set_child(self._adjustments_content)
            self._adjustments_expander.set_expanded(False)
            self._adjustments_slot.append(self._adjustments_expander)
        self._adjustments_inline = inline
        self._scroller.get_vadjustment().set_value(0)

    def _on_adjustments_expanded(self, expander: Gtk.Expander, _property: object) -> None:
        """Make newly opened controls reachable after GTK has allocated them."""
        if expander.get_expanded():
            GLib.timeout_add(50, self._scroll_adjustments_into_view)

    def _scroll_adjustments_into_view(self) -> bool:
        """Scroll just enough to expose adjustments without moving the footer."""
        if self.get_mapped() and self._adjustments_expander.get_expanded():
            success, bounds = self._adjustments_content.compute_bounds(self._body)
            if success:
                adjustment = self._scroller.get_vadjustment()
                adjustment.set_value(
                    max(
                        adjustment.get_value(),
                        bounds.get_y() + bounds.get_height() - adjustment.get_page_size(),
                    ),
                )
        return GLib.SOURCE_REMOVE

    def _build_recording_controls(
        self,
        on_stop: Callable[[], None],
        on_start_record: Callable[[], None],
    ) -> None:
        """Build the paired workout navigation and recording controls."""
        self._on_stop = on_stop
        self._stop_hold_started_at: float | None = None
        self._stop_hold_source_id: int | None = None

        action_pair = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        action_pair.set_hexpand(True)
        self.btn_stop = Gtk.Button()
        self.btn_stop.add_css_class("destructive-action")
        self.btn_stop.add_css_class("stop-activity")
        self.btn_stop.set_tooltip_text(
            f"Press and hold for {_STOP_HOLD_SECONDS} seconds to stop the activity",
        )
        stop_overlay = Gtk.Overlay()
        self._stop_progress = Gtk.ProgressBar()
        self._stop_progress.add_css_class("stop-hold-progress")
        self._stop_progress.set_fraction(0.0)
        self._stop_progress.set_hexpand(True)
        self._stop_progress.set_valign(Gtk.Align.CENTER)
        stop_overlay.set_child(self._stop_progress)
        stop_label = Gtk.Label(label="Hold to stop")
        stop_overlay.add_overlay(stop_label)
        stop_overlay.set_measure_overlay(stop_label, measure=True)
        self.btn_stop.set_child(stop_overlay)
        self.btn_start = Gtk.Button.new_with_label("▶️  Start")
        self.btn_start.add_css_class("suggested-action")
        self.btn_start.set_hexpand(True)
        self.btn_stop.set_hexpand(True)
        self.btn_stop.update_property([Gtk.AccessibleProperty.LABEL], ["Hold to stop activity"])
        self._connect_stop_controls()

        self.btn_start.connect("clicked", lambda *_: on_start_record())
        action_pair.append(self.btn_stop)
        action_pair.append(self.btn_start)

        controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        controls.set_hexpand(True)
        controls.add_css_class("session-actions")
        controls.append(self._workout_navigation)
        controls.append(action_pair)
        self._controls = controls

    def _connect_stop_controls(self) -> None:
        """Handle pointer and keyboard holds, including cancellation."""
        stop_gesture = Gtk.GestureClick.new()
        stop_gesture.set_button(1)
        stop_gesture.connect("pressed", lambda *_: self._begin_stop_hold(stop_gesture))
        stop_gesture.connect("update", self._on_stop_gesture_updated)
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

    def _begin_stop_hold(self, gesture: Gtk.GestureClick | None = None) -> None:
        """Start the stop countdown without allowing repeated presses to restart it."""
        if self._stop_hold_started_at is not None:
            return
        self._stop_hold_gesture = gesture
        self._stop_hold_sequence = gesture.get_current_sequence() if gesture is not None else None
        self._stop_hold_started_at = monotonic()
        self._stop_hold_start_geometry = self._stop_hold_geometry()
        self._stop_progress.set_fraction(0.0)
        self._stop_hold_source_id = GLib.timeout_add(
            _STOP_HOLD_TICK_MS,
            self._update_stop_hold,
        )

    def _stop_hold_geometry(self) -> tuple[float, float, float, float]:
        """Capture control placement even when rotation keeps the same breakpoint."""
        _success, bounds = self.btn_stop.compute_bounds(self)
        return bounds.get_x(), bounds.get_y(), bounds.get_width(), bounds.get_height()

    def _stop_hold_position_valid(self) -> bool:
        """Check press coordinates even while GTK implicitly grabs the pointer."""
        if self._stop_hold_gesture is None:
            return True
        valid, x, y = self._stop_hold_gesture.get_point(self._stop_hold_sequence)
        return valid and 0 <= x < self.btn_stop.get_width() and 0 <= y < self.btn_stop.get_height()

    def _on_stop_gesture_updated(
        self,
        gesture: Gtk.Gesture,
        _sequence: Gdk.EventSequence | None,
    ) -> None:
        """Cancel immediately when a held pointer or touch leaves the button."""
        if (
            self._stop_hold_started_at is not None
            and self._stop_hold_gesture is gesture
            and not self._stop_hold_position_valid()
        ):
            self._cancel_stop_hold()

    def _update_stop_hold(self) -> bool:
        """Update the countdown and stop once the full hold duration elapses."""
        started_at = self._stop_hold_started_at
        if started_at is None:
            return GLib.SOURCE_REMOVE
        if (
            self._stop_hold_geometry() != self._stop_hold_start_geometry
            or not self._stop_hold_position_valid()
        ):
            self._cancel_stop_hold()
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
        if (
            self._stop_hold_geometry() != self._stop_hold_start_geometry
            or not self._stop_hold_position_valid()
        ):
            self._cancel_stop_hold()
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
        self.live_chart.canvas.set_size_request(200, 160)
        self._live_chart_frame.set_child(self.live_chart.canvas)
        self._readings.append(self._live_chart_frame)

    def set_workout_visible(self, *, visible: bool) -> None:
        """Reveal or hide workout guidance without replacing the session page."""
        self._workout_visible = visible
        self._guidance.set_vexpand(visible)
        self._cancel_stop_hold()
        self._workout_revealer.set_reveal_child(visible)
        self.timer.set_visible(not visible)
        self._live_chart_frame.set_visible(not visible)
        self._workout_navigation.set_visible(visible)
        if self.trainer_target_control:
            self.trainer_target_control.set_mode_available(
                TrainerMode.BIAS,
                available=visible,
            )
        if visible:
            self.btn_start.set_label("Start")
        else:
            self.btn_start.set_label("▶️  Start")
        self._queue_adjustments_layout()

    def set_title(self, title: str) -> None:
        """Update the workout title shown in the guidance panel."""
        self.title_label.set_text(title or "Workout")

    def add_toast(self, toast: Adw.Toast) -> None:
        """Show session notices above the pinned recording controls."""
        self._toast_overlay.add_toast(toast)

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
        self._sensor_status = status
        self._update_gauge_status()

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
        self._update_gauge_status()

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
        else:
            self._target_domain = TargetDomain.NONE
            self.gauge.clear_target()
            self._update_gauge_status()

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

    @property
    def chart_is_displayed(self) -> bool:
        """Return whether the live chart is mapped into the visible widget tree."""
        return bool(self._live_chart_frame.get_mapped())

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
        self._target_domain = TargetDomain.POWER
        self._target_difference = format_target_deviation(
            self._target_domain,
            current_w,
            target_w_lo,
            target_w_hi,
            self.app.unit_system,
        )
        self.gauge.set_state(
            value=current_w,
            target_lo=target_w_lo,
            target_mid=target_w_mid,
            target_hi=target_w_hi,
            headline=f"{round(current_w)} W",
            subline=f"Target: {target_w_mid} W",
            domain_pad=0.5,
        )
        self._update_gauge_status()

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
        self._target_domain = TargetDomain.PACE
        self._target_difference = format_target_deviation(
            self._target_domain,
            current_mps,
            target_mps_lo,
            target_mps_hi,
            self.app.unit_system,
        )
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
        self._update_gauge_status()

    def set_gauge_hr(
        self,
        *,
        current_bpm: float,
        target_bpm_lo: float,
        target_bpm_mid: float,
        target_bpm_hi: float,
    ) -> None:
        """Show a heart-rate target band."""
        self._target_domain = TargetDomain.HEART_RATE
        self._target_difference = format_target_deviation(
            self._target_domain,
            current_bpm,
            target_bpm_lo,
            target_bpm_hi,
            self.app.unit_system,
        )
        self.gauge.set_state(
            value=current_bpm,
            target_lo=target_bpm_lo,
            target_mid=target_bpm_mid,
            target_hi=target_bpm_hi,
            headline=f"{round(current_bpm)} bpm",
            subline=f"Target: {round(target_bpm_mid)} bpm",
            domain_pad=0.25,
        )
        self._update_gauge_status()

    def _update_gauge_status(self) -> None:
        """Keep gauge availability and its accessible target description current."""
        sensor = {
            TargetDomain.POWER: SensorKind.POWER,
            TargetDomain.PACE: SensorKind.SPEED,
            TargetDomain.HEART_RATE: SensorKind.HEART_RATE,
        }.get(self._target_domain)
        connected = sensor is not None and self._sensor_status.is_connected(sensor)
        self.gauge.set_available(connected)
        if sensor is None:
            text = "No target"
        elif not connected:
            text = f"Waiting for {sensor.value} sensor"
        else:
            text = self._target_difference
        self.gauge.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [f"{self.lbl_target.get_text()}. {text}"],
        )
