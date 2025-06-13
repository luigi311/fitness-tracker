import gi
from matplotlib.figure import Figure
from matplotlib.backends.backend_gtk4agg import FigureCanvasGTK4Agg as FigureCanvas
from .recorder import Recorder

gi.require_versions({"Gtk": "4.0", "Adw": "1"})
from gi.repository import Adw, Gtk, GLib, Pango

Adw.init()


class FitnessAppUI(Adw.Application):
    def __init__(self):
        super().__init__(application_id="com.example.Fitness")
        self.window = None
        self.recorder: Recorder | None = None
        self._times: list[float] = []
        self._bpms: list[int] = []
        self._line = None

    def do_activate(self):
        if not self.window:
            self._build_ui()
            self.recorder = Recorder(on_bpm_update=self._on_bpm)
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
        ctrl_box.append(self.start_btn)
        ctrl_box.append(self.stop_btn)
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
        window = 60.0
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
