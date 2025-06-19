import gi
from matplotlib.backends.backend_gtk4agg import FigureCanvasGTK4Agg as FigureCanvas
from matplotlib.figure import Figure

gi.require_versions({"Gtk": "4.0", "Adw": "1"})
from gi.repository import GLib, Gtk  # noqa: E402


class TrackerPageUI:
    def __init__(self, app: "FitnessAppUI"):
        self.app = app
        # copy over any per‐page state
        self._times: list[float] = []
        self._bpms: list[int] = []
        self._line = None

    def build_page(self) -> Gtk.Widget:
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

        self.app.start_btn = Gtk.Button.new_from_icon_name("media-record-symbolic")
        self.app.start_btn.set_label("Start")
        self.app.start_btn.get_style_context().add_class("suggested-action")

        self.app.stop_btn = Gtk.Button.new_from_icon_name("media-playback-stop-symbolic")
        self.app.stop_btn.set_label("Stop")
        self.app.stop_btn.get_style_context().add_class("destructive-action")
        self.app.stop_btn.set_sensitive(False)

        ctrl_box.append(self.app.start_btn)
        ctrl_box.append(self.app.stop_btn)
        vbox.append(ctrl_box)

        self.bpm_label = Gtk.Label()
        self.bpm_label.set_use_markup(True)
        self.bpm_label.set_markup(f'<span font="28" color="{self.app.DARK_FG}">— BPM —</span>')
        self.bpm_label.set_halign(Gtk.Align.CENTER)
        self.bpm_label.set_valign(Gtk.Align.CENTER)
        vbox.append(self.bpm_label)

        frame = Gtk.Frame(label="Live Heart Rate")
        self.fig = Figure(figsize=(6, 3))
        self.ax = self.fig.add_subplot(111)
        # draw zones behind live data
        self.app.draw_zones(self.ax)
        (self._line,) = self.ax.plot([], [], lw=2)
        self.ax.set_xlabel("Time (s)")
        self.ax.set_ylabel("BPM")

        # Dark/light styling
        self.fig.patch.set_facecolor(self.app.DARK_BG)
        self.ax.set_facecolor(self.app.DARK_BG)
        self.ax.xaxis.label.set_color(self.app.DARK_FG)
        self.ax.yaxis.label.set_color(self.app.DARK_FG)
        self.ax.tick_params(colors=self.app.DARK_FG)
        self.ax.grid(color=self.app.DARK_GRID)

        canvas = FigureCanvas(self.fig)
        canvas.set_vexpand(True)
        frame.set_child(canvas)
        vbox.append(frame)

        self.app.start_btn.connect("clicked", self._on_start)
        self.app.stop_btn.connect("clicked", self._on_stop)

        return vbox

    def _on_start(self, button: Gtk.Button):
        self.app.start_btn.set_sensitive(False)
        self.app.stop_btn.set_sensitive(True)
        self._times.clear()
        self._bpms.clear()
        self._line.set_data([], [])
        self.ax.relim()
        self.ax.autoscale_view()
        self.fig.tight_layout()
        self.fig.canvas.draw_idle()
        if self.app.recorder:
            self.app.recorder.start_recording()

    def _on_stop(self, button: Gtk.Button):
        self.app.stop_btn.set_sensitive(False)
        self.app.start_btn.set_sensitive(True)
        if self.app.recorder:
            self.app.recorder.stop_recording()

    def on_bpm(self, time_s: float, bpm: int):
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
        self.ax.set_xlim(left=max(0, cutoff), right=time_s if time_s > cutoff else cutoff + 1)
        self.ax.relim()
        self.ax.autoscale_view(scaley=True)
        self.fig.tight_layout()
        self.fig.canvas.draw_idle()
