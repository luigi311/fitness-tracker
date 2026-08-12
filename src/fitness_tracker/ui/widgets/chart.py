"""Reusable matplotlib chart surfaces and theme styling."""

from __future__ import annotations

from typing import TYPE_CHECKING

import gi
import numpy as np
from matplotlib.backends.backend_gtk4agg import FigureCanvasGTK4Agg as FigureCanvas
from matplotlib.figure import Figure

from fitness_tracker.core.zones import hex_to_rgb

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402  # ty:ignore[unresolved-import]

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cairo import Context
    from matplotlib.axes import Axes

    from fitness_tracker.core.zones import ChartTheme, ZoneThresholds

_ZONE_ALPHA = 0.25
_SPARKLINE_MAX_POINTS = 160
_SPARKLINE_MIN_DIMENSION = 4


def style_axes(
    ax: Axes,
    theme: ChartTheme,
    *,
    zones: ZoneThresholds | None = None,
) -> None:
    """Apply the shared application style and optional heart-rate bands."""
    ax.figure.patch.set_facecolor(theme.background)
    ax.set_facecolor(theme.background)
    ax.xaxis.label.set_color(theme.foreground)
    ax.yaxis.label.set_color(theme.foreground)
    ax.tick_params(colors=theme.foreground)
    ax.grid(color=theme.grid)

    if zones is not None:
        for (_, (low, high)), color in zip(zones.items(), theme.zone_colors, strict=True):
            ax.axhspan(low, high, facecolor=color, alpha=_ZONE_ALPHA, zorder=0)


class LiveChart:
    """Live heart-rate and power chart used by the session dashboard."""

    def __init__(
        self,
        theme: ChartTheme,
        zones: ZoneThresholds,
        *,
        resting_hr: float,
        max_hr: float,
    ) -> None:
        self.figure = Figure(figsize=(6, 3), dpi=96)
        self.hr_axes = self.figure.add_subplot(111)
        self.power_axes = self.hr_axes.twinx()
        (self.line_pw,) = self.power_axes.plot(
            [],
            [],
            lw=2,
            linestyle="--",
            color="#00FFFF",
            zorder=1,
        )
        (self.line_hr,) = self.hr_axes.plot([], [], lw=2, zorder=2)
        self.canvas = FigureCanvas(self.figure)
        self.canvas.set_vexpand(True)
        self.refresh_theme(theme, zones, resting_hr=resting_hr, max_hr=max_hr)

    def refresh_theme(
        self,
        theme: ChartTheme,
        zones: ZoneThresholds,
        *,
        resting_hr: float,
        max_hr: float,
    ) -> None:
        """Restyle the chart while preserving its current samples."""
        hr_x = self.line_hr.get_xdata()
        hr_y = self.line_hr.get_ydata()
        hr_color = self.line_hr.get_color()

        self.hr_axes.clear()
        style_axes(self.hr_axes, theme, zones=zones)
        self.hr_axes.set_xlim(0, 60)
        self.hr_axes.set_ylim(resting_hr - 20, max_hr + 20)
        self.hr_axes.set_autoscaley_on(False)

        tick_locs = sorted({y for low, high in zones.values() for y in (low, high)})
        for y in tick_locs:
            self.hr_axes.axhline(
                y,
                color=theme.background,
                linewidth=1.6,
                alpha=0.65,
                zorder=1,
            )
        self.hr_axes.set_yticks(tick_locs)
        self.hr_axes.set_yticklabels(
            [f"{int(value)}" for value in tick_locs],
            color=theme.foreground,
        )
        self.hr_axes.set_xticks(list(range(0, 61, 10)))
        self.hr_axes.set_xlabel("Last 60s", color=theme.foreground)
        (self.line_hr,) = self.hr_axes.plot(
            hr_x,
            hr_y,
            lw=2,
            color=hr_color,
            zorder=2,
        )

        style_axes(self.power_axes, theme)
        self.power_axes.set_autoscaley_on(True)
        self.power_axes.margins(y=0.15)
        self.power_axes.set_ylim(0, 500)
        for spine in self.power_axes.spines.values():
            spine.set_alpha(0.35)
        self.canvas.draw_idle()


class CompareChart:
    """Matplotlib surface used for activity comparison."""

    def __init__(self) -> None:
        self.figure = Figure(figsize=(6, 3), dpi=96, constrained_layout=True)
        self.axes = self.figure.add_subplot(111)
        self.canvas = FigureCanvas(self.figure)
        self.canvas.set_vexpand(True)

    def style(self, theme: ChartTheme, *, zones: ZoneThresholds | None = None) -> None:
        """Apply the shared style to the comparison axes."""
        style_axes(self.axes, theme, zones=zones)


class Sparkline:
    """Compact themed Cairo activity sparkline."""

    def __init__(self, xs: Sequence[float], ys: Sequence[float], theme: ChartTheme) -> None:
        points = tuple((float(x), float(y)) for x, y in zip(xs, ys, strict=True))
        self._points = _downsample(points, _SPARKLINE_MAX_POINTS)
        self._foreground = (0.0, 0.0, 0.0)
        self._background = (1.0, 1.0, 1.0)
        self.canvas = Gtk.DrawingArea()
        self.canvas.set_size_request(260, 44)
        self.canvas.set_draw_func(self._draw)
        self.refresh_theme(theme)

    def refresh_theme(self, theme: ChartTheme) -> None:
        """Restyle the sparkline without changing its samples."""
        self._foreground = hex_to_rgb(theme.foreground)
        self._background = hex_to_rgb(theme.background)
        self.canvas.queue_draw()

    def _draw(
        self,
        _area: Gtk.DrawingArea,
        context: Context,
        width: int,
        height: int,
    ) -> None:
        """Render the downsampled points into the GTK drawing area."""
        context.set_source_rgb(*self._background)
        context.rectangle(0, 0, width, height)
        context.fill()
        if (
            not self._points
            or width <= _SPARKLINE_MIN_DIMENSION
            or height <= _SPARKLINE_MIN_DIMENSION
        ):
            return

        min_x = self._points[0][0]
        max_x = self._points[-1][0]
        min_y = min(y for _x, y in self._points)
        max_y = max(y for _x, y in self._points)
        x_span = max_x - min_x or 1.0
        y_span = max_y - min_y or 1.0
        padding = 2.0
        plot_width = max(width - 2 * padding, 1.0)
        plot_height = max(height - 2 * padding, 1.0)

        context.set_source_rgb(*self._foreground)
        context.set_line_width(1.2)
        for index, (x, y) in enumerate(self._points):
            px = padding + (x - min_x) / x_span * plot_width
            py = height - padding - (y - min_y) / y_span * plot_height
            if index == 0:
                context.move_to(px, py)
            else:
                context.line_to(px, py)
        context.stroke()


def _downsample(
    points: Sequence[tuple[float, float]],
    max_points: int,
) -> tuple[tuple[float, float], ...]:
    """Decimate a line while retaining its first and last points."""
    if len(points) <= max_points:
        return tuple(points)
    indices = np.linspace(0, len(points) - 1, max_points, dtype=int)
    return tuple(points[index] for index in indices)
