from collections import deque

import gi
import matplotlib.colors as mcolors
import numpy as np
from matplotlib.backends.backend_gtk4agg import FigureCanvasGTK4Agg as FigureCanvas
from matplotlib.collections import LineCollection
from matplotlib.figure import Figure

gi.require_versions({"Gtk": "4.0", "Adw": "1"})
from gi.repository import GLib, Gtk


class TrackerPageUI:
    def __init__(self, app: "FitnessAppUI"):
        self.app = app
        self.window = 10.0  # seconds shown in plot
        self.buffer = 2.5  # extra seconds of headroom
        self.window_ms = self.window * 1000.0
        self.ymin = self.app.resting_hr - 20
        self.ymax = self.app.max_hr + 20
        self.prev_angle = None
        self._last_time_ms = None

        # rolling buffers for timestamps (ms) and BPM
        self._times = deque()
        self._bpms = deque()

    def build_page(self) -> Gtk.Widget:
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for edge in ("top", "bottom", "start", "end"):
            vbox.set_property(f"margin_{edge}", 12)

        # Title
        title = Gtk.Label(label="Fitness Tracker")
        title.set_halign(Gtk.Align.CENTER)
        vbox.append(title)

        # Start / Stop
        ctrl = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        ctrl.set_halign(Gtk.Align.CENTER)
        self.app.start_btn = Gtk.Button.new_from_icon_name("media-record-symbolic")
        self.app.start_btn.set_label("Start")
        self.app.start_btn.get_style_context().add_class("suggested-action")
        self.app.stop_btn = Gtk.Button.new_from_icon_name("media-playback-stop-symbolic")
        self.app.stop_btn.set_label("Stop")
        self.app.stop_btn.get_style_context().add_class("destructive-action")
        self.app.stop_btn.set_sensitive(False)
        ctrl.append(self.app.start_btn)
        ctrl.append(self.app.stop_btn)
        vbox.append(ctrl)

        # BPM label
        self.bpm_label = Gtk.Label(use_markup=True)
        self.bpm_label.set_markup(f'<span font="28" color="{self.app.DARK_FG}">— BPM —</span>')
        self.bpm_label.set_halign(Gtk.Align.CENTER)
        vbox.append(self.bpm_label)

        # Plot frame
        frame = Gtk.Frame(label="Live Heart Rate")
        self.fig = Figure(figsize=(6, 3))
        self.ax = self.fig.add_subplot(111)

        # One-time styling + create tail & ship
        self.tail, self.ship_marker = self._style_figure()

        # Embed
        canvas = FigureCanvas(self.fig)
        canvas.set_vexpand(True)
        frame.set_child(canvas)
        vbox.append(frame)

        # Signals
        self.app.start_btn.connect("clicked", self._on_start)
        self.app.stop_btn.connect("clicked", self._on_stop)

        # Tight layout once (avoids per-frame squeezing)
        self.fig.tight_layout()

        return vbox

    def _style_figure(self):
        # draw background HR zones
        self.app.draw_zones(self.ax)

        # recompute Y‐limits based on current resting/max HR
        ymin = self.app.resting_hr - 20
        ymax = self.app.max_hr + 20

        # fading tail
        tail = LineCollection([], linewidths=2, zorder=2)
        self.ax.add_collection(tail)

        # rotating triangle “ship”
        (ship_marker,) = self.ax.plot(
            [],
            [],
            marker=(3, 0, 0),  # triangle, angle=0°
            markersize=15,
            markerfacecolor="white",
            markeredgecolor="white",
            zorder=3,
        )
        ship_marker.set_clip_on(False)

        # styling
        self.fig.patch.set_facecolor(self.app.DARK_BG)
        self.ax.set_facecolor(self.app.DARK_BG)
        self.ax.xaxis.set_visible(False)
        self.ax.yaxis.label.set_color(self.app.DARK_FG)
        self.ax.tick_params(colors=self.app.DARK_FG)
        self.ax.grid(color=self.app.DARK_GRID)

        # fixed limits
        self.ax.set_xlim(0, self.window + self.buffer)
        self.ax.set_ylim(ymin, ymax)

        return tail, ship_marker

    def configure_axes(self):
        self.ax.clear()
        self.app.draw_zones(self.ax)

        self.ax.set_facecolor(self.app.DARK_BG)
        # hide X-axis
        self.ax.xaxis.set_visible(False)

        self.ax.yaxis.label.set_color(self.app.DARK_FG)
        self.ax.tick_params(colors=self.app.DARK_FG)
        self.ax.grid(color=self.app.DARK_GRID)

        # recompute limits
        ymin = self.app.resting_hr - 20
        ymax = self.app.max_hr + 20
        self.ax.set_xlim(0, self.window + self.buffer)
        self.ax.set_ylim(ymin, ymax)

        self.ax.add_collection(self.tail)
        self.ax.add_line(self.ship_marker)

    def _zone_color(self, hr: float) -> tuple[float, float, float]:
        # Fresh thresholds → RGB
        thresholds = self.app.calculate_hr_zones().items()
        for (_, (lo, hi)), hexcol in zip(thresholds, self.app.ZONE_COLORS):
            if lo <= hr < hi:
                return mcolors.to_rgb(hexcol)
        return mcolors.to_rgb(self.app.DARK_FG)

    def _on_start(self, button: Gtk.Button):
        # Reset state
        self._last_time_ms = None
        self.prev_angle = None
        self.configure_axes()

        self._times.clear()
        self._bpms.clear()
        self.tail.set_segments([])
        self.ship_marker.set_data([], [])

        self.fig.canvas.draw_idle()
        self.app.start_btn.set_sensitive(False)
        self.app.stop_btn.set_sensitive(True)
        if self.app.recorder:
            self.app.recorder.start_recording()

    def _on_stop(self, button: Gtk.Button):
        self.app.stop_btn.set_sensitive(False)
        self.app.start_btn.set_sensitive(True)
        if self.app.recorder:
            self.app.recorder.stop_recording()

    def on_bpm(self, time_ms: float, bpm: int):
        # Drop out-of-order
        if self._last_time_ms is not None and time_ms <= self._last_time_ms:
            return
        self._last_time_ms = time_ms

        # Update label
        GLib.idle_add(self.bpm_label.set_markup, f'<span font="28">{bpm} BPM</span>')

        # Append & trim
        self._times.append(time_ms)
        self._bpms.append(bpm)
        cutoff = time_ms - self.window_ms
        while self._times and self._times[0] < cutoff:
            self._times.popleft()
            self._bpms.popleft()

        if len(self._times) < 2:
            return

        # Build arrays & seconds-ago
        times_ms = np.array(self._times)
        bpms = np.array(self._bpms)
        rel_s = (times_ms - cutoff) / 1000.0

        # Tail segments + fading colors
        pts = np.vstack([rel_s, bpms]).T.reshape(-1, 1, 2)
        segs = np.concatenate([pts[:-1], pts[1:]], axis=1)
        mids = 0.5 * (bpms[:-1] + bpms[1:])
        ages = time_ms - 0.5 * (times_ms[:-1] + times_ms[1:])
        alphas = np.clip((self.window_ms - ages) / self.window_ms, 0, 1)

        colors = [(*self._zone_color(m), a) for m, a in zip(mids, alphas)]
        self.tail.set_segments(segs)
        self.tail.set_color(colors)

        # Ship heading & position
        dx = rel_s[-1] - rel_s[-2]
        dy = bpms[-1] - bpms[-2]
        raw = np.degrees(np.arctan2(dy, dx))
        angle = (0.7 * self.prev_angle + 0.3 * raw) if self.prev_angle is not None else raw
        self.prev_angle = angle

        last_x, last_y = rel_s[-1], bpms[-1]
        self.ship_marker.set_marker((3, 0, angle - 90))
        self.ship_marker.set_data([last_x], [last_y])
        r, g, b = self._zone_color(bpm)
        self.ship_marker.set_markerfacecolor((r, g, b, 1.0))
        self.ship_marker.set_markeredgecolor((r, g, b, 1.0))

        # Redraw (limits are fixed)
        self.fig.canvas.draw_idle()
