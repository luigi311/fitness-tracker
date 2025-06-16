import datetime
from zoneinfo import ZoneInfo
from os.path import expanduser
from pathlib import Path
from configparser import ConfigParser

import threading
from matplotlib.figure import Figure
from matplotlib.backends.backend_gtk4agg import FigureCanvasGTK4Agg as FigureCanvas

from fitness_tracker.database import Activity, HeartRate
from fitness_tracker.recorder import Recorder
from fitness_tracker.hr_provider import AVAILABLE_PROVIDERS

import gi

gi.require_versions({"Gtk": "4.0", "Adw": "1"})
from gi.repository import Adw, Gtk, GLib

Adw.init()


class FitnessAppUI(Adw.Application):
    def __init__(self):
        super().__init__(application_id="io.Luigi311.Fitness")
        self.window = None
        self.recorder: Recorder | None = None
        self._times: list[float] = []
        self._bpms: list[int] = []
        self._line = None

        # keep track of which activities the user has ticked
        self.selected_activities: set[int] = set()
        # store each activity’s start-time so we can label the legend
        self.activity_start_times: dict[int, datetime.datetime] = {}

        self.history_filter = "week"

        # Set up application directory
        app_dir = Path(expanduser("~/.local/share/io.Luigi311.Fitness"))
        app_dir.mkdir(parents=True, exist_ok=True)
        self.database = app_dir / "fitness.db"
        self.config_file = app_dir / "config.ini"

        # load existing configuration
        cfg = ConfigParser()
        self.database_dsn = ""
        self.device_map: dict[str, str] = {}
        self.device_choices = []
        if self.config_file.exists():
            cfg.read(self.config_file)
            self.database_dsn = cfg.get("server", "database_dsn", fallback="")
            self.device_name = cfg.get("tracker", "device_name", fallback="")
            self.device_address = cfg.get("tracker", "device_address", fallback="")

    def show_toast(self, message: str):
        print(message)
        # Create and display a toast on our overlay
        toast = Adw.Toast.new(message)
        self.toast_overlay.add_toast(toast)

    def do_activate(self):
        if not self.window:
            self._build_ui()
            self.recorder = Recorder(
                on_bpm_update=self._on_bpm,
                database_url=f"sqlite:///{self.database}",
                device_name=self.device_name,
                on_error=self.show_toast,
                device_address=self.device_address or None,
            )
            self.recorder.start()
            # Load history after recorder is initialized
            threading.Thread(target=self._load_history, daemon=True).start()
        self.window.present()

    def _build_ui(self):
        self.window = Adw.ApplicationWindow(application=self)
        self.window.set_title("Fitness Tracker")
        self.window.set_default_size(640, 520)
        self.toast_overlay = Adw.ToastOverlay()
        self.window.set_content(self.toast_overlay)

        toolbar_view = Adw.ToolbarView()
        self.toast_overlay.set_child(toolbar_view)

        # Create ViewStack
        self.stack = Adw.ViewStack()
        self.stack.set_vexpand(True)

        # Add pages
        tracker_page = self._build_tracker_page()
        history_page = self._build_history_page()
        settings_page = self._build_settings_page()

        self.stack.add_titled(tracker_page, "tracker", "Tracker").set_icon_name(
            "media-playback-start-symbolic"
        )
        self.stack.add_titled(history_page, "history", "History").set_icon_name(
            "view-list-symbolic"
        )
        self.stack.add_titled(settings_page, "settings", "Settings").set_icon_name(
            "emblem-system-symbolic"
        )

        # Create bottom tab bar
        switcher_bar = Adw.ViewSwitcherBar()
        switcher_bar.set_stack(self.stack)
        switcher_bar.set_reveal(True)  # ensure it’s always visible

        # Attach content and bottom bar
        toolbar_view.set_content(self.stack)
        toolbar_view.add_bottom_bar(switcher_bar)

    def _on_start(self, button: Gtk.Button):
        self.start_btn.set_sensitive(False)
        self.stop_btn.set_sensitive(True)
        self._times.clear()
        self._bpms.clear()
        self._line.set_data([], [])
        self.ax.relim()
        self.ax.autoscale_view()
        self.fig.tight_layout()
        self.fig.canvas.draw_idle()
        if self.recorder:
            self.recorder.start_recording()

    def _on_stop(self, button: Gtk.Button):
        self.stop_btn.set_sensitive(False)
        self.start_btn.set_sensitive(True)
        if self.recorder:
            self.recorder.stop_recording()

    def _on_bpm(self, time_s: float, bpm: int):
        # Update the BPM label
        GLib.idle_add(self.bpm_label.set_markup, f'<span font="28">{bpm} BPM</span>')

        # Maintain sliding window
        window = 300.0
        self._times.append(time_s)
        self._bpms.append(bpm)
        cutoff = time_s - window
        while self._times and self._times[0] < cutoff:
            self._times.pop(0)
            self._bpms.pop(0)

        # Update line data and axes
        self._line.set_data(self._times, self._bpms)
        self.ax.set_xlim(
            left=max(0, cutoff), right=time_s if time_s > cutoff else cutoff + 1
        )
        self.ax.relim()
        self.ax.autoscale_view(scaley=True)
        self.fig.tight_layout()
        self.fig.canvas.draw_idle()

    def _on_sync(self, button):
        # disable while syncing
        self.sync_btn.set_sensitive(False)
        GLib.idle_add(self.bpm_label.set_markup, '<span font="16">Syncing...</span>')

        # perform sync in a background thread so UI stays responsive
        def do_sync():
            if not self.database_dsn:
                GLib.idle_add(
                    self.bpm_label.set_markup,
                    '<span font="16">No DSN configured</span>',
                )
                GLib.idle_add(self.sync_btn.set_sensitive, True)
                return

            self.recorder.db.sync_to_database(self.database_dsn)

            # clear & reload history on the main thread
            GLib.idle_add(self._clear_history)
            # reload in background
            threading.Thread(target=self._load_history, daemon=True).start()

            # then update UI status & re-enable button
            GLib.idle_add(
                self.bpm_label.set_markup, '<span font="16">Sync complete</span>'
            )
            GLib.idle_add(self.sync_btn.set_sensitive, True)

        threading.Thread(target=do_sync, daemon=True).start()

    def _build_tracker_page(self) -> Gtk.Widget:
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        vbox.set_margin_top(12)
        vbox.set_margin_bottom(12)
        vbox.set_margin_start(12)
        vbox.set_margin_end(12)

        title = Gtk.Label(label="Fitness Tracker")
        title.set_halign(Gtk.Align.CENTER)
        vbox.append(title)

        ctrl_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        ctrl_box.set_halign(Gtk.Align.CENTER)

        self.start_btn = Gtk.Button.new_from_icon_name("media-record-symbolic")
        self.start_btn.set_label("Start")
        self.start_btn.get_style_context().add_class("suggested-action")

        self.stop_btn = Gtk.Button.new_from_icon_name("media-playback-stop-symbolic")
        self.stop_btn.set_label("Stop")
        self.stop_btn.get_style_context().add_class("destructive-action")
        self.stop_btn.set_sensitive(False)

        self.sync_btn = Gtk.Button(label="Sync to Server")
        self.sync_btn.get_style_context().add_class("secondary")

        ctrl_box.append(self.start_btn)
        ctrl_box.append(self.stop_btn)
        ctrl_box.append(self.sync_btn)
        vbox.append(ctrl_box)

        self.bpm_label = Gtk.Label()
        self.bpm_label.set_use_markup(True)
        self.bpm_label.set_markup('<span font="28">— BPM —</span>')
        self.bpm_label.set_halign(Gtk.Align.CENTER)
        self.bpm_label.set_valign(Gtk.Align.CENTER)
        vbox.append(self.bpm_label)

        frame = Gtk.Frame(label="Live Heart Rate")
        self.fig = Figure(figsize=(6, 3))
        self.ax = self.fig.add_subplot(111)
        (self._line,) = self.ax.plot([], [], lw=2)
        self.ax.set_xlabel("Time (s)")
        self.ax.set_ylabel("BPM")
        self.ax.grid(alpha=0.3)
        self.ax.set_facecolor("#f9f9f9")

        canvas = FigureCanvas(self.fig)
        canvas.set_vexpand(True)
        frame.set_child(canvas)
        vbox.append(frame)

        self.start_btn.connect("clicked", self._on_start)
        self.stop_btn.connect("clicked", self._on_stop)
        self.sync_btn.connect("clicked", self._on_sync)

        return vbox

    def _build_settings_page(self) -> Gtk.Widget:
        # General settings group
        prefs_vbox = Adw.PreferencesGroup()
        prefs_vbox.set_title("General Settings")

        # Database DSN row
        dsn_row = Adw.ActionRow()
        dsn_row.set_title("Database DSN")
        self.dsn_entry = Gtk.Entry()
        self.dsn_entry.set_hexpand(True)
        self.dsn_entry.set_text(self.database_dsn)
        dsn_row.add_suffix(self.dsn_entry)
        prefs_vbox.add(dsn_row)

        # Tracker device group
        dev_group = Adw.PreferencesGroup()
        dev_group.set_title("Tracker Device")

        # Device selection row with spinner + combo
        self.device_row = Adw.ActionRow()
        self.device_row.set_title("Select Device")
        self.device_spinner = Gtk.Spinner()
        self.device_spinner.set_halign(Gtk.Align.START)
        # start spinner only if no preselected device
        if not self.device_name:
            self.device_spinner.start()
        self.device_combo = Gtk.ComboBoxText()
        self.device_combo.set_hexpand(True)
        self.device_row.add_prefix(self.device_spinner)
        self.device_row.add_suffix(self.device_combo)
        dev_group.add(self.device_row)

        # Rescan row
        rescan_row = Adw.ActionRow()
        rescan_row.set_title("Rescan for Devices")
        self.rescan_button = Gtk.Button(label="Rescan")
        self.rescan_button.get_style_context().add_class("suggested-action")
        self.rescan_button.connect(
            "clicked",
            lambda _: threading.Thread(target=self._fill_devices, daemon=True).start(),
        )
        rescan_row.add_suffix(self.rescan_button)
        dev_group.add(rescan_row)

        # Save Settings row
        save_row = Adw.ActionRow()
        save_row.set_activatable(True)
        save_row.set_title("Save Settings")
        self.save_button = Gtk.Button(label="Save")
        self.save_button.connect("clicked", self._on_save_settings)
        save_row.add_suffix(self.save_button)
        dev_group.add(save_row)

        # Layout container
        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        container.set_margin_top(12)
        container.set_margin_bottom(12)
        container.set_margin_start(12)
        container.set_margin_end(12)
        container.append(prefs_vbox)
        container.append(dev_group)

        # If a device was saved in config, pre-populate and skip initial scan
        if self.device_name:
            # stop spinner and set subtitle
            self.device_spinner.stop()
            # populate combo and select
            self.device_combo.append_text(self.device_name)
            self.device_combo.set_active(0)
            # map remains empty until explicit rescan
            self.device_map = {self.device_name: self.device_address}
        else:
            # Kick off initial scan in background
            threading.Thread(target=self._fill_devices, daemon=True).start()

        return container

    def _fill_devices(self):
        # Indicate scanning
        GLib.idle_add(self.device_spinner.start)
        self.device_row.set_subtitle("Scanning for devices…")

        import asyncio
        from bleak import BleakScanner

        async def _scan():
            devices = await BleakScanner.discover(timeout=5.0)
            mapping = {}
            for d in devices:
                if d.name and any(p.matches(d.name) for p in AVAILABLE_PROVIDERS):
                    mapping.setdefault(d.name, d.address)

            names = sorted(mapping.keys())
            # Update UI
            GLib.idle_add(self.device_spinner.stop)
            subtitle = "No supported devices found" if not names else ""
            GLib.idle_add(self.device_row.set_subtitle, subtitle)
            GLib.idle_add(self.device_combo.remove_all)
            for name in names:
                GLib.idle_add(self.device_combo.append_text, name)
            # restore selection if saved
            if self.device_name and self.device_name in names:
                idx = names.index(self.device_name)
                GLib.idle_add(self.device_combo.set_active, idx)
            self.device_map = mapping

        asyncio.run(_scan())

    def _on_save_settings(self, _button):
        # Persist database DSN and tracker selection
        self.database_dsn = self.dsn_entry.get_text()
        self.device_name = self.device_combo.get_active_text() or ""
        if self.device_name in self.device_map:
            self.device_address = self.device_map[self.device_name]
        cfg = ConfigParser()
        cfg["server"] = {"database_dsn": self.database_dsn}
        cfg["tracker"] = {"device_name": self.device_name, "device_address": self.device_address}
        with open(self.config_file, "w") as f:
            cfg.write(f)
        # Confirmation toast
        toast = Adw.Toast.new("Settings saved successfully")
        GLib.idle_add(self.toast_overlay.add_toast, toast)


    def _build_history_page(self) -> Gtk.Widget:
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        vbox.set_margin_top(12)
        vbox.set_margin_bottom(12)
        vbox.set_margin_start(12)
        vbox.set_margin_end(12)

        # Filter controls
        filter_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        filter_label = Gtk.Label(label="Show:")
        filter_label.set_halign(Gtk.Align.START)
        filter_box.append(filter_label)

        self.filter_combo = Gtk.ComboBoxText()
        self.filter_combo.append("week", "Last 7 Days")
        self.filter_combo.append("month", "This Month")
        self.filter_combo.append("all", "All Time")
        self.filter_combo.set_active_id(self.history_filter)
        self.filter_combo.connect("changed", self._on_filter_changed)
        filter_box.append(self.filter_combo)
        vbox.append(filter_box)

        # Scrollable container for history items
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        self.history_container = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8
        )
        scroller.set_child(self.history_container)
        vbox.append(scroller)

        # Activity Details chart below
        frame = Gtk.Frame(label="Activity Details")
        self.history_fig = Figure(figsize=(6, 3))
        self.history_ax = self.history_fig.add_subplot(111)
        self.history_ax.set_xlabel("Time (s)")
        self.history_ax.set_ylabel("BPM")
        self.history_canvas = FigureCanvas(self.history_fig)
        self.history_canvas.set_vexpand(True)
        frame.set_child(self.history_canvas)
        vbox.append(frame)

        return vbox

    def _on_filter_changed(self, combo: Gtk.ComboBoxText):
        self.history_filter = combo.get_active_id()
        # Clear existing UI elements on the main thread
        GLib.idle_add(self._clear_history)
        # Reload history in background
        threading.Thread(target=self._load_history, daemon=True).start()

    def _clear_history(self):
        # GTK4: use get_first_child()/get_next_sibling() to iterate
        child = self.history_container.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.history_container.remove(child)
            child = next_child

    def _load_history(self):
        if not self.recorder:
            return
        Session = self.recorder.db.Session

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
            activities = (
                session.query(Activity).order_by(Activity.start_time.desc()).all()
            )
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

    def _add_history_group_header(self, date: datetime.date):
        header = Gtk.Label(label=date.strftime("%B %d, %Y"))
        header.get_style_context().add_class("heading")
        header.set_margin_top(12)
        header.set_margin_bottom(6)
        self.history_container.append(header)

    def _add_history_row(
        self,
        act_id: int,
        start: datetime.datetime,
        duration: datetime.timedelta,
        avg_bpm: float,
        max_bpm: int,
        total_kj: float,
        times: list[float],
        bpms: list[int],
    ):
        # Keep track of start times for plotting comparisons
        self.activity_start_times[act_id] = start

        # Use a Frame as a card container since Adw.Card isn't available
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

        summary = (
            f"Dur: {int(duration.total_seconds()//60)}m {int(duration.total_seconds()%60)}s, "
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
            ax.plot(times, bpms, lw=1)
            ax.axis("off")
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
        self._update_history_plot()

    def _update_history_plot(self):
        self.history_ax.clear()
        Session = self.recorder.db.Session
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
                label = (
                    self.activity_start_times[aid]
                    .replace(tzinfo=ZoneInfo("UTC"))
                    .astimezone()
                    .strftime("%Y-%m-%d %H:%M")
                )
                self.history_ax.plot(times, bpms, lw=2, label=label)

        if self.selected_activities:
            self.history_ax.set_xlabel("Time (s)")
            self.history_ax.set_ylabel("BPM")
            self.history_ax.legend()
        else:
            self.history_ax.set_title("No activities selected")

        self.history_fig.tight_layout()
        self.history_canvas.draw_idle()
