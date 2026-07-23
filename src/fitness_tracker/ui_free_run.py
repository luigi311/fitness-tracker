from __future__ import annotations

from typing import TYPE_CHECKING

import gi
from matplotlib.backends.backend_gtk4agg import FigureCanvasGTK4Agg as FigureCanvas
from matplotlib.figure import Figure

from fitness_tracker.database import SportTypesEnum
from fitness_tracker.ui_mode import IndoorOutdoorEnum
from fitness_tracker.ui_workout import InclineControl

gi.require_versions({"Gtk": "4.0", "Adw": "1"})
from gi.repository import Gtk  # noqa: E402  # ty:ignore[unresolved-import]

if TYPE_CHECKING:
    import numpy as np


class _MetricCard(Gtk.Frame):
    def __init__(self, title: str, unit: str | None = None) -> None:
        super().__init__()
        self.set_hexpand(True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for m in ("top", "bottom"):
            getattr(box, f"set_margin_{m}")(12)

        for m in ("start", "end"):
            getattr(box, f"set_margin_{m}")(4)

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


class TrainerTargetControl(Gtk.Frame):
    """Sport-specific free trainer target control."""

    BIKE_MODES = {
        "Power": {"minimum": 0, "maximum": 2000, "step": 5, "unit": "W"},
        "Resistance": {"minimum": 0, "maximum": 100, "step": 1, "unit": "%"},
    }
    RUN_MODES = {
        "Speed": {"minimum": 0.0, "maximum": 15.0, "step": 0.1, "unit": "mph"},
    }

    def __init__(self, on_change, sport_type: SportTypesEnum) -> None:
        super().__init__()
        self._on_change = on_change
        self.MODES = self.BIKE_MODES if sport_type == SportTypesEnum.biking else self.RUN_MODES
        self._mode = "Power" if sport_type == SportTypesEnum.biking else "Speed"
        self._values = {"Power": 100, "Resistance": 2, "Speed": 3.0}

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for margin in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{margin}")(8)

        if sport_type == SportTypesEnum.biking:
            mode_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            mode_row.set_homogeneous(True)
            mode_row.set_hexpand(True)

            self._power_mode_btn = Gtk.Button(label="Power")
            self._power_mode_btn.set_size_request(-1, 56)
            self._power_mode_btn.add_css_class("suggested-action")

            self._resistance_mode_btn = Gtk.Button(label="Resistance")
            self._resistance_mode_btn.set_size_request(-1, 56)

            self._power_mode_btn.connect("clicked", self._on_mode_clicked, "Power")
            self._resistance_mode_btn.connect("clicked", self._on_mode_clicked, "Resistance")
            mode_row.append(self._power_mode_btn)
            mode_row.append(self._resistance_mode_btn)
            outer.append(mode_row)
        else:
            title = Gtk.Label(label="Speed")
            title.add_css_class("caption")
            outer.append(title)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._btn_down = Gtk.Button(label="-")
        self._btn_down.add_css_class("destructive-action")
        self._btn_down.set_hexpand(True)
        self._btn_down.set_size_request(-1, 72)
        self._btn_down.get_child().add_css_class("title-1")
        self._btn_down.connect("clicked", lambda *_: self._change(-1))

        self._lbl_value = Gtk.Label()
        self._lbl_value.add_css_class("title-1")
        self._lbl_value.add_css_class("numeric")
        self._lbl_value.set_hexpand(True)
        self._lbl_value.set_xalign(0.5)

        self._btn_up = Gtk.Button(label="+")
        self._btn_up.add_css_class("suggested-action")
        self._btn_up.set_hexpand(True)
        self._btn_up.set_size_request(-1, 72)
        self._btn_up.get_child().add_css_class("title-1")
        self._btn_up.connect("clicked", lambda *_: self._change(1))

        row.append(self._btn_down)
        row.append(self._lbl_value)
        row.append(self._btn_up)
        outer.append(row)
        self.set_child(outer)
        self._refresh()

    def _change(self, direction: int) -> None:
        config = self.MODES[self._mode]
        value = self._values[self._mode] + direction * config["step"]
        value = max(config["minimum"], min(config["maximum"], value))
        self._values[self._mode] = round(value, 1) if self._mode == "Speed" else value
        self._refresh()
        self._on_change(self._mode, self._values[self._mode])

    def _on_mode_clicked(self, _button, mode: str) -> None:
        if mode == self._mode:
            return
        self._mode = mode
        self._power_mode_btn.set_css_classes(["suggested-action"] if mode == "Power" else [])
        self._resistance_mode_btn.set_css_classes(
            ["suggested-action"] if mode == "Resistance" else []
        )
        self._refresh()
        self._on_change(self._mode, self._values[self._mode])

    def _refresh(self) -> None:
        config = self.MODES[self._mode]
        value = self._values[self._mode]
        value_text = f"{value:.1f}" if self._mode == "Speed" else str(value)
        self._lbl_value.set_text(f"{value_text} {config['unit']}")
        self._btn_down.set_sensitive(value > config["minimum"])
        self._btn_up.set_sensitive(value < config["maximum"])


class FreeRunView(Gtk.Box):
    """
    The full dashboard (timer, metric cards, live HR/Power chart) as a reusable widget.
    Controller should call:
      - set_timer("hh:mm:ss")
      - set_metrics(dist_mi, pace_str, cadence, mph, bpm, watts)
      - set_statuses(hr_ok, speed_ok, cad_ok, pow_ok, dist_ok)
      - update_chart(x_seconds, hr_series, pw_series, colors)
      - set_recording(recording_bool)  # toggles Start/Stop buttons.
    """

    def __init__(
        self, app, sport_type: SportTypesEnum, in_outdoor: IndoorOutdoorEnum, trainer: bool = False
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.app = app
        self.sport_type = sport_type
        self.in_outdoor = in_outdoor
        self.trainer = trainer
        for m in ("top", "bottom"):
            getattr(self, f"set_margin_{m}")(12)

        for m in ("start", "end"):
            getattr(self, f"set_margin_{m}")(4)

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

        # Incline control — spans full width below metric cards
        initial_incline = (
            self.app.recorder.incline_percent
            if self.app.recorder and self.app.recorder.incline_percent is not None
            else 0.0
        )
        self.incline_control = InclineControl(
            on_change=self._on_incline_change,
            initial_value=initial_incline,
        )

        # Only enable incline control for footpod running mode
        # since it's only useful for treadmill runs
        if (
            self.sport_type == SportTypesEnum.running
            and self.app.recorder
            and self.in_outdoor == IndoorOutdoorEnum.indoor
            and not self.trainer
        ):
            grid.attach(self.incline_control, 0, 4, 2, 1)

        self.trainer_target_control = TrainerTargetControl(
            self._on_trainer_target_change,
            self.sport_type,
        )
        if self.trainer:
            grid.attach(self.trainer_target_control, 0, 4, 2, 1)

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

    def set_metrics(
        self,
        dist_mi: float,
        pace_str: str,
        cadence: int,
        mph: float,
        bpm: int,
        watts: int,
    ) -> None:
        self.card_distance.set_value(f"{dist_mi:.2f}")
        self.card_pace.set_value(pace_str)
        # Running is double the cadence
        self.card_cadence.set_value(
            f"{int(cadence * 2) if self.sport_type == SportTypesEnum.running else int(cadence)}",
        )
        self.card_mph.set_value(f"{mph:.1f}")
        self.card_hr.set_value(f"{int(bpm)}")
        self.card_power.set_value(f"{int(watts)}")

    def set_statuses(
        self,
        hr_ok: bool,
        speed_ok: bool,
        cad_ok: bool,
        pow_ok: bool,
        dist_ok: bool,
    ) -> None:
        self.card_hr.set_status(
            hr_ok,
            "HR sensor connected" if hr_ok else "HR sensor not connected",
        )
        self.card_distance.set_status(
            dist_ok,
            "Distance sensor connected" if dist_ok else "Distance sensor not connected",
        )
        self.card_pace.set_status(
            speed_ok,
            "Speed sensor connected" if speed_ok else "Speed sensor not connected",
        )
        self.card_cadence.set_status(
            cad_ok,
            "Cadence sensor connected" if cad_ok else "Cadence sensor not connected",
        )
        self.card_mph.set_status(
            speed_ok,
            "Speed sensor connected" if speed_ok else "Speed sensor not connected",
        )
        self.card_power.set_status(
            pow_ok,
            "Power sensor connected" if pow_ok else "Power sensor not connected",
        )

    def set_recording(self, recording: bool) -> None:
        """Toggle Start/Stop button sensitivity/visibility."""
        self.btn_start.set_sensitive(not recording)
        self.btn_stop.set_sensitive(True)  # allow stop to also act as 'back' if needed

    def update_chart(
        self,
        x_secs: np.ndarray,
        hr: np.ndarray,
        pw: np.ndarray,
        hr_rgb=(1, 1, 1),
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

    def _on_incline_change(self, percent: float) -> None:
        """Propagated up to the app via on_incline if set."""
        if callable(getattr(self, "_incline_cb", None)):
            self._incline_cb(percent)

    def set_incline_callback(self, cb) -> None:
        """Register a callable(percent: float) to fire on every incline change."""
        self._incline_cb = cb

    def _on_trainer_target_change(self, mode: str, value: int | float) -> None:
        if callable(getattr(self, "_trainer_target_cb", None)):
            self._trainer_target_cb(mode, value)

    def set_trainer_target_callback(self, cb) -> None:
        """Register a callable(mode, value) for trainer target changes."""
        self._trainer_target_cb = cb

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
        ymin = self.app.app_settings.personal.resting_hr - 20
        ymax = self.app.app_settings.personal.max_hr + 20
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
