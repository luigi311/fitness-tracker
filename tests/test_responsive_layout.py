"""Opt-in allocation tests for real GTK widgets (run under a display or Xvfb).

FITNESS_TRACKER_GTK_TESTS=1 uv run pytest tests/test_responsive_layout.py
Set FITNESS_TRACKER_SCREENSHOTS to a directory to save the rendered sample views.
"""

# ruff: noqa: SLF001

from __future__ import annotations

import ctypes
import ctypes.util
import json
import math
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import cairo
import gi
import numpy as np
import pytest
from fitness_tracker.core.environment import Environment
from fitness_tracker.core.guidance import PebbleTargetKind, StepGuidance, TargetDomain
from fitness_tracker.core.sensor_status import SensorStatus
from fitness_tracker.core.session_capabilities import SessionCapabilities
from fitness_tracker.core.session_state import SessionState
from fitness_tracker.core.settings import AppSettings
from fitness_tracker.core.sports import SportTypesEnum
from fitness_tracker.core.trainer_mode import TrainerMode
from fitness_tracker.core.units import UnitSystem
from fitness_tracker.core.zones import ChartTheme, HeartRateZones

gi.require_versions({"Gtk": "4.0", "Adw": "1", "Graphene": "1.0"})
from fitness_tracker.ui.pages.mode import ModeSelectView  # noqa: E402
from fitness_tracker.ui.pages.session import SessionView  # noqa: E402
from fitness_tracker.ui.widgets import session_controls as session_controls_module  # noqa: E402
from gi.repository import Adw, Gio, GLib, Graphene, Gtk  # noqa: E402  # ty:ignore[unresolved-import]

pytestmark = pytest.mark.skipif(
    os.environ.get("FITNESS_TRACKER_GTK_TESTS") != "1",
    reason="Opt in with FITNESS_TRACKER_GTK_TESTS=1 and a display (or Xvfb)",
)
MIN_TOUCH_HEIGHT = 48
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


def settle():
    context = GLib.MainContext.default()
    deadline = time.monotonic() + 0.12
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(may_block=False)
        time.sleep(0.005)


@pytest.fixture
def app_stub():
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
    window.present()
    settle()
    return window


def assert_inside(widget, container):
    success, bounds = widget.compute_bounds(container)
    assert success
    assert widget.get_mapped()
    assert bounds.get_x() >= -1
    assert bounds.get_y() >= -1
    assert bounds.get_x() + bounds.get_width() <= container.get_width() + 1
    assert bounds.get_y() + bounds.get_height() <= container.get_height() + 1


def descendant_labels(widget):
    if isinstance(widget, Gtk.Label):
        yield widget
    for child in widget:
        yield from descendant_labels(child)


def gauge_ink_bounds(surface, width, height):
    surface.flush()
    pixels = np.frombuffer(surface.get_data(), dtype=np.uint8).reshape(height, surface.get_stride())
    rows, columns = np.nonzero(pixels[:, :width])
    assert len(rows)
    return columns.min(), rows.min(), columns.max() + 1, rows.max() + 1


def assert_gauge_centered(bounds, width, height):
    left, top, right, bottom = bounds
    assert (left + right) / 2 == pytest.approx(width / 2, abs=1.5)
    assert (top + bottom) / 2 == pytest.approx(height / 2, abs=1.5)
    assert left > 0
    assert top > 0
    assert right < width
    assert bottom < height


def save_screenshot(window, name):
    directory = os.environ.get("FITNESS_TRACKER_SCREENSHOTS")
    if not directory:
        return
    destination = Path(directory)
    destination.mkdir(parents=True, exist_ok=True)
    snapshot = Gtk.Snapshot()
    Gtk.WidgetPaintable.new(window).snapshot(snapshot, window.get_width(), window.get_height())
    bounds = Graphene.Rect().init(0, 0, window.get_width(), window.get_height())
    texture = window.get_renderer().render_texture(snapshot.to_node(), bounds)
    assert texture.save_to_png(str(destination / f"{name}.png"))


@pytest.mark.parametrize(("width", "height"), VIEWPORTS)
@pytest.mark.parametrize("kind", ["workout", "trainer", "incline", "free", "outdoor"])
def test_session_allocations(app_stub, width, height, kind):
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
        assert view._body.get_width() <= width
        if kind == "free":
            assert not view.step_position.get_mapped()
        if width >= MIN_COMPACT_WIDTH:
            # At normal text size the target, timers, metrics, and statuses
            # must all be visible together, including the tight Comet layout.
            essential = [view.metrics]
            if kind != "free":
                essential.extend([view.gauge, view.timer_remaining, view.step_position])
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
    settings = Gtk.Settings.get_default()
    original = settings.get_property("gtk-xft-dpi")
    settings.set_property("gtk-xft-dpi", round(96 * text_scale * 1024))
    view, _callbacks = make_session(app_stub, "trainer")
    window = show_view(view, width, height)
    try:
        assert_inside(view.btn_stop, window)
        assert_inside(view.btn_start, window)
        assert view._body.get_width() <= width
        assert_inside(view.metrics, view._scroller)
        assert_inside(view.gauge, view._scroller)
        assert_inside(view.step_position, view._scroller)
        assert_inside(view.step_position.value, view._timers)
        if view._adjustments_inline:
            assert_inside(view.trainer_target_control._btn_down, view._scroller)
            assert_inside(view.trainer_target_control._btn_up, view._scroller)
        overlay = view.btn_stop.get_child()
        stop_label = overlay.get_last_child()
        assert_inside(stop_label, overlay)
        assert stop_label.measure(Gtk.Orientation.HORIZONTAL, -1)[1] <= stop_label.get_width()
        save_screenshot(window, f"text{round(text_scale * 100)}-workout-{width}x{height}")
    finally:
        window.destroy()
        settings.set_property("gtk-xft-dpi", original)
        settle()


@pytest.mark.parametrize("kind", ["trainer", "free"])
def test_rotation_preserves_session_state_and_cancels_stop_hold(app_stub, kind):
    view, callbacks = make_session(app_stub, kind)
    window = show_view(view, 360, 720, production_shell=True)
    timer, gauge, control = view.timer_remaining, view.gauge, view.trainer_target_control
    try:
        for width, height in (
            (720, 360),
            (540, 620),
            (620, 540),
            (432, 496),
            (496, 432),
            (240, 480),
            (480, 240),
            (360, 720),
        ):
            view._begin_stop_hold()
            window.set_default_size(width, height)
            settle()
            assert view._stop_hold_started_at is None
            assert view.timer_remaining is timer
            assert view.gauge is gauge
            assert view.trainer_target_control is control
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
    view, _callbacks = make_session(app_stub, "trainer")
    assert view.gauge._available
    view.set_statuses(SensorStatus())
    assert not view.gauge._available
    assert view.card_distance.value.get_text() == "4.28"
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
    del app_stub  # GTK initialization is shared with the session checks.
    start_free = Mock()
    view = ModeSelectView(tmp_path, tmp_path, start_free, Mock(), unit_system=UnitSystem.METRIC)
    window = show_view(view, width, height)
    try:
        assert view._setup.get_width() <= width
        assert view._library.get_width() <= width
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
    [(540, 620, 6), (620, 540, 4), (432, 496, 4), (496, 432, 2)],
)
def test_comet_setup_leaves_room_for_workouts(app_stub, tmp_path, width, height, visible_rows):
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
        assert view.get_width() <= width
        setup_scroll = view._setup.get_parent()
        assert view.has_css_class("mini-setup")
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


def test_full_application_shell_fits_short_landscape(app_stub, monkeypatch, tmp_path):
    from fitness_tracker.ui.app import FitnessAppUI  # noqa: PLC0415

    del app_stub
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    app = FitnessAppUI(test_mode=True)
    app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
    app.set_application_id(f"com.luigi311.fitness-tracker.test_{tmp_path.name}")
    app.register(None)
    app._build_ui()
    view, _callbacks = make_session(app, "workout")
    app.tracker.nav.push(Adw.NavigationPage.new(view, "Workout"))
    app.window.set_decorated(False)
    app.window.present()
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
            header = app.header_revealer.get_child()
            if header.has_css_class("roomy-navigation"):
                labels = [
                    label
                    for label in descendant_labels(header)
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
        app.window.destroy()
        app.jobs.shutdown()
        GLib.source_remove(app.tracker._status_timer_id)
        settle()


@pytest.fixture
def shell_app(app_stub, monkeypatch, tmp_path):
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


def test_real_application_setup_fits_librem_three_x(shell_app):
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
        assert view.get_width() <= width
        for card in view._env_cards.values():
            assert_inside(card, setup_scroll)
        assert_inside(view._btn_start_free, app.window)
        assert_inside(view._list.get_first_child(), view._library.get_last_child().get_child())
        save_screenshot(app.window, f"real-librem-three-x-setup-{width}x{height}")


def test_session_toasts_leave_recording_and_step_buttons_hit_testable(shell_app):
    app = shell_app
    view, _callbacks = make_session(app, "workout")
    app.tracker.session_view = view
    app.tracker.nav.push(Adw.NavigationPage.new(view, "Workout"))
    try:
        for width, height in ((360, 720), (720, 360), (432, 496), (240, 480), (480, 240)):
            app.window.set_default_size(width, height)
            settle()
            app.show_workout_step_notification(2, 8, "Target: 200 - 230 W")
            settle()
            settle()
            assert any(
                label.get_mapped() and label.get_text() == "Workout step 2 of 8"
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
        assert view._body.get_width() <= width
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
        (360, 720, False),
        (720, 360, False),
        (1080, 1240, True),
        (1240, 1080, True),
        (540, 620, True),
        (620, 540, False),
        (432, 496, True),
        (496, 432, False),
    ],
)
def test_trainer_controls_are_directly_accessible_when_dashboard_fits(
    app_stub,
    width,
    height,
    inline,
):
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
    view, callbacks = make_session(app_stub, "trainer")
    window = show_view(view, 540, 620)
    control = view.trainer_target_control
    try:
        control._btn_up.emit("clicked")
        callbacks.target.assert_called_once_with(TrainerMode.BIAS, 5.0)
        summary = control.summary
        callbacks.reset_mock()
        allocations = ((620, 540, False), (1080, 1240, True), (540, 620, True))
        for width, height, inline in allocations * 3:
            window.set_default_size(width, height)
            settle()
            assert view._adjustments_inline is inline
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


def test_trainer_disclosure_adapts_to_text_size_without_resize(app_stub):
    settings = Gtk.Settings.get_default()
    original_dpi = settings.get_property("gtk-xft-dpi")
    view, callbacks = make_session(app_stub, "trainer")
    window = show_view(view, 432, 496)
    try:
        assert view._adjustments_inline
        for text_scale, inline in ((1.5, False), (1.0, True)):
            settings.set_property("gtk-xft-dpi", round(96 * text_scale * 1024))
            settle()
            assert view._adjustments_inline is inline
            assert_inside(view.metrics, view._scroller)
            assert_inside(view.gauge, view._scroller)
            assert_inside(view.btn_stop, window)
            assert not callbacks.mock_calls
    finally:
        window.destroy()
        settings.set_property("gtk-xft-dpi", original_dpi)
        settle()


@pytest.mark.parametrize("kind", ["workout", "trainer", "incline", "free", "outdoor"])
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
            assert button.get_height() == pytest.approx(48 * scale, abs=2)
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
    try:
        window.set_default_size(width, height)
        settle()
        for label, reference in zip(labels, fonts, strict=True):
            assert label.get_pango_context().get_font_description().get_size() == pytest.approx(
                reference * scale,
                rel=0.01,
            )
        assert_inside(view.metrics, view._scroller)
        assert view._state is SessionState.RUNNING
        assert view.timer_remaining.value.get_text() == "04:18"
        assert not callbacks.mock_calls
        window.set_default_size(reference_width, reference_height)
        settle()
        for label, reference in zip(labels, fonts, strict=True):
            assert label.get_pango_context().get_font_description().get_size() == reference
        assert view.btn_start.get_height() == MIN_TOUCH_HEIGHT
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
    view, callbacks = make_session(app_stub, "trainer")
    window = show_view(view, width, height)
    control = view.trainer_target_control
    labels = (
        view.title_label,
        view.lbl_target,
        view.lbl_next,
        view.timer_elapsed.value,
        view.timer_elapsed.get_child().get_first_child(),
        view.step_position.value,
        view.card_power.label,
        view.card_power.value,
        view.card_power.unit,
        view.card_power.dot,
        control._lbl_value,
        control._mode_buttons[TrainerMode.BIAS].get_child(),
        view.btn_start.get_child(),
        view.btn_stop.get_child().get_last_child(),
    )
    reference_fonts = [
        label.get_pango_context().get_font_description().get_size() for label in labels
    ]
    reference_button_height = view.btn_start.get_height()
    try:
        window.set_default_size(width * 2, height * 2)
        settle()
        for label, reference in zip(labels, reference_fonts, strict=True):
            assert label.get_pango_context().get_font_description().get_size() == pytest.approx(
                reference * 2,
                rel=0.001,
            )
        assert view.btn_start.get_height() == reference_button_height * 2
        assert_inside(view.metrics, view._scroller)
        assert_inside(view.gauge, view._scroller)
        assert_inside(view.btn_stop, window)
        assert_inside(view.btn_start, window)
        if view._adjustments_inline:
            assert_inside(control._btn_down, view._scroller)
            assert_inside(control._btn_up, view._scroller)
        assert view.timer_remaining.value.get_text() == "04:18"
        assert not callbacks.mock_calls
        save_screenshot(window, f"readable-trainer-{width * 2}x{height * 2}")
        window.set_default_size(width, height)
        settle()
        for label, reference in zip(labels, reference_fonts, strict=True):
            assert label.get_pango_context().get_font_description().get_size() == reference
        assert view.btn_start.get_height() == reference_button_height
        assert not callbacks.mock_calls
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
    view, callbacks = make_session(app_stub, "free")
    window = show_view(view, width, height, production_shell=True)
    chart = view.live_chart
    try:
        chart.canvas.draw()
        settle()
        label = chart.hr_axes.xaxis.label
        assert label.get_fontsize() == 10 * scale
        for tick in chart.hr_axes.get_xticklabels():
            assert tick.get_fontsize() == 10 * scale
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
        band.update(geometry)
        draw_band(ctx, **geometry)

    def capture_label(ctx, layout):
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
    ("width", "height", "compact"),
    [
        (360, 720, True),
        (720, 360, True),
        (432, 496, True),
        (240, 480, True),
        (1080, 1240, False),
        (1440, 900, False),
    ],
)
def test_gauge_is_smaller_and_centered_in_compact_sessions(
    app_stub,
    width,
    height,
    compact,
    available,
):
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
        if compact:
            assert bounds[2] - bounds[0] <= (reference_bounds[2] - reference_bounds[0]) * 0.94
            assert bounds[3] - bounds[1] < reference_bounds[3] - reference_bounds[1]
        else:
            assert bounds == reference_bounds
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
        xtest.XTestFakeMotionEvent(display, -1, round(x), round(y), 0)
        x11.XSync(display, 0)

    def button(*, pressed):
        xtest.XTestFakeButtonEvent(display, 1, pressed, 0)
        x11.XSync(display, 0)

    def origin(window):
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


def test_real_pointer_dragging_off_stop_cancels_until_a_fresh_press(app_stub, x_pointer):
    view, callbacks = make_session(app_stub, "workout")
    window = show_view(view, 360, 720, production_shell=True)
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
