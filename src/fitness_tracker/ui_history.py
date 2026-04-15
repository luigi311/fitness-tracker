import datetime
import math
import statistics
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import gi
import numpy as np
from loguru import logger
from matplotlib.backends.backend_gtk4agg import FigureCanvasGTK4Agg as FigureCanvas
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter

from fitness_tracker.activity_stats import ActivityStats
from fitness_tracker.database import (
    Activity,
    CyclingMetrics,
    HeartRate,
    RunningMetrics,
    SportTypesEnum,
)
from fitness_tracker.exporters import activity_to_tcx, infer_sport

gi.require_versions({"Gtk": "4.0", "Adw": "1"})
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402  # ty:ignore[unresolved-import]


def _format_hms(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def _rolling(ys: list[float | None], window: int, use_median: bool = False) -> list[float | None]:
    """Rolling mean/median over `window` samples, ignoring None values.

    Returns a list the same length as `ys`. Bins with no valid samples stay None.
    """
    if window <= 1 or not ys:
        return ys
    arr = np.array([np.nan if v is None else float(v) for v in ys], dtype=float)
    n = len(arr)
    half = window // 2
    out: list[float | None] = [None] * n
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        seg = arr[lo:hi]
        seg = seg[~np.isnan(seg)]
        if seg.size == 0:
            continue
        out[i] = float(np.median(seg) if use_median else np.mean(seg))
    return out


def _format_pace_from_mps(mps: float) -> str:
    if mps <= 0.01:
        return "—"
    mph = mps * 2.23693629
    mins_per_mile = 60.0 / max(mph, 1e-6)
    mm = int(mins_per_mile)
    ss = round((mins_per_mile - mm) * 60)
    if ss == 60:
        mm += 1
        ss = 0
    return f"{mm}:{ss:02d} min/mi"


def _pace_min_per_mile_from_mps(mps: float) -> float:
    if mps <= 0.01:
        return float("inf")
    mph = mps * 2.23693629
    return 60.0 / mph


def _format_distance_m(distance_m: float | None) -> str:
    if not distance_m:
        return "—"
    miles = distance_m * 0.00062137119
    return f"{miles:.2f} mi"


def _format_float(v: float | None, unit: str = "", digits: int = 0) -> str:
    if v is None:
        return "—"
    fmt = f"{{:.{digits}f}}"
    return (fmt.format(v) + (f" {unit}" if unit else "")).strip()


def _tz_aware_localize(dt: datetime.datetime) -> datetime.datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone()


def _safe_avg(vals: Iterable[float]) -> float | None:
    vals = [v for v in vals if v is not None]
    return (sum(vals) / len(vals)) if vals else None


# ---------- History Page UI ----------


class HistoryPageUI:
    def __init__(self, app) -> None:
        self.app = app

        # State
        self.filter_id: str = app.history_filter or "week"
        self.sort_id: str = "date_desc"
        self.selected_ids: set[int] = set()

        self._listbox: Gtk.ListBox | None = None
        # Cached flat stats rows (ActivityStats ORM objects) in display order.
        self._displayed: list[ActivityStats] = []

        # Compare chart
        self._cmp_fig = None
        self._cmp_ax = None
        self._cmp_canvas = None
        self._cmp_metric_id = "hr"

    def refresh(self) -> None:
        """Full reload from the stats table.  Safe to call from GLib.idle_add."""
        self._reload_everything()

    def append_activity(self, activity_id: int) -> None:
        """Partial refresh: add a newly-computed activity card to the list.

        Call this after ``StatsCalculator.compute_for_activity(activity_id)``
        so the just-finished workout appears immediately without re-querying
        the whole table.
        """
        if not self.app.recorder:
            return
        with self.app.recorder.db.Session() as session:
            row = session.query(ActivityStats).filter_by(activity_id=activity_id).one_or_none()
            if row is None:
                logger.warning(f"append_activity: no stats row for {activity_id}")
                return

        # Add to internal cache; resort and rebind so current sort order is preserved
        self._displayed.append(row)
        if self._listbox:
            # Re-apply active sort and rebuild the listbox/summary bindings
            self._resort_and_rebind()

    def build_page(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for m in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{m}")(12)

        # Top controls (filter + sort), grouped so they wrap as pairs
        self.filter_combo = Gtk.ComboBoxText()
        self.filter_combo.append("week", "Last 7 Days")
        self.filter_combo.append("month", "Last 30 Days")
        self.filter_combo.append("all", "All Time")
        self.filter_combo.set_active_id(self.filter_id)
        self.filter_combo.connect("changed", self._on_filter_changed)

        self.sort_combo = Gtk.ComboBoxText()
        self.sort_combo.append("date_desc", "Date (newest)")
        self.sort_combo.append("date_asc", "Date (oldest)")
        self.sort_combo.append("dur_desc", "Duration (longest)")
        self.sort_combo.append("dist_desc", "Distance (longest)")
        self.sort_combo.append("avghr_desc", "Avg HR (highest)")
        self.sort_combo.set_active_id(self.sort_id)
        self.sort_combo.connect("changed", self._on_sort_changed)

        def control_pair(label_text: str, widget: Gtk.Widget) -> Gtk.FlowBoxChild:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            # tighten margins so pairs feel compact and aligned
            for m in ("top", "bottom", "start", "end"):
                getattr(row, f"set_margin_{m}")(0)

            lbl = Gtk.Label(label=label_text)
            lbl.add_css_class("dim-label")
            lbl.set_xalign(0)

            # keep the combo from stretching the whole width
            widget.set_hexpand(False)
            # give it a sensible width so both pairs can sit on one line on wider phones
            if hasattr(widget, "set_width_chars"):
                widget.set_width_chars(14)
            widget.set_size_request(160, -1)

            row.append(lbl)
            row.append(widget)

            child = Gtk.FlowBoxChild()
            child.set_child(row)
            return child

        ctrl_wrap = Gtk.FlowBox()
        ctrl_wrap.set_selection_mode(Gtk.SelectionMode.NONE)
        ctrl_wrap.set_max_children_per_line(2)  # 2 pairs per row when there’s room
        ctrl_wrap.set_row_spacing(8)
        ctrl_wrap.set_column_spacing(12)

        ctrl_wrap.insert(control_pair("Show", self.filter_combo), -1)
        ctrl_wrap.insert(control_pair("Sort", self.sort_combo), -1)

        outer.append(ctrl_wrap)

        # Summary header (totals in the filtered window)
        self.summary_box = self._build_summary_header()
        outer.append(self.summary_box)

        # Tabs: Activities / Compare
        self.stack = Adw.ViewStack()
        self.stack.set_vexpand(True)

        # Activities tab
        activities_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        self._listbox = Gtk.ListBox()
        self._listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        scroller.set_child(self._listbox)
        activities_box.append(scroller)

        # Compare tab
        compare_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        # Controls for compare
        ctrl_cmp = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        lbl_metric = Gtk.Label(label="Metric")
        lbl_metric.add_css_class("dim-label")
        lbl_metric.set_xalign(0)
        self.cmp_metric_combo = Gtk.ComboBoxText()
        # Order here defines display order
        self.cmp_metric_combo.append("hr", "Heart Rate (BPM)")
        self.cmp_metric_combo.append("pace", "Pace (min/mi)")
        self.cmp_metric_combo.append("speed", "Speed (mph)")
        self.cmp_metric_combo.append("power", "Power (W)")
        self.cmp_metric_combo.append("cadence", "Cadence (spm/rpm)")
        self.cmp_metric_combo.set_active_id(self._cmp_metric_id)
        self.cmp_metric_combo.connect("changed", self._on_cmp_metric_changed)
        ctrl_cmp.append(lbl_metric)
        ctrl_cmp.append(self.cmp_metric_combo)
        compare_box.append(ctrl_cmp)

        compare_box.append(Gtk.Label(label="Compare selected activities (toggle on each card)."))
        cmp_frame = Gtk.Frame()
        self._cmp_fig = Figure(figsize=(6, 3), dpi=96, constrained_layout=True)
        self._cmp_ax = self._cmp_fig.add_subplot(111)

        # Initial style (HR as default)
        self._apply_chart_style(self._cmp_ax, draw_hr_zones=True)
        self._cmp_ax.set_xlabel("Time (s)", color=self.app.DARK_FG)
        # Y label will be set by _redraw_compare_chart() based on metric

        self._cmp_canvas = FigureCanvas(self._cmp_fig)
        self._cmp_canvas.set_vexpand(True)
        cmp_frame.set_child(self._cmp_canvas)
        compare_box.append(cmp_frame)

        page = self.stack.add_titled(activities_box, "activities", "Activities")
        page.set_icon_name("view-list-symbolic")

        page = self.stack.add_titled(compare_box, "compare", "Compare")
        page.set_icon_name("media-playlist-consecutive-symbolic")

        # Switcher for small screens
        switch = Adw.ViewSwitcherBar()
        switch.set_stack(self.stack)
        switch.set_reveal(True)

        outer.append(self.stack)
        outer.append(switch)

        # Initial load
        GLib.idle_add(self._reload_everything)
        return outer

    # ---- Summary header (totals) ----
    def _build_summary_header(self) -> Gtk.Widget:
        frame = Gtk.Frame()
        grid = Gtk.Grid(column_spacing=6, row_spacing=6)
        for m in ("top", "bottom", "start", "end"):
            getattr(grid, f"set_margin_{m}")(6)

        self.lbl_scope = Gtk.Label(label="In view:")
        self.lbl_scope.set_xalign(0)

        self.lbl_total_acts = Gtk.Label(label="0 activities")
        self.lbl_total_acts.set_xalign(0)
        self.lbl_total_dur = Gtk.Label(label="0:00")
        self.lbl_total_dur.set_xalign(0)
        self.lbl_total_dist = Gtk.Label(label="—")
        self.lbl_total_dist.set_xalign(0)
        self.lbl_avg_hr = Gtk.Label(label="—")
        self.lbl_avg_hr.set_xalign(0)

        grid.attach(self.lbl_scope, 0, 0, 1, 1)
        grid.attach(self.lbl_total_acts, 1, 0, 1, 1)
        grid.attach(Gtk.Label(label="Time:"), 0, 1, 1, 1)
        grid.attach(self.lbl_total_dur, 1, 1, 1, 1)
        grid.attach(Gtk.Label(label="Distance:"), 0, 2, 1, 1)
        grid.attach(self.lbl_total_dist, 1, 2, 1, 1)
        grid.attach(Gtk.Label(label="Avg HR:"), 0, 3, 1, 1)
        grid.attach(self.lbl_avg_hr, 1, 3, 1, 1)

        frame.set_child(grid)
        return frame

    # ---- Event handlers ----
    def _on_filter_changed(self, combo: Gtk.ComboBoxText) -> None:
        self.filter_id = combo.get_active_id()
        GLib.idle_add(self._reload_everything)

    def _on_sort_changed(self, combo: Gtk.ComboBoxText) -> None:
        self.sort_id = combo.get_active_id()
        GLib.idle_add(self._resort_and_rebind)

    def _on_cmp_metric_changed(self, combo: Gtk.ComboBoxText) -> None:
        self._cmp_metric_id = combo.get_active_id()
        self._redraw_compare_chart()

    # ---- Data fetchers ----
    def _filter_cutoff(self) -> datetime.datetime | None:
        now = datetime.datetime.now().astimezone()
        if self.filter_id == "week":
            return now - datetime.timedelta(days=7)
        if self.filter_id == "month":
            return now - datetime.timedelta(days=30)
        return None

    def _fetch_stats_rows(self) -> list[ActivityStats]:
        """Single SELECT against activity_stats with optional cutoff filter."""
        if not self.app.recorder:
            return []

        cutoff = self._filter_cutoff()
        with self.app.recorder.db.Session() as session:
            q = session.query(ActivityStats).filter(
                ActivityStats.sport_type_id != SportTypesEnum.unknown.value,
            )
            if cutoff:
                # start_time is stored UTC-aware; cutoff is local-aware — both
                # are comparable once Python converts to UTC internally.
                q = q.filter(ActivityStats.start_time >= cutoff)
            return q.all()

    def _sort_rows(self, rows: list[ActivityStats]) -> list[ActivityStats]:
        key_funcs: dict[str, object] = {
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
        if not self.app.recorder:
            return False

        # Avoid kicking off multiple concurrent backfills if refresh is
        # requested repeatedly while one is already running.
        if getattr(self, "_stats_backfill_in_progress", False):
            return False
        self._stats_backfill_in_progress = True

        def _finish_reload() -> bool:
            """Run lightweight UI updates on the GTK main thread."""
            try:
                rows = self._sort_rows(self._fetch_stats_rows())
                self._displayed = rows
                self._bind_summary(rows)
                self._bind_list(rows)
                self._redraw_compare_chart()
            finally:
                # Mark backfill as finished, even if something went wrong.
                self._stats_backfill_in_progress = False
            # Returning False removes this idle source.
            return False

        def _worker() -> None:
            try:
                # Perform potentially expensive computation off the main loop.
                self.app.recorder.stat_calc.compute_all(force=False)
            finally:
                # Schedule UI update back on the GTK main thread.
                GLib.idle_add(_finish_reload)

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()

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
    def _bind_summary(self, rows: list[ActivityStats]) -> None:
        subset = (
            [r for r in rows if r.activity_id in self.selected_ids] if self.selected_ids else rows
        )
        self.lbl_scope.set_text("Selected:" if self.selected_ids else "In view:")
        self.lbl_total_acts.set_text(f"{len(subset)} activities")

        total_dur = sum(r.duration_s or 0 for r in subset)
        self.lbl_total_dur.set_text(_format_hms(total_dur))

        miles = sum((r.distance_m or 0.0) * 0.00062137119 for r in subset)
        self.lbl_total_dist.set_text(f"{miles:.2f} mi" if miles > 0 else "—")

        avgs = [r.avg_bpm for r in subset if r.avg_bpm is not None]
        self.lbl_avg_hr.set_text(f"{statistics.mean(avgs):.0f} bpm" if avgs else "—")

    # ---- Bind list ----
    def _bind_list(self, items: list[ActivityStats]) -> None:
        # Clear
        if not self._listbox:
            return
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
            row = self._make_activity_row(stats)
            setattr(row, "_activity_id", stats.activity_id)
            self._listbox.append(row)

    def _make_activity_row(self, stats: ActivityStats) -> Gtk.ListBoxRow:
        sport = SportTypesEnum(stats.sport_type_id)
        local_start = _tz_aware_localize(stats.start_time)

        row = Gtk.ListBoxRow()
        frame = Gtk.Frame()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(8)

        # Header line: date/time + compare toggle
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        title = Gtk.Label(label=local_start.strftime("%a, %b %d • %I:%M %p"))
        title.add_css_class("title-3")
        title.set_hexpand(True)
        title.set_xalign(0)
        head.append(title)

        # Export button
        export_btn = Gtk.Button.new_with_label("Export")
        export_btn.add_css_class("flat")
        export_btn.set_has_frame(False)
        export_btn.set_tooltip_text("Export this activity to a TCX file")
        export_btn.connect(
            "clicked",
            lambda _b, aid=stats.activity_id: self._on_export_clicked(aid),
        )
        head.append(export_btn)

        chk = Gtk.CheckButton()
        chk.set_active(stats.activity_id in self.selected_ids)
        chk.set_tooltip_text("Select for Compare")
        chk.connect(
            "toggled",
            lambda cb, aid=stats.activity_id: self._on_select_toggle(aid, cb.get_active()),
        )
        head.append(chk)
        box.append(head)

        # Add a click gesture to the whole row box
        gesture = Gtk.GestureClick.new()
        gesture.connect(
            "released",
            lambda _g, _n, _x, _y, aid=stats.activity_id, cb=chk: cb.set_active(
                not cb.get_active(),
            ),
        )
        box.add_controller(gesture)

        # Metrics
        metrics_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        metrics_box.set_can_focus(False)

        def add_chip(text: str) -> None:
            lbl = Gtk.Label(label=text)
            lbl.add_css_class("dim-label")
            lbl.set_can_focus(False)
            lbl.set_focusable(False)
            lbl.set_selectable(False)
            metrics_box.append(lbl)

        add_chip(_format_hms(stats.duration_s or 0))
        add_chip(_format_distance_m(stats.distance_m))

        parts = [_format_hms(stats.duration_s or 0), _format_distance_m(stats.distance_m)]

        if sport == SportTypesEnum.running and stats.avg_speed_mps:
            parts.append(_format_pace_from_mps(stats.avg_speed_mps))
        elif sport == SportTypesEnum.biking and stats.avg_speed_mps:
            mph = stats.avg_speed_mps * 2.23693629
            parts.append(f"{mph:.1f} mph")

        parts.append(f"Avg {_format_float(stats.avg_bpm, 'bpm', 0)}")

        if stats.max_bpm is not None:
            parts.append(f"Max {stats.max_bpm} bpm")

        if stats.avg_cadence is not None:
            unit = "spm" if sport == SportTypesEnum.running else "rpm"
            parts.append(f"{round(stats.avg_cadence)} {unit}")

        if stats.avg_power_watts is not None:
            parts.append(f"{round(stats.avg_power_watts)} W")

        if stats.total_energy_kj and stats.total_energy_kj > 0:
            parts.append(f"{stats.total_energy_kj:.1f} kJ")

        if stats.total_ascent_m:
            parts.append(f"↑ {stats.total_ascent_m:.0f} m")

        parts.append(sport.name)

        metrics_lbl = Gtk.Label(label="  ·  ".join(parts))
        metrics_lbl.add_css_class("dim-label")
        metrics_lbl.set_wrap(True)
        metrics_lbl.set_wrap_mode(0)
        metrics_lbl.set_xalign(0)
        metrics_lbl.set_can_focus(False)
        metrics_lbl.set_focusable(False)
        metrics_lbl.set_selectable(False)
        box.append(metrics_lbl)

        # Tiny sparkline (HR)
        spark = self._build_sparkline(stats.activity_id)
        if spark:
            box.append(spark)

        frame.set_child(box)
        row.set_child(frame)
        return row

    # Sparkline (targeted query — only when building the card)
    def _build_sparkline(self, act_id: int) -> Gtk.Widget | None:
        if not self.app.recorder:
            return None

        with self.app.recorder.db.Session() as session:
            hrs = (
                session.query(HeartRate.timestamp_ms, HeartRate.bpm)
                .filter_by(activity_id=act_id)
                .order_by(HeartRate.timestamp_ms)
                .all()
            )
        if not hrs:
            return None

        t0 = hrs[0].timestamp_ms
        xs = [(h.timestamp_ms - t0) / 1000.0 for h in hrs]
        ys = [h.bpm for h in hrs]

        fig = Figure(figsize=(2.5, 0.6), dpi=96)
        ax = fig.add_axes([0, 0, 1, 1])
        # Match app theme
        fig.patch.set_facecolor(self.app.DARK_BG)
        ax.set_facecolor(self.app.DARK_BG)
        ax.plot(xs, ys, lw=1.2)
        ax.axis("off")

        canvas = FigureCanvas(fig)
        canvas.set_size_request(260, 44)
        return canvas

    def _on_select_toggle(self, act_id: int, active: bool) -> None:
        if active:
            self.selected_ids.add(act_id)
        else:
            self.selected_ids.discard(act_id)
        self._bind_summary(self._displayed)
        self._redraw_compare_chart()

    # ---- Compare chart ----
    def _redraw_compare_chart(self) -> None:
        def mmss(x, _pos):
            m, s = divmod(int(max(0, x)), 60)
            return f"{m:d}:{s:02d}"

        if not self._cmp_ax:
            return
        ax = self._cmp_ax
        ax.clear()
        # Draw HR zones only for the HR metric
        hr_mode = self._cmp_metric_id == "hr"
        self._apply_chart_style(ax, draw_hr_zones=hr_mode)

        if not self.app.recorder or not self.selected_ids:
            ax.set_title("Select activities to compare.", color=self.app.DARK_FG)
            self._cmp_canvas.draw_idle()
            return

        max_t = 0.0
        any_series = False
        with self.app.recorder.db.Session() as session:
            for aid in sorted(self.selected_ids):
                act = session.get(Activity, aid)
                if not act:
                    continue
                label = _tz_aware_localize(act.start_time).strftime("%Y-%m-%d %H:%M")

                if self._cmp_metric_id == "hr":
                    hrs = (
                        session.query(HeartRate)
                        .filter_by(activity_id=aid)
                        .order_by(HeartRate.timestamp_ms)
                        .all()
                    )
                    if not hrs:
                        continue
                    t0 = hrs[0].timestamp_ms
                    xs = [(h.timestamp_ms - t0) / 1000.0 for h in hrs]
                    ys = [h.bpm for h in hrs]
                else:
                    # Retrieve the pre-computed sport from activity_stats
                    stats_row = (
                        session.query(ActivityStats).filter_by(activity_id=aid).one_or_none()
                    )
                    if not stats_row:
                        continue
                    sport = SportTypesEnum(stats_row.sport_type_id)

                    if sport == SportTypesEnum.running:
                        primary = (
                            session.query(RunningMetrics)
                            .filter_by(activity_id=aid)
                            .order_by(RunningMetrics.timestamp_ms)
                            .all()
                        )
                    elif sport == SportTypesEnum.biking:
                        primary = (
                            session.query(CyclingMetrics)
                            .filter_by(activity_id=aid)
                            .order_by(CyclingMetrics.timestamp_ms)
                            .all()
                        )
                    else:
                        continue

                    if not primary:
                        continue

                    t0 = primary[0].timestamp_ms
                    xs = [(p.timestamp_ms - t0) / 1000.0 for p in primary]

                    if self._cmp_metric_id == "pace":
                        if sport != SportTypesEnum.running:
                            continue
                        vals = [_pace_min_per_mile_from_mps(float(p.speed_mps)) for p in primary]
                        ys = [None if math.isinf(v) else v for v in vals]
                    elif self._cmp_metric_id == "speed":
                        ys = [
                            (float(p.speed_mps) * 2.23693629) if p.speed_mps is not None else None
                            for p in primary
                        ]
                    elif self._cmp_metric_id == "power":
                        ys = [
                            float(p.power_watts) if p.power_watts is not None else None
                            for p in primary
                        ]
                    elif self._cmp_metric_id == "cadence":
                        if sport == SportTypesEnum.running:
                            ys = [
                                float(p.cadence_spm) if p.cadence_spm is not None else None
                                for p in primary
                            ]
                        else:
                            ys = [
                                float(p.cadence_rpm) if p.cadence_rpm is not None else None
                                for p in primary
                            ]
                    else:
                        ys = []

                # Smooth: pace is spiky, use median; others use mean.
                # ~15s window for HR/power/cadence/speed/pace.
                # Estimate sample rate from xs to convert seconds -> samples.
                if len(xs) >= 2:
                    dt = (xs[-1] - xs[0]) / max(1, len(xs) - 1)
                    sample_hz = 1.0 / dt if dt > 0 else 1.0
                else:
                    sample_hz = 1.0

                window = max(3, int(round(15 * sample_hz)))
                ys = _rolling(ys, window, use_median=False)

                any_series = True
                if xs:
                    max_t = max(max_t, xs[-1])
                    ax.plot(xs, ys, lw=2, label=label)

        if not any_series:
            ax.set_title("No data available for the chosen metric.", color=self.app.DARK_FG)
            self._cmp_canvas.draw_idle()
            return

        if max_t > 0:
            ax.set_xlim(0, max_t)
            ax.xaxis.set_major_formatter(FuncFormatter(mmss))
            leg = ax.legend(loc="lower right", frameon=True, ncol=1)
            leg.get_frame().set_facecolor(self.app.DARK_BG)
            leg.get_frame().set_edgecolor(self.app.DARK_GRID)
            for t in leg.get_texts():
                t.set_color(self.app.DARK_FG)

        # Y-axis label per metric
        ylabels = {
            "hr": "BPM",
            "pace": "Pace (min/mi)",
            "speed": "Speed (mph)",
            "power": "Watts",
            "cadence": "Cadence (spm/rpm)",
        }
        ax.set_ylabel(ylabels.get(self._cmp_metric_id, ""), color=self.app.DARK_FG)

        # Invert Y for pace so faster (lower min/mi) appears higher
        if self._cmp_metric_id == "pace":
            ax.invert_yaxis()

        self._cmp_canvas.draw_idle()

    # Export
    def _on_export_clicked(self, act_id: int) -> None:
        if not self.app.recorder:
            return

        # Build a default filename from the activity start time
        with self.app.recorder.db.Session() as session:
            act = session.get(Activity, act_id)
            if not act:
                self.app.show_toast("Activity not found")
                return
            local_start = _tz_aware_localize(act.start_time)

            # Gather samples
            hrs = (
                session.query(HeartRate)
                .filter_by(activity_id=act_id)
                .order_by(HeartRate.timestamp_ms)
                .all()
            )
            runs = (
                session.query(RunningMetrics)
                .filter_by(activity_id=act_id)
                .order_by(RunningMetrics.timestamp_ms)
                .all()
            )
            cycles = (
                session.query(CyclingMetrics)
                .filter_by(activity_id=act_id)
                .order_by(CyclingMetrics.timestamp_ms)
                .all()
            )
            stats_row = session.query(ActivityStats).filter_by(activity_id=act_id).one_or_none()

        sport_type = (
            SportTypesEnum(stats_row.sport_type_id)
            if stats_row
            else infer_sport(hrs, runs, cycles, act_id)
        )

        if sport_type == SportTypesEnum.unknown:
            msg = f"Cannot export: Unknown sport for activity {act_id}"
            logger.warning(msg)
            self.app.show_toast(msg)
            return

        default_name = f"{local_start.strftime('%Y-%m-%d_%H-%M-%S')}_{sport_type.name}.tcx"

        try:
            tcx_bytes = activity_to_tcx(
                act=act,
                heart_rates=hrs,
                running=runs,
                cycling=cycles,
                sport_type=sport_type,
            )
        except Exception as e:
            self.app.show_toast(f"Export failed: {e}")
            return

        # File save dialog
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Save TCX")
        # Suggest default name
        init_file = Gio.File.new_for_path(default_name)
        dialog.set_initial_file(init_file)
        # Limit to .tcx by default (still lets user change)
        filter_tcx = Gtk.FileFilter()
        filter_tcx.set_name("TCX files")
        filter_tcx.add_suffix("tcx")
        dialog.set_default_filter(filter_tcx)

        def _on_save_done(_dlg, res):
            try:
                gfile = dialog.save_finish(res)
                if not gfile:
                    return  # user cancelled
                # Ensure .tcx extension
                path = gfile.get_path() or default_name
                if not path.lower().endswith(".tcx"):
                    path += ".tcx"
                # Write bytes
                out = Gio.File.new_for_path(path)
                out.replace_contents(
                    tcx_bytes,
                    None,  # etag
                    False,  # make_backup
                    Gio.FileCreateFlags.REPLACE_DESTINATION,
                    None,  # cancellable
                )
                self.app.show_toast(f"Saved: {path}")
            except Exception as e:
                self.app.show_toast(f"Save failed: {e}")

        dialog.save(self.app.window, None, _on_save_done)

    # ------------------------------------------------------------------
    # Chart style helper
    # ------------------------------------------------------------------

    def _apply_chart_style(self, ax, draw_hr_zones: bool = True) -> None:
        if draw_hr_zones:
            self.app.draw_zones(ax)
        ax.figure.patch.set_facecolor(self.app.DARK_BG)
        ax.set_facecolor(self.app.DARK_BG)
        ax.xaxis.label.set_color(self.app.DARK_FG)
        ax.yaxis.label.set_color(self.app.DARK_FG)
        ax.tick_params(colors=self.app.DARK_FG)
        ax.grid(color=self.app.DARK_GRID)
