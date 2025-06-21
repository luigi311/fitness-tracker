import datetime
import threading
from zoneinfo import ZoneInfo

import gi
import matplotlib.dates as mdates
from matplotlib.backends.backend_gtk4agg import FigureCanvasGTK4Agg as FigureCanvas
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter

from fitness_tracker.database import Activity, HeartRate

gi.require_versions({"Gtk": "4.0", "Adw": "1"})
from gi.repository import GLib, Gtk  # noqa: E402


class HistoryPageUI:
    def __init__(self, app: "FitnessAppUI"):
        self.app = app
        self.selected_activities: set[int] = set()
        self.activity_start_times: dict[int, datetime.datetime] = {}
        self.history_filter = app.history_filter

    def build_page(self) -> Gtk.Widget:
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for margin in ("top", "bottom", "start", "end"):
            getattr(vbox, f"set_margin_{margin}")(12)

        filter_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        filter_label = Gtk.Label(label="Show:")
        filter_box.append(filter_label)
        self.filter_combo = Gtk.ComboBoxText()
        for key, text in [("week", "Last 7 Days"), ("month", "This Month"), ("all", "All Time")]:
            self.filter_combo.append(key, text)
        self.filter_combo.set_active_id(self.history_filter)
        self.filter_combo.connect("changed", self._on_filter_changed)
        filter_box.append(self.filter_combo)
        vbox.append(filter_box)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        self.history_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        scroller.set_child(self.history_container)
        vbox.append(scroller)

        frame = Gtk.Frame(label="Activity Details")
        self.history_fig = Figure(figsize=(6, 3))
        self.history_ax = self.history_fig.add_subplot(111)
        self._apply_chart_style(self.history_ax)
        self.history_canvas = FigureCanvas(self.history_fig)
        self.history_canvas.set_vexpand(True)
        frame.set_child(self.history_canvas)
        vbox.append(frame)

        # Seed the initial summary plot
        GLib.idle_add(self.update_history_plot)

        return vbox

    def _on_filter_changed(self, combo: Gtk.ComboBoxText):
        self.history_filter = combo.get_active_id()
        # Clear existing UI elements on the main thread
        GLib.idle_add(self._clear_history)

        # redraw summary immediately
        GLib.idle_add(self.update_history_plot)

        # Reload history in background
        threading.Thread(target=self.load_history, daemon=True).start()

    def load_history(self):
        if not self.app.recorder:
            return
        Session = self.app.recorder.db.Session

        # Determine cutoff based on filter (use local now)
        now = datetime.datetime.now().astimezone()
        if self.history_filter == "week":
            cutoff = now - datetime.timedelta(days=7)
        elif self.history_filter == "month":
            cutoff = now - datetime.timedelta(days=30)
        else:
            cutoff = None

        last_date = None
        with Session() as session:
            activities = session.query(Activity).order_by(Activity.start_time.desc()).all()
            for act in activities:
                # Convert start to aware and local
                start = act.start_time
                if start.tzinfo is None:
                    start = start.replace(tzinfo=ZoneInfo("UTC"))
                start = start.astimezone()

                # Skip if before cutoff
                if cutoff and start < cutoff:
                    continue

                # Determine end, also aware and local
                if act.end_time:
                    end = act.end_time
                    if end.tzinfo is None:
                        end = end.replace(tzinfo=ZoneInfo("UTC"))
                    end = end.astimezone()
                else:
                    end = datetime.datetime.now().astimezone()

                # Group by calendar date
                date_only = start.date()
                if date_only != last_date:
                    GLib.idle_add(self._add_history_group_header, date_only)
                    last_date = date_only

                # Metrics and sparkline data
                hrs = list(act.heart_rates)
                if hrs:
                    start_ms = hrs[0].timestamp_ms
                    times = [(hr.timestamp_ms - start_ms) / 1000.0 for hr in hrs]
                    bpms = [hr.bpm for hr in hrs]
                    avg_bpm = sum(bpms) / len(bpms)
                    max_bpm = max(bpms)
                    total_kj = sum(hr.energy_kj or 0 for hr in hrs)
                else:
                    times = []
                    bpms = []
                    avg_bpm = max_bpm = total_kj = 0

                duration = end - start
                GLib.idle_add(
                    self._add_history_row,
                    act.id,
                    start,
                    duration,
                    avg_bpm,
                    max_bpm,
                    total_kj,
                    times,
                    bpms,
                )

    def _clear_history(self):
        child = self.history_container.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.history_container.remove(child)
            child = next_child

    def _add_history_group_header(self, date: datetime.date):
        header = Gtk.Label(label=date.strftime("%B %d, %Y"))
        header.get_style_context().add_class("heading")
        header.set_margin_top(12)
        header.set_margin_bottom(6)
        self.history_container.append(header)

    def _add_history_row(self, act_id, start, duration, avg_bpm, max_bpm, total_kj, times, bpms):
        self.activity_start_times[act_id] = start
        frame = Gtk.Frame()
        frame.set_margin_start(8)
        frame.set_margin_end(8)
        frame.set_margin_top(4)
        frame.set_margin_bottom(4)

        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        title = Gtk.Label(label=start.strftime("%Y-%m-%d %H:%M"))
        title.get_style_context().add_class("title")
        header_box.append(title)
        header_box.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        check = Gtk.CheckButton()
        check.set_active(act_id in self.selected_activities)
        check.connect(
            "toggled",
            lambda cb, aid=act_id: self._on_history_card_toggled(aid, cb.get_active()),
        )
        header_box.append(check)

        # make the whole frame tappable—but only if the user doesn't move too far
        # to avoid accidental toggles
        click = Gtk.GestureClick.new()
        start_point = {"x": 0.0, "y": 0.0}
        threshold = 25  # max pixels of movement allowed

        def on_pressed(gesture, n_press, x, y):
            start_point["x"], start_point["y"] = x, y

        def on_released(gesture, n_press, x, y):
            dx = abs(x - start_point["x"])
            dy = abs(y - start_point["y"])
            # only toggle if movement was small (i.e. a tap, not a drag)
            if dx <= threshold and dy <= threshold:
                check.set_active(not check.get_active())

        click.connect("pressed", on_pressed)
        click.connect("released", on_released)
        frame.add_controller(click)

        summary = (
            f"Dur: {int(duration.total_seconds() // 60)}m {int(duration.total_seconds() % 60)}s, "
            f"Avg: {int(avg_bpm)} BPM, Max: {max_bpm} BPM"
        )
        summary_label = Gtk.Label(label=summary)
        summary_label.set_halign(Gtk.Align.START)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        content.append(header_box)
        content.append(summary_label)

        if times and bpms:
            spark_fig = Figure(figsize=(2, 0.5), dpi=80)
            ax = spark_fig.add_axes([0, 0, 1, 1])
            (line_spark,) = ax.plot(times, bpms, lw=1)
            ax.axis("off")
            # Dark-mode background
            spark_fig.patch.set_facecolor(self.app.DARK_BG)
            ax.set_facecolor(self.app.DARK_BG)

            spark_canvas = FigureCanvas(spark_fig)
            spark_canvas.set_size_request(200, 50)
            content.append(spark_canvas)

        frame.set_child(content)
        self.history_container.append(frame)

    def _on_history_card_toggled(self, act_id: int, selected: bool):
        if selected:
            self.selected_activities.add(act_id)
        else:
            self.selected_activities.discard(act_id)
        self.update_history_plot()

    def _apply_chart_style(self, ax):
        # draw background HR zones based on current resting/max HR
        self.app.draw_zones(ax)
        # apply both figure and axis backgrounds
        ax.figure.patch.set_facecolor(self.app.DARK_BG)
        ax.set_facecolor(self.app.DARK_BG)
        ax.xaxis.label.set_color(self.app.DARK_FG)
        ax.yaxis.label.set_color(self.app.DARK_FG)
        ax.tick_params(colors=self.app.DARK_FG)
        ax.grid(color=self.app.DARK_GRID)

    def update_history_plot(self):
        # clear axes and re-apply styles
        self.history_ax.clear()
        self._apply_chart_style(self.history_ax)

        Session = self.app.recorder.db.Session
        with Session() as session:
            for aid in sorted(self.selected_activities):
                hrs = (
                    session.query(HeartRate)
                    .filter_by(activity_id=aid)
                    .order_by(HeartRate.timestamp_ms)
                    .all()
                )
                if not hrs:
                    continue
                start_ms = hrs[0].timestamp_ms
                times = [(h.timestamp_ms - start_ms) / 1000.0 for h in hrs]
                bpms = [h.bpm for h in hrs]
                label = self.activity_start_times[aid].astimezone().strftime("%Y-%m-%d %H:%M")
                self.history_ax.plot(times, bpms, lw=2, label=label)

        if self.selected_activities:
            self.history_ax.set_xlabel("Time (s)", color=self.app.DARK_FG)
            self.history_ax.set_ylabel("BPM", color=self.app.DARK_FG)

            # after plotting all series, compute the longest:
            global_max = 0.0
            for aid in self.selected_activities:
                hrs = (
                    session.query(HeartRate)
                        .filter_by(activity_id=aid)
                        .order_by(HeartRate.timestamp_ms)
                        .all()
                )
                if not hrs:
                    continue
                start_ms = hrs[0].timestamp_ms
                end_ms   = hrs[-1].timestamp_ms
                dur_sec  = (end_ms - start_ms) / 1000.0
                global_max = max(global_max, dur_sec)

            # now clamp from 0 → longest
            self.history_ax.set_xlim(0, global_max)


            # format ticks as MM:SS
            def mmss(x, pos):
                m, s = divmod(int(x), 60)
                return f"{m:d}:{s:02d}"

            self.history_ax.xaxis.set_major_formatter(FuncFormatter(mmss))

            leg = self.history_ax.legend(frameon=True)
            leg.get_frame().set_facecolor(self.app.DARK_BG)
            leg.get_frame().set_edgecolor(self.app.DARK_GRID)
            for text in leg.get_texts():
                text.set_color(self.app.DARK_FG)

        else:
            # determine cutoff like in load_history()
            now = datetime.datetime.now().astimezone()
            if self.history_filter == "week":
                cutoff = now - datetime.timedelta(days=7)
            elif self.history_filter == "month":
                cutoff = now - datetime.timedelta(days=30)
            else:
                cutoff = None

            # aggregate avg BPM per calendar date
            daily: dict[datetime.date, list[int]] = {}
            activities = session.query(Activity).order_by(Activity.start_time).all()
            for act in activities:
                # convert to aware/local
                st = act.start_time
                if st.tzinfo is None:
                    st = st.replace(tzinfo=ZoneInfo("UTC"))
                st = st.astimezone()
                if cutoff and st < cutoff:
                    continue
                date = st.date()
                # pull HR points
                hrs = session.query(HeartRate).filter_by(activity_id=act.id).all()
                for hr in hrs:
                    daily.setdefault(date, []).append(hr.bpm)

            # build date‐sorted lists
            dates = sorted(daily.keys())
            avg_bpms = [sum(daily[d]) / len(daily[d]) for d in dates]

            if dates:
                # plot date vs avg BPM
                self.history_ax.plot(dates, avg_bpms, lw=2, marker="o")

                # clamp x‐axis to the filter window
                if cutoff:
                    # for week/month: from cutoff→now
                    self.history_ax.set_xlim(cutoff, now)
                else:
                    # for all time: from first date → last date
                    self.history_ax.set_xlim(dates[0], dates[-1])

                # nice automatic date ticks & formatting
                locator = mdates.AutoDateLocator()
                fmt     = mdates.ConciseDateFormatter(locator)
                self.history_ax.xaxis.set_major_locator(locator)
                self.history_ax.xaxis.set_major_formatter(fmt)
                self.history_fig.autofmt_xdate()  # rotate labels

                self.history_ax.set_xlabel("Date", color=self.app.DARK_FG)
                self.history_ax.set_ylabel("Avg BPM", color=self.app.DARK_FG)
                # rotate date labels for readability
                for lbl in self.history_ax.get_xticklabels():
                    lbl.set_rotation(45)
                    lbl.set_color(self.app.DARK_FG)
            else:
                # truly empty
                self.history_ax.set_title("No data in this time frame", color=self.app.DARK_FG)

        self.history_fig.tight_layout()
        self.history_canvas.draw_idle()
