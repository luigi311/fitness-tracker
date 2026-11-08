from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

import gi
from loguru import logger
from matplotlib.colors import to_hex
from matplotlib.ticker import FuncFormatter

from fitness_tracker.core.sports import SportTypesEnum
from fitness_tracker.core.units import (
    DurationStyle,
    display_cadence,
    format_distance,
    format_duration,
    format_pace,
    format_speed,
    unit_label,
)
from fitness_tracker.exporters import activity_to_tcx, infer_sport
from fitness_tracker.services.history_query import (
    MAX_COMPARE_POINTS,
    CompareActivity,
    CompareChartData,
    CompareChartRequest,
    CompareMetric,
    build_compare_chart_data,
)
from fitness_tracker.services.jobs import CancellationToken, DuplicateJobError, JobHandle
from fitness_tracker.ui.widgets.chart import CompareChart, Sparkline, style_axes
from fitness_tracker.ui.widgets.history_layout import HistoryChoice, HistoryLayout

if TYPE_CHECKING:
    from collections.abc import Callable

    from matplotlib.axes import Axes

    from fitness_tracker.data.repositories import ActivityRepository, ActivityStatsRow
    from fitness_tracker.ui.app import FitnessAppUI

gi.require_versions({"Gtk": "4.0", "Adw": "1"})
from gi.repository import Gio, GLib, Gtk, Pango  # noqa: E402  # ty:ignore[unresolved-import]


def _format_float(v: float | None, unit: str = "", digits: int = 0) -> str:
    if v is None:
        return "—"
    fmt = f"{{:.{digits}f}}"
    return (fmt.format(v) + (f" {unit}" if unit else "")).strip()


def _tz_aware_localize(value: dt.datetime) -> dt.datetime:
    """Convert a timestamp to local time, treating naive values as UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone()


def _configure_activity_title(title: Gtk.Label) -> None:
    """Configure an activity title to yield horizontal space on narrow screens."""
    title.add_css_class("title-3")
    title.set_single_line_mode(True)
    title.set_ellipsize(Pango.EllipsizeMode.END)
    title.set_hexpand(True)
    title.set_xalign(0)


@dataclass(frozen=True)
class _HistoryReloadResult:
    filter_id: str
    rows: list[ActivityStatsRow]
    heart_rate_series: dict[int, list[tuple[int, int]]]


@dataclass(frozen=True)
class _HistoryExportResult:
    default_name: str
    tcx_bytes: bytes


class _HistoryExportError(RuntimeError):
    """An expected activity-export failure suitable for display."""


def _tcx_output_target(gfile: Gio.File) -> Gio.File:
    """Return the selected Gio target with a TCX suffix."""
    basename = gfile.get_basename()
    if not basename:
        message = "Selected export target has no filename"
        raise _HistoryExportError(message)
    if basename.lower().endswith(".tcx"):
        return gfile
    parent = gfile.get_parent()
    if parent is None:
        message = "Selected export target has no parent"
        raise _HistoryExportError(message)
    return parent.get_child(f"{basename}.tcx")


# ---------- History Page UI ----------


class HistoryPageUI:
    """Render activity history, summaries, comparisons, and exports."""

    def __init__(self, app: FitnessAppUI) -> None:
        """Initialize history selection, chart state, and display preferences."""
        self.app = app

        # State
        self.filter_id: str = app.history_filter or "week"
        self.sort_id: str = "date_desc"
        self.selected_ids: set[int] = set()

        self._listbox: Gtk.ListBox | None = None
        # Cached flat stats rows (ActivityStats ORM objects) in display order.
        self._displayed: list[ActivityStatsRow] = []
        self._heart_rate_series: dict[int, list[tuple[int, int]]] = {}
        self._sparkline_charts: list[Sparkline] = []
        self._stats_backfill_in_progress = False
        self._stats_reload_pending = False

        # Compare chart
        self._cmp_chart: CompareChart | None = None
        self._cmp_ax = None
        self._cmp_canvas = None
        self._cmp_metric_id = "hr"
        self.lbl_scope: Gtk.Label | None = None
        self.cmp_metric_combo: HistoryChoice | None = None
        self._compare_legend_items: list[tuple[str, str]] = []
        self._updating_cmp_metric_combo = False
        self._compare_generation = 0
        self._compare_job: JobHandle | None = None

    def _get_repository(self) -> ActivityRepository:
        return self.app.database.repository

    def refresh(self) -> None:
        """Full reload from the stats table.  Safe to call from GLib.idle_add."""
        self._reload_everything()

    def refresh_units(self) -> None:
        """Rebind visible summaries and charts after a unit preference change."""
        if self.lbl_scope is None:
            return
        self._populate_compare_metric_combo()
        self._bind_summary(self._displayed)
        self._bind_list(self._displayed)
        self._redraw_compare_chart()

    def _populate_compare_metric_combo(self) -> None:
        """Build compare-metric labels using the active display unit system."""
        if self.cmp_metric_combo is None:
            return
        self._updating_cmp_metric_combo = True
        try:
            self.cmp_metric_combo.remove_all()
            options = (
                ("hr", "Heart Rate (BPM)"),
                ("pace", f"Pace (min/{unit_label('pace', self.app.unit_system)})"),
                ("speed", f"Speed ({unit_label('speed', self.app.unit_system)})"),
                ("power", "Power (W)"),
                ("cadence", "Cadence (spm/rpm)"),
            )
            for metric_id, label in options:
                self.cmp_metric_combo.append(metric_id, label)
            self.cmp_metric_combo.set_active_id(self._cmp_metric_id)
        finally:
            self._updating_cmp_metric_combo = False

    def append_activity(self, activity_id: int) -> None:
        """Partial refresh: add a newly-computed activity card to the list.

        Call this after ``StatsCalculator.compute_for_activity(activity_id)``
        so the just-finished workout appears immediately without re-querying
        the whole table.
        """
        repository = self._get_repository()
        row = repository.get_activity_stats(activity_id)
        if row is None:
            logger.warning(f"append_activity: no stats row for {activity_id}")
            return

        # Add to internal cache; resort and rebind so current sort order is preserved
        self._displayed.append(row)
        self._heart_rate_series[activity_id] = repository.list_heart_rate_series(
            [activity_id],
        ).get(activity_id, [])
        if self._listbox:
            # Re-apply active sort and rebuild the listbox/summary bindings
            self._resort_and_rebind()

    def _build_history_controls(self) -> Gtk.FlowBox:
        """Build the filter and sort controls."""
        # Top controls (filter + sort), grouped so they wrap as pairs
        self.filter_combo = HistoryChoice()
        self.filter_combo.append("week", "Last 7 Days")
        self.filter_combo.append("month", "Last 30 Days")
        self.filter_combo.append("all", "All Time")
        self.filter_combo.append("failed", "Failed Uploads")
        self.filter_combo.set_active_id(self.filter_id)
        self.filter_combo.connect("changed", self._on_filter_changed)

        self.sort_combo = HistoryChoice()
        self.sort_combo.append("date_desc", "Newest first")
        self.sort_combo.append("date_asc", "Oldest first")
        self.sort_combo.append("dur_desc", "Longest time")
        self.sort_combo.append("dist_desc", "Longest distance")
        self.sort_combo.append("avghr_desc", "Highest HR")
        self.sort_combo.set_active_id(self.sort_id)
        self.sort_combo.connect("changed", self._on_sort_changed)

        def control_pair(label_text: str, widget: Gtk.Widget) -> Gtk.Widget:
            """Group an accessible choice control with its caption."""
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            label = Gtk.Label(label=label_text, xalign=0)
            label.add_css_class("caption")
            label.add_css_class("dim-label")
            widget.update_property([Gtk.AccessibleProperty.LABEL], [label_text])
            row.append(label)
            row.append(widget)
            return row

        ctrl_wrap = Gtk.FlowBox()
        ctrl_wrap.set_selection_mode(Gtk.SelectionMode.NONE)
        ctrl_wrap.set_min_children_per_line(2)
        ctrl_wrap.set_max_children_per_line(2)
        ctrl_wrap.set_homogeneous(True)
        ctrl_wrap.set_row_spacing(8)
        ctrl_wrap.set_column_spacing(4)

        ctrl_wrap.insert(control_pair("Show", self.filter_combo), -1)
        ctrl_wrap.insert(control_pair("Sort", self.sort_combo), -1)
        return ctrl_wrap

    def _build_activities_tab(self) -> Gtk.ListBox:
        """Build the activity list; the adaptive view owns its scrolling."""
        self._listbox = Gtk.ListBox()
        self._listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        return self._listbox

    def _build_compare_tab(self) -> tuple[Gtk.Box, Gtk.Frame]:
        """Build compact metric selection, a legend, and an expanding chart."""
        controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.cmp_metric_combo = HistoryChoice()
        self._populate_compare_metric_combo()
        self.cmp_metric_combo.connect("changed", self._on_cmp_metric_changed)
        self.cmp_metric_combo.update_property(
            [Gtk.AccessibleProperty.LABEL],
            ["Comparison metric"],
        )
        row.append(self.cmp_metric_combo)
        self._legend_menu = Gtk.MenuButton(label="0 selected")
        self._legend_menu.set_tooltip_text("Activities and line colors in this comparison")
        popover = Gtk.Popover()
        popover.add_css_class("content-width-popup")
        self._legend_popup_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        popup_scroller = self._legend_scroller(self._legend_popup_box)
        popup_scroller.set_propagate_natural_width(True)
        popover.set_child(popup_scroller)
        self._legend_menu.set_popover(popover)
        row.append(self._legend_menu)
        controls.append(row)
        self._legend_inline_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._legend_inline = self._legend_scroller(self._legend_inline_box)
        controls.append(self._legend_inline)

        self._cmp_chart = CompareChart()
        self._cmp_ax = self._cmp_chart.axes
        self._cmp_chart.style(self.app.chart_theme, zones=self.app.calculate_hr_zones())
        self._cmp_canvas = self._cmp_chart.canvas
        self._compare_plot = Gtk.Stack()
        self._compare_plot.set_vexpand(True)
        self._compare_plot.set_hexpand(True)
        self._compare_plot.add_named(self._cmp_canvas, "chart")
        self._compare_message = Gtk.Label(label="Select activities to compare.", wrap=True)
        self._compare_message.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self._compare_message.set_justify(Gtk.Justification.CENTER)
        self._compare_message.set_margin_start(12)
        self._compare_message.set_margin_end(12)
        self._compare_plot.add_named(self._compare_message, "message")
        self._compare_plot.set_visible_child_name("message")
        frame = Gtk.Frame()
        frame.set_hexpand(True)
        frame.set_vexpand(True)
        frame.set_child(self._compare_plot)
        return controls, frame

    @staticmethod
    def _legend_scroller(child: Gtk.Widget) -> Gtk.ScrolledWindow:
        """Keep comparison identities readable within a bounded scroll area."""
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(64)
        scroller.set_max_content_height(320)
        scroller.set_propagate_natural_height(True)
        scroller.set_vexpand(True)
        scroller.set_child(child)
        return scroller

    def build_page(self) -> Gtk.Widget:
        """Build history with one portrait scroll area or independent landscape panes."""
        self._top_controls = self._build_history_controls()
        self.summary_box = self._build_summary_header()
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        sidebar.append(self._top_controls)
        sidebar.append(self.summary_box)
        activities = self._build_activities_tab()
        controls, frame = self._build_compare_tab()
        self.layout = HistoryLayout(
            sidebar=sidebar,
            activities=activities,
            summary_metrics=self._summary_metrics,
            compare_controls=controls,
            compare_frame=frame,
            legend=self._legend_inline,
            legend_menu=self._legend_menu,
            chart=cast("CompareChart", self._cmp_chart),
        )
        self.stack = self.layout.stack
        GLib.idle_add(self._reload_everything)
        return self.layout

    # ---- Summary header (totals) ----
    def _build_summary_header(self) -> Gtk.Widget:
        """Build the activity count and aggregate metrics for the current view."""
        frame = Gtk.Frame()
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(body, f"set_margin_{side}")(6)
        heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.lbl_scope = Gtk.Label(label="In view:", xalign=0, wrap=True)
        self.lbl_scope.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.lbl_total_acts = Gtk.Label(label="0 activities", xalign=0, wrap=True)
        self.lbl_total_acts.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        heading.append(self.lbl_scope)
        body.append(heading)
        self._summary_metrics = Gtk.FlowBox()
        self._summary_metrics.set_selection_mode(Gtk.SelectionMode.NONE)
        self._summary_metrics.set_homogeneous(True)
        self._summary_metrics.set_min_children_per_line(2)
        self._summary_metrics.set_max_children_per_line(2)
        self.lbl_total_dur = Gtk.Label(label="0:00", xalign=0)
        self.lbl_total_dist = Gtk.Label(label="—", xalign=0)
        self.lbl_avg_hr = Gtk.Label(label="—", xalign=0)
        for name, value in (
            ("Activities", self.lbl_total_acts),
            ("Time", self.lbl_total_dur),
            ("Distance", self.lbl_total_dist),
            ("Avg HR", self.lbl_avg_hr),
        ):
            cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            caption = Gtk.Label(label=name, xalign=0)
            caption.add_css_class("caption")
            caption.add_css_class("dim-label")
            value.add_css_class("history-summary-value")
            value.set_wrap(True)
            value.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            cell.append(caption)
            cell.append(value)
            self._summary_metrics.insert(cell, -1)
        body.append(self._summary_metrics)
        frame.set_child(body)
        return frame

    # ---- Event handlers ----
    def _on_filter_changed(self, combo: HistoryChoice) -> None:
        """Reload history when a valid activity filter is selected."""
        choice = combo.get_active_id()
        if choice is None:
            return
        self.filter_id = choice
        GLib.idle_add(self._reload_everything)

    def _on_sort_changed(self, combo: HistoryChoice) -> None:
        """Reorder displayed activities using the selected sort choice."""
        choice = combo.get_active_id()
        if choice is None:
            return
        self.sort_id = choice
        GLib.idle_add(self._resort_and_rebind)

    def _on_cmp_metric_changed(self, combo: HistoryChoice) -> None:
        """Rebuild comparison data for the selected metric."""
        if self._updating_cmp_metric_combo:
            return
        choice = combo.get_active_id()
        if choice is None:
            return
        self._cmp_metric_id = choice
        self._redraw_compare_chart()

    # ---- Data fetchers ----
    def _filter_cutoff(self, filter_id: str | None = None) -> dt.datetime | None:
        """Return the local cutoff for a week or month filter, or None."""
        now = dt.datetime.now().astimezone()
        selected_filter = self.filter_id if filter_id is None else filter_id
        if selected_filter == "week":
            return now - dt.timedelta(days=7)
        if selected_filter == "month":
            return now - dt.timedelta(days=30)
        return None

    def _fetch_stats_rows(self, filter_id: str | None = None) -> list[ActivityStatsRow]:
        """Single SELECT against activity_stats with optional cutoff filter."""
        repository = self._get_repository()
        selected_filter = self.filter_id if filter_id is None else filter_id
        if selected_filter == "failed":
            return repository.list_failed_activity_stats()
        cutoff = self._filter_cutoff(filter_id)
        # The repository compares the stored UTC value with the local-aware cutoff.
        return repository.list_activity_stats(cutoff)

    def _sort_rows(self, rows: list[ActivityStatsRow]) -> list[ActivityStatsRow]:
        key_funcs: dict[str, Callable[[ActivityStatsRow], float]] = {
            "date_desc": lambda r: -(r.start_time.timestamp() if r.start_time else 0),
            "date_asc": lambda r: r.start_time.timestamp() if r.start_time else 0,
            "dur_desc": lambda r: -(r.duration_s or 0),
            "dist_desc": lambda r: -(r.distance_m or 0),
            "avghr_desc": lambda r: -(r.avg_bpm or 0),
        }
        rows.sort(key=key_funcs.get(self.sort_id, key_funcs["date_desc"]))
        return rows

    # ------------------------------------------------------------------
    # Reload helpers
    # ------------------------------------------------------------------

    def _reload_everything(self) -> bool:
        # Avoid kicking off multiple concurrent backfills if refresh is
        # requested repeatedly while one is already running.
        if self._stats_backfill_in_progress:
            self._stats_reload_pending = True
            return False
        self._stats_backfill_in_progress = True
        self._stats_reload_pending = False

        stat_calc = self.app.database.stat_calc
        requested_filter = self.filter_id

        def _finish_reload(result: _HistoryReloadResult) -> None:
            """Run lightweight UI updates on the GTK main thread."""
            if result.filter_id != self.filter_id:
                self._stats_reload_pending = True
                return
            rows = self._sort_rows(result.rows)
            self._displayed = rows
            self._heart_rate_series = result.heart_rate_series
            self._bind_summary(rows)
            self._bind_list(rows)
            self._redraw_compare_chart()

        def work(token: CancellationToken) -> _HistoryReloadResult:
            token.raise_if_cancelled()
            stat_calc.compute_all(force=False)
            token.raise_if_cancelled()
            rows = self._fetch_stats_rows(requested_filter)
            heart_rate_series = self._get_repository().list_heart_rate_series(
                [stats.activity_id for stats in rows],
            )
            return _HistoryReloadResult(requested_filter, rows, heart_rate_series)

        def on_error(error: Exception) -> None:
            logger.error("History stats backfill failed: {}", error)
            self.app.show_toast(f"History refresh failed: {error}")

        def on_finally() -> None:
            self._stats_backfill_in_progress = False
            if self._stats_reload_pending:
                self._stats_reload_pending = False
                self._reload_everything()

        def on_discard() -> None:
            self._stats_backfill_in_progress = False
            self._stats_reload_pending = False

        try:
            self.app.jobs.submit(
                "stats-backfill",
                work,
                on_success=_finish_reload,
                on_error=on_error,
                on_finally=on_finally,
                on_discard=on_discard,
            )
        except DuplicateJobError:
            self._stats_backfill_in_progress = False
            logger.debug("History stats backfill is already running")

        # Returning False removes the idle source that invoked this method.
        # The actual UI reload will happen in _finish_reload once the
        # background work completes.
        return False  # GLib.idle_add return value

    def _resort_and_rebind(self) -> bool:
        """Re-sort the already-fetched rows without hitting the DB again."""
        rows = self._sort_rows(list(self._displayed))
        self._displayed = rows
        self._bind_list(rows)
        self._bind_summary(rows)
        self._redraw_compare_chart()
        return False

    # ---- Bind summary ----
    def _bind_summary(self, rows: list[ActivityStatsRow]) -> None:
        """Show aggregates for selected activities or the entire filtered view."""
        if self.lbl_scope is None:
            return
        subset = (
            [r for r in rows if r.activity_id in self.selected_ids] if self.selected_ids else rows
        )
        self.lbl_scope.set_text("Selected:" if self.selected_ids else "In view:")
        self.lbl_total_acts.set_text(str(len(subset)))

        total_dur = sum(r.duration_s or 0 for r in subset)
        self.lbl_total_dur.set_text(
            format_duration(
                float(total_dur),
                DurationStyle.CLOCK,
                always_hours=False,
                pad_hours=False,
                pad_minutes=False,
            ),
        )

        total_distance_m = sum((r.distance_m or 0.0) for r in subset)
        self.lbl_total_dist.set_text(
            format_distance(
                total_distance_m,
                self.app.unit_system,
                empty="—",
            ),
        )

        avgs = [r.avg_bpm for r in subset if r.avg_bpm is not None]
        self.lbl_avg_hr.set_text(f"{statistics.mean(avgs):.0f} bpm" if avgs else "—")

    # ---- Bind list ----
    def _bind_list(self, items: list[ActivityStatsRow]) -> None:
        # Clear
        if not self._listbox:
            return
        self._sparkline_charts.clear()
        child = self._listbox.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self._listbox.remove(child)
            child = nxt

        if not items:
            empty = Gtk.Label(label="No activities in this time window.")
            empty.set_wrap(True)
            empty.set_xalign(0.5)
            row = Gtk.ListBoxRow()
            row.set_child(empty)
            self._listbox.append(row)
            return

        for stats in items:
            row = self._make_activity_row(
                stats,
                self._heart_rate_series.get(stats.activity_id, []),
            )
            self._listbox.append(row)

    def _append_activity_header(self, box: Gtk.Box, stats: ActivityStatsRow) -> None:
        """Keep the full date above the activity's metrics and actions."""
        local_start = _tz_aware_localize(stats.start_time)
        title = Gtk.Label(label=local_start.strftime("%a, %b %d • %I:%M %p"))
        _configure_activity_title(title)
        title.set_tooltip_text(local_start.strftime("%Y-%m-%d %H:%M:%S %Z"))
        box.append(title)

    def _append_activity_actions(
        self,
        box: Gtk.Box,
        stats: ActivityStatsRow,
        samples: list[tuple[int, int]],
    ) -> None:
        """Keep compare selection and export separate from each other."""
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        spark = self._build_sparkline(samples)
        filler = spark if spark is not None else Gtk.Box()
        filler.set_hexpand(True)
        footer.append(filler)
        chk = Gtk.CheckButton(label="Compare")
        chk.set_active(stats.activity_id in self.selected_ids)
        chk.set_tooltip_text("Include this activity in the comparison")
        chk.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [f"Compare activity from {_tz_aware_localize(stats.start_time):%Y-%m-%d %H:%M}"],
        )
        chk.connect(
            "toggled",
            lambda current, aid=stats.activity_id: self._on_select_toggle(
                aid,
                active=current.get_active(),
            ),
        )
        footer.append(chk)
        export_btn = Gtk.Button.new_from_icon_name("document-save-symbolic")
        export_btn.set_tooltip_text("Export this activity to a TCX file")
        export_btn.update_property([Gtk.AccessibleProperty.LABEL], ["Export activity as TCX"])
        export_btn.connect(
            "clicked",
            lambda _button, aid=stats.activity_id: self._on_export_clicked(aid),
        )
        footer.append(export_btn)
        box.append(footer)

    def _activity_metric_parts(
        self,
        stats: ActivityStatsRow,
        sport: SportTypesEnum,
    ) -> list[str]:
        """Format the metrics shown in an activity row."""
        duration_text = format_duration(
            float(stats.duration_s or 0),
            DurationStyle.CLOCK,
            always_hours=False,
            pad_hours=False,
            pad_minutes=False,
        )
        distance_text = format_distance(stats.distance_m, self.app.unit_system, empty="—")
        parts = [duration_text, distance_text]
        if sport == SportTypesEnum.running and stats.avg_speed_mps:
            parts.append(
                format_pace(
                    stats.avg_speed_mps,
                    self.app.unit_system,
                    empty="—",
                    include_unit=True,
                ),
            )
        elif sport == SportTypesEnum.biking and stats.avg_speed_mps:
            parts.append(
                format_speed(stats.avg_speed_mps, self.app.unit_system, include_unit=True),
            )
        parts.append(f"Avg {_format_float(stats.avg_bpm, 'bpm', 0)}")
        if stats.max_bpm is not None:
            parts.append(f"Max {stats.max_bpm} bpm")
        if stats.avg_cadence is not None:
            unit = "spm" if sport == SportTypesEnum.running else "rpm"
            cadence = display_cadence(stats.avg_cadence, sport)
            parts.append(f"{cadence} {unit}")
        if stats.avg_power_watts is not None:
            parts.append(f"{round(stats.avg_power_watts)} W")
        if stats.total_ascent_m:
            parts.append(
                f"↑ {format_distance(stats.total_ascent_m, self.app.unit_system)}",
            )
        parts.append(sport.name)
        return parts

    def _append_activity_metrics(
        self,
        box: Gtk.Box,
        stats: ActivityStatsRow,
        sport: SportTypesEnum,
    ) -> None:
        """Append the formatted metric summary to an activity row."""
        parts = self._activity_metric_parts(stats, sport)
        metrics_lbl = Gtk.Label(label="  ·  ".join(parts))
        metrics_lbl.add_css_class("dim-label")
        metrics_lbl.set_wrap(True)
        metrics_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        metrics_lbl.set_xalign(0)
        metrics_lbl.set_can_focus(False)
        metrics_lbl.set_focusable(False)
        metrics_lbl.set_selectable(False)
        box.append(metrics_lbl)

    def _make_activity_row(
        self,
        stats: ActivityStatsRow,
        heart_rate_series: list[tuple[int, int]],
    ) -> Gtk.ListBoxRow:
        """Build an activity card with metrics, comparison selection, and export."""
        sport = SportTypesEnum(stats.sport_type_id)
        row = Gtk.ListBoxRow()
        frame = Gtk.Frame()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for margin in ("top", "bottom"):
            getattr(box, f"set_margin_{margin}")(8)
        for margin in ("start", "end"):
            getattr(box, f"set_margin_{margin}")(4)

        self._append_activity_header(box, stats)
        self._append_activity_metrics(box, stats, sport)

        self._append_activity_actions(box, stats, heart_rate_series)

        frame.set_child(box)
        row.set_child(frame)
        return row

    def _build_sparkline(self, samples: list[tuple[int, int]]) -> Gtk.Widget | None:
        """Build a Cairo sparkline from the already-batched heart-rate points."""
        if not samples:
            return None

        t0 = samples[0][0]
        xs = [(timestamp_ms - t0) / 1000.0 for timestamp_ms, _bpm in samples]
        ys = [bpm for _timestamp_ms, bpm in samples]

        sparkline = Sparkline(xs, ys, self.app.chart_theme)
        sparkline.canvas.set_size_request(40, -1)
        sparkline.canvas.add_css_class("history-sparkline")
        sparkline.canvas.update_property(
            [Gtk.AccessibleProperty.LABEL],
            ["Heart rate during activity"],
        )
        self._sparkline_charts.append(sparkline)
        return sparkline.canvas

    def refresh_theme(self) -> None:
        """Restyle compare and sparkline charts after a desktop theme change."""
        self._redraw_compare_chart()
        for sparkline in self._sparkline_charts:
            sparkline.refresh_theme(self.app.chart_theme)

    def _on_select_toggle(self, act_id: int, *, active: bool) -> None:
        if active:
            self.selected_ids.add(act_id)
        else:
            self.selected_ids.discard(act_id)
        self._bind_summary(self._displayed)
        self._redraw_compare_chart()

    # ---- Compare chart ----
    def _cancel_compare_job(self) -> None:
        """Cancel the previous compare query before starting a new one."""
        if self._compare_job is not None:
            self._compare_job.cancel()
            self._compare_job = None

    def _redraw_compare_chart(self) -> None:
        """Request comparison data for the current selection and metric."""
        if self._cmp_ax is None or self._cmp_canvas is None:
            return
        ax = self._cmp_ax
        canvas = self._cmp_canvas
        ax.clear()
        # Draw HR zones only for the HR metric
        hr_mode = self._cmp_metric_id == "hr"
        self._apply_chart_style(ax, draw_hr_zones=hr_mode)
        ax.set_xlabel("Elapsed time", color=self.app.chart_theme.foreground)
        canvas.draw_idle()

        self._compare_generation += 1
        generation = self._compare_generation
        self._cancel_compare_job()

        activities = tuple(
            sorted(
                (
                    CompareActivity(
                        activity_id=row.activity_id,
                        sport=SportTypesEnum(row.sport_type_id),
                        start_time=row.start_time,
                    )
                    for row in self._displayed
                    if row.activity_id in self.selected_ids
                ),
                key=lambda activity: activity.activity_id,
            ),
        )
        if not activities:
            self._render_compare_message("Select activities to compare.")
            canvas.draw_idle()
            return

        metric = cast("CompareMetric", self._cmp_metric_id)
        request = CompareChartRequest(
            generation=generation,
            metric=metric,
            unit_system=self.app.unit_system,
            activities=activities,
            max_points=MAX_COMPARE_POINTS,
        )

        def work(token: CancellationToken) -> CompareChartData:
            return build_compare_chart_data(request, self._get_repository(), token)

        def on_success(data: CompareChartData) -> None:
            if data.generation != self._compare_generation:
                return
            self._compare_job = None
            self._render_compare_chart(data)

        def on_error(error: Exception) -> None:
            if generation != self._compare_generation:
                return
            self._compare_job = None
            logger.error("History compare chart failed: {}", error)
            self._render_compare_message("Compare data unavailable.")
            canvas.draw_idle()

        def on_finally() -> None:
            if generation == self._compare_generation:
                self._compare_job = None

        self._compare_job = self.app.jobs.submit(
            f"history-compare-{generation}",
            work,
            on_success=on_success,
            on_error=on_error,
            on_finally=on_finally,
        )

    def _render_compare_message(self, message: str) -> None:
        """Replace the comparison plot with an explanatory message."""
        self._compare_message.set_text(message)
        self._compare_plot.set_visible_child_name("message")
        self._set_compare_legend([])

    def _set_compare_legend(self, items: list[tuple[str, str]]) -> None:
        """Expose line identities without placing long legends over the data."""
        self._compare_legend_items = items
        count = sum(row.activity_id in self.selected_ids for row in self._displayed)
        self._legend_menu.set_label(f"{count} selected")
        for box in (self._legend_inline_box, self._legend_popup_box):
            while child := box.get_first_child():
                box.remove(child)
            for title, color in items:
                label = Gtk.Label(xalign=0, wrap=True)
                label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
                if box is self._legend_inline_box:
                    label.set_max_width_chars(28)
                label.set_markup(
                    f'<span foreground="{color}">━</span> {GLib.markup_escape_text(title)}',
                )
                label.update_property([Gtk.AccessibleProperty.LABEL], [title])
                box.append(label)
            if not items and count == 0:
                label = Gtk.Label(label="Select activities on the Activities tab.", wrap=True)
                label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
                label.set_max_width_chars(24)
                box.append(label)

    def _render_compare_chart(self, data: CompareChartData) -> None:
        """Render prepared series with external legends and consistent scaling."""
        if self._cmp_ax is None or self._cmp_canvas is None or self._cmp_chart is None:
            return
        ax = self._cmp_ax
        canvas = self._cmp_canvas
        if not data.series:
            self._render_compare_message("No data available for the chosen metric.")
            canvas.draw_idle()
            return

        def mmss(x: float, _pos: float) -> str:
            return format_duration(x, DurationStyle.COUNTDOWN, pad_minutes=False)

        legend_items = []
        for series in data.series:
            label = _tz_aware_localize(series.start_time).strftime("%Y-%m-%d %H:%M")
            plot_ys = [float("nan") if y is None else y for y in series.ys]
            (line,) = ax.plot(series.xs, plot_ys, lw=2, label=label)
            legend_items.append((label, to_hex(line.get_color())))

        if data.max_time_s > 0:
            ax.set_xlim(0, data.max_time_s)
            ax.xaxis.set_major_formatter(FuncFormatter(mmss))

        self._set_compare_legend(legend_items)
        self._compare_plot.set_visible_child_name("chart")
        ylabels = {
            "hr": "BPM",
            "pace": f"Pace (min/{unit_label('pace', self.app.unit_system)})",
            "speed": f"Speed ({unit_label('speed', self.app.unit_system)})",
            "power": "Watts",
            "cadence": "Cadence (spm/rpm)",
        }
        ax.set_ylabel(
            ylabels.get(data.metric, ""),
            color=self.app.chart_theme.foreground,
        )

        if data.metric == "pace":
            ax.invert_yaxis()
        self._cmp_chart.set_display_scale(self.layout.chart_display_scale)
        canvas.draw_idle()

    # Export
    def _prepare_export(
        self,
        act_id: int,
        token: CancellationToken,
    ) -> _HistoryExportResult:
        repository = self._get_repository()
        token.raise_if_cancelled()
        act = repository.get_activity(act_id)
        if not act:
            message = "Activity not found"
            raise _HistoryExportError(message)
        hrs = repository.list_heart_rates(act_id)
        runs = repository.list_running_metrics(act_id)
        cycles = repository.list_cycling_metrics(act_id)
        locations = repository.list_location_points(act_id)
        stats_row = repository.get_activity_stats(act_id)
        token.raise_if_cancelled()
        sport_type = (
            SportTypesEnum(stats_row.sport_type_id)
            if stats_row
            else infer_sport(hrs, runs, cycles, act_id)
        )
        if sport_type == SportTypesEnum.unknown:
            message = f"Cannot export: Unknown sport for activity {act_id}"
            logger.warning(message)
            raise _HistoryExportError(message)
        tcx_bytes = activity_to_tcx(
            act=act,
            heart_rates=hrs,
            running=runs,
            cycling=cycles,
            locations=locations,
            sport_type=sport_type,
        )
        local_start = _tz_aware_localize(act.start_time)
        default_name = f"{local_start.strftime('%Y-%m-%d_%H-%M-%S')}_{sport_type.name}.tcx"
        return _HistoryExportResult(default_name, tcx_bytes)

    def _show_export_dialog(self, result: _HistoryExportResult) -> None:
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Save TCX")
        init_file = Gio.File.new_for_path(result.default_name)
        dialog.set_initial_file(init_file)
        filter_tcx = Gtk.FileFilter()
        filter_tcx.set_name("TCX files")
        filter_tcx.add_suffix("tcx")
        dialog.set_default_filter(filter_tcx)

        def on_save_done(_dialog: Gtk.FileDialog, response: Gio.AsyncResult) -> None:
            try:
                gfile = dialog.save_finish(response)
                if not gfile:
                    return
                gfile = _tcx_output_target(gfile)

                def on_write_done(
                    target: Gio.File,
                    write_response: Gio.AsyncResult,
                    _user_data: object | None = None,
                ) -> None:
                    try:
                        target.replace_contents_finish(write_response)
                        self.app.show_toast(f"Saved: {target.get_parse_name()}")
                    except Exception as error:
                        self.app.show_toast(f"Save failed: {error}")

                gfile.replace_contents_async(
                    result.tcx_bytes,
                    None,
                    make_backup=False,
                    flags=Gio.FileCreateFlags.REPLACE_DESTINATION,
                    cancellable=None,
                    callback=on_write_done,
                )
            except Exception as error:
                self.app.show_toast(f"Save failed: {error}")

        dialog.save(self.app.window, None, on_save_done)

    def _on_export_clicked(self, act_id: int) -> None:
        def on_error(error: Exception) -> None:
            message = (
                str(error) if isinstance(error, _HistoryExportError) else f"Export failed: {error}"
            )
            self.app.show_toast(message)

        try:
            self.app.jobs.submit(
                f"history-export-{act_id}",
                lambda token: self._prepare_export(act_id, token),
                on_success=self._show_export_dialog,
                on_error=on_error,
            )
        except DuplicateJobError:
            self.app.show_toast("Export already in progress")

    # ------------------------------------------------------------------
    # Chart style helper
    # ------------------------------------------------------------------

    def _apply_chart_style(self, ax: Axes, *, draw_hr_zones: bool = True) -> None:
        style_axes(
            ax,
            self.app.chart_theme,
            zones=self.app.calculate_hr_zones() if draw_hr_zones else None,
        )
