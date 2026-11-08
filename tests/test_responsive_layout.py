"""Opt-in allocation tests for real GTK widgets (run under a display or Xvfb).

FITNESS_TRACKER_GTK_TESTS=1 uv run pytest tests/test_responsive_layout.py
Set FITNESS_TRACKER_SCREENSHOTS to a directory to save the rendered sample views.
"""

# ruff: noqa: E402, SLF001  # Opt in before importing native GTK dependencies.

from __future__ import annotations

import ctypes
import ctypes.util
import json
import math
import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

if os.environ.get("FITNESS_TRACKER_GTK_TESTS") != "1":
    pytest.skip("Opt in with FITNESS_TRACKER_GTK_TESTS=1 and a display", allow_module_level=True)

import cairo
import gi
import numpy as np
from fitness_tracker.core.environment import Environment
from fitness_tracker.core.guidance import PebbleTargetKind, StepGuidance, TargetDomain
from fitness_tracker.core.layout import LayoutFamily
from fitness_tracker.core.sensor_status import SensorStatus
from fitness_tracker.core.session_capabilities import SessionCapabilities
from fitness_tracker.core.session_state import SessionState
from fitness_tracker.core.settings import AppSettings
from fitness_tracker.core.sports import SportTypesEnum
from fitness_tracker.core.trainer_mode import TrainerMode
from fitness_tracker.core.units import UnitSystem
from fitness_tracker.core.zones import ChartTheme, HeartRateZones
from fitness_tracker.hardware.recorder import FinalizationClaim, FinalizationStatus
from fitness_tracker.services.jobs import DuplicateJobError, JobRunnerShutdownError

from gtk_test_helpers import assert_inside, descendant_labels, descendants, save_screenshot, settle

gi.require_versions({"Gtk": "4.0", "Adw": "1", "Graphene": "1.0"})
from fitness_tracker.ui.pages.mode import ModeSelectView
from fitness_tracker.ui.pages.session import SessionView
from fitness_tracker.ui.widgets import session_controls as session_controls_module
from fitness_tracker.ui.widgets.scaled_view import ScaledView
from gi.repository import Adw, Gio, GLib, Graphene, Gtk  # ty:ignore[unresolved-import]

MIN_TOUCH_HEIGHT = 48
CHART_REFERENCE_FONT_SIZE = 10
MIN_COMPACT_WIDTH = 300
GAUGE_LABEL_COUNT = 2
MIN_FILL_CONTRAST = 3
MIN_TEXT_CONTRAST = 4.5
SRGB_LINEAR_THRESHOLD = 0.04045
SMALL_DENSITIES = (
    (432, 496, 0.8, 540, 620),
    (496, 432, 0.8, 620, 540),
    (240, 480, 2 / 3, 360, 720),
    (480, 240, 2 / 3, 720, 360),
)
# Outdoor workouts share this view; GPS behavior has separate lifecycle tests.
SESSION_KINDS = ("workout", "trainer", "incline", "free")

VIEWPORTS = (
    (360, 720),
    (360, 610),
    (360, 630),
    (720, 360),
    (720, 310),
    (540, 620),
    (620, 540),
    (360, 413),
    (413, 360),
    (360, 380),
    (360, 365),
    (320, 480),
    (1080, 1240),
    (1240, 1080),
)


@pytest.fixture
def app_stub():
    """Initialize GTK and supply session settings without recorder hardware."""
    Adw.init()
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
    return SimpleNamespace(
        unit_system=UnitSystem.METRIC,
        recorder=None,
        chart_theme=ChartTheme.for_style(is_dark=True),
        app_settings=AppSettings(),
        calculate_hr_zones=lambda: HeartRateZones(resting_hr=60, max_hr=190).thresholds,
    )


def make_session(app, kind):
    """Build a running sample session with live readings and tracked callbacks."""
    callbacks = Mock()
    view = SessionView(
        app,
        sport_type=SportTypesEnum.biking if kind == "trainer" else SportTypesEnum.running,
        title="Tempo · 4 x 8 minutes",
        on_prev=callbacks.previous,
        on_next=callbacks.next,
        on_stop=callbacks.stop,
        on_start_record=callbacks.start,
        on_incline=callbacks.incline,
        on_trainer_target=callbacks.target,
        capabilities=SessionCapabilities(
            incline=kind == "incline",
            trainer_targets=kind == "trainer",
        ),
        workout=kind != "free",
    )
    view.set_profile_ready(ready=True)
    view.set_state(SessionState.RUNNING)
    view.set_timer("00:18:42")
    view.set_elapsed_text("18:42")
    view.set_step_remaining_text("04:18")
    view.set_step_position(5, 15)
    view.set_target_text("Tempo · 4:45-5:00 /km")
    view.set_next_text("Next: Recover · 2 min")
    view.set_progress(0.47)
    view.set_statuses(SensorStatus(hr=True, speed=True, cadence=True, distance=True, power=True))
    view.set_metrics(
        bpm=158,
        pace_mps=3.2,
        cadence_spm=86,
        speed_mps=3.2,
        distance_m=4280,
        power_watts=182,
    )
    view.set_gauge_pace(
        current_mps=3.2,
        current_pace_text="5:13",
        target_pace_text="4:52",
        target_mps_lo=3.33,
        target_mps_mid=3.42,
        target_mps_hi=3.51,
    )
    if kind == "trainer":
        view.set_target_text("Tempo · 200-230 W")
        view.set_gauge_power(current_w=182, target_w_lo=200, target_w_mid=215, target_w_hi=230)
    callbacks.reset_mock()
    return view, callbacks


def show_view(view, width, height, *, production_shell=False):
    """Map a view inside a navigation shell and finish its initial allocation."""
    window = Gtk.Window(decorated=False)
    window.set_default_size(width, height)
    toolbar = Adw.ToolbarView()
    stack = Adw.ViewStack()
    stack.add_titled(view, "tracker", "Tracker").set_icon_name("media-playback-start-symbolic")
    stack.add_titled(Gtk.Box(), "history", "History").set_icon_name("view-list-symbolic")
    stack.add_titled(Gtk.Box(), "settings", "Settings").set_icon_name("emblem-system-symbolic")
    bar = Adw.ViewSwitcherBar()
    bar.set_stack(stack)
    bar.set_reveal(True)
    toolbar.set_content(stack)
    toolbar.add_bottom_bar(bar)
    if production_shell:
        from fitness_tracker.ui.app import _NAVIGATION_CSS, FitnessAppUI  # noqa: PLC0415

        viewport = Adw.BreakpointBin()
        viewport.set_size_request(200, 160)
        viewport.set_child(toolbar)
        window.set_child(viewport)
        header = Adw.HeaderBar()
        switcher = Adw.ViewSwitcher()
        switcher.set_stack(stack)
        header.set_title_widget(switcher)
        revealer = Gtk.Revealer()
        revealer.set_reveal_child(False)
        revealer.set_child(header)
        toolbar.add_top_bar(revealer)
        css = Gtk.CssProvider()
        css.load_from_data(_NAVIGATION_CSS)
        Gtk.StyleContext.add_provider_for_display(
            window.get_display(),
            css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        FitnessAppUI._build_shell_breakpoints(
            SimpleNamespace(window=viewport, header_revealer=revealer),
            header,
            bar,
        )
    else:
        window.set_child(toolbar)
    child = window.get_child()
    window.set_child(None)
    window.set_child(ScaledView(child))
    window.present()
    settle()
    if isinstance(view, SessionView):
        # Mapping refreshes hidden fonts before the measured layout is applied.
        settle()
    return window


def gauge_ink_bounds(surface, width, height):
    """Return the bounding rectangle of painted pixels in a gauge surface."""
    surface.flush()
    pixels = np.frombuffer(surface.get_data(), dtype=np.uint8).reshape(height, surface.get_stride())
    rows, columns = np.nonzero(pixels[:, :width])
    assert len(rows)
    return columns.min(), rows.min(), columns.max() + 1, rows.max() + 1


def assert_gauge_centered(bounds, width, height):
    """Require painted gauge content to stay centered and inside its surface."""
    left, top, right, bottom = bounds
    assert (left + right) / 2 == pytest.approx(width / 2, abs=1.5)
    assert (top + bottom) / 2 == pytest.approx(height / 2, abs=1.5)
    assert left > 0
    assert top > 0
    assert right < width
    assert bottom < height


def panel_placement(view):
    """Capture actual panel parents and orientations across a scale change."""
    return (
        view.layout_family,
        view._body.get_orientation(),
        view._hero.get_orientation(),
        view._readings.get_orientation(),
        view._adjustments_slot.get_parent()
        if view.capabilities.trainer_targets or view.capabilities.incline
        else None,
        view._adjustments_inline,
        view.metrics.get_max_children_per_line(),
        view._controls.get_orientation(),
        view.btn_prev.get_parent(),
        view.btn_next.get_parent(),
    )


def assert_fixed_placement(view):
    """Verify fixed positions independently of the interface's physical scale."""
    equipment = view.capabilities.trainer_targets or view.capabilities.incline
    if view.layout_family is LayoutFamily.PORTRAIT:
        assert view._body.get_orientation() is Gtk.Orientation.VERTICAL
        assert view._hero.get_orientation() is Gtk.Orientation.VERTICAL
        assert not view._trainer_rail
        assert view._adjustments_inline is equipment
    elif view.layout_family is LayoutFamily.LANDSCAPE:
        assert view._body.get_orientation() is Gtk.Orientation.HORIZONTAL
        assert view._hero.get_orientation() is Gtk.Orientation.VERTICAL
        assert view._trainer_rail is equipment
        if equipment:
            assert view._adjustments_slot.get_parent() is view._side_controls
            _, metrics = view.metrics.compute_bounds(view._body)
            _, controls = view._adjustments_slot.compute_bounds(view._body)
            assert metrics.get_x() + metrics.get_width() <= controls.get_x()
    elif equipment and view._workout_visible:
        assert view._quadrants_active
        assert view._hero.get_orientation() is Gtk.Orientation.HORIZONTAL
        assert view._readings.get_orientation() is Gtk.Orientation.HORIZONTAL
        assert not view._trainer_rail


def assert_reachable(widget, scroller):
    """Scroll enlarged secondary content into view without relocating its panels."""
    if widget.get_height() > scroller.get_height() or widget.get_width() > scroller.get_width():
        children = list(widget)
        if children:
            for child in children:
                assert_reachable(child, scroller)
            return
    success, bounds = widget.compute_bounds(scroller.get_child())
    assert success
    scroller.get_hadjustment().clamp_page(bounds.get_x(), bounds.get_x() + bounds.get_width())
    scroller.get_vadjustment().clamp_page(bounds.get_y(), bounds.get_y() + bounds.get_height())
    settle()
    assert_inside(widget, scroller)


@pytest.mark.parametrize("kind", ["trainer", "incline", "free"])
@pytest.mark.parametrize(
    ("panel_width", "panel_height"),
    [(720, 1440), (1440, 720), (1080, 1240), (1240, 1080)],
)
def test_zoom_preserves_actual_panel_positions_and_physical_sizes(
    app_stub,
    kind,
    panel_width,
    panel_height,
):
    """Verify real allocations, fonts, targets, and callbacks across fractional zoom."""
    view, callbacks = make_session(app_stub, kind)
    window = show_view(view, panel_width, panel_height, production_shell=True)
    placement = panel_placement(view)
    widgets = (view.btn_stop, view.btn_start, view.card_power.value, view.card_power.dot)
    reference_heights = [widget.compute_bounds(window)[1].get_height() for widget in widgets]
    try:
        for percent in (100, 115, 125, 150, 175, 200, 250, 300):
            zoom = percent / 100
            if zoom != 1:
                view._begin_stop_hold()
            window.set_default_size(round(panel_width / zoom), round(panel_height / zoom))
            settle()
            assert panel_placement(view) == placement
            assert_fixed_placement(view)
            assert view._stop_hold_started_at is None
            for widget, reference in zip(widgets, reference_heights, strict=True):
                assert_inside(widget, window)
                assert widget.compute_bounds(window)[1].get_height() * zoom == pytest.approx(
                    reference,
                    abs=2,
                )
            for tile in view._status_tiles:
                assert_inside(tile.value, view._scroller)
                assert_inside(tile.dot, view._scroller)
            assert view.timer_remaining.value.get_text() == "04:18"
            assert view._state is SessionState.RUNNING
            assert not callbacks.mock_calls
    finally:
        window.destroy()
        settle()


def test_native_popup_matches_the_scaled_interface(app_stub):
    """Scale native popup contents and retain their live selection callbacks."""
    del app_stub
    selected = Mock()
    button = Gtk.MenuButton(label="Options")
    choice = Gtk.Button(label="Select this option")
    choice.connect("clicked", selected)
    popup = Gtk.Popover(child=choice)
    button.set_popover(popup)
    window = show_view(button, 360, 720)
    allocations = ((720, 1440), (313, 626), (360, 720), (240, 480))
    try:
        for width, height in allocations:
            window.set_default_size(width, height)
            settle()
            button.popup()
            settle()
            scale = window.get_child().layout_frame.scale
            success, bounds = choice.compute_bounds(popup)
            assert success
            allocation = choice.get_allocation()
            assert bounds.get_width() == pytest.approx(allocation.width * scale, abs=1)
            assert bounds.get_height() == pytest.approx(allocation.height * scale, abs=1)
            assert (
                popup.get_surface().get_width() >= button.compute_bounds(window)[1].get_width() - 1
            )
            choice.emit("clicked")
            popup.popdown()
            settle()
        assert selected.call_count == len(allocations)
    finally:
        window.destroy()
        settle()


@pytest.mark.parametrize(("width", "height"), [(240, 480), (720, 1440)])
def test_popup_width_ignores_size_requests_set_before_interface_scaling(app_stub, width, height):
    """Keep identical popup content and anchors consistent despite native preset sizes."""
    del app_stub
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    buttons = []
    for requested_width in (-1, 360):
        button = Gtk.MenuButton(label="Options")
        popup = Gtk.Popover(child=Gtk.Button(label="Select this option"))
        popup.set_size_request(requested_width, -1)
        button.set_popover(popup)
        box.append(button)
        buttons.append(button)
    window = show_view(box, width, height)
    widths = []
    try:
        for button in buttons:
            button.popup()
            settle()
            popup = button.get_popover()
            assert popup.get_mapped()
            widths.append(popup.get_surface().get_width())
            button.popdown()
            settle()
        assert widths[0] == pytest.approx(widths[1], abs=1)
    finally:
        window.destroy()
        settle()


def test_popup_discovery_skips_hidden_pages_and_scales_them_when_shown(app_stub):
    """Keep hidden history-sized trees out of the recording update path."""
    del app_stub
    traversed = []

    class CountedPage(Gtk.Box):
        """Observe Python traversal without adding timing-sensitive assertions."""

        def __iter__(self):
            traversed.append(self)
            return super().__iter__()

    stack = Gtk.Stack()
    stack.add_named(Gtk.Label(label="Recording"), "tracker")
    hidden = CountedPage()
    button = Gtk.MenuButton(label="History choices")
    choice = Gtk.Button(label="Select this option")
    popup = Gtk.Popover(child=choice)
    button.set_popover(popup)
    hidden.append(button)
    stack.add_named(hidden, "history")
    window = show_view(stack, 240, 480)
    try:
        traversed.clear()
        window.get_child()._sync_popups()
        assert not traversed
        stack.set_visible_child_name("history")
        settle()
        button.popup()
        settle()
        assert traversed
        assert popup.get_mapped()
        success, bounds = choice.compute_bounds(popup)
        assert success
        assert bounds.get_height() == pytest.approx(
            choice.get_allocation().height * window.get_child().layout_frame.scale,
            abs=1,
        )
    finally:
        window.destroy()
        settle()


@pytest.mark.parametrize(("width", "height"), [(360, 720), (800, 1280)])
def test_portrait_free_chart_uses_available_space_without_an_empty_footer(
    app_stub,
    width,
    height,
):
    """Retain a readable expanding chart and one action row in portrait free sessions."""
    view, callbacks = make_session(app_stub, "free")
    window = show_view(view, width, height, production_shell=True)
    try:
        assert_inside(view.live_chart.canvas, view._scroller)
        assert_inside(view.btn_stop, window)
        assert_inside(view.btn_start, window)
        assert view.live_chart.canvas.get_height() > view._scroller.get_height() / 2
        assert view._footer.get_height() == view._controls.get_height()
        assert not view._workout_navigation.get_mapped()
        assert not callbacks.mock_calls
    finally:
        window.destroy()
        settle()


@pytest.mark.parametrize(
    ("width", "height", "family"),
    [(800, 1000, LayoutFamily.SQUARE), (1350, 1000, LayoutFamily.SQUARE)],
)
def test_first_allocation_obeys_exact_aspect_boundaries(app_stub, width, height, family):
    """Apply hysteresis only after an actual allocation establishes the layout."""
    view, _callbacks = make_session(app_stub, "trainer")
    window = show_view(view, width, height)
    try:
        assert view.layout_family is family
    finally:
        window.destroy()
        settle()


@pytest.mark.parametrize(("width", "height"), VIEWPORTS)
@pytest.mark.parametrize("kind", SESSION_KINDS)
def test_session_allocations(app_stub, width, height, kind):
    """Keep recording actions and essential workout readings within each viewport."""
    view, callbacks = make_session(app_stub, kind)
    window = show_view(view, width, height)
    try:
        assert window.get_width() == width
        assert window.get_height() == height
        assert_inside(view.btn_stop, window)
        assert_inside(view.btn_start, window)
        assert_inside(view._controls, window)
        assert view.btn_stop.get_height() >= MIN_TOUCH_HEIGHT
        overlay = view.btn_stop.get_child()
        stop_label = overlay.get_last_child()
        assert_inside(stop_label, overlay)
        assert stop_label.measure(Gtk.Orientation.HORIZONTAL, -1)[1] <= stop_label.get_width()
        assert view._body.get_width() <= view._scroller.get_width()
        if kind == "free":
            assert not view.step_position.get_mapped()
        if width >= MIN_COMPACT_WIDTH:
            # At normal text size the target, timers, metrics, and statuses
            # must all be visible together, including the tight Comet layout.
            essential = [view.metrics]
            if kind != "free":
                essential.extend(
                    [
                        view.gauge,
                        view.timer_remaining,
                        view.step_position,
                        view.title_label,
                        view.lbl_target,
                        view.lbl_next,
                    ],
                )
            for widget in essential:
                assert_inside(widget, view._scroller)
        assert not callbacks.mock_calls
        save_screenshot(window, f"{kind}-{width}x{height}")
    finally:
        window.destroy()
        settle()


@pytest.mark.parametrize("text_scale", [1.1, 1.15, 1.18, 1.2, 1.25, 1.35, 1.5])
@pytest.mark.parametrize(
    ("width", "height"),
    [(360, 648), (360, 720), (720, 310), (540, 620), (620, 540), (708, 617)],
)
def test_enlarged_text_keeps_actions_visible(app_stub, width, height, text_scale):
    """Keep actions pinned and enlarged content reachable without relocating panels."""
    settings = Gtk.Settings.get_default()
    original = settings.get_property("gtk-xft-dpi")
    view, callbacks = make_session(app_stub, "trainer")
    window = show_view(view, width, height)
    placement = panel_placement(view)
    try:
        settings.set_property("gtk-xft-dpi", round(96 * text_scale * 1024))
        settle()
        assert panel_placement(view) == placement
        assert_inside(view.btn_stop, window)
        assert_inside(view.btn_start, window)
        assert_reachable(view.metrics, view._scroller)
        assert_reachable(view.gauge, view._scroller)
        assert_reachable(view.step_position, view._scroller)
        overlay = view.btn_stop.get_child()
        assert_inside(overlay.get_last_child(), overlay)
        assert not callbacks.mock_calls
        save_screenshot(window, f"text{round(text_scale * 100)}-workout-{width}x{height}")
    finally:
        window.destroy()
        settings.set_property("gtk-xft-dpi", original)
        settle()


@pytest.mark.parametrize("kind", ["trainer", "incline", "free"])
def test_rotation_preserves_session_state_and_cancels_stop_hold(app_stub, kind):
    """Cancel pending stops on rotation while retaining session widgets and state."""
    view, callbacks = make_session(app_stub, kind)
    window = show_view(view, 360, 720, production_shell=True)
    timer, gauge = view.timer_remaining, view.gauge
    control = view.trainer_target_control or view.incline_control
    try:
        for width, height in (
            (720, 360),
            (540, 620),
            (620, 540),
            (432, 496),
            (496, 432),
            (240, 480),
            (480, 240),
            (800, 1280),
            (1280, 800),
            (900, 1600),
            (1600, 900),
            (360, 720),
        ):
            view._begin_stop_hold()
            window.set_default_size(width, height)
            settle()
            assert view._stop_hold_started_at is None
            assert view.timer_remaining is timer
            assert view.gauge is gauge
            assert (view.trainer_target_control or view.incline_control) is control
            assert timer.value.get_text() == "04:18"
            assert view._state is SessionState.RUNNING
            assert view._profile_ready
            assert view.step_position.value.get_text() == "5/15"
            assert_inside(view.metrics, view._scroller)
            if kind == "free":
                assert not view.live_chart.canvas.get_focusable()
            assert not callbacks.mock_calls
    finally:
        window.destroy()
        settle()


def test_missing_sensor_and_untargeted_step_clear_gauge_reading(app_stub):
    """Clear unavailable target readings while retaining accumulated distance."""
    view, _callbacks = make_session(app_stub, "trainer")
    assert view.gauge._available
    assert view.card_power.dot.get_text() == "● Live"
    assert view.card_power.dot.get_tooltip_text() == "Power sensor connected"
    view.set_statuses(SensorStatus())
    assert not view.gauge._available
    assert view.card_distance.value.get_text() == "4.28"
    assert view.card_power.value.get_text() == "—"
    assert view.card_power.dot.get_text() == "○ Dead"
    assert view.card_power.dot.get_tooltip_text() == "Power sensor not connected"
    view.set_statuses(SensorStatus(power=True))
    assert view.gauge._available
    view.apply_guidance(
        StepGuidance(
            domain=TargetDomain.NONE,
            low=0,
            mid=0,
            high=0,
            target_text="Recover",
            next_text="",
            pebble_kind=PebbleTargetKind.NONE,
        ),
        SimpleNamespace(),
    )
    assert not view.gauge._available
    assert view.gauge._subline == "No target"


@pytest.mark.parametrize(
    ("width", "height"),
    [(360, 650), (720, 310), (360, 380), (540, 590), (432, 496), (496, 432)],
)
def test_activity_setup_reflows_and_remains_accessible(app_stub, tmp_path, width, height):
    """Keep setup selections, environment icons, and the start action accessible."""
    del app_stub  # GTK initialization is shared with the session checks.
    start_free = Mock()
    view = ModeSelectView(tmp_path, tmp_path, start_free, Mock(), unit_system=UnitSystem.METRIC)
    window = show_view(view, width, height)
    try:
        assert view._setup.get_width() <= view.get_width()
        assert view._library.get_width() <= view.get_width()
        assert_inside(view._btn_start_free, window)
        assert_inside(view._btn_start_free, view)
        assert_inside(view._btn_start_free, view._setup_pane)
        view._btn_cycle.set_active(True)
        assert view._btn_cycle.get_active()
        assert not view._btn_run.get_active()
        assert all(isinstance(card, Gtk.ToggleButton) for card in view._env_cards.values())
        icons = {Environment.INDOOR: "🏠", Environment.OUTDOOR: "🌲", Environment.TRAINER: "⚡"}
        for environment, card in view._env_cards.items():
            assert isinstance(card._icon_lbl, Gtk.Label)
            assert card._icon_lbl.get_text() == icons[environment]
            assert_inside(card._icon_lbl, card)
            card.emit("clicked")
            assert card.get_active()
            assert view._selected_env is environment
            assert sum(item.get_active() for item in view._env_cards.values()) == 1
        view._btn_start_free.emit("clicked")
        start_free.assert_called_once_with(
            sport_type=SportTypesEnum.biking,
            environment=Environment.TRAINER,
        )
        settle()
        save_screenshot(window, f"setup-{width}x{height}")
    finally:
        window.destroy()
        settle()


@pytest.mark.parametrize(
    ("width", "height", "visible_rows"),
    [(540, 620, 5), (620, 540, 4), (432, 496, 5), (496, 432, 4)],
)
def test_comet_setup_leaves_room_for_workouts(app_stub, tmp_path, width, height, visible_rows):
    """Reserve visible workout rows beside usable setup controls on Comet layouts."""
    del app_stub
    workout_count = 6
    for index in range(workout_count):
        (tmp_path / f"{index}.json").write_text(
            json.dumps({"name": f"Workout {index + 1}", "steps": [{"duration": 1200}]}),
            encoding="utf-8",
        )
    view = ModeSelectView(tmp_path, tmp_path, Mock(), Mock(), unit_system=UnitSystem.METRIC)
    window = show_view(view, width, height, production_shell=True)
    try:
        assert_inside(view._btn_start_free, view._setup_pane)
        setup_scroll = view._setup_pane.get_first_child()
        for card in view._env_cards.values():
            assert_inside(card, setup_scroll)
            for badge in card._badge_box:
                assert_inside(badge, setup_scroll)
        library_scroll = view._library.get_last_child().get_child()
        rows_in_view = 0
        assert len(list(view._list)) == workout_count
        for row in view._list:
            success, bounds = row.compute_bounds(library_scroll)
            assert success
            if (
                bounds.get_y() >= 0
                and bounds.get_y() + bounds.get_height() <= library_scroll.get_height()
            ):
                rows_in_view += 1
        assert rows_in_view >= visible_rows
        save_screenshot(window, f"populated-setup-{width}x{height}")
    finally:
        window.destroy()
        settle()


@pytest.mark.parametrize(("width", "height"), [(240, 480), (480, 240)])
def test_librem_three_x_setup_keeps_selections_and_starts_visible(
    app_stub,
    tmp_path,
    width,
    height,
):
    """Fit populated setup and its start actions at Librem 5 three-times scaling."""
    del app_stub
    for index in range(6):
        (tmp_path / f"{index}.json").write_text(
            json.dumps({"name": f"Endurance workout {index + 1}", "steps": [{"duration": 1200}]}),
            encoding="utf-8",
        )
    start = Mock()
    view = ModeSelectView(tmp_path, tmp_path, start, Mock(), unit_system=UnitSystem.METRIC)
    window = show_view(view, width, height, production_shell=True)
    try:
        assert window.get_width() == width
        assert_inside(view, window)
        setup_scroll = view._setup.get_parent()
        assert window.get_child().layout_frame.family in (
            LayoutFamily.PORTRAIT,
            LayoutFamily.LANDSCAPE,
        )
        assert_inside(view._btn_run, setup_scroll)
        assert_inside(view._btn_cycle, setup_scroll)
        view._btn_cycle.set_active(True)
        settle()
        for card in view._env_cards.values():
            assert_inside(card, setup_scroll)
            for badge in card._badge_box:
                assert_inside(badge, setup_scroll)
            card.emit("clicked")
        settle()
        assert_inside(view._btn_start_free, window)
        assert_inside(view._btn_start_free, view._setup_pane)
        view._btn_start_free.emit("clicked")
        start.assert_called_once_with(
            sport_type=SportTypesEnum.biking,
            environment=Environment.TRAINER,
        )
        library_scroll = view._library.get_last_child().get_child()
        row = view._list.get_first_child()
        assert_inside(row, library_scroll)
        assert_inside(row.get_last_child(), library_scroll)
        if width > height:
            assert view._setup_pane.get_parent().get_orientation() is Gtk.Orientation.HORIZONTAL
        save_screenshot(window, f"librem-three-x-setup-{width}x{height}")
    finally:
        window.destroy()
        settle()


def test_navigation_away_cancels_stop_hold(app_stub):
    """Cancel a pending stop when its session view leaves the screen."""
    view, callbacks = make_session(app_stub, "workout")
    window = show_view(view, 360, 720)
    try:
        view._begin_stop_hold()
        view.set_visible(False)
        settle()
        assert view._stop_hold_started_at is None
        assert not callbacks.mock_calls
    finally:
        window.destroy()
        settle()


def test_session_font_handlers_disconnect_and_reconnect_on_remap(app_stub, monkeypatch):
    """Release global font callbacks while hidden and refresh on every remap."""
    updates = []
    original = SessionView._on_adjustments_font_changed

    def track_updates(view, *args):
        """Record delivery without replacing the font-measurement behavior."""
        updates.append(view)
        original(view, *args)

    monkeypatch.setattr(SessionView, "_on_adjustments_font_changed", track_updates)
    settings = Gtk.Settings.get_default()
    original_dpi = settings.get_property("gtk-xft-dpi")
    view, _callbacks = make_session(app_stub, "trainer")
    assert not view._settings_handler_ids
    window = show_view(view, 432, 496)
    stack = window.get_child().get_first_child().get_content()
    watched_settings = ("gtk-xft-dpi", "gtk-font-name")
    try:
        for _ in range(3):
            handlers = tuple(view._settings_handler_ids)
            assert len(handlers) == len(watched_settings)
            assert all(settings.handler_is_connected(handler) for handler in handlers)
            stack.set_visible_child_name("history")
            settle()
            assert not view._settings_handler_ids
            assert view._adjustments_layout_source_id is None
            assert not any(settings.handler_is_connected(handler) for handler in handlers)
            updates.clear()
            settings.set_property("gtk-xft-dpi", round(96 * 1024 * 1.5))
            for setting in watched_settings:
                settings.notify(setting)
            settle()
            assert not updates
            stack.set_visible_child_name("tracker")
            settle()
            settle()
            assert updates
            assert view._adjustments_inline
            for setting in watched_settings:
                updates.clear()
                settings.notify(setting)
                assert updates == [view]
                settle()
    finally:
        window.destroy()
        settle()
        updates.clear()
        settings.set_property("gtk-xft-dpi", original_dpi)
        settings.notify("gtk-font-name")
        settle()
        assert not view._settings_handler_ids
        assert not updates


def test_full_application_shell_fits_short_landscape(shell_app):
    """Fit the real shell around session readings, actions, and bottom navigation."""
    app = shell_app
    view, _callbacks = make_session(app, "workout")
    app.tracker.nav.push(Adw.NavigationPage.new(view, "Workout"))
    try:
        for width, height in (
            (360, 650),
            (720, 310),
            (540, 590),
            (1080, 1240),
            (432, 496),
            (496, 432),
            (240, 480),
            (480, 240),
        ):
            app.window.set_default_size(width, height)
            settle()
            assert app.window.get_surface().get_width() == width
            assert app.window.get_surface().get_height() == height
            assert_inside(app.toast_overlay, app.window)
            assert_inside(view.btn_stop, app.window)
            assert_inside(view.btn_start, app.window)
            assert_inside(view.metrics, view._scroller)
            navigation = app._navigation_bar
            assert navigation.get_reveal()
            assert_inside(navigation, app.window)
            assert app.header_revealer.get_child().get_title_widget() is None
            assert not app.header_revealer.get_visible()
            assert not app.header_revealer.get_child().get_mapped()
            _, actions = view._controls.compute_bounds(app.window)
            _, nav_bounds = navigation.compute_bounds(app.window)
            assert actions.get_y() + actions.get_height() <= nav_bounds.get_y()
            if navigation.has_css_class("roomy-navigation"):
                labels = [
                    label
                    for label in descendant_labels(navigation)
                    if label.get_mapped() and label.get_text() in {"Tracker", "History", "Settings"}
                ]
                assert {label.get_text() for label in labels} == {"Tracker", "History", "Settings"}
                metric_caption_size = (
                    view.card_speed.label.get_pango_context().get_font_description().get_size()
                )
                assert all(
                    label.get_pango_context().get_font_description().get_size()
                    >= metric_caption_size * 0.9
                    for label in labels
                )
            save_screenshot(app.window, f"shell-{width}x{height}")
    finally:
        view.set_visible(False)


@pytest.fixture
def shell_app(app_stub, monkeypatch, tmp_path):
    """Provide a native application with isolated settings and automatic cleanup."""
    from fitness_tracker.ui.app import FitnessAppUI  # noqa: PLC0415

    del app_stub
    settings = Gtk.Settings.get_default()
    animations = settings.get_property("gtk-enable-animations")
    settings.props.gtk_enable_animations = False
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    app = FitnessAppUI(test_mode=True)
    app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
    app.set_application_id(f"com.luigi311.fitness-tracker.test_{tmp_path.name}")
    app.register(None)
    app._build_ui()
    app._send_background_notification = Mock()
    app.window.set_decorated(False)
    app.window.present()
    try:
        yield app
    finally:
        app.window.destroy()
        app.jobs.shutdown()
        GLib.source_remove(app.tracker._status_timer_id)
        settings.set_property("gtk-enable-animations", animations)
        settle()


@pytest.mark.parametrize("page", ["tracker", "history", "settings"])
def test_application_navigation_stays_at_the_bottom_on_every_page(shell_app, page):
    """Keep complete application navigation at the bottom across pages and sizes."""
    shell_app.stack.set_visible_child_name(page)
    navigation = shell_app._navigation_bar
    for width, height in (
        (240, 480),
        (480, 240),
        (360, 720),
        (720, 360),
        (540, 620),
        (620, 540),
        (1080, 1240),
        (1240, 1080),
        (800, 1280),
        (1280, 800),
        (900, 1600),
        (1600, 900),
    ):
        shell_app.window.set_default_size(width, height)
        settle()
        assert shell_app.window.get_surface().get_width() == width
        assert shell_app.window.get_surface().get_height() == height
        assert navigation.get_reveal()
        assert_inside(navigation, shell_app.window)
        assert shell_app.header_revealer.get_child().get_title_widget() is None
        assert not shell_app.header_revealer.get_visible()
        assert not shell_app.header_revealer.get_child().get_mapped()
        _, bounds = navigation.compute_bounds(shell_app.window)
        assert bounds.get_y() > shell_app.window.get_height() / 2
        labels = [
            label
            for label in descendant_labels(navigation)
            if label.get_mapped() and label.get_text() in {"Tracker", "History", "Settings"}
        ]
        assert {label.get_text() for label in labels} == {"Tracker", "History", "Settings"}
        for label in labels:
            assert_inside(label, shell_app.window)


def test_real_application_setup_fits_librem_three_x(shell_app):
    """Keep populated setup usable in the real shell at Librem 5 three-times scaling."""
    app = shell_app
    for directory in (app.workouts_running_dir, app.workouts_cycling_dir):
        for index in range(6):
            payload = {"name": f"Endurance workout {index + 1}", "steps": [{"duration": 1200}]}
            (directory / f"{index}.json").write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
    view = app.tracker.mode_view
    view._btn_cycle.set_active(True)
    view.refresh()
    for width, height in ((240, 480), (480, 240), (240, 480)):
        app.window.set_default_size(width, height)
        settle()
        setup_scroll = view._setup.get_parent()
        assert_inside(view, app.window)
        for card in view._env_cards.values():
            assert_inside(card, setup_scroll)
        assert_inside(view._btn_start_free, app.window)
        assert_inside(view._list.get_first_child(), view._library.get_last_child().get_child())
        save_screenshot(app.window, f"real-librem-three-x-setup-{width}x{height}")


@pytest.mark.parametrize("submission_error", [None, DuplicateJobError, JobRunnerShutdownError])
@pytest.mark.parametrize("neutralization_fails", [False, True])
def test_finalization_notices_use_application_overlay_during_teardown(
    shell_app,
    submission_error,
    neutralization_fails,
):
    """Keep stop cleanup and retry notices alive after their session is removed."""
    app = shell_app
    tracker = app.tracker
    view, _callbacks = make_session(app, "trainer")
    tracker.session_view = view
    tracker._workout_session = object()
    tracker._active_environment = Environment.TRAINER
    tracker._session_state = SessionState.RUNNING
    tracker._start_requested = True
    tracker.nav.push(Adw.NavigationPage.new(view, "Workout"))
    settle()
    recorder = Mock(finalization_pending=True)
    recorder.begin_finalization.return_value = FinalizationClaim(
        FinalizationStatus.STARTED if submission_error else FinalizationStatus.ALREADY_RUNNING,
        7,
    )
    if neutralization_fails:
        recorder.neutralize_trainer.side_effect = RuntimeError("trainer disconnected")
    app.recorder = recorder
    app.pebble_bridge = Mock()
    for attribute in ("_timer_source_id", "_chart_timer_id", "_test_source"):
        setattr(tracker, attribute, GLib.timeout_add(60_000, lambda: True))
    try:
        with (
            patch.object(app.jobs, "submit", side_effect=submission_error),
            patch.object(app.toast_overlay, "add_toast") as app_toasts,
            patch.object(view, "add_toast") as session_toasts,
        ):
            tracker._stop_run_and_back()
            assert app_toasts.call_count == (2 if neutralization_fails else 1)
            session_toasts.assert_not_called()
            titles = [call.args[0].get_title() for call in app_toasts.call_args_list]
            expected = (
                "Activity finalization is pending"
                if submission_error
                else "Activity finalization is already running"
            )
            assert titles[0] == expected
        recorder.neutralize_trainer.assert_called_once_with()
        app.pebble_bridge.stop.assert_called_once_with(wait=False)
        if submission_error:
            recorder.abort_finalization.assert_called_once_with(7)
            assert app._pending_finalizations == [recorder]
        assert tracker.session_view is None
        assert tracker._workout_session is None
        assert tracker._active_environment is None
        assert tracker._session_state is None
        assert not tracker._start_requested
        assert tracker.nav.get_visible_page() is tracker.mode_page
        for attribute in ("_timer_source_id", "_chart_timer_id", "_test_source"):
            assert getattr(tracker, attribute) is None
    finally:
        app.recorder = None
        app.pebble_bridge = None
        app._pending_finalizations.clear()


def test_session_toasts_leave_recording_and_step_buttons_hit_testable(shell_app):
    """Keep session actions clickable while a notice is displayed above them."""
    app = shell_app
    view, _callbacks = make_session(app, "workout")
    app.tracker.session_view = view
    app.tracker.nav.push(Adw.NavigationPage.new(view, "Workout"))
    try:
        for width, height in ((360, 720), (720, 360), (432, 496), (240, 480), (480, 240)):
            app.window.set_default_size(width, height)
            settle()
            app.show_toast("Waiting for distance sensor")
            settle()
            settle()
            assert any(
                label.get_mapped() and label.get_text() == "Waiting for distance sensor"
                for label in descendant_labels(view._toast_overlay)
            )
            for button in (view.btn_stop, view.btn_start, view.btn_prev, view.btn_next):
                assert_inside(button, app.window)
                success, bounds = button.compute_bounds(app.window)
                assert success
                picked = app.window.pick(
                    bounds.get_x() + bounds.get_width() / 2,
                    bounds.get_y() + bounds.get_height() / 2,
                    Gtk.PickFlags.DEFAULT,
                )
                assert picked is button or (picked is not None and picked.is_ancestor(button))
            save_screenshot(app.window, f"safe-session-toast-{width}x{height}")
    finally:
        view.set_visible(False)


@pytest.mark.parametrize("kind", ["trainer", "incline"])
@pytest.mark.parametrize(
    ("width", "height"),
    [
        (360, 720),
        (320, 480),
        (360, 380),
        (720, 310),
        (540, 590),
        (432, 496),
        (496, 432),
        (240, 480),
        (480, 240),
    ],
)
def test_expanded_controls_keep_recording_actions_visible(app_stub, width, height, kind):
    """Keep expanded equipment adjustments reachable alongside recording actions."""
    view, callbacks = make_session(app_stub, kind)
    window = show_view(view, width, height, production_shell=True)
    control = view.trainer_target_control if kind == "trainer" else view.incline_control
    try:
        if not view._adjustments_inline:
            view._adjustments_expander.set_expanded(True)
        settle()
        assert_inside(view.btn_stop, window)
        assert_inside(view.btn_start, window)
        assert_inside(control._btn_up, view._scroller)
        assert_inside(control._btn_down, view._scroller)
        assert view._body.get_width() <= view._scroller.get_width()
        assert not callbacks.mock_calls
        save_screenshot(window, f"expanded-{kind}-{width}x{height}")
    finally:
        window.destroy()
        settle()


@pytest.mark.parametrize(
    ("width", "height", "inline"),
    [
        (1440, 900, True),
        (900, 1440, True),
        (1024, 768, True),
        (768, 1024, True),
        (360, 720, True),
        (720, 360, True),
        (1080, 1240, True),
        (1240, 1080, True),
        (540, 620, True),
        (620, 540, True),
        (432, 496, True),
        (496, 432, True),
    ],
)
def test_trainer_controls_are_directly_accessible_when_dashboard_fits(
    app_stub,
    width,
    height,
    inline,
):
    """Expose trainer controls inline only when the complete dashboard fits."""
    view, callbacks = make_session(app_stub, "trainer")
    window = show_view(view, width, height)
    control = view.trainer_target_control
    try:
        assert view._adjustments_inline is inline
        assert_inside(view.metrics, view._scroller)
        assert_inside(view.gauge, view._scroller)
        assert_inside(view.btn_stop, window)
        assert_inside(view.btn_start, window)
        if inline:
            assert not view._adjustments_expander.get_mapped()
            for widget in (control, control._btn_down, control._btn_up):
                assert_inside(widget, view._scroller)
            for button in control._mode_buttons.values():
                assert_inside(button, view._scroller)
        else:
            assert view._adjustments_expander.get_mapped()
            assert not view._adjustments_expander.get_expanded()
            assert (
                view._adjustments_expander.get_label_widget().get_height()
                >= view.btn_start.get_height()
            )
        assert not callbacks.mock_calls
        save_screenshot(window, f"adaptive-trainer-{width}x{height}")
    finally:
        window.destroy()
        settle()


def test_trainer_controls_preserve_bias_and_step_during_reflow(app_stub):
    """Preserve trainer bias, workout timing, and control identity during reflow."""
    view, callbacks = make_session(app_stub, "trainer")
    window = show_view(view, 540, 620)
    control = view.trainer_target_control
    try:
        control._btn_up.emit("clicked")
        callbacks.target.assert_called_once_with(TrainerMode.BIAS, 5.0)
        summary = control.summary
        callbacks.reset_mock()
        allocations = (
            (720, 360, True, True),
            (720, 310, True, True),
            (620, 540, True, False),
            (1240, 1080, True, False),
            (540, 620, True, False),
            (360, 720, True, False),
            (240, 480, True, False),
        )
        for width, height, inline, rail in allocations * 3:
            window.set_default_size(width, height)
            settle()
            assert view._adjustments_inline is inline
            assert view._trainer_rail is rail
            assert view.trainer_target_control is control
            assert control.summary == summary
            assert view.timer_remaining.value.get_text() == "04:18"
            assert view._state is SessionState.RUNNING
            assert_inside(view.metrics, view._scroller)
            assert_inside(view.gauge, view._scroller)
            assert not callbacks.mock_calls
    finally:
        window.destroy()
        settle()


@pytest.mark.parametrize(
    ("width", "height"),
    [(720, 1440), (626, 1252), (360, 720), (288, 576), (240, 480), (360, 610)],
)
@pytest.mark.parametrize("kind", ["trainer", "incline"])
def test_portrait_equipment_shows_controls_and_all_readings(app_stub, width, height, kind):
    """Keep equipment controls open, metrics compact, and step navigation in the footer."""
    view, callbacks = make_session(app_stub, kind)
    window = show_view(view, width, height, production_shell=True)
    control = view.trainer_target_control or view.incline_control
    try:
        assert view._adjustments_inline
        assert not view._adjustments_expander.get_mapped()
        assert view._workout_navigation.get_mapped()
        assert view._controls.get_orientation() is Gtk.Orientation.VERTICAL
        assert view.metrics.get_max_children_per_line() == 2  # noqa: PLR2004
        for widget in (
            view.title_label,
            view.gauge,
            view._step_details,
            view.lbl_target,
            view.lbl_next,
            view.metrics,
            control,
            *control.touch_buttons,
        ):
            assert_inside(widget, view._scroller)
        for button in (view.btn_prev, view.btn_next, view.btn_stop, view.btn_start):
            assert button.get_allocation().height >= MIN_TOUCH_HEIGHT
            _, bounds = button.compute_bounds(window)
            picked = window.pick(
                bounds.get_x() + bounds.get_width() / 2,
                bounds.get_y() + bounds.get_height() / 2,
                Gtk.PickFlags.DEFAULT,
            )
            assert picked is button or (picked is not None and picked.is_ancestor(button))
        assert view.btn_prev.get_parent() is view._workout_navigation
        assert view.btn_next.get_parent() is view._workout_navigation
        assert_inside(view._workout_navigation, view._footer)
        for tile in view._status_tiles:
            assert tile.dot.get_parent() is tile.value.get_parent()
            _, number = tile.value.compute_bounds(tile)
            _, source = tile.dot.compute_bounds(tile)
            assert number.get_x() + number.get_width() <= source.get_x()
            assert number.get_y() <= source.get_y()
            assert source.get_y() + source.get_height() <= number.get_y() + number.get_height()
            assert tile.value.measure(Gtk.Orientation.HORIZONTAL, -1)[1] <= tile.value.get_width()
        assert not callbacks.mock_calls
        control._btn_up.emit("clicked")
        if kind == "trainer":
            callbacks.target.assert_called_once_with(TrainerMode.BIAS, 5.0)
        else:
            callbacks.incline.assert_called_once_with(1.0)
        view.btn_next.emit("clicked")
        callbacks.next.assert_called_once_with()
        view.btn_prev.emit("clicked")
        callbacks.previous.assert_called_once_with()
        if kind == "trainer":
            for mode in (TrainerMode.POWER, TrainerMode.RESISTANCE, TrainerMode.BIAS):
                control._mode_buttons[mode].emit("clicked")
                settle()
                assert_inside(control, view._scroller)
                for button in control._mode_buttons.values():
                    assert button.has_css_class("trainer-mode")
                    assert_inside(button, view._scroller)
        save_screenshot(window, f"portrait-{kind}-inline-{width}x{height}")
    finally:
        window.destroy()
        settle()


@pytest.mark.parametrize("kind", ["trainer", "incline"])
@pytest.mark.parametrize("font_name", ["Sans 11", "Adwaita Sans 11"])
def test_oneplus_six_portrait_avoids_scroll_with_eleven_point_font(app_stub, kind, font_name):
    """Fit the OnePlus 6 preview's content area with a standard larger system font."""
    settings = Gtk.Settings.get_default()
    original_font = settings.get_property("gtk-font-name")
    view, callbacks = make_session(app_stub, kind)
    # The 360x760 preset reserves 32px above and 18px below the application.
    window = show_view(view, 360, 710, production_shell=True)
    try:
        settings.set_property("gtk-font-name", font_name)
        settle()
        settle()
        adjustment = view._scroller.get_vadjustment()
        assert adjustment.get_upper() <= adjustment.get_page_size()
        control = view.trainer_target_control or view.incline_control
        for widget in (view.gauge, view._step_details, view.metrics, control):
            assert_inside(widget, view._scroller)
        for button in (view.btn_prev, view.btn_next, view.btn_stop, view.btn_start):
            assert_inside(button, window)
        assert not callbacks.mock_calls
        save_screenshot(window, f"oneplus-six-portrait-{kind}")
    finally:
        window.destroy()
        settings.set_property("gtk-font-name", original_font)
        settle()


@pytest.mark.parametrize("connected", [True, False])
@pytest.mark.parametrize(
    ("width", "height"),
    [(360, 720), (720, 360), (540, 620), (620, 540), (240, 480), (480, 240)],
)
def test_source_placement_preserves_long_distance(app_stub, width, height, connected):
    """Keep Live/Dead labels beside portrait readings and underneath wider layouts."""
    view, callbacks = make_session(app_stub, "trainer")
    tile = view.card_distance
    tile.set_value("100.00")
    tile.set_status(connected=connected)
    window = show_view(view, width, height, production_shell=True)
    try:
        assert tile.value.get_text() == "100.00"
        assert tile.dot.get_text() == ("● Live" if connected else "○ Dead")
        assert tile.value.measure(Gtk.Orientation.HORIZONTAL, -1)[1] <= tile.value.get_width()
        assert tile.dot.measure(Gtk.Orientation.HORIZONTAL, -1)[1] <= tile.dot.get_width()
        assert_inside(tile.value, tile)
        assert_inside(tile.dot, tile)
        _, number = tile.value.compute_bounds(tile)
        _, source = tile.dot.compute_bounds(tile)
        if view.layout_family is LayoutFamily.PORTRAIT:
            assert number.get_x() + number.get_width() <= source.get_x()
            assert number.get_y() <= source.get_y()
            assert source.get_y() + source.get_height() <= number.get_y() + number.get_height()
        else:
            assert source.get_y() >= number.get_y() + number.get_height()
        assert not callbacks.mock_calls
    finally:
        window.destroy()
        settle()


@pytest.mark.parametrize(
    ("width", "height", "rail"),
    [
        (720, 360, True),
        (480, 240, True),
        (720, 310, True),
    ],
)
def test_phone_landscape_stacks_guidance_and_keeps_trainer_controls_on_the_right(
    app_stub,
    width,
    height,
    rail,
):
    """Stack phone landscape guidance and place trainer controls beside it when feasible."""
    view, callbacks = make_session(app_stub, "trainer")
    window = show_view(view, width, height, production_shell=True)
    try:
        assert window.get_width() == width
        assert window.get_height() == height
        assert view._trainer_rail
        _, gauge = view.gauge.compute_bounds(view._body)
        _, timers = view._timers.compute_bounds(view._body)
        _, controls = view._adjustments_slot.compute_bounds(view._body)
        _, metrics = view.metrics.compute_bounds(view._body)
        assert gauge.get_y() + gauge.get_height() <= timers.get_y()
        assert gauge.get_x() + gauge.get_width() <= metrics.get_x()
        assert controls.get_x() >= metrics.get_x()
        if rail:
            assert metrics.get_x() + metrics.get_width() <= controls.get_x()
        assert_inside(view.metrics, view._scroller)
        assert_inside(view.gauge, view._scroller)
        assert_inside(view._timers, view._scroller)
        assert_inside(view._adjustments_slot, view._scroller)
        for tile in view._status_tiles:
            assert not tile.value.get_layout().is_ellipsized()
        if view._adjustments_inline:
            for button in view.trainer_target_control.touch_buttons:
                assert_inside(button, view._scroller)
        for button in (view.btn_start, view.btn_stop):
            assert_inside(button, window)
        assert not callbacks.mock_calls
        save_screenshot(window, f"landscape-trainer-{width}x{height}")
    finally:
        window.destroy()
        settle()


@pytest.mark.parametrize("kind", ["trainer", "incline"])
@pytest.mark.parametrize(
    ("width", "height"),
    [(540, 620), (620, 540), (432, 496), (496, 432), (1080, 1240), (1240, 1080)],
)
def test_comet_workouts_use_four_visible_quadrants(app_stub, width, height, kind):
    """Arrange Comet guidance, timers, metrics, and equipment controls in quadrants."""
    view, callbacks = make_session(app_stub, kind)
    window = show_view(view, width, height, production_shell=True)
    try:
        assert window.get_width() == width
        assert window.get_height() == height
        assert view._quadrants_active
        assert view._adjustments_inline
        _, gauge = view.gauge.compute_bounds(view._body)
        _, details = view._step_details.compute_bounds(view._body)
        _, metrics = view.metrics.compute_bounds(view._body)
        _, controls = view._adjustments_slot.compute_bounds(view._body)
        assert gauge.get_x() + gauge.get_width() <= details.get_x()
        assert metrics.get_x() + metrics.get_width() <= controls.get_x()
        assert gauge.get_y() + gauge.get_height() <= metrics.get_y()
        assert details.get_y() + details.get_height() <= controls.get_y()
        for widget in (view.gauge, view._step_details, view.metrics, view._adjustments_slot):
            assert_inside(widget, view._scroller)
        for tile in view._status_tiles:
            assert not tile.value.get_layout().is_ellipsized()
            assert_inside(tile.value, view._scroller)
            assert_inside(tile.dot, view._scroller)
        control = view.trainer_target_control or view.incline_control
        for button in control.touch_buttons:
            assert_inside(button, view._scroller)
        assert view.step_position.value.get_text() == "5/15"
        assert_inside(view.btn_stop, window)
        assert_inside(view.btn_start, window)
        assert not callbacks.mock_calls
        save_screenshot(window, f"quadrants-{kind}-{width}x{height}")
    finally:
        window.destroy()
        settle()


@pytest.mark.parametrize("kind", ["trainer", "incline"])
@pytest.mark.parametrize(("width", "height"), [(1600, 900), (1280, 800), (1024, 768)])
def test_large_workouts_use_their_aspect_arrangement(app_stub, width, height, kind):
    """Use the same aspect-defined placement on a desktop and a handheld window."""
    view, callbacks = make_session(app_stub, kind)
    window = show_view(view, width, height, production_shell=True)
    try:
        assert_fixed_placement(view)
        assert_inside(view.gauge, view._scroller)
        assert_inside(view.metrics, view._scroller)
        assert_inside(view._adjustments_slot, view._scroller)
        assert_inside(view.btn_stop, window)
        assert_inside(view.btn_start, window)
        assert not callbacks.mock_calls
        save_screenshot(window, f"large-landscape-{kind}-{width}x{height}")
    finally:
        window.destroy()
        settle()


@pytest.mark.parametrize("kind", ["workout", "trainer", "incline", "free"])
@pytest.mark.parametrize(("width", "height"), [(800, 1280), (768, 1024), (900, 1600)])
def test_large_portrait_sessions_stack_the_sections(app_stub, width, height, kind):
    """Stack guidance, metrics, and equipment controls on large portrait allocations."""
    view, callbacks = make_session(app_stub, kind)
    window = show_view(view, width, height, production_shell=True)
    try:
        assert view.layout_family is LayoutFamily.PORTRAIT
        assert not view._quadrants_active
        assert view._body.get_orientation() is Gtk.Orientation.VERTICAL
        _, metrics = view.metrics.compute_bounds(view._body)
        if kind == "free":
            assert_inside(view.timer, view._scroller)
            _, timer = view.timer.compute_bounds(view._body)
            assert timer.get_y() + timer.get_height() <= metrics.get_y()
            _, chart = view._live_chart_frame.compute_bounds(view._body)
            assert metrics.get_y() + metrics.get_height() <= chart.get_y()
        else:
            _, gauge = view.gauge.compute_bounds(view._body)
            _, details = view._step_details.compute_bounds(view._body)
            assert gauge.get_y() + gauge.get_height() <= details.get_y()
            assert details.get_y() + details.get_height() <= metrics.get_y()
            assert view.gauge.get_height() < view._scroller.get_height() / 2
            assert_inside(view.gauge, view._scroller)
            assert_inside(view._step_details, view._scroller)
        if view.capabilities.incline or view.capabilities.trainer_targets:
            _, controls = view._adjustments_slot.compute_bounds(view._body)
            assert metrics.get_y() + metrics.get_height() <= controls.get_y()
            assert view._adjustments_inline
            assert_inside(view._adjustments_slot, view._scroller)
        for tile in view._status_tiles:
            assert_inside(tile.value, view._scroller)
            assert_inside(tile.dot, view._scroller)
        assert_inside(view.btn_stop, window)
        assert_inside(view.btn_start, window)
        assert not callbacks.mock_calls
        save_screenshot(window, f"large-portrait-{kind}-{width}x{height}")
    finally:
        window.destroy()
        settle()


@pytest.mark.parametrize(("width", "height"), [(620, 540), (496, 432), (1240, 1080)])
def test_landscape_workout_to_free_transition_restores_the_chart_layout(app_stub, width, height):
    """Restore the free-session chart layout without replacing trainer controls."""
    settings = Gtk.Settings.get_default()
    animations = settings.get_property("gtk-enable-animations")
    settings.props.gtk_enable_animations = False
    view, callbacks = make_session(app_stub, "trainer")
    window = show_view(view, width, height, production_shell=True)
    control = view.trainer_target_control
    try:
        for workout in (False, True, False, True):
            view.set_workout_visible(visible=workout)
            settle()
            assert view.trainer_target_control is control
            assert view._state is SessionState.RUNNING
            assert view.step_position.value.get_text() == "5/15"
            assert view._live_chart_frame.get_mapped() is not workout
            assert view.gauge.get_mapped() is workout
            assert_inside(view.metrics, view._scroller)
            assert_inside(view.btn_start, window)
            assert_inside(view.btn_stop, window)
            if not workout:
                assert not view._side_controls.get_visible()
                assert view._body.get_orientation() is Gtk.Orientation.VERTICAL
            assert not callbacks.mock_calls
    finally:
        window.destroy()
        settings.set_property("gtk-enable-animations", animations)
        settle()


def test_trainer_disclosure_adapts_to_text_size_without_resize(app_stub):
    """Keep trainer placement stable while larger text makes content scrollable."""
    settings = Gtk.Settings.get_default()
    original_dpi = settings.get_property("gtk-xft-dpi")
    view, callbacks = make_session(app_stub, "trainer")
    window = show_view(view, 432, 496)
    placement = panel_placement(view)
    try:
        for text_scale in (1.5, 1.0):
            settings.set_property("gtk-xft-dpi", round(96 * text_scale * 1024))
            settle()
            assert panel_placement(view) == placement
            assert view._adjustments_inline
            assert_reachable(view.trainer_target_control._btn_up, view._scroller)
            assert_inside(view.btn_stop, window)
            assert not callbacks.mock_calls
    finally:
        window.destroy()
        settings.set_property("gtk-xft-dpi", original_dpi)
        settle()


def test_trainer_mode_focus_survives_text_and_zoom_reflow(app_stub):
    """Keep keyboard activation on the same mode through font and zoom changes."""
    settings = Gtk.Settings.get_default()
    original_dpi = settings.get_property("gtk-xft-dpi")
    settings.set_property("gtk-xft-dpi", 96 * 1024)
    view, callbacks = make_session(app_stub, "trainer")
    window = show_view(view, 360, 720, production_shell=True)
    control = view.trainer_target_control
    button = control._mode_buttons[TrainerMode.POWER]
    portrait_columns, enlarged_columns = 3, 2
    try:
        assert button.grab_focus()
        assert window.get_focus() is button
        assert control._mode_columns == portrait_columns
        settings.set_property("gtk-xft-dpi", round(1.5 * 96 * 1024))
        settle()
        assert control._mode_columns == enlarged_columns
        assert window.get_focus() is button
        window.set_default_size(720, 1440)
        settle()
        assert window.get_focus() is button
        assert not callbacks.mock_calls
        assert button.activate()
        for _ in range(5):
            settle()
            if callbacks.target.called:
                break
        callbacks.target.assert_called_once_with(TrainerMode.POWER, 100.0)
    finally:
        window.destroy()
        settings.set_property("gtk-xft-dpi", original_dpi)
        settle()


@pytest.mark.parametrize("kind", SESSION_KINDS)
@pytest.mark.parametrize(
    ("width", "height", "scale", "reference_width", "reference_height"),
    SMALL_DENSITIES,
)
def test_high_scale_devices_keep_the_dashboard_visible(
    *,
    app_stub,
    kind,
    width,
    height,
    scale,
    reference_width,
    reference_height,
):
    """Keep live readings and proportionate actions visible on high-scale displays."""
    del reference_width, reference_height
    view, callbacks = make_session(app_stub, kind)
    window = show_view(view, width, height, production_shell=True)
    try:
        assert window.get_width() == width
        assert window.get_height() == height
        assert view._body.get_width() <= view._scroller.get_width()
        assert_inside(view.metrics, view._scroller)
        for tile in view._status_tiles:
            assert_inside(tile.value, view._scroller)
            assert_inside(tile.dot, view._scroller)
        if kind != "free":
            assert_inside(view.gauge, view._scroller)
            assert_inside(view.timer_remaining, view._scroller)
            assert_inside(view.step_position, view._scroller)
        for button in (view.btn_start, view.btn_stop):
            assert_inside(button, window)
            assert button.compute_bounds(window)[1].get_height() == pytest.approx(48 * scale, abs=2)
        overlay = view.btn_stop.get_child()
        assert_inside(overlay.get_last_child(), overlay)
        if view.trainer_target_control and view._adjustments_inline:
            for button in view.trainer_target_control.touch_buttons:
                assert_inside(button, view._scroller)
        assert not callbacks.mock_calls
        save_screenshot(window, f"high-scale-{kind}-{width}x{height}")
    finally:
        window.destroy()
        settle()


@pytest.mark.parametrize(
    ("width", "height", "scale", "reference_width", "reference_height"),
    SMALL_DENSITIES,
)
def test_high_scale_text_matches_the_two_x_reference(
    *,
    app_stub,
    width,
    height,
    scale,
    reference_width,
    reference_height,
):
    """Scale actual painted labels uniformly while retaining reference font metrics."""
    view, callbacks = make_session(app_stub, "trainer")
    window = show_view(view, reference_width, reference_height, production_shell=True)
    labels = (
        view.lbl_target,
        view.timer_elapsed.value,
        view.timer_remaining.value,
        view.step_position.value,
        view.card_power.label,
        view.card_power.value,
        view.card_power.unit,
        view.card_power.dot,
        view.btn_start.get_child(),
        view.btn_stop.get_child().get_last_child(),
    )
    fonts = [label.get_pango_context().get_font_description().get_size() for label in labels]
    bounds = [label.compute_bounds(window)[1].get_height() for label in labels]
    placement = panel_placement(view)
    try:
        window.set_default_size(width, height)
        settle()
        assert panel_placement(view) == placement
        for label, font, reference in zip(labels, fonts, bounds, strict=True):
            assert label.get_pango_context().get_font_description().get_size() == font
            assert label.compute_bounds(window)[1].get_height() == pytest.approx(
                reference * scale,
                abs=1,
            )
        assert_inside(view.metrics, view._scroller)
        assert view._state is SessionState.RUNNING
        assert view.timer_remaining.value.get_text() == "04:18"
        window.set_default_size(reference_width, reference_height)
        settle()
        for label, reference in zip(labels, bounds, strict=True):
            assert label.compute_bounds(window)[1].get_height() == pytest.approx(reference, abs=1)
        assert not callbacks.mock_calls
    finally:
        window.destroy()
        settle()


@pytest.mark.parametrize("kind", ["trainer", "free"])
@pytest.mark.parametrize(
    ("width", "height", "scale", "reference_width", "reference_height"),
    SMALL_DENSITIES,
)
def test_high_scale_large_text_keeps_actions_and_scrolling_usable(
    *,
    app_stub,
    kind,
    width,
    height,
    scale,
    reference_width,
    reference_height,
):
    """Keep recording actions visible and metrics reachable with dense enlarged text."""
    del scale, reference_width, reference_height
    settings = Gtk.Settings.get_default()
    original_dpi = settings.get_property("gtk-xft-dpi")
    settings.set_property("gtk-xft-dpi", round(96 * 1.5 * 1024))
    view, callbacks = make_session(app_stub, kind)
    window = show_view(view, width, height, production_shell=True)
    try:
        assert view._body.get_width() <= view._scroller.get_width()
        assert_inside(view.btn_start, window)
        assert_inside(view.btn_stop, window)
        last_tile = list(view.metrics)[-1]
        success, bounds = last_tile.compute_bounds(view._body)
        assert success
        view._scroller.get_vadjustment().clamp_page(
            bounds.get_y(),
            bounds.get_y() + bounds.get_height(),
        )
        settle()
        assert_inside(last_tile, view._scroller)
        assert not callbacks.mock_calls
        save_screenshot(window, f"high-scale-text150-{kind}-{width}x{height}")
    finally:
        window.destroy()
        settings.set_property("gtk-xft-dpi", original_dpi)
        settle()


@pytest.mark.parametrize(("width", "height"), [(540, 620), (620, 540)])
def test_comet_one_x_matches_two_x_text_readability(app_stub, width, height):
    """Match painted text and controls across Comet zoom without changing placement."""
    view, callbacks = make_session(app_stub, "trainer")
    window = show_view(view, width, height, production_shell=True)
    labels = (
        view.lbl_target,
        view.timer_elapsed.value,
        view.step_position.value,
        view.card_power.label,
        view.card_power.value,
        view.card_power.unit,
        view.card_power.dot,
        view.trainer_target_control._lbl_value,
        view.btn_start.get_child(),
        view.btn_stop.get_child().get_last_child(),
    )
    heights = [label.compute_bounds(window)[1].get_height() for label in labels]
    button_height = view.btn_start.compute_bounds(window)[1].get_height()
    placement = panel_placement(view)
    try:
        window.set_default_size(width * 2, height * 2)
        settle()
        assert panel_placement(view) == placement
        for label, reference in zip(labels, heights, strict=True):
            assert label.compute_bounds(window)[1].get_height() == pytest.approx(
                reference * 2,
                abs=1,
            )
        assert view.btn_start.compute_bounds(window)[1].get_height() == pytest.approx(
            button_height * 2,
        )
        assert_inside(view.metrics, view._scroller)
        assert_inside(view.gauge, view._scroller)
        assert not callbacks.mock_calls
        save_screenshot(window, f"readable-trainer-{width * 2}x{height * 2}")
        window.set_default_size(width, height)
        settle()
        assert panel_placement(view) == placement
        assert view.btn_start.compute_bounds(window)[1].get_height() == pytest.approx(button_height)
    finally:
        window.destroy()
        settle()


@pytest.mark.parametrize(
    ("width", "height", "scale"),
    [
        (540, 620, 1),
        (1080, 1240, 2),
        (1240, 1080, 2),
        (432, 496, 0.8),
        (496, 432, 0.8),
        (240, 480, 2 / 3),
        (480, 240, 2 / 3),
    ],
)
def test_comet_chart_labels_match_dashboard_density(app_stub, width, height, scale):
    """Scale live-chart labels with dashboard density without clipping the axes."""
    view, callbacks = make_session(app_stub, "free")
    window = show_view(view, width, height, production_shell=True)
    chart = view.live_chart
    try:
        chart.canvas.draw()
        settle()
        label = chart.hr_axes.xaxis.label
        assert label.get_fontsize() == CHART_REFERENCE_FONT_SIZE
        surface = window.get_surface()
        native_scale = (
            surface.get_scale() if hasattr(surface, "get_scale") else window.get_scale_factor()
        )
        assert chart.canvas.device_pixel_ratio == pytest.approx(scale * native_scale, rel=0.01)
        for tick in chart.hr_axes.get_xticklabels():
            assert tick.get_fontsize() == CHART_REFERENCE_FONT_SIZE
        bounds = label.get_window_extent(chart.canvas.get_renderer())
        assert bounds.x0 >= 0
        assert bounds.y0 >= 0
        assert bounds.x1 <= chart.figure.bbox.width
        assert bounds.y1 <= chart.figure.bbox.height
        assert_inside(view.metrics, view._scroller)
        assert_inside(view.btn_stop, window)
        assert not callbacks.mock_calls
        save_screenshot(window, f"readable-free-{width}x{height}")
    finally:
        window.destroy()
        settle()


def test_light_theme_keeps_metric_values_and_gauge_visible(app_stub):
    """Keep primary workout readings visible in the light appearance."""
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
    view, _callbacks = make_session(app_stub, "workout")
    window = show_view(view, 540, 620)
    try:
        assert_inside(view.metrics, view._scroller)
        assert_inside(view.gauge, view._scroller)
        save_screenshot(window, "light-workout-540x620")
    finally:
        window.destroy()
        settle()


@pytest.mark.parametrize("text_scale", [1.0, 1.5])
@pytest.mark.parametrize(
    ("width", "height", "label_scale"),
    [
        (110, 60, 1),
        (140, 80, 1),
        (160, 125, 1),
        (340, 240, 2),
        (520, 500, 2),
        (73, 53, 2 / 3),
        (128, 100, 0.8),
        (230, 107, 2 / 3),
    ],
)
@pytest.mark.parametrize(
    ("headline", "subline"),
    [
        ("12:59 /mi", "Target: 12:59 /mi"),
        ("2000 W", "Target: 2000 W"),
        ("199 bpm", "Target: 199 bpm"),
    ],
)
def test_gauge_text_stays_clear_of_arc_and_other_line(
    *,
    app_stub,
    monkeypatch,
    width,
    height,
    label_scale,
    text_scale,
    headline,
    subline,
):
    """Keep fitted gauge labels separate, inside the arc, and centered as a group."""
    del app_stub
    settings = Gtk.Settings.get_default()
    original_dpi = settings.get_property("gtk-xft-dpi")
    settings.set_property("gtk-xft-dpi", round(96 * text_scale * 1024))
    gauge = session_controls_module.TargetGauge()
    gauge.label_scale = label_scale
    gauge.size_scale = 0.9 if label_scale <= 1 else 1
    gauge.set_content_width(width)
    gauge.set_content_height(height)
    gauge.set_state(
        value=180,
        target_lo=190,
        target_mid=200,
        target_hi=210,
        headline=headline,
        subline=subline,
    )
    gauge.set_available(True)
    window = Gtk.Window(decorated=False)
    window.set_default_size(width, height)
    window.set_child(gauge)
    window.present()
    settle()
    surface = cairo.ImageSurface(cairo.FORMAT_A8, width, height)
    context = cairo.Context(surface)
    labels = []
    band = {}
    draw_band = gauge._draw_target_band
    show_layout = session_controls_module.PangoCairo.show_layout

    def capture_band(ctx, **geometry):
        """Record target-band geometry while preserving the actual drawing."""
        band.update(geometry)
        draw_band(ctx, **geometry)

    def capture_label(ctx, layout):
        """Record painted text bounds while preserving the actual label rendering."""
        x, y = ctx.get_current_point()
        ink, _logical = layout.get_pixel_extents()
        labels.append((x + ink.x, y + ink.y, ink.width, ink.height))
        show_layout(ctx, layout)

    monkeypatch.setattr(gauge, "_draw_target_band", capture_band)
    monkeypatch.setattr(session_controls_module.PangoCairo, "show_layout", capture_label)
    try:
        gauge._on_draw(gauge, context, width, height)
        assert len(labels) == GAUGE_LABEL_COUNT
        inner_edge = band["radius"] - band["bar_width"] / 2
        for x, y, label_width, label_height in labels:
            for px in (x, x + label_width):
                for py in (y, y + label_height):
                    assert math.hypot(px - band["center_x"], py - band["center_y"]) < inner_edge
        assert labels[0][1] + labels[0][3] < labels[1][1]
        assert_gauge_centered(gauge_ink_bounds(surface, width, height), width, height)
    finally:
        window.destroy()
        settings.set_property("gtk-xft-dpi", original_dpi)
        settle()


@pytest.mark.parametrize("available", [True, False])
@pytest.mark.parametrize(
    ("width", "height"),
    [
        (360, 720),
        (720, 360),
        (432, 496),
        (240, 480),
        (1080, 1240),
        (1440, 900),
    ],
)
def test_gauge_is_centered_at_its_reference_size(
    app_stub,
    width,
    height,
    available,
):
    """Keep the same centered gauge proportions throughout interface scaling."""
    view, callbacks = make_session(app_stub, "trainer")
    window = show_view(view, width, height, production_shell=True)
    gauge = view.gauge
    gauge.set_available(available)
    gauge_width, gauge_height = gauge.get_width(), gauge.get_height()
    try:
        surface = cairo.ImageSurface(cairo.FORMAT_A8, gauge_width, gauge_height)
        gauge._on_draw(gauge, cairo.Context(surface), gauge_width, gauge_height)
        bounds = gauge_ink_bounds(surface, gauge_width, gauge_height)
        assert_gauge_centered(bounds, gauge_width, gauge_height)
        gauge.size_scale = 1
        reference = cairo.ImageSurface(cairo.FORMAT_A8, gauge_width, gauge_height)
        gauge._on_draw(gauge, cairo.Context(reference), gauge_width, gauge_height)
        reference_bounds = gauge_ink_bounds(reference, gauge_width, gauge_height)
        assert bounds[2] - bounds[0] <= (reference_bounds[2] - reference_bounds[0]) * 0.94
        assert bounds[3] - bounds[1] < reference_bounds[3] - reference_bounds[1]
        assert not callbacks.mock_calls
    finally:
        window.destroy()
        settle()


@pytest.mark.parametrize("text_scale", [1.0, 1.5])
@pytest.mark.parametrize(
    ("width", "height"),
    [
        (320, 480),
        (360, 720),
        (720, 310),
        (540, 620),
        (1080, 1240),
        (1240, 1080),
        (432, 496),
        (496, 432),
        (240, 480),
        (480, 240),
    ],
)
def test_stop_fill_is_vertically_centered(app_stub, width, height, text_scale):
    """Center stop-hold progress vertically across densities and text sizes."""
    settings = Gtk.Settings.get_default()
    original_dpi = settings.get_property("gtk-xft-dpi")
    settings.set_property("gtk-xft-dpi", round(96 * text_scale * 1024))
    view, _callbacks = make_session(app_stub, "workout")
    window = show_view(view, width, height, production_shell=True)
    try:
        view._stop_progress.set_fraction(0.5)
        settle()
        overlay = view.btn_stop.get_child()
        trough = view._stop_progress.get_first_child()
        fill = trough.get_first_child()
        for widget in (trough, fill):
            success, bounds = widget.compute_bounds(overlay)
            assert success
            assert bounds.get_y() + bounds.get_height() / 2 == pytest.approx(
                overlay.get_height() / 2,
                abs=1,
            )
        save_screenshot(window, f"stop-half-text{round(text_scale * 100)}-{width}x{height}")
    finally:
        window.destroy()
        settings.set_property("gtk-xft-dpi", original_dpi)
        settle()


@pytest.mark.parametrize("dark", [False, True])
def test_stop_fill_and_label_have_contrast_in_both_themes(app_stub, tmp_path, dark):
    """Maintain visible stop progress and readable labels in light and dark themes."""
    style = Adw.StyleManager.get_default()
    style.set_color_scheme(
        Adw.ColorScheme.FORCE_DARK if dark else Adw.ColorScheme.FORCE_LIGHT,
    )
    view, _callbacks = make_session(app_stub, "workout")
    window = show_view(view, 540, 620)
    try:
        view._stop_progress.set_fraction(0.5)
        settle()
        button = view.btn_stop
        width, height = button.get_width(), button.get_height()
        snapshot = Gtk.Snapshot()
        Gtk.WidgetPaintable.new(button).snapshot(snapshot, width, height)
        texture = window.get_renderer().render_texture(
            snapshot.to_node(),
            Graphene.Rect().init(0, 0, width, height),
        )
        path = tmp_path / "stop-contrast.png"
        texture.save_to_png(str(path))
        surface = cairo.ImageSurface.create_from_png(str(path))
        pixels = np.frombuffer(surface.get_data(), dtype=np.uint8).reshape(
            height,
            surface.get_stride() // 4,
            4,
        )
        success, bounds = view._stop_progress.get_first_child().compute_bounds(button)
        assert success
        y = height // 2
        fill = pixels[y, round(bounds.get_x()) + 20, :3][::-1] / 255
        unfilled = pixels[y, round(bounds.get_x() + bounds.get_width()) - 20, :3][::-1] / 255
        label = button.get_child().get_last_child().get_style_context().get_color()

        def luminance(rgb):
            channels = [
                c / 12.92 if c <= SRGB_LINEAR_THRESHOLD else ((c + 0.055) / 1.055) ** 2.4
                for c in rgb
            ]
            return sum(
                c * weight for c, weight in zip(channels, (0.2126, 0.7152, 0.0722), strict=True)
            )

        def contrast(first, second):
            values = sorted((luminance(first), luminance(second)))
            return (values[1] + 0.05) / (values[0] + 0.05)

        assert contrast(fill, unfilled) >= MIN_FILL_CONTRAST
        for background in (fill, unfilled):
            assert contrast((label.red, label.green, label.blue), background) >= MIN_TEXT_CONTRAST
        save_screenshot(window, f"stop-contrast-{'dark' if dark else 'light'}")
    finally:
        window.destroy()
        style.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        settle()


@pytest.fixture
def x_pointer():
    """Provide optional pointer injection restricted to an isolated X display."""
    if os.environ.get("FITNESS_TRACKER_XINPUT_TESTS") != "1":
        pytest.skip("Opt in to pointer injection on an isolated X server")
    try:
        x11 = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        xtest = ctypes.CDLL(ctypes.util.find_library("Xtst") or "libXtst.so.6")
    except OSError:
        pytest.skip("X11/XTEST libraries are unavailable")
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    x11.XDefaultRootWindow.restype = ctypes.c_ulong
    x11.XTranslateCoordinates.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_ulong),
    ]
    xtest.XTestFakeMotionEvent.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_ulong,
    ]
    xtest.XTestFakeButtonEvent.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_int,
        ctypes.c_ulong,
    ]
    display = x11.XOpenDisplay(os.environ["DISPLAY"].encode())
    assert display

    def move(x, y):
        """Move the pointer to coordinates on the isolated display."""
        xtest.XTestFakeMotionEvent(display, -1, round(x), round(y), 0)
        x11.XSync(display, 0)

    def button(*, pressed):
        """Inject a primary-button press or release on the isolated display."""
        xtest.XTestFakeButtonEvent(display, 1, pressed, 0)
        x11.XSync(display, 0)

    def origin(window):
        """Return the native window origin in root-window coordinates."""
        x, y, child = ctypes.c_int(), ctypes.c_int(), ctypes.c_ulong()
        assert x11.XTranslateCoordinates(
            display,
            window.get_surface().get_xid(),
            x11.XDefaultRootWindow(display),
            0,
            0,
            ctypes.byref(x),
            ctypes.byref(y),
            ctypes.byref(child),
        )
        return x.value, y.value

    try:
        yield SimpleNamespace(move=move, button=button, origin=origin)
    finally:
        button(pressed=False)
        x11.XCloseDisplay(display)


@pytest.mark.parametrize(("width", "height"), [(240, 480), (360, 720), (720, 1440)])
def test_real_pointer_dragging_off_stop_cancels_until_a_fresh_press(
    app_stub,
    x_pointer,
    width,
    height,
):
    """Cancel a dragged-off stop hold and require a fresh press before stopping."""
    view, callbacks = make_session(app_stub, "workout")
    window = show_view(view, width, height, production_shell=True)
    try:
        root_x, root_y = x_pointer.origin(window)
        success, bounds = view.btn_stop.compute_bounds(window)
        assert success
        x = root_x + bounds.get_x() + bounds.get_width() / 2
        y = root_y + bounds.get_y() + bounds.get_height() / 2
        x_pointer.move(x, y)
        settle()
        x_pointer.button(pressed=True)
        settle()
        assert view._stop_hold_started_at is not None
        x_pointer.move(x, y - 250)
        settle()
        assert view._stop_hold_started_at is None
        # Returning while still pressed must not restart the cancelled hold.
        x_pointer.move(x, y)
        for _ in range(14):
            settle()
        callbacks.stop.assert_not_called()
        x_pointer.button(pressed=False)
        settle()
        x_pointer.button(pressed=True)
        for _ in range(14):
            settle()
        callbacks.stop.assert_called_once_with()
    finally:
        x_pointer.button(pressed=False)
        window.destroy()
        settle()


@pytest.mark.parametrize("kind", ["dropdown", "combo"])
@pytest.mark.parametrize(("width", "height"), [(240, 480), (720, 1440)])
def test_real_pointer_scaled_gtk_choices(app_stub, x_pointer, kind, width, height):
    """Preserve GTK-owned popup structure and native pointer selection at each scale."""
    del app_stub
    choices = ["First option", "Second option", "Third option"]
    if kind == "dropdown":
        control = Gtk.DropDown.new_from_strings(choices)
    else:
        control = Gtk.ComboBoxText()
        for choice in choices:
            control.append_text(choice)
        control.set_active(0)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.append(control)
    window = show_view(box, width, height)
    popups = list(descendants(control, Gtk.Popover))
    original_children = [popup.get_child() for popup in popups]
    try:
        root_x, root_y = x_pointer.origin(window)
        _, bounds = control.compute_bounds(window)
        x_pointer.move(
            root_x + bounds.get_x() + bounds.get_width() / 2,
            root_y + bounds.get_y() + bounds.get_height() / 2,
        )
        x_pointer.button(pressed=True)
        x_pointer.button(pressed=False)
        settle()
        assert [popup.get_child() for popup in popups] == original_children
        popup = next(popup for popup in popups if popup.get_mapped())
        popup_x, popup_y = x_pointer.origin(popup)
        if kind == "dropdown":
            label = next(
                label
                for label in descendant_labels(popup)
                if label.get_text() == choices[1] and label.get_mapped()
            )
            _, bounds = label.compute_bounds(popup)
            scale = window.get_child().layout_frame.scale
            assert bounds.get_height() == pytest.approx(label.get_height() * scale, abs=1)
            x = bounds.get_x() + bounds.get_width() / 2
            y = bounds.get_y() + bounds.get_height() / 2
        else:
            rows = [row for row in descendants(popup, Gtk.CellView) if row.get_mapped()]
            _, bounds = rows[1].compute_bounds(popup)
            x = bounds.get_x() + bounds.get_width() / 2
            y = bounds.get_y() + bounds.get_height() / 2
        x_pointer.move(popup_x + x, popup_y + y)
        x_pointer.button(pressed=True)
        x_pointer.button(pressed=False)
        settle()
        assert (control.get_selected() if kind == "dropdown" else control.get_active()) == 1
    finally:
        window.destroy()
        settle()
