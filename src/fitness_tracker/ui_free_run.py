from __future__ import annotations

import gi
import numpy as np
from matplotlib.backends.backend_gtk4agg import FigureCanvasGTK4Agg as FigureCanvas
from matplotlib.figure import Figure

gi.require_versions({"Gtk": "4.0", "Adw": "1"})
from gi.repository import Gtk


class _MetricCard(Gtk.Frame):
    def __init__(self, title: str, unit: str | None = None) -> None:
        super().__init__()
        self.set_hexpand(True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(12)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.title = Gtk.Label(label=title)
        self.title.add_css_class("caption")
        self.title.set_xalign(0)
        self.title.set_hexpand(True)

        self.status = Gtk.Label(label="")
        self.status.set_use_markup(True)
        self.status.set_xalign(1.0)

        header.append(self.title)
        header.append(self.status)

        self.value = Gtk.Label(label="0")
        self.value.add_css_class("title-1")
        self.value.set_xalign(0)

        self.unit = Gtk.Label(label=unit or "")
        self.unit.add_css_class("dim-label")
        self.unit.set_xalign(0)
        self.unit.set_visible(bool(unit))

        box.append(header)
        box.append(self.value)
        box.append(self.unit)
        self.set_child(box)

    def set_value(self, value_text: str, unit_text: str | None = None):
        self.value.set_text(value_text)
        if unit_text is not None:
            self.unit.set_text(unit_text)
            self.unit.set_visible(True)

    def set_status(self, connected: bool, tooltip: str | None = None) -> None:
        self.status.set_markup("🟢" if connected else "⚫")
        self.status.set_tooltip_text(tooltip or None)
        alpha = 1.0 if connected else 0.55
        self.value.set_opacity(alpha)
        self.unit.set_opacity(alpha)


class _Timer(Gtk.Frame):
    def __init__(self) -> None:
        super().__init__()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(16)

        self.lbl = Gtk.Label(label="00:00:00")
        self.lbl.add_css_class("title-1")
        self.lbl.set_halign(Gtk.Align.CENTER)
        self.lbl.set_xalign(0.5)

        box.set_halign(Gtk.Align.CENTER)
        box.append(self.lbl)
        self.set_child(box)

    def set_text(self, text: str) -> None:
        self.lbl.set_text(text)


class FreeRunView(Gtk.Box):
    """
    The full dashboard (timer, metric cards, live HR/Power chart) as a reusable widget.
    Controller should call:
      - set_timer("hh:mm:ss")
      - set_metrics(dist_mi, pace_str, cadence, mph, bpm, watts)
      - set_statuses(hr_ok, speed_ok, cad_ok, pow_ok)
      - update_chart(x_seconds, hr_series, pw_series, colors)
      - set_recording(recording_bool)  # toggles Start/Stop buttons.
    """

    def __init__(self, app) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.app = app
        for m in ("top", "bottom", "start", "end"):
            getattr(self, f"set_margin_{m}")(12)

        self.type = "run"

        # Start/Stop row
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row.set_halign(Gtk.Align.CENTER)

        self.btn_start = Gtk.Button.new_with_label("▶️  Start")
        self.btn_start.add_css_class("suggested-action")

        self.btn_stop = Gtk.Button.new_with_label("⏹️  Stop")
        self.btn_stop.add_css_class("destructive-action")

        row.append(self.btn_stop)
        row.append(self.btn_start)
        self.append(row)

        # Grid metrics
        grid = Gtk.Grid(column_spacing=6, row_spacing=6)
        self.timer = _Timer()
        grid.attach(self.timer, 0, 0, 2, 1)

        self.card_distance = _MetricCard("Distance", "mi")
        self.card_pace = _MetricCard("Pace", "min/mi")
        grid.attach(self.card_distance, 0, 1, 1, 1)
        grid.attach(self.card_pace, 1, 1, 1, 1)

        self.card_cadence = _MetricCard("Cadence", "spm")
        self.card_mph = _MetricCard("MPH")
        grid.attach(self.card_cadence, 0, 2, 1, 1)
        grid.attach(self.card_mph, 1, 2, 1, 1)

        self.card_hr = _MetricCard("Heart Rate", "bpm")
        self.card_power = _MetricCard("Power", "W")
        grid.attach(self.card_hr, 0, 3, 1, 1)
        grid.attach(self.card_power, 1, 3, 1, 1)

        self.append(grid)

        # Chart
        self.fig = Figure(figsize=(6, 3), dpi=96)
        self.ax_hr = self.fig.add_subplot(111)
        self._style_hr_axis()

        self.ax_pw = self.ax_hr.twinx()
        self._style_pw_axis()

        (self.line_pw,) = self.ax_pw.plot([], [], lw=2, linestyle="--", color="#00FFFF", zorder=1)
        (self.line_hr,) = self.ax_hr.plot([], [], lw=2, zorder=2)

        canvas = FigureCanvas(self.fig)
        canvas.set_vexpand(True)
        frame = Gtk.Frame(label="Live HR / Power")
        frame.set_child(canvas)
        self.append(frame)

        # initial values
        self.set_metrics(0.0, "0:00", 0, 0.0, 0, 0)
        self.set_recording(False)

    # ---- public setters
    def set_timer(self, text: str) -> None:
        self.timer.set_text(text)

    def set_metrics(self, dist_mi, pace_str, cadence, mph, bpm, watts) -> None:
        self.card_distance.set_value(f"{dist_mi:.2f}")
        self.card_pace.set_value(pace_str)
        # Running is double the cadence
        self.card_cadence.set_value(f"{int(cadence*2) if self.type == "run" else int(cadence)}")
        self.card_mph.set_value(f"{mph:.1f}")
        self.card_hr.set_value(f"{int(bpm)}")
        self.card_power.set_value(f"{int(watts)}")

    def set_statuses(self, hr_ok: bool, speed_ok: bool, cad_ok: bool, pow_ok: bool) -> None:
        self.card_hr.set_status(
            hr_ok, "HR sensor connected" if hr_ok else "HR sensor not connected"
        )
        self.card_distance.set_status(
            speed_ok, "Speed sensor connected" if speed_ok else "Speed sensor not connected"
        )
        self.card_pace.set_status(
            speed_ok, "Speed sensor connected" if speed_ok else "Speed sensor not connected"
        )
        self.card_cadence.set_status(
            cad_ok, "Cadence sensor connected" if cad_ok else "Cadence sensor not connected"
        )
        self.card_mph.set_status(
            speed_ok, "Speed sensor connected" if speed_ok else "Speed sensor not connected"
        )
        self.card_power.set_status(
            pow_ok, "Power sensor connected" if pow_ok else "Power sensor not connected"
        )

    def set_recording(self, recording: bool) -> None:
        """Toggle Start/Stop button sensitivity/visibility."""
        self.btn_start.set_sensitive(not recording)
        self.btn_stop.set_sensitive(True)  # allow stop to also act as 'back' if needed

    def update_chart(
        self, x_secs: np.ndarray, hr: np.ndarray, pw: np.ndarray, hr_rgb=(1, 1, 1)
    ) -> None:
        self.line_hr.set_data(x_secs, hr)
        self.line_pw.set_data(x_secs, pw)
        self.line_hr.set_color(hr_rgb)

        if len(pw) >= 2:
            pmin, pmax = float(pw.min()), float(pw.max())
            pad = max(10.0, 0.1 * (pmax - pmin if pmax != pmin else max(1.0, pmax)))
            self.ax_pw.set_ylim(max(0.0, pmin - pad), pmax + pad)
        else:
            self.ax_pw.set_ylim(0, 500)

        self.fig.canvas.draw_idle()

    # ---- style helpers
    def _style_hr_axis(self) -> None:
        zones = self.app.calculate_hr_zones()
        colors = self.app.ZONE_COLORS

        self.fig.patch.set_facecolor(self.app.DARK_BG)
        ax = self.ax_hr
        ax.clear()
        ax.set_facecolor(self.app.DARK_BG)
        ax.grid(color=self.app.DARK_GRID, linewidth=0.8)
        ax.tick_params(colors=self.app.DARK_FG)

        ax.set_xlim(0, 60)
        ymin = self.app.resting_hr - 20
        ymax = self.app.max_hr + 20
        ax.set_ylim(ymin, ymax)
        ax.set_autoscaley_on(False)

        for i, (lo_hi) in enumerate(zones.values()):
            lo, hi = lo_hi
            ax.axhspan(lo, hi, facecolor=colors[i], alpha=0.35, zorder=0)

        tick_locs = sorted({y for (lo, hi) in zones.values() for y in (lo, hi)})
        for y in tick_locs:
            ax.axhline(y, color=self.app.DARK_BG, linewidth=1.6, alpha=0.65, zorder=1)

        ax.set_yticks(tick_locs)
        ax.set_yticklabels([f"{int(v)}" for v in tick_locs], color=self.app.DARK_FG)
        ax.set_xticks(list(range(0, 61, 10)))
        ax.set_xlabel("Last 60s", color=self.app.DARK_FG)

    def _style_pw_axis(self) -> None:
        ax = self.ax_pw
        ax.tick_params(colors=self.app.DARK_FG)
        ax.yaxis.label.set_color(self.app.DARK_FG)
        ax.set_autoscaley_on(True)
        ax.margins(y=0.15)
        ax.set_ylim(0, 500)
        for spine in ax.spines.values():
            spine.set_alpha(0.35)
