from os.path import expanduser
from pathlib import Path
import threading
import gi
from matplotlib.figure import Figure
from matplotlib.backends.backend_gtk4agg import FigureCanvasGTK4Agg as FigureCanvas
from .recorder import Recorder

gi.require_versions({"Gtk": "4.0", "Adw": "1"})
from gi.repository import Adw, Gtk, GLib, Pango

Adw.init()


class FitnessAppUI(Adw.Application):
    def __init__(self):
        super().__init__(application_id="io.Luigi311.Fitness")
        self.window = None
        self.recorder: Recorder | None = None
        self._times: list[float] = []
        self._bpms: list[int] = []
        self._line = None
        # Set up application directory
        app_dir = Path(expanduser("~/.local/share/io.Luigi311.Fitness"))
        app_dir.mkdir(parents=True, exist_ok=True)
        self.database = app_dir / "fitness.db"
        self.config_file = app_dir / "config.ini"
        # load existing DSN
        from configparser import ConfigParser
        cfg = ConfigParser()
        self.postgres_dsn = ''
        if self.config_file.exists():
            cfg.read(self.config_file)
            self.postgres_dsn = cfg.get('server', 'dsn', fallback='')

    def do_activate(self):
        if not self.window:
            self._build_ui()
            self.recorder = Recorder(
                on_bpm_update=self._on_bpm, database_url=f"sqlite:///{self.database}"
            )
            self.recorder.start()
        self.window.present()

    def _build_ui(self):
        self.window = Adw.ApplicationWindow(application=self)
        self.window.set_title("Polar H10 Tracker")
        self.window.set_default_size(640, 520)

        layout = self._create_main_layout()
        self.window.set_content(layout)

    def _create_main_layout(self) -> Gtk.Widget:
        # Main container
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        vbox.set_margin_top(12)
        vbox.set_margin_bottom(12)
        vbox.set_margin_start(12)
        vbox.set_margin_end(12)

        # Title label
        title = Gtk.Label(label="Polar H10 Tracker")
        title.get_style_context().add_class("title")
        title.set_halign(Gtk.Align.CENTER)
        vbox.append(title)

        # Control buttons
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
        # Settings button (gear icon)
        self.settings_btn = Gtk.Button.new_from_icon_name("emblem-system-symbolic")
        self.settings_btn.set_tooltip_text("Configure server DSN")

        ctrl_box.append(self.start_btn)
        ctrl_box.append(self.stop_btn)
        ctrl_box.append(self.sync_btn)
        ctrl_box.append(self.settings_btn)
        vbox.append(ctrl_box)

        # BPM display with markup
        self.bpm_label = Gtk.Label()
        self.bpm_label.set_use_markup(True)
        self.bpm_label.set_markup('<span font="28">— BPM —</span>')
        self.bpm_label.set_halign(Gtk.Align.CENTER)
        self.bpm_label.set_valign(Gtk.Align.CENTER)
        vbox.append(self.bpm_label)

        # Live plot in a framed container
        frame = Gtk.Frame(label="Live Heart Rate")
        frame.set_margin_top(8)
        frame.set_margin_bottom(8)
        frame.set_margin_start(8)
        frame.set_margin_end(8)

        # Prepare Matplotlib figure
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

        # Connect signals
        self.start_btn.connect("clicked", self._on_start)
        self.stop_btn.connect("clicked", self._on_stop)
        self.sync_btn.connect("clicked", self._on_sync)
        self.settings_btn.connect("clicked", self._on_settings)

        return vbox

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
            # build your DSN however you like (env vars, settings dialog, etc.)
            if not self.postgres_dsn:
                GLib.idle_add(
                    self.bpm_label.set_markup,
                    '<span font="16">No DSN configured</span>',
                )
                GLib.idle_add(self.sync_btn.set_sensitive, True)
                return

            self.recorder.db.sync_to_postgres(self.postgres_dsn)
            # back on the main thread:
            GLib.idle_add(
                self.bpm_label.set_markup, '<span font="16">Sync complete</span>'
            )
            GLib.idle_add(self.sync_btn.set_sensitive, True)

        threading.Thread(target=do_sync, daemon=True).start()

    def _on_settings(self, _):
        # Open a dialog to set the Postgres DSN
        dialog = Gtk.Dialog(title="Server Settings", transient_for=self.window)
        dialog.set_modal(True)
        dialog.add_buttons(
            "Cancel", Gtk.ResponseType.CANCEL,
            "OK",     Gtk.ResponseType.OK,
        )
        content = dialog.get_content_area()
        entry = Gtk.Entry()
        entry.set_hexpand(True)
        # prefill with current DSN
        entry.set_text(self.postgres_dsn)
        label = Gtk.Label(label="Postgres DSN:")
        grid = Gtk.Grid(row_spacing=6, column_spacing=6)
        grid.attach(label, 0, 0, 1, 1)
        grid.attach(entry, 1, 0, 1, 1)
        content.append(grid)
        dialog.show()

        # handle user response via signal (GTK4 uses responses)
        def on_response(dialog_obj, response_id):
            if response_id == Gtk.ResponseType.OK:
                # save new DSN
                self.postgres_dsn = entry.get_text()
                from configparser import ConfigParser
                cfg = ConfigParser()
                cfg['server'] = {'dsn': self.postgres_dsn}
                with open(self.config_file, 'w') as f:
                    cfg.write(f)
            dialog_obj.destroy()

        dialog.connect("response", on_response)