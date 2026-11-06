"""Render the real GTK application with deterministic, isolated sample data.

Run with the target revision's Python environment; this harness can live outside
that checkout so the same fixtures render both sides of a pull request.
"""

# ruff: noqa: PLC0415, SLF001

from __future__ import annotations

import argparse
import json
import math
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from fitness_tracker.ui.app import FitnessAppUI
    from fitness_tracker.ui.pages.session import SessionView
    from gi.repository import Gtk

MATRIX = json.loads(Path(__file__).with_name("matrix.json").read_text(encoding="utf-8"))
ACTIVITY_COUNT = 2
SAMPLE_SECONDS = tuple(range(0, 1201, 15))


def settle(seconds: float = 0.2) -> None:
    """Drain GTK layout, chart draws and transitions for a bounded duration."""
    from gi.repository import GLib

    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(may_block=False)
        time.sleep(0.005)


def wait_until(ready: Callable[[], bool], description: str) -> None:
    """Wait for actual background results rather than photographing a loader."""
    deadline = time.monotonic() + 20
    while not ready():
        if time.monotonic() > deadline:
            message = f"Timed out waiting for {description}"
            raise RuntimeError(message)
        settle(0.05)
    settle()


def seed_data(app: FitnessAppUI) -> None:
    """Populate a private database and workout library with fixed examples."""
    from fitness_tracker.core.environment import Environment
    from fitness_tracker.core.sports import SportTypesEnum
    from fitness_tracker.core.units import UnitSystem
    from fitness_tracker.data.models import Activity, ActivitySport, HeartRate, RunningMetrics

    app.history_filter = "all"
    app.app_settings.display.unit_system = UnitSystem.METRIC
    with app.database.Session() as session:
        for index in range(ACTIVITY_COUNT):
            start = datetime(2026, 1, 12 + index, 9, tzinfo=UTC)
            activity = Activity(
                start_time=start,
                end_time=start + timedelta(minutes=20),
                environment=Environment.INDOOR.value,
            )
            session.add(activity)
            session.flush()
            session.add(
                ActivitySport(activity_id=activity.id, sport_type_id=SportTypesEnum.running.value),
            )
            for second in SAMPLE_SECONDS:
                timestamp = int((start.timestamp() + second) * 1000)
                session.add(
                    HeartRate(
                        activity_id=activity.id,
                        timestamp_ms=timestamp,
                        bpm=round(140 + index * 8 + 12 * math.sin(second / 150)),
                    ),
                )
                session.add(
                    RunningMetrics(
                        activity_id=activity.id,
                        timestamp_ms=timestamp,
                        speed_mps=3.2 + index * 0.2,
                        cadence_spm=86 + index * 2,
                        total_distance_m=second * (3.2 + index * 0.2),
                        power_watts=182 + index * 15,
                    ),
                )
        session.commit()
    app.database.stat_calc.compute_all(force=True)
    for index, name in enumerate(("Tempo intervals", "Easy recovery", "Long endurance run")):
        (app.workouts_running_dir / f"{index}.json").write_text(
            json.dumps({"name": name, "steps": [{"duration": 1200}]}),
            encoding="utf-8",
        )


def sample_session(app: FitnessAppUI, *, workout: bool) -> SessionView:
    """Use production session construction and fixed live readings, without timers."""
    import numpy as np
    from fitness_tracker.core.environment import Environment
    from fitness_tracker.core.sensor_status import SensorStatus
    from fitness_tracker.core.session_state import SessionState
    from fitness_tracker.core.sports import SportTypesEnum

    view = app.tracker._make_session_view(
        SportTypesEnum.running,
        Environment.INDOOR,
        title="Tempo intervals" if workout else "Free Run",
        workout=workout,
    )
    view.set_profile_ready(ready=True)
    view.set_state(SessionState.RUNNING)
    view.set_timer("00:18:42")
    view.set_elapsed_text("18:42")
    view.set_step_remaining_text("04:18")
    view.set_target_text("Tempo · 4:45-5:00 /km")
    view.set_next_text("Next: Recover · 2 min")
    view.set_progress(0.47)
    view.set_statuses(SensorStatus(hr=True, speed=True, cadence=True, distance=True, power=True))
    view.set_metrics(
        bpm=158,
        pace_mps=3.2,
        cadence_spm=86,
        speed_mps=3.2,
        distance_m=3584,
        power_watts=182,
    )
    if workout:
        view.set_gauge_pace(
            current_mps=3.2,
            current_pace_text="5:12",
            target_pace_text="4:52",
            target_mps_lo=3.33,
            target_mps_mid=3.42,
            target_mps_hi=3.51,
        )
    view.update_chart(
        np.array(SAMPLE_SECONDS),
        np.array([140 + 12 * math.sin(second / 150) for second in SAMPLE_SECONDS]),
        np.array([182 + 15 * math.sin(second / 100) for second in SAMPLE_SECONDS]),
    )
    return view


def save_window(window: Gtk.Window, output: Path, viewport: dict) -> None:
    """Capture the specified logical viewport at its physical panel resolution.

    Scaling the snapshot supports fractional compositor scales under Xvfb. The
    allocation drives GTK breakpoints; changing font DPI would test text scaling
    instead. Clip to the requested viewport if a regression forces a larger window.
    """
    from gi.repository import Graphene, Gtk

    width, height, scale = viewport["width"], viewport["height"], viewport["scale"]
    snapshot = Gtk.Snapshot()
    snapshot.scale(scale, scale)
    # Window paintables include the surface's desktop frame. Remove that offset
    # to capture only application content, without changing the page's styling.
    paintable = Gtk.WidgetPaintable.new(window)
    offset_x, offset_y = window.get_surface_transform()
    snapshot.translate(Graphene.Point().init(-offset_x, -offset_y))
    paintable.snapshot(snapshot, paintable.get_intrinsic_width(), paintable.get_intrinsic_height())
    bounds = Graphene.Rect().init(0, 0, round(width * scale), round(height * scale))
    node = snapshot.to_node()
    if node is None:
        message = f"Empty GTK snapshot: {output}"
        raise RuntimeError(message)
    texture = window.get_renderer().render_texture(node, bounds)
    output.parent.mkdir(parents=True, exist_ok=True)
    if not texture.save_to_png(str(output)):
        message = f"Could not save {output}"
        raise RuntimeError(message)


def select_page(app: FitnessAppUI, page: str) -> None:
    """Select a populated page, waiting for comparison chart results if needed."""
    from gi.repository import Adw

    app.stack.set_visible_child_name("tracker")
    if page in {"workout", "free-run"}:
        view = sample_session(app, workout=page == "workout")
        app.tracker.nav.push(Adw.NavigationPage.new(view, page))
    elif page in {"history", "history-comparison"}:
        app.stack.set_visible_child_name("history")
        app.history.stack.set_visible_child_name(
            "compare" if page == "history-comparison" else "activities",
        )
        if page == "history-comparison":
            app.history.selected_ids = {row.activity_id for row in app.history._displayed}
            app.history._bind_summary(app.history._displayed)
            app.history._redraw_compare_chart()
            wait_until(lambda: app.history._compare_job is None, "history comparison")
            if not app.history._cmp_ax.get_legend():
                message = "Comparison did not render the sample activities"
                raise RuntimeError(message)
    elif page == "settings":
        app.stack.set_visible_child_name("settings")


def capture(output: Path, pages: list[str], viewports: list[dict]) -> None:
    """Render every selected page and allocation in the actual application shell."""
    import gi

    gi.require_versions({"Gtk": "4.0", "Adw": "1", "Graphene": "1.0"})
    from fitness_tracker.ui.app import FitnessAppUI
    from gi.repository import Adw, Gio, GLib, Gtk

    Adw.init()
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
    settings = Gtk.Settings.get_default()
    settings.props.gtk_enable_animations = False
    settings.props.gtk_cursor_blink = False
    settings.set_property("gtk-font-name", "DejaVu Sans 10")
    settings.set_property("gtk-icon-theme-name", "Adwaita")
    settings.set_property("gtk-xft-dpi", 96 * 1024)
    app = FitnessAppUI(test_mode=True)
    app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
    app.register(None)
    try:
        seed_data(app)
        app._build_ui()
        # Avoid sensor-status ticks changing the fixed session readings.
        if app.tracker._status_timer_id is not None:
            GLib.source_remove(app.tracker._status_timer_id)
            app.tracker._status_timer_id = None
        app.window.set_decorated(False)
        app.window.present()
        wait_until(
            lambda: (
                len(app.history._displayed) == ACTIVITY_COUNT
                and not app.history._stats_backfill_in_progress
            ),
            "populated history",
        )
        for page in pages:
            select_page(app, page)
            for viewport in viewports:
                surface = app.window.get_surface()
                frame_width = surface.get_width() - app.window.get_width()
                frame_height = surface.get_height() - app.window.get_height()
                app.window.set_default_size(
                    viewport["width"] + frame_width,
                    viewport["height"] + frame_height,
                )
                settle()
                # GTK may auto-focus a settings entry when switching pages. Clear
                # focus so caret/selection state cannot create a false UI change.
                app.window.set_focus(None)
                settle(0.03)
                destination = output / viewport["id"] / f"{page}.png"
                save_window(app.window, destination, viewport)
                print(destination)
            if page in {"workout", "free-run"}:
                app.tracker.nav.pop()
                settle()
    finally:
        if app.window is not None:
            app.window.destroy()
        app.jobs.shutdown()
        app.database.engine.dispose()
        settle()


def main() -> None:
    """Parse optional local smoke-test selections and capture the matrix."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--page", action="append", choices=[p["id"] for p in MATRIX["pages"]])
    parser.add_argument(
        "--viewport",
        action="append",
        choices=[v["id"] for v in MATRIX["viewports"]],
    )
    args = parser.parse_args()
    viewports = [v for v in MATRIX["viewports"] if not args.viewport or v["id"] in args.viewport]
    with TemporaryDirectory(prefix="fitness-screenshot-data-") as directory:
        # Configure rendering before importing GTK or matplotlib, both of which
        # may otherwise read desktop preferences or the user's plotting config.
        os.environ.update(
            GDK_BACKEND="x11",
            GDK_SCALE="1",
            GDK_DPI_SCALE="1",
            GSK_RENDERER="cairo",
            GTK_A11Y="none",
            GSETTINGS_BACKEND="memory",
            TZ="UTC",
            LC_ALL="C.UTF-8",
            XDG_CONFIG_HOME=f"{directory}/config",
            XDG_DATA_HOME=f"{directory}/data",
            XDG_CACHE_HOME=f"{directory}/cache",
            MPLCONFIGDIR=f"{directory}/matplotlib",
        )
        time.tzset()
        capture(args.output.resolve(), args.page or [p["id"] for p in MATRIX["pages"]], viewports)


if __name__ == "__main__":
    main()
