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
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        label = Gtk.Label(label="Database DSN:")
        label.set_halign(Gtk.Align.START)

        self.dsn_entry = Gtk.Entry()
        self.dsn_entry.set_hexpand(True)
        self.dsn_entry.set_text(self.database_dsn)

        device_label = Gtk.Label(label="Select Tracker Device:")
        device_label.set_halign(Gtk.Align.START)

        self.device_combo = Gtk.ComboBoxText()
        self.device_combo.set_hexpand(True)

        self.scan_status = Gtk.Label(label="Scanning for devices...")
        self.scan_status.set_halign(Gtk.Align.START)

        self.rescan_button = Gtk.Button(label="Rescan")
        self.rescan_button.set_halign(Gtk.Align.END)
        self.rescan_button.connect(
            "clicked",
            lambda _: threading.Thread(target=fill_devices, daemon=True).start(),
        )

        # Fill device list asynchronously
        def fill_devices():
            import asyncio
            from bleak import BleakScanner

            def update_status(text):
                GLib.idle_add(self.scan_status.set_text, text)

            async def _scan():
                update_status("Scanning for devices…")
                devices = await BleakScanner.discover(timeout=5.0)

                # build name→address map
                mapping: dict[str, str] = {}
                for d in devices:
                    if d.name and any(p.matches(d.name) for p in AVAILABLE_PROVIDERS):
                        mapping.setdefault(d.name, d.address)
                names = sorted(mapping.keys())
                update_status(
                    "No supported devices found" if not names else "Scan complete"
                )
                GLib.idle_add(self._update_device_combo, names)
                # save the map and update UI
                self.device_map = mapping
                GLib.idle_add(self._update_device_combo, names)

            asyncio.run(_scan())

        if self.device_name:
            # Seed the combo with the one saved name
            self._update_device_combo([self.device_name])
            self.scan_status.set_text(f"Current device: {self.device_name}")
        else:
            # No saved device yet
            self.device_combo.remove_all()
            self.scan_status.set_text(
                "No device selected. Press Rescan to find devices."
            )

        save_button = Gtk.Button(label="Save")
        save_button.set_halign(Gtk.Align.END)
        save_button.connect("clicked", self._on_save_settings)

        box.append(label)
        box.append(self.dsn_entry)
        box.append(device_label)
        box.append(self.device_combo)
        box.append(self.scan_status)
        box.append(self.rescan_button)
        box.append(save_button)

        return box

    def _update_device_combo(self, names):
        self.device_combo.remove_all()
        for name in names:
            self.device_combo.append_text(name)

        # Ensure default matches saved device_name
        if self.device_name and self.device_name in names:
            self.device_combo.set_active(names.index(self.device_name))
        else:
            self.device_combo.set_active(-1 if not names else 0)

    def _on_save_settings(self, _button):
        self.database_dsn = self.dsn_entry.get_text()
        self.device_name = self.device_combo.get_active_text() or ""
        # only overwrite MAC if this name was in our last scan
        if self.device_name in self.device_map:
            self.device_address = self.device_map[self.device_name]

        from configparser import ConfigParser

        cfg = ConfigParser()
        cfg["server"] = {"database_dsn": self.database_dsn}
        cfg["tracker"] = {
            "device_name": self.device_name,
            "device_address": self.device_address,
        }

        with open(self.config_file, "w") as f:
            cfg.write(f)

        # Show confirmation message as a toast
        from gi.repository import Adw

        toast_overlay = self.window.get_content()
        if isinstance(toast_overlay, Adw.ToastOverlay):
            toast = Adw.Toast.new("Settings saved successfully")
            GLib.idle_add(toast_overlay.add_toast, toast)

    def _clear_history(self):
        """Remove all rows from the History list."""
        # Must run on main GTK thread
        if self.history_list:
            self.history_list.remove_all()

    def _build_history_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        title = Gtk.Label(label="History")
        title.set_halign(Gtk.Align.CENTER)
        box.append(title)

        # List of past activities
        self.history_list = Gtk.ListBox()
        self.history_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.history_list.set_vexpand(True)
        box.append(self.history_list)

        # Chart for selected activity
        frame = Gtk.Frame(label="Activity Details")
        self.history_fig = Figure(figsize=(6, 3))
        self.history_ax = self.history_fig.add_subplot(111)
        self.history_ax.set_xlabel("Time (s)")
        self.history_ax.set_ylabel("BPM")
        self.history_canvas = FigureCanvas(self.history_fig)
        self.history_canvas.set_vexpand(True)
        frame.set_child(self.history_canvas)
        box.append(frame)

        return box

    def _load_history(self):
        # guard to ensure recorder is ready
        if not self.recorder:
            return
        Session = self.recorder.db.Session
        with Session() as session:
            activities = (
                session.query(Activity).order_by(Activity.start_time.desc()).all()
            )
            for act in activities:
                start = act.start_time
                end = act.end_time or datetime.utcnow()
                duration = end - start
                hr_vals = [hr.bpm for hr in act.heart_rates]
                avg_bpm = sum(hr_vals) / len(hr_vals) if hr_vals else 0
                max_bpm = max(hr_vals) if hr_vals else 0
                total_kj = sum(hr.energy_kj or 0 for hr in act.heart_rates)
                GLib.idle_add(
                    self._add_history_row,
                    act.id,
                    start,
                    duration,
                    avg_bpm,
                    max_bpm,
                    total_kj,
                )

    def _add_history_row(
        self,
        act_id: int,
        start: datetime,
        duration,
        avg_bpm: float,
        max_bpm: int,
        total_kj: float,
    ):
        # store start-time for legend label later
        self.activity_start_times[act_id] = start

        row = Adw.ActionRow()
        # ensure UTC→local
        if start.tzinfo is None:
            # assume stored as UTC
            start = start.replace(tzinfo=ZoneInfo("UTC"))
        # astimezone() with no args converts to the system local tz
        local = start.astimezone()
        row.set_title(local.strftime("%Y-%m-%d %H:%M"))

        summary = (
            f"Dur: {int(duration.total_seconds() // 60)}m {int(duration.total_seconds() % 60)}s, "
            f"Avg: {int(avg_bpm)} BPM"
        )
        label = Gtk.Label(label=summary)
        label.set_halign(Gtk.Align.END)
        row.add_suffix(label)
        row.set_activatable(True)

        # make it clickable
        row.set_activatable(True)
        row.connect("activated", lambda r, aid=act_id: self._toggle_history_row(r, aid))

        self.history_list.append(row)

    def _show_activity_details(self, act_id: int):
        times = []
        bpms = []
        if not self.recorder:
            return
        Session = self.recorder.db.Session
        with Session() as session:
            hrs = (
                session.query(HeartRate)
                .filter_by(activity_id=act_id)
                .order_by(HeartRate.timestamp_ms)
                .all()
            )
            start_ms = None
            for hr in hrs:
                if start_ms is None:
                    start_ms = hr.timestamp_ms
                times.append((hr.timestamp_ms - start_ms) / 1000.0)
                bpms.append(hr.bpm)

        # Update chart
        self.history_ax.clear()
        self.history_ax.plot(times, bpms, lw=2)
        self.history_fig.tight_layout()
        self.history_canvas.draw_idle()

    def _toggle_history_row(self, row: Adw.ActionRow, act_id: int):
        ctx = row.get_style_context()
        if act_id in self.selected_activities:
            self.selected_activities.remove(act_id)
            ctx.remove_class("selected")
        else:
            self.selected_activities.add(act_id)
            ctx.add_class("selected")
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
