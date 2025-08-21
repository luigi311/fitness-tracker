from collections import deque
import math
import random
import time

import gi
import numpy as np
from matplotlib.backends.backend_gtk4agg import FigureCanvasGTK4Agg as FigureCanvas
from matplotlib.figure import Figure

gi.require_versions({"Gtk": "4.0", "Adw": "1"})
from gi.repository import GLib, Gtk, Adw

# ---------- Small reusable “metric cards” ----------

class MetricCard(Gtk.Frame):
    def __init__(self, title: str, unit: str | None = None):
        super().__init__()
        self.set_hexpand(True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(12)

        self.title = Gtk.Label(label=title)
        self.title.add_css_class("caption")
        self.title.set_xalign(0)

        self.value = Gtk.Label(label="0")
        self.value.add_css_class("title-1")
        self.value.set_xalign(0)

        self.unit = Gtk.Label(label=unit or "")
        self.unit.add_css_class("dim-label")
        self.unit.set_xalign(0)
        self.unit.set_visible(bool(unit))

        box.append(self.title)
        box.append(self.value)
        box.append(self.unit)
        self.set_child(box)

    def set(self, value_text: str, unit_text: str | None = None):
        self.value.set_text(value_text)
        if unit_text is not None:
            self.unit.set_text(unit_text)
            self.unit.set_visible(True)


class TimerBlock(Gtk.Frame):
    def __init__(self, emoji: str = "🏃"):
        super().__init__()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(16)

        self.icon = Gtk.Label(label=emoji)
        self.icon.add_css_class("title-1")
        self.timer = Gtk.Label(label="00:00:00")
        self.timer.add_css_class("title-1")
        self.timer.set_xalign(0)

        box.append(self.icon)
        box.append(self.timer)
        self.set_child(box)

    def set_time(self, text: str):
        self.timer.set_text(text)


# ---------- Tracker Page ----------

class TrackerPageUI:
    def __init__(self, app: "FitnessAppUI"):
        self.app = app

        # live buffers (ms + values)
        self.window_sec = 60         # seconds in chart window
        self.window_ms = self.window_sec * 1000.0
        self._times = deque()
        self._bpms = deque()
        self._powers = deque()

        self._last_ms = None
        self._running = False

        # stats
        self._bpm_sum = 0.0
        self._bpm_n = 0
        self._bpm_max = 0

        # test-mode signal id
        self._test_source = None
        self._start_monotonic = None

    # ---- UI ----
    def build_page(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        for m in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{m}")(12)

        # Title
        title = Gtk.Label(label="Tracker")
        title.add_css_class("title-1")
        title.set_halign(Gtk.Align.CENTER)
        outer.append(title)

        # Controls
        ctrl = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        ctrl.set_halign(Gtk.Align.CENTER)
        self.app.start_btn = Gtk.Button.new_with_label("Start")
        self.app.start_btn.add_css_class("suggested-action")
        self.app.stop_btn = Gtk.Button.new_with_label("Stop")
        self.app.stop_btn.add_css_class("destructive-action")
        self.app.stop_btn.set_sensitive(False)
        ctrl.append(self.app.start_btn)
        ctrl.append(self.app.stop_btn)
        outer.append(ctrl)

        # Metrics grid (match your new layout)
        grid = Gtk.Grid(column_spacing=12, row_spacing=12)

        # Row 0: [icon + timer] spans both columns
        self.timer_block = TimerBlock("🏃")
        grid.attach(self.timer_block, 0, 0, 2, 1)

        # Row 1: [Distance] [Pace]
        self.card_distance = MetricCard("Distance", "mi")
        self.card_pace     = MetricCard("Pace", "min/mi")
        grid.attach(self.card_distance, 0, 1, 1, 1)
        grid.attach(self.card_pace,     1, 1, 1, 1)

        # Row 2: [Cadence] [MPH]
        self.card_cadence = MetricCard("Cadence", "spm")
        self.card_mph     = MetricCard("MPH", None)
        grid.attach(self.card_cadence, 0, 2, 1, 1)
        grid.attach(self.card_mph,     1, 2, 1, 1)

        # Row 3: [Heart Rate] [Power]
        self.card_hr    = MetricCard("Heart Rate", "bpm")
        self.card_power = MetricCard("Power", "W")
        grid.attach(self.card_hr,    0, 3, 1, 1)
        grid.attach(self.card_power, 1, 3, 1, 1)

        outer.append(grid)

        # Live chart (HR + Power)
        frame = Gtk.Frame(label="Live HR / Power")
        self.fig = Figure(figsize=(6, 3), dpi=96)
        self.ax_hr = self.fig.add_subplot(111)
        self._style_axes()

        # Secondary Y for Power
        self.ax_pw = self.ax_hr.twinx()
        self._style_power_axis()

        # Keep HR axis fixed to resting_hr-20 → max_hr+20
        self.ax_hr.set_ylim(self.app.resting_hr - 20, self.app.max_hr + 20)
        self.ax_hr.set_autoscaley_on(False)

        # Allow autoscaling for Power axis with some headroom
        self.ax_pw.set_autoscaley_on(True)
        self.ax_pw.margins(y=0.15)

        (self.line_hr,) = self.ax_hr.plot([], [], lw=2)
        (self.line_pw,) = self.ax_pw.plot([], [], lw=2, linestyle="--", color="#00FFFF")

        canvas = FigureCanvas(self.fig)
        canvas.set_vexpand(True)
        frame.set_child(canvas)
        outer.append(frame)

        # Wire buttons
        self.app.start_btn.connect("clicked", self._on_start)
        self.app.stop_btn.connect("clicked", self._on_stop)

        # Initial zeros
        self._set_all_instant(0, 0, 0, 0, 0, 0)

        return outer

    # ---- Styling helpers ----
    def _style_axes(self):
        zones = self.app.calculate_hr_zones()
        names = list(zones.keys())
        bands = [(n, zones[n]) for n in names]
        colors = self.app.ZONE_COLORS

        self.fig.patch.set_facecolor(self.app.DARK_BG)
        ax = self.ax_hr
        ax.clear()
        ax.set_facecolor(self.app.DARK_BG)
        ax.grid(color=self.app.DARK_GRID, linewidth=0.8)
        ax.tick_params(colors=self.app.DARK_FG)

        ax.set_xlim(0, self.window_sec)

        ymin = self.app.resting_hr - 20
        ymax = self.app.max_hr + 20
        ax.set_ylim(ymin, ymax)

        # Bands
        for i, (_, (lo, hi)) in enumerate(bands):
            ax.axhspan(lo, hi, facecolor=colors[i], alpha=0.35, zorder=0)

        # Boundaries + ticks at zone edges (no mids)
        tick_locs = sorted({y for _, (lo, hi) in bands for y in (lo, hi)})
        for y in tick_locs:
            ax.axhline(y, color=self.app.DARK_BG, linewidth=1.6, alpha=0.65, zorder=1)

        ax.set_yticks(tick_locs)
        ax.set_yticklabels([f"{int(v)}" for v in tick_locs], color=self.app.DARK_FG)

        # 10-second x ticks
        ax.set_xticks(list(range(0, self.window_sec + 1, 10)))
        ax.set_xlabel("Last 60s", color=self.app.DARK_FG)
        ax.set_autoscaley_on(False)

    def _style_power_axis(self):
        ax = self.ax_pw
        ax.tick_params(colors=self.app.DARK_FG)
        ax.yaxis.label.set_color(self.app.DARK_FG)

        ax.set_autoscaley_on(True)
        ax.margins(y=0.15)

        # Optional: initial view before data arrives
        ax.set_ylim(0, 500)

        # Make power visually secondary
        for spine in ax.spines.values():
            spine.set_alpha(0.35)

    # ---- Start/Stop ----
    def _on_start(self, *_):
        self._running = True
        self._start_monotonic = time.monotonic()
        self._times.clear()
        self._bpms.clear()
        self._powers.clear()
        self._last_ms = None

        self._bpm_sum = 0.0
        self._bpm_n = 0
        self._bpm_max = 0

        self.app.start_btn.set_sensitive(False)
        self.app.stop_btn.set_sensitive(True)

        # Start BLE recording if available (not in test mode)
        if self.app.recorder:
            self.app.recorder.start_recording()

        # Kick off test generator when --test was used
        if self.app.test_mode and self._test_source is None:
            # HR simulation state
            self._hrsim_last_ms = None
            self._hrsim_bpm = float(self.app.resting_hr)
            self._test_source = GLib.timeout_add(1000, self._tick_test)

    def _on_stop(self, *_):
        self._running = False
        self.app.stop_btn.set_sensitive(False)
        self.app.start_btn.set_sensitive(True)

        if self.app.recorder:
            self.app.recorder.stop_recording()

        if self._test_source:
            GLib.source_remove(self._test_source)
            self._test_source = None

    # ---- Public API used by Recorder (HR only) ----
    def on_bpm(self, delta_ms: float, bpm: int):
        """Called by Recorder with elapsed-ms + smoothed BPM."""
        if not self._running:
            return

        # Keep a monotonic elapsed time in ms
        t_ms = delta_ms
        self._push_sample(t_ms, bpm, self._current_power_for_time(t_ms))

    # ---- Internal: push combined HR/Power sample ----
    def _push_sample(self, t_ms: float, bpm: int, watts: float):
        # Update stats
        self._bpm_sum += bpm
        self._bpm_n += 1
        self._bpm_max = max(self._bpm_max, bpm)

        # Trim window
        cutoff = t_ms - self.window_ms
        self._times.append(t_ms)
        self._bpms.append(bpm)
        self._powers.append(watts)
        while self._times and self._times[0] < cutoff:
            self._times.popleft(); self._bpms.popleft(); self._powers.popleft()

        # X in seconds from left edge of window
        x = (np.array(self._times) - cutoff) / 1000.0
        self.line_hr.set_data(x, np.array(self._bpms))
        self.line_pw.set_data(x, np.array(self._powers))

        if len(self._powers) >= 2:
            # Robust autoscale with padding; clamps bottom at 0
            p = np.array(self._powers)
            pmin, pmax = float(p.min()), float(p.max())
            if pmax == pmin:
                pad = max(10.0, 0.1 * pmax)   # flat line edge case
            else:
                pad = max(10.0, 0.1 * (pmax - pmin))
            self.ax_pw.set_ylim(max(0.0, pmin - pad), pmax + pad)
        else:
            # fall back early on
            self.ax_pw.set_ylim(0, 500)

        # Update instantaneous cards (distance, pace, etc.)
        elapsed_s = max(0, int(t_ms // 1_000))
        hh, rem = divmod(elapsed_s, 3600)
        mm, ss = divmod(rem, 60)
        self.timer_block.set_time(f"{hh:02d}:{mm:02d}:{ss:02d}")

        # In non-test mode, keep zeros for non-HR metrics
        if self.app.test_mode:
            mph = self._last_mph
            cadence = self._last_cadence
            dist_mi = self._integrated_distance_miles
            pace_str = self._pace_from_mph(mph)
        else:
            mph = 0.0
            cadence = 0
            dist_mi = 0.0
            pace_str = "0:00"

        self._set_all_instant(dist_mi, pace_str, cadence, mph, bpm, int(watts))

        # Make HR line tint match the current zone (subtle)
        _, _, _, rgb = self._zone_info(self._bpms[-1])
        self.line_hr.set_color(rgb)

        # Redraw
        self.fig.canvas.draw_idle()

    def _set_all_instant(self, dist_mi, pace_str, cadence, mph, bpm, watts):
        self.card_distance.set(f"{dist_mi:.2f}")
        self.card_pace.set(f"{pace_str}")
        self.card_cadence.set(f"{int(cadence)}")
        self.card_mph.set(f"{mph:.1f}")
        self.card_hr.set(f"{int(bpm)}")
        self.card_power.set(f"{int(watts)}")

    # ---- Test-data generator (runs when --test) ----
    def _tick_test(self):
        if not self._running:
            return False

        # Elapsed time
        t_now = time.monotonic()
        t_ms = int((t_now - self._start_monotonic) * 1000)
        t_min = t_ms / 60000.0
        dt_s = 1.0
        if self._hrsim_last_ms is not None:
            dt_s = max(0.001, (t_ms - self._hrsim_last_ms) / 1000.0)
        self._hrsim_last_ms = t_ms

        # --- Power with 2-minute interval cycles (two 30s surges + two 30s recoveries) ---
        cycle_len = 120.0  # seconds
        phase = (t_min * 60) % cycle_len

        if phase < 30:
            # Surge 1
            target_power = random.uniform(400, 600)
            interval_intense = True
        elif phase < 60:
            # Recovery 1
            target_power = random.uniform(180, 250)
            interval_intense = False
        elif phase < 90:
            # Surge 2
            target_power = random.uniform(350, 550)
            interval_intense = True
        else:
            # Recovery 2
            target_power = random.uniform(180, 240)
            interval_intense = False

        # Smooth power so it’s not jumpy
        last_power = getattr(self, "_last_power", 250.0)
        self._last_power = last_power + 0.4 * (target_power - last_power)

        # --- Heart rate tied to intervals with realistic kinetics ---
        zones = self.app.calculate_hr_zones()
        z2_lo, _ = zones["Zone 3"]
        # Targets
        if interval_intense:
            # Aim near max during surges
            hr_target = self.app.max_hr - random.uniform(0, 3)
            tau = 10.0  # rise time constant (s): faster to go up
        else:
            # Recover toward lower Zone 2
            hr_target = z2_lo - random.uniform(0, 5)
            tau = 45.0  # decay time constant (s): slower to come down

        # First-order response toward target
        alpha = 1.0 - math.exp(-dt_s / tau)
        self._hrsim_bpm += alpha * (hr_target - self._hrsim_bpm)

        # Small organic jitter
        self._hrsim_bpm += random.uniform(-0.8, 0.8)

        # Clamp
        self._hrsim_bpm = float(
            max(self.app.resting_hr, min(self.app.max_hr, self._hrsim_bpm))
        )
        bpm = int(round(self._hrsim_bpm))

        # --- Speed / cadence dummy (running) ---
        self._last_mph = max(
            2.0, min(10.0, getattr(self, "_last_mph", 6.8) + random.uniform(-0.3, 0.3))
        )
        self._last_cadence = max(
            150, min(190, getattr(self, "_last_cadence", 172) + random.uniform(-2, 2))
        )

        # Integrate distance
        dmiles = self._last_mph / 3600.0
        self._integrated_distance_miles = getattr(self, "_integrated_distance_miles", 0.0) + dmiles

        # Push the sample
        self._push_sample(t_ms, bpm, self._last_power)
        return True



    # Helpers
    def _zone_info(self, hr: float):
        """
        Return (zone_name, lo, hi, rgb_tuple) for a given HR.
        Falls back to closest band if outside.
        """
        zones = self.app.calculate_hr_zones()
        order = list(zones.keys())
        for name, color in zip(order, self.app.ZONE_COLORS):
            lo, hi = zones[name]
            if lo <= hr < hi:
                return name, lo, hi, self._rgb(color)
        # below first or above last
        first, last = order[0], order[-1]
        if hr < zones[first][0]:
            return first, *zones[first], self._rgb(self.app.ZONE_COLORS[0])
        return last, *zones[last], self._rgb(self.app.ZONE_COLORS[-1])

    def _rgb(self, hex_color: str):
        # lightweight converter (no matplotlib.colors import needed here)
        hex_color = hex_color.lstrip("#")
        r = int(hex_color[0:2], 16) / 255.0
        g = int(hex_color[2:4], 16) / 255.0
        b = int(hex_color[4:6], 16) / 255.0
        return (r, g, b)

    @staticmethod
    def _pace_from_mph(mph: float) -> str:
        if mph <= 0.01:
            return "0:00"
        mins_per_mile = 60.0 / mph
        m = int(mins_per_mile)
        s = int(round((mins_per_mile - m) * 60))
        if s == 60:
            m += 1; s = 0
        return f"{m}:{s:02d}"

    def _current_power_for_time(self, _t_ms: float) -> float:
        """When not in test mode, power is 0 (no sensor yet)."""
        if self.app.test_mode:
            return getattr(self, "_last_power", 0.0)
        return 0.0
