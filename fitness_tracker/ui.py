import gi
gi.require_versions({"Gtk": "4.0", "Adw": "1"})

from gi.repository import Adw, Gtk, GLib
from matplotlib.figure import Figure
from matplotlib.backends.backend_gtk4agg import (
    FigureCanvasGTK4Agg as FigureCanvas
)
from .recorder import Recorder

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
            self._build()
            self.recorder = Recorder(on_bpm_update=self._update_plot)
            self.recorder.start()
        self.window.present()

    def _build(self):
        self.window = Adw.ApplicationWindow(application=self)
        self.window.set_title("Polar H10 Tracker")
        self.window.set_default_size(480, 400)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.start_btn = Gtk.Button(label="Start Recording")
        self.stop_btn  = Gtk.Button(label="Stop Recording")
        self.stop_btn.set_sensitive(False)
        btn_box.append(self.start_btn)
        btn_box.append(self.stop_btn)
        vbox.append(btn_box)

        self.bpm_label = Gtk.Label(label="BPM: —")
        vbox.append(self.bpm_label)

        # Matplotlib Figure with a pre-created Line2D
        self.fig = Figure(figsize=(4,2))
        self.ax = self.fig.add_subplot(111)
        self._line, = self.ax.plot([], [], lw=1)
        self.ax.set_xlabel('Time (s)')
        self.ax.set_ylabel('BPM')
        self.ax.grid(True)
        self.canvas = FigureCanvas(self.fig)
        vbox.append(self.canvas)

        self.window.set_content(vbox)

        self.start_btn.connect("clicked", lambda w: self._on_start())
        self.stop_btn.connect("clicked",  lambda w: self._on_stop())

    def _on_start(self):
        self.start_btn.set_sensitive(False)
        self.stop_btn.set_sensitive(True)
        # reset data
        self._times.clear()
        self._bpms.clear()
        self._line.set_data([], [])
        self.ax.relim()
        self.ax.autoscale_view()
        self.canvas.draw_idle()
        self.recorder.start_recording()

    def _on_stop(self):
        self.stop_btn.set_sensitive(False)
        self.start_btn.set_sensitive(True)
        self.recorder.stop_recording()

    def _update_plot(self, time_s: float, bpm: int):
        # update label
        self.bpm_label.set_text(f"BPM: {bpm}")

        # sliding window of last N seconds
        window = 300.0
        self._times.append(time_s)
        self._bpms.append(bpm)
        # filter window
        start = max(0.0, time_s - window)
        keep = [(tt, bb) for tt, bb in zip(self._times, self._bpms) if tt >= start]
        if keep:
            ts, bs = zip(*keep)
        else:
            ts, bs = [], []
        self._times, self._bpms = list(ts), list(bs)

        self._line.set_data(self._times, self._bpms)
        self.ax.set_xlim(left=start, right=time_s if time_s>start else start+0.1)
        self.ax.relim()
        self.ax.autoscale_view(scaley=True)
        self.canvas.draw_idle()