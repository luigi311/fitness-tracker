"""Opt-in native GTK history checks with a populated private database."""

# ruff: noqa: SLF001

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from unittest.mock import patch

import gi
import pytest
from fitness_tracker.core.units import UnitSystem
from fitness_tracker.services.history_query import CompareChartData

from test_responsive_layout import assert_inside, descendant_labels, save_screenshot, settle

gi.require_versions({"Gtk": "4.0", "Adw": "1"})
from fitness_tracker.ui.app import FitnessAppUI  # noqa: E402
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402  # ty:ignore[unresolved-import]

pytestmark = pytest.mark.skipif(
    os.environ.get("FITNESS_TRACKER_GTK_TESTS") != "1",
    reason="Opt in with FITNESS_TRACKER_GTK_TESTS=1 and a display",
)

VIEWPORTS = (
    (240, 480),
    (480, 240),
    (360, 720),
    (720, 360),
    (432, 496),
    (496, 432),
    (540, 620),
    (620, 540),
    (1080, 1240),
    (1240, 1080),
    (800, 1280),
    (1280, 800),
    (900, 1600),
    (1600, 900),
)
ACTIVITIES = 8
COMPARE_COUNT = 2


def wait_for(predicate):
    for _ in range(50):
        if predicate():
            settle()
            return
        settle()
    pytest.fail("History background work did not finish")


@pytest.fixture(scope="module")
def populated_app(tmp_path_factory):
    directory = tmp_path_factory.mktemp("native-history")
    with patch.dict(
        os.environ,
        {
            "XDG_CONFIG_HOME": str(directory / "config"),
            "XDG_DATA_HOME": str(directory / "data"),
        },
    ):
        Adw.init()
        Gtk.Settings.get_default().props.gtk_enable_animations = False
        app = FitnessAppUI(test_mode=True)
        app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
        app.register(None)
        path = Path(__file__).parents[1] / "scripts/ui_screenshots/capture.py"
        spec = importlib.util.spec_from_file_location("history_sample_data", path)
        seed = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(seed)
        seed.ACTIVITY_COUNT = ACTIVITIES
        seed.seed_data(app)
        app._build_ui()
        app.window.set_decorated(False)
        app.window.present()
        app.stack.set_visible_child_name("history")
        wait_for(
            lambda: (
                len(app.history._displayed) == ACTIVITIES
                and not app.history._stats_backfill_in_progress
            ),
        )
        yield app
        wait_for(
            lambda: (
                app.history._compare_job is None and not app.history._stats_backfill_in_progress
            ),
        )
        app.window.destroy()
        app.jobs.shutdown()
        app.database.engine.dispose()
        GLib.source_remove(app.tracker._status_timer_id)
        settle()


@pytest.fixture
def history_app(populated_app):
    app = populated_app
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
    app.history.selected_ids.clear()
    app.app_settings.display.unit_system = UnitSystem.METRIC
    app.history.filter_combo.set_active_id("all")
    app.history.sort_combo.set_active_id("date_desc")
    wait_for(
        lambda: (
            len(app.history._displayed) == ACTIVITIES
            and not app.history._stats_backfill_in_progress
        ),
    )
    app.history.refresh_units()
    app.history.stack.set_visible_child_name("activities")
    app.history.layout.activity_scroll.get_vadjustment().set_value(0)
    app.history.layout.list_scroll.get_vadjustment().set_value(0)
    settle()
    return app


def resize(app, width, height):
    app.window.set_default_size(width, height)
    settle()
    settle()
    assert app.window.get_surface().get_width() == width
    assert app.window.get_surface().get_height() == height


def select_pair(page):
    for row in page._displayed[:2]:
        page._on_select_toggle(row.activity_id, active=True)
    wait_for(lambda: page._compare_job is None and len(page._compare_legend_items) == COMPARE_COUNT)


def descendants(widget, kind):
    if isinstance(widget, kind):
        yield widget
    for child in widget:
        yield from descendants(child, kind)


@pytest.mark.parametrize(("width", "height"), VIEWPORTS)
@pytest.mark.parametrize("tab", ["activities", "compare"])
def test_populated_history_fits_all_supported_sizes(history_app, width, height, tab):
    app = history_app
    page = app.history
    resize(app, width, height)
    if tab == "compare":
        select_pair(page)
    page.stack.set_visible_child_name(tab)
    settle()
    view = page.layout
    assert view.body.get_width() <= view._adaptive.get_width()
    for widget in (view.btn_activities, view.btn_compare, app._navigation_bar):
        assert_inside(widget, app.window)
    if tab == "activities":
        scroller = view.list_scroll if view._landscape else view.activity_scroll
        first = page._listbox.get_first_child()
        assert first is not None
        for widget in (page.filter_combo, page.sort_combo):
            assert widget.measure(Gtk.Orientation.HORIZONTAL, -1)[0] <= view.sidebar.get_width()
        for action in descendants(first, Gtk.Button):
            assert_inside(action, scroller)
            assert action.get_height() >= round(48 * view.density) - 1
        check = next(descendants(first, Gtk.CheckButton))
        assert_inside(check, scroller)
        assert_inside(first.get_child().get_child().get_first_child(), scroller)
        scroller.get_vadjustment().set_value(scroller.get_vadjustment().get_upper())
        settle()
        assert_inside(page._listbox.get_last_child(), scroller)
    else:
        assert_inside(page.cmp_metric_combo, app.window)
        assert_inside(page._cmp_canvas, app.window)
        page._cmp_canvas.draw()
        renderer = page._cmp_canvas.get_renderer()
        figure = page._cmp_chart.figure
        labels = [page._cmp_ax.xaxis.label, page._cmp_ax.yaxis.label]
        for tick, label in zip(
            page._cmp_ax.get_xticks(),
            page._cmp_ax.get_xticklabels(),
            strict=True,
        ):
            if page._cmp_ax.get_xlim()[0] <= tick <= page._cmp_ax.get_xlim()[1]:
                labels.append(label)
        for label in labels:
            bounds = label.get_window_extent(renderer)
            assert bounds.x0 >= -1
            assert bounds.y0 >= -1
            assert bounds.x1 <= figure.bbox.width + 1
            assert bounds.y1 <= figure.bbox.height + 1
        assert len(page._cmp_ax.lines) == COMPARE_COUNT
        assert page._cmp_ax.get_legend() is None
        assert len(page._compare_legend_items) == COMPARE_COUNT
    save_screenshot(app.window, f"history-{tab}-{width}x{height}")


def test_rotation_keeps_selection_choices_widgets_and_compare_data(history_app):
    app = history_app
    page = app.history
    select_pair(page)
    page.cmp_metric_combo.set_active_id("power")
    wait_for(lambda: page._compare_job is None)
    selected = set(page.selected_ids)
    generation = page._compare_generation
    first_row = page._listbox.get_first_child()
    canvas = page._cmp_canvas
    for tab in ("activities", "compare"):
        page.stack.set_visible_child_name(tab)
        for width, height in VIEWPORTS:
            resize(app, width, height)
            assert page.selected_ids == selected
            assert page.filter_combo.get_active_id() == "all"
            assert page.sort_combo.get_active_id() == "date_desc"
            assert page.cmp_metric_combo.get_active_id() == "power"
            assert page._listbox.get_first_child() is first_row
            assert page._cmp_canvas is canvas
            assert page._compare_generation == generation
            assert_inside(app._navigation_bar, app.window)


def test_filters_sort_units_and_export_keep_their_meaning(history_app):
    app = history_app
    page = app.history
    resize(app, 540, 620)
    page.layout.btn_compare.emit("clicked")
    assert page.stack.get_visible_child_name() == "compare"
    page.layout.btn_compare.emit("clicked")
    assert page.layout.btn_compare.get_active()
    page.layout.btn_activities.emit("clicked")
    assert page.stack.get_visible_child_name() == "activities"
    page.sort_combo.set_active_id("date_asc")
    settle()
    assert page._displayed[0].start_time <= page._displayed[-1].start_time
    page.filter_combo.set_active_id("failed")
    wait_for(lambda: not page._stats_backfill_in_progress and not page._displayed)
    page.filter_combo.set_active_id("all")
    wait_for(lambda: not page._stats_backfill_in_progress and len(page._displayed) == ACTIVITIES)
    row = page._listbox.get_first_child()
    checkbox = next(descendants(row, Gtk.CheckButton))
    checkbox.set_active(True)
    selected = set(page.selected_ids)
    export = next(descendants(row, Gtk.Button))
    with patch.object(page, "_on_export_clicked") as exporter:
        export.emit("clicked")
        exporter.assert_called_once_with(page._displayed[0].activity_id)
        assert page.selected_ids == selected
    select_pair(page)
    page.cmp_metric_combo.set_active_id("speed")
    app.app_settings.display.unit_system = UnitSystem.IMPERIAL
    page.refresh_units()
    wait_for(lambda: page._compare_job is None)
    assert page.cmp_metric_combo.get_active_id() == "speed"
    assert "mph" in page._cmp_ax.get_ylabel()
    assert "mi" in page.lbl_total_dist.get_text()
    assert page.selected_ids == selected | {page._displayed[1].activity_id}


@pytest.mark.parametrize("text_scale", [1.25, 1.5])
@pytest.mark.parametrize(
    ("width", "height"),
    [(240, 480), (480, 240), (360, 720), (720, 360), (540, 620), (620, 540)],
)
def test_enlarged_text_keeps_history_navigation_and_content_reachable(
    history_app,
    width,
    height,
    text_scale,
):
    app = history_app
    settings = Gtk.Settings.get_default()
    original = settings.get_property("gtk-xft-dpi")
    try:
        select_pair(app.history)
        settings.set_property("gtk-xft-dpi", round(96 * 1024 * text_scale))
        resize(app, width, height)
        page = app.history
        for tab in ("activities", "compare"):
            page.stack.set_visible_child_name(tab)
            settle()
            assert page.layout.body.get_width() <= page.layout._adaptive.get_width()
            for widget in (
                page.layout.btn_activities,
                page.layout.btn_compare,
                app._navigation_bar,
            ):
                assert_inside(widget, app.window)
            scroll = (
                page.layout.compare_scroll
                if tab == "compare"
                else (
                    page.layout.list_scroll
                    if page.layout._landscape
                    else page.layout.activity_scroll
                )
            )
            scroll.get_vadjustment().set_value(0)
            settle()
            if tab == "activities":
                controls_scroll = page.layout.activity_scroll
                controls_scroll.get_vadjustment().set_value(0)
                settle()
                assert_inside(page.filter_combo, controls_scroll)
                assert_inside(page.sort_combo, controls_scroll)
                if page.layout._landscape:
                    controls_scroll.get_vadjustment().set_value(
                        controls_scroll.get_vadjustment().get_upper(),
                    )
                    settle()
                    assert_inside(page.lbl_avg_hr, controls_scroll)
            else:
                assert_inside(page.cmp_metric_combo, app.window)
            scroll.get_vadjustment().set_value(scroll.get_vadjustment().get_upper())
            settle()
            if tab == "activities":
                last = page._listbox.get_last_child()
                for action in descendants(last, Gtk.Button):
                    assert_inside(action, scroll)
                assert_inside(next(descendants(last, Gtk.CheckButton)), scroll)
            else:
                assert_inside(page._cmp_canvas, scroll)
    finally:
        settings.set_property("gtk-xft-dpi", original)
        settle()


def test_empty_history_and_comparison_explain_the_next_action(history_app):
    app = history_app
    page = app.history
    resize(app, 240, 480)
    page.filter_combo.set_active_id("failed")
    wait_for(lambda: not page._stats_backfill_in_progress and not page._displayed)
    page.stack.set_visible_child_name("compare")
    settle()
    assert page._compare_plot.get_visible_child_name() == "message"
    assert "Select activities" in page._compare_message.get_text()
    assert not page._compare_legend_items
    assert_inside(page._compare_message, page.layout.compare_scroll)
    assert_inside(page.layout.btn_activities, app.window)
    page.layout.btn_activities.set_active(True)
    settle()
    assert page.stack.get_visible_child_name() == "activities"
    assert any("No activities" in label.get_text() for label in descendant_labels(page._listbox))


def test_light_theme_keeps_comparison_and_selection_visible(history_app):
    app = history_app
    page = app.history
    select_pair(page)
    selected = set(page.selected_ids)
    resize(app, 540, 620)
    page.stack.set_visible_child_name("compare")
    manager = Adw.StyleManager.get_default()
    try:
        manager.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        wait_for(lambda: page._compare_job is None)
        assert page.selected_ids == selected
        assert len(page._compare_legend_items) == COMPARE_COUNT
        assert_inside(page._cmp_canvas, app.window)
        save_screenshot(app.window, "history-light-comparison")
    finally:
        manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        wait_for(lambda: page._compare_job is None)


def test_selected_activities_without_metric_data_do_not_prompt_for_selection(history_app):
    """Distinguish missing metric data from an empty comparison selection."""
    page = history_app.history
    select_pair(page)
    page._render_compare_chart(
        CompareChartData(
            generation=page._compare_generation,
            metric="power",
            series=(),
            max_time_s=0,
        ),
    )
    assert page._compare_plot.get_visible_child_name() == "message"
    assert "No data" in page._compare_message.get_text()
    assert page._legend_menu.get_label() == f"{COMPARE_COUNT} selected"
    for box in (page._legend_inline_box, page._legend_popup_box):
        assert box.get_first_child() is None
    page.selected_ids.clear()
    page._set_compare_legend([])
    for box in (page._legend_inline_box, page._legend_popup_box):
        assert "Select activities" in box.get_first_child().get_text()


@pytest.mark.parametrize(("width", "height"), [(540, 620), (1080, 1240)])
@pytest.mark.parametrize("dpi_scale", [None, 1.25, 1.5])
def test_compare_data_refresh_preserves_dpi_adjusted_chart_scale(
    history_app,
    width,
    height,
    dpi_scale,
):
    """Keep data renders and layout updates at the same text and line scale."""
    page = history_app.history
    settings = Gtk.Settings.get_default()
    original_dpi = settings.get_property("gtk-xft-dpi")
    try:
        settings.set_property("gtk-xft-dpi", round(96 * 1024 * dpi_scale) if dpi_scale else -1)
        resize(history_app, width, height)
        page.stack.set_visible_child_name("compare")
        select_pair(page)
        expected = page.layout.density * (dpi_scale or 1)
        for metric in ("speed", "hr"):
            page.cmp_metric_combo.set_active_id(metric)
            wait_for(lambda: page._compare_job is None)
            assert page._cmp_chart._display_scale == pytest.approx(expected)
            assert page._cmp_ax.xaxis.label.get_fontsize() == pytest.approx(10 * expected)
            assert all(
                line.get_linewidth() == pytest.approx(2 * expected) for line in page._cmp_ax.lines
            )
            page.layout._on_layout_changed()
            assert page._cmp_chart._display_scale == pytest.approx(expected)
    finally:
        settings.set_property("gtk-xft-dpi", original_dpi)
        settle()
