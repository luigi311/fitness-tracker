import dataclasses
import datetime
import math
import statistics
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

import gi
import numpy as np
from matplotlib.backends.backend_gtk4agg import FigureCanvasGTK4Agg as FigureCanvas
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter

from fitness_tracker.database import Activity, HeartRate, RunningMetrics

gi.require_versions({"Gtk": "4.0", "Adw": "1"})
from gi.repository import GLib, Gtk, Adw  # noqa: E402


# ---------- Helpers / small data structures ----------


@dataclasses.dataclass
class ActivitySummary:
    id: int
    start_local: datetime.datetime
    end_local: datetime.datetime
    duration_s: int
    distance_m: Optional[float]
    avg_bpm: Optional[float]
    max_bpm: Optional[int]
    avg_cadence: Optional[float]
    avg_power: Optional[float]
    total_energy_kj: float


def _safe_avg(vals: Iterable[float]) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return (sum(vals) / len(vals)) if vals else None


def _format_hms(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def _format_pace_from_mps(mps: float) -> str:
    if mps <= 0.01:
        return "—"
    # default pace in min/mi; switch to min/km if the app wants that later
    mph = mps * 2.23693629
    mins_per_mile = 60.0 / max(mph, 1e-6)
    mm = int(mins_per_mile)
    ss = int(round((mins_per_mile - mm) * 60))
    if ss == 60:
        mm += 1
        ss = 0
    return f"{mm}:{ss:02d} min/mi"


def _pace_min_per_mile_from_mps(mps: float) -> float:
    """Return numeric minutes-per-mile for plotting; inf for effectively stopped."""
    if mps <= 0.01:
        return float("inf")
    mph = mps * 2.23693629
    return 60.0 / mph


def _format_distance_m(distance_m: Optional[float]) -> str:
    if not distance_m:
        return "—"
    miles = distance_m * 0.00062137119
    return f"{miles:.2f} mi"


def _format_float(v: Optional[float], unit: str = "", digits: int = 0) -> str:
    if v is None:
        return "—"
    fmt = f"{{:.{digits}f}}"
    return (fmt.format(v) + (f" {unit}" if unit else "")).strip()


def _tz_aware_localize(dt: datetime.datetime) -> datetime.datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone()


# ---------- History Page UI ----------


class HistoryPageUI:
    def __init__(self, app: "FitnessAppUI"):
        self.app = app

        # State
        self.filter_id = app.history_filter or "week"  # "week" | "month" | "all"
        self.sort_id = "date_desc"  # default sort
        self.selected_ids: set[int] = set()
        self._listbox: Gtk.ListBox | None = None
        self._last_items: list[ActivitySummary] = []

        # Compare chart
        self._cmp_fig = None
        self._cmp_ax = None
        self._cmp_canvas = None

        # Compare metric selection
        self._cmp_metric_id = "hr"

    def refresh(self):
        # Safe to call from GLib.idle_add
        self._reload_everything()

    # ---- Public: build page ----
    def build_page(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
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
        self._listbox.set_activate_on_single_click(True)
        self._listbox.connect("row-activated", self._on_row_activated)
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
        self.cmp_metric_combo.append("cadence", "Cadence (spm)")
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

        self.stack.add_titled(activities_box, "activities", "Activities")
        self.stack.add_titled(compare_box, "compare", "Compare")

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
        grid = Gtk.Grid(column_spacing=12, row_spacing=6)
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
    def _on_filter_changed(self, combo: Gtk.ComboBoxText):
        self.filter_id = combo.get_active_id()
        GLib.idle_add(self._reload_everything)

    def _on_sort_changed(self, combo: Gtk.ComboBoxText):
        self.sort_id = combo.get_active_id()
        GLib.idle_add(self._reload_list_only)

    def _on_row_activated(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow):
        # row child holds an attribute with the activity id
        act_id = getattr(row, "_activity_id", None)
        if not act_id:
            return
        self._open_details_dialog(act_id)

    # ---- Load & bind ----
    def _reload_everything(self):
        # Update summary + list + compare chart
        summaries = self._fetch_summaries()
        self._last_items = summaries
        self._bind_summary(summaries)
        self._bind_list(summaries)
        self._redraw_compare_chart()
        return False

    def _reload_list_only(self):
        summaries = self._fetch_summaries()
        self._last_items = summaries
        self._bind_list(summaries)
        self._redraw_compare_chart()
        # keep the header in sync too
        self._bind_summary(summaries)
        return False

    # ---- Data fetchers ----
    def _filter_cutoff(self) -> Optional[datetime.datetime]:
        now = datetime.datetime.now().astimezone()
        if self.filter_id == "week":
            return now - datetime.timedelta(days=7)
        if self.filter_id == "month":
            return now - datetime.timedelta(days=30)
        return None

    def _fetch_summaries(self) -> list[ActivitySummary]:
        # Handle test mode or first-run (no recorder)
        if self.app.test_mode or not self.app.recorder:
            return []

        cutoff = self._filter_cutoff()
        out: list[ActivitySummary] = []
        with self.app.recorder.db.Session() as session:
            acts = session.query(Activity).order_by(Activity.start_time.desc()).all()
            for act in acts:
                st = _tz_aware_localize(act.start_time)
                if cutoff and st < cutoff:
                    continue
                et = (
                    _tz_aware_localize(act.end_time)
                    if act.end_time
                    else datetime.datetime.now().astimezone()
                )
                dur_s = max(0, int((et - st).total_seconds()))

                # HR stats
                hrs = list(act.heart_rates)
                if hrs:
                    bpms = [h.bpm for h in hrs]
                    avg_bpm = sum(bpms) / len(bpms)
                    max_bpm = max(bpms)
                    total_kj = sum(h.energy_kj or 0.0 for h in hrs)
                else:
                    avg_bpm = None
                    max_bpm = None
                    total_kj = 0.0

                # Running stats (distance, cadence, power)
                runs = list(act.running_metrics)
                if runs:
                    # distance: prefer last non-None total_distance_m
                    dists = [r.total_distance_m for r in runs if r.total_distance_m is not None]
                    distance_m = dists[-1] if dists else None
                    avg_cad = _safe_avg([float(r.cadence_spm) for r in runs])
                    avg_pow = _safe_avg(
                        [float(r.power_watts) for r in runs if r.power_watts is not None]
                    )
                else:
                    distance_m = None
                    avg_cad = None
                    avg_pow = None

                out.append(
                    ActivitySummary(
                        id=int(act.id),
                        start_local=st,
                        end_local=et,
                        duration_s=dur_s,
                        distance_m=distance_m,
                        avg_bpm=avg_bpm,
                        max_bpm=max_bpm if max_bpm is not None else None,
                        avg_cadence=avg_cad,
                        avg_power=avg_pow,
                        total_energy_kj=float(total_kj),
                    )
                )

        # Sorting
        key_funcs = {
            "date_desc": lambda a: (-a.start_local.timestamp()),
            "date_asc": lambda a: (a.start_local.timestamp()),
            "dur_desc": lambda a: (-a.duration_s),
            "dist_desc": lambda a: (-(a.distance_m or -1)),
            "avghr_desc": lambda a: (-(a.avg_bpm or -1)),
        }
        keyf = key_funcs.get(self.sort_id, key_funcs["date_desc"])
        out.sort(key=keyf)
        return out

    # ---- Bind summary ----
    def _bind_summary(self, items: list[ActivitySummary]):
        # If there is a selection, summarize only those items
        if self.selected_ids:
            items = [a for a in items if a.id in self.selected_ids]
            self.lbl_scope.set_text("Selected:")
        else:
            self.lbl_scope.set_text("In view:")

        self.lbl_total_acts.set_text(f"{len(items)} activities")

        total_dur = sum(a.duration_s for a in items)
        self.lbl_total_dur.set_text(_format_hms(total_dur))

        miles_sum = sum((a.distance_m or 0.0) * 0.00062137119 for a in items)
        self.lbl_total_dist.set_text(f"{miles_sum:.2f} mi" if miles_sum > 0 else "—")

        avgs = [a.avg_bpm for a in items if a.avg_bpm is not None]
        self.lbl_avg_hr.set_text(f"{statistics.mean(avgs):.0f} bpm" if avgs else "—")

    # ---- Bind list ----
    def _bind_list(self, items: list[ActivitySummary]):
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

        for a in items:
            row = self._make_activity_row(a)
            setattr(row, "_activity_id", a.id)
            self._listbox.append(row)

    def _make_activity_row(self, a: ActivitySummary) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        frame = Gtk.Frame()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(8)

        # Header line: date/time + compare toggle
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        title = Gtk.Label(label=a.start_local.strftime("%a, %b %d • %I:%M %p"))
        title.add_css_class("title-3")
        title.set_hexpand(True)
        title.set_xalign(0)
        head.append(title)

        chk = Gtk.CheckButton()
        chk.set_active(a.id in self.selected_ids)
        chk.set_tooltip_text("Select for Compare")
        chk.connect("toggled", lambda cb, aid=a.id: self._on_select_toggle(aid, cb.get_active()))
        head.append(chk)
        box.append(head)

        # Metrics line (wrap on small screens)
        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_max_children_per_line(12)

        def chip(text: str):
            l = Gtk.Label(label=text)
            l.add_css_class("dim-label")
            c = Gtk.FlowBoxChild()
            c.set_child(l)
            return c

        flow.insert(chip(f"{_format_hms(a.duration_s)}"), -1)
        flow.insert(chip(f"{_format_distance_m(a.distance_m)}"), -1)

        # Show pace if running distance exists
        pace_chip = None
        if a.distance_m and a.duration_s > 0:
            mps = (a.distance_m or 0.0) / max(a.duration_s, 1)
            pace_chip = chip(_format_pace_from_mps(mps))
            flow.insert(pace_chip, -1)

        flow.insert(chip(f"Avg {_format_float(a.avg_bpm, 'bpm', 0)}"), -1)
        if a.max_bpm is not None:
            flow.insert(chip(f"Max {a.max_bpm} bpm"), -1)
        if a.avg_cadence is not None:
            flow.insert(chip(f"{int(round(a.avg_cadence))} spm"), -1)
        if a.avg_power is not None:
            flow.insert(chip(f"{int(round(a.avg_power))} W"), -1)
        if a.total_energy_kj > 0:
            flow.insert(chip(f"{a.total_energy_kj:.1f} kJ"), -1)

        box.append(flow)

        # Tiny sparkline (HR)
        spark = self._build_sparkline(a.id)
        if spark:
            box.append(spark)

        frame.set_child(box)
        row.set_child(frame)
        return row

    def _build_sparkline(self, act_id: int) -> Optional[Gtk.Widget]:
        if self.app.test_mode or not self.app.recorder:
            return None

        with self.app.recorder.db.Session() as session:
            hrs = (
                session.query(HeartRate)
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

    def _on_select_toggle(self, act_id: int, active: bool):
        if active:
            self.selected_ids.add(act_id)
        else:
            self.selected_ids.discard(act_id)
        # Recompute header using cached list
        if self._last_items:
            self._bind_summary(self._last_items)
        self._redraw_compare_chart()

    # ---- Compare chart ----
    def _redraw_compare_chart(self):
        if not self._cmp_ax:
            return
        ax = self._cmp_ax
        ax.clear()
        # Draw HR zones only for the HR metric
        hr_mode = self._cmp_metric_id == "hr"
        self._apply_chart_style(ax, draw_hr_zones=hr_mode)

        if self.app.test_mode or not self.app.recorder or not self.selected_ids:
            ax.set_title("Select activities to compare.", color=self.app.DARK_FG)
            self._cmp_canvas.draw_idle()
            return

        max_t = 0.0
        any_series = False
        with self.app.recorder.db.Session() as session:
            for aid in sorted(self.selected_ids):
                label = _tz_aware_localize(session.get(Activity, aid).start_time).strftime(
                    "%Y-%m-%d %H:%M"
                )

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
                    runs = (
                        session.query(RunningMetrics)
                        .filter_by(activity_id=aid)
                        .order_by(RunningMetrics.timestamp_ms)
                        .all()
                    )
                    if not runs:
                        continue
                    t0 = runs[0].timestamp_ms
                    xs = [(r.timestamp_ms - t0) / 1000.0 for r in runs]
                    if self._cmp_metric_id == "pace":
                        # minutes per mile; use None when stopped (drops from plot)
                        vals = [
                            (
                                _pace_min_per_mile_from_mps(float(r.speed_mps))
                                if r.speed_mps is not None
                                else math.inf
                            )
                            for r in runs
                        ]
                        ys = [None if (v is None or math.isinf(v)) else v for v in vals]
                    elif self._cmp_metric_id == "speed":
                        ys = [
                            ((float(r.speed_mps) * 2.23693629) if r.speed_mps is not None else None)
                            for r in runs
                        ]
                    elif self._cmp_metric_id == "power":
                        ys = [
                            (float(r.power_watts) if r.power_watts is not None else None)
                            for r in runs
                        ]
                    elif self._cmp_metric_id == "cadence":
                        ys = [
                            float(r.cadence_spm) if r.cadence_spm is not None else None
                            for r in runs
                        ]
                    else:
                        ys = []

                    # Drop None values to avoid gaps/NaNs; keep aligned xs
                    xs, ys = (
                        zip(*[(x, y) for x, y in zip(xs, ys) if y is not None]) if ys else ([], [])
                    )
                    xs, ys = list(xs), list(ys)

                    if not xs:
                        continue

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

            def mmss(x, _pos):
                m, s = divmod(int(max(0, x)), 60)
                return f"{m:d}:{s:02d}"

            ax.xaxis.set_major_formatter(FuncFormatter(mmss))
            leg = ax.legend(
                loc="lower right",
                frameon=True,
                ncol=1,
            )
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
            "cadence": "Cadence (spm)",
        }
        ax.set_ylabel(ylabels.get(self._cmp_metric_id, ""), color=self.app.DARK_FG)

        # Invert Y for pace so faster (lower min/mi) appears higher
        if self._cmp_metric_id == "pace":
            ax.invert_yaxis()

        self._cmp_canvas.draw_idle()

    # ---- Details dialog ----
    def _open_details_dialog(self, act_id: int):
        if self.app.test_mode or not self.app.recorder:
            return

        dlg = Adw.Dialog()
        dlg.set_title("Activity Details")

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(content, f"set_margin_{m}")(12)

        # Read data
        with self.app.recorder.db.Session() as session:
            act = session.get(Activity, act_id)
            st = _tz_aware_localize(act.start_time)
            et = (
                _tz_aware_localize(act.end_time)
                if act.end_time
                else datetime.datetime.now().astimezone()
            )
            dur_s = max(0, int((et - st).total_seconds()))

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

        # Header title + quick stats
        hdr = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title = Gtk.Label(label=st.strftime("%A, %B %d • %I:%M %p"))
        title.add_css_class("title-2")
        title.set_xalign(0)
        hdr.append(title)

        # chips
        chips = Gtk.FlowBox()
        chips.set_selection_mode(Gtk.SelectionMode.NONE)

        def chip(text: str):
            l = Gtk.Label(label=text)
            l.add_css_class("dim-label")
            c = Gtk.FlowBoxChild()
            c.set_child(l)
            return c

        # compute summaries
        if hrs:
            bpm_vals = [h.bpm for h in hrs]
            avg_bpm = sum(bpm_vals) / len(bpm_vals)
            max_bpm = max(bpm_vals)
            total_kj = sum(h.energy_kj or 0.0 for h in hrs)
        else:
            avg_bpm = None
            max_bpm = None
            total_kj = 0.0

        if runs:
            dists = [r.total_distance_m for r in runs if r.total_distance_m is not None]
            distance_m = dists[-1] if dists else None
            avg_cad = _safe_avg([float(r.cadence_spm) for r in runs])
            avg_pow = _safe_avg([float(r.power_watts) for r in runs if r.power_watts is not None])
            avg_speed = _safe_avg([float(r.speed_mps) for r in runs])
        else:
            distance_m = None
            avg_cad = None
            avg_pow = None
            avg_speed = None

        chips.insert(chip(_format_hms(dur_s)), -1)
        chips.insert(chip(_format_distance_m(distance_m)), -1)
        if distance_m and dur_s:
            chips.insert(chip(_format_pace_from_mps((distance_m or 0) / max(1, dur_s))), -1)
        chips.insert(chip(f"Avg {_format_float(avg_bpm, 'bpm', 0)}"), -1)
        if max_bpm is not None:
            chips.insert(chip(f"Max {max_bpm} bpm"), -1)
        if avg_cad is not None:
            chips.insert(chip(f"{int(round(avg_cad))} spm"), -1)
        if avg_pow is not None:
            chips.insert(chip(f"{int(round(avg_pow))} W"), -1)
        if total_kj > 0:
            chips.insert(chip(f"{total_kj:.1f} kJ"), -1)

        hdr.append(chips)
        content.append(hdr)

        # Charts list (stack vertically, no overflow)
        charts_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        sc = Gtk.ScrolledWindow()
        sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sc.set_vexpand(True)
        sc.set_child(charts_box)

        # HR chart
        if hrs:
            t0 = hrs[0].timestamp_ms
            xs = np.array([(h.timestamp_ms - t0) / 1000.0 for h in hrs])
            ys = np.array([h.bpm for h in hrs])

            fig = Figure(figsize=(6, 2.8), dpi=96)
            ax = fig.add_subplot(111)
            self._apply_chart_style(ax)
            ax.plot(xs, ys, lw=2)
            ax.set_xlabel("Time (s)", color=self.app.DARK_FG)
            ax.set_ylabel("BPM", color=self.app.DARK_FG)

            def mmss(x, _pos):
                m, s = divmod(int(max(0, x)), 60)
                return f"{m:d}:{s:02d}"

            ax.xaxis.set_major_formatter(FuncFormatter(mmss))

            canvas = FigureCanvas(fig)
            frm = Gtk.Frame(label="Heart Rate")
            frm.set_child(canvas)
            charts_box.append(frm)

        # Speed/Pace chart (from running metrics)
        if runs:
            t0 = runs[0].timestamp_ms
            xs = np.array([(r.timestamp_ms - t0) / 1000.0 for r in runs])
            speed = np.array([float(r.speed_mps) for r in runs])

            fig2 = Figure(figsize=(6, 2.6), dpi=96)
            ax2 = fig2.add_subplot(111)
            fig2.patch.set_facecolor(self.app.DARK_BG)
            ax2.set_facecolor(self.app.DARK_BG)
            ax2.grid(color=self.app.DARK_GRID)
            ax2.tick_params(colors=self.app.DARK_FG)
            ax2.xaxis.label.set_color(self.app.DARK_FG)
            ax2.yaxis.label.set_color(self.app.DARK_FG)

            # Plot pace as min/mi on the LEFT by transforming speed
            def pace_from_speed(mps):
                if mps <= 0.01:
                    return math.inf
                mph = mps * 2.23693629
                return 60.0 / mph  # minutes per mile

            paces = np.array([pace_from_speed(s) for s in speed])
            ax2.plot(xs, paces, lw=2)
            ax2.set_ylabel("Pace (min/mi)", color=self.app.DARK_FG)

            def mmss(x, _pos):
                m, s = divmod(int(max(0, x)), 60)
                return f"{m:d}:{s:02d}"

            ax2.xaxis.set_major_formatter(FuncFormatter(mmss))
            canvas2 = FigureCanvas(fig2)
            frm2 = Gtk.Frame(label="Pace")
            frm2.set_child(canvas2)
            charts_box.append(frm2)

            # Power chart (if present)
            pw = [r.power_watts for r in runs if r.power_watts is not None]
            if pw:
                pw_full = np.array([float(r.power_watts or 0.0) for r in runs])
                fig3 = Figure(figsize=(6, 2.4), dpi=96)
                ax3 = fig3.add_subplot(111)
                fig3.patch.set_facecolor(self.app.DARK_BG)
                ax3.set_facecolor(self.app.DARK_BG)
                ax3.grid(color=self.app.DARK_GRID)
                ax3.tick_params(colors=self.app.DARK_FG)
                ax3.xaxis.label.set_color(self.app.DARK_FG)
                ax3.yaxis.label.set_color(self.app.DARK_FG)
                ax3.plot(xs, pw_full, lw=2)
                ax3.set_ylabel("Watts", color=self.app.DARK_FG)
                ax3.xaxis.set_major_formatter(FuncFormatter(mmss))
                canvas3 = FigureCanvas(fig3)
                frm3 = Gtk.Frame(label="Power")
                frm3.set_child(canvas3)
                charts_box.append(frm3)

            # Cadence chart
            cad = np.array([float(r.cadence_spm) for r in runs])
            fig4 = Figure(figsize=(6, 2.2), dpi=96)
            ax4 = fig4.add_subplot(111)
            fig4.patch.set_facecolor(self.app.DARK_BG)
            ax4.set_facecolor(self.app.DARK_BG)
            ax4.grid(color=self.app.DARK_GRID)
            ax4.tick_params(colors=self.app.DARK_FG)
            ax4.xaxis.label.set_color(self.app.DARK_FG)
            ax4.yaxis.label.set_color(self.app.DARK_FG)
            ax4.plot(xs, cad, lw=2)
            ax4.set_ylabel("Cadence (spm)", color=self.app.DARK_FG)
            ax4.xaxis.set_major_formatter(FuncFormatter(mmss))
            canvas4 = FigureCanvas(fig4)
            frm4 = Gtk.Frame(label="Cadence")
            frm4.set_child(canvas4)
            charts_box.append(frm4)

            # Splits (per mile) if distance exists and monotonically increases
            dists = [r.total_distance_m for r in runs if r.total_distance_m is not None]
            if dists and dists[-1] and dists[-1] > 0:
                splits_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
                splits_frame = Gtk.Frame(label="Splits")
                mile_markers = [i * 1609.344 for i in range(1, int(dists[-1] // 1609.344) + 1)]
                last_mark = 0.0
                last_t = runs[0].timestamp_ms
                mm_list = []
                for mark in mile_markers:
                    # find first sample >= mark
                    for r in runs:
                        if r.total_distance_m is not None and r.total_distance_m >= mark:
                            dt_s = (r.timestamp_ms - last_t) / 1000.0
                            seg_m = r.total_distance_m - last_mark
                            pace = _format_pace_from_mps(seg_m / max(dt_s, 1))
                            mm_list.append(pace)
                            last_mark = r.total_distance_m or last_mark
                            last_t = r.timestamp_ms
                            break
                if mm_list:
                    # render simple list
                    for i, p in enumerate(mm_list, start=1):
                        splits_box.append(Gtk.Label(label=f"Mile {i}: {p}", xalign=0))
                    splits_frame.set_child(splits_box)
                    charts_box.append(splits_frame)

        # Put charts into dialog
        content.append(sc)

        # Close button
        btn = Gtk.Button(label="Close")
        btn.connect("clicked", lambda _b: dlg.close())
        content.append(btn)

        dlg.set_child(content)
        dlg.present(parent=self.app.window)

    # ---- Utilities (chart styling reused) ----
    def _on_cmp_metric_changed(self, combo: Gtk.ComboBoxText):
        self._cmp_metric_id = combo.get_active_id()
        self._redraw_compare_chart()

    def _apply_chart_style(self, ax, draw_hr_zones: bool = True):
        if draw_hr_zones:
            self.app.draw_zones(ax)
        ax.figure.patch.set_facecolor(self.app.DARK_BG)
        ax.set_facecolor(self.app.DARK_BG)
        ax.xaxis.label.set_color(self.app.DARK_FG)
        ax.yaxis.label.set_color(self.app.DARK_FG)
        ax.tick_params(colors=self.app.DARK_FG)
        ax.grid(color=self.app.DARK_GRID)
