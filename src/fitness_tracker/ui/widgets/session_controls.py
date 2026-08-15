"""Workout-specific controls shared by the unified session page."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import cairo
import gi

gi.require_versions({"Gtk": "4.0", "Adw": "1"})
from gi.repository import Gdk, Gtk, Pango, PangoCairo  # noqa: E402  # ty:ignore[unresolved-import]

if TYPE_CHECKING:
    from collections.abc import Callable

    from cairo import Context


_GAUGE_REFERENCE_HEIGHT = 200
_GAUGE_CONTENT_HEIGHT = 125
_GAUGE_BOTTOM_PADDING = 15


class InclineControl(Gtk.Frame):
    """Large-touch incline control for compatible indoor sessions."""

    MIN_PCT = -20.0
    MAX_PCT = 20.0
    STEP = 1.0

    def __init__(
        self,
        on_change: Callable[[float], None],
        initial_value: float = 0.0,
    ) -> None:
        super().__init__()
        self._value = self._clamp_value(initial_value)
        self._on_change = on_change

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        outer.set_margin_top(8)
        outer.set_margin_bottom(8)
        outer.set_margin_start(4)
        outer.set_margin_end(4)

        lbl_title = Gtk.Label(label="⛰  Incline")
        lbl_title.add_css_class("caption")
        lbl_title.set_xalign(0.5)
        outer.append(lbl_title)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self._btn_down = Gtk.Button(label="-")
        self._btn_down.add_css_class("destructive-action")
        self._btn_down.set_hexpand(True)
        self._btn_down.set_size_request(-1, 72)
        self._btn_down.get_child().add_css_class("title-1")
        self._btn_down.connect("clicked", lambda *_: self._change(-self.STEP))

        val_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        val_box.set_hexpand(True)
        val_box.set_valign(Gtk.Align.CENTER)

        self._lbl_value = Gtk.Label()
        self._lbl_value.add_css_class("title-1")
        self._lbl_value.add_css_class("numeric")
        self._lbl_value.set_xalign(0.5)
        self._lbl_value.set_width_chars(6)

        self._lbl_unit = Gtk.Label(label="% grade")
        self._lbl_unit.add_css_class("dim-label")
        self._lbl_unit.set_xalign(0.5)

        val_box.append(self._lbl_value)
        val_box.append(self._lbl_unit)

        self._btn_up = Gtk.Button(label="+")
        self._btn_up.add_css_class("suggested-action")
        self._btn_up.set_hexpand(True)
        self._btn_up.set_size_request(-1, 72)
        self._btn_up.get_child().add_css_class("title-1")
        self._btn_up.connect("clicked", lambda *_: self._change(+self.STEP))

        row.append(self._btn_down)
        row.append(val_box)
        row.append(self._btn_up)
        outer.append(row)
        self.set_child(outer)
        self._refresh()

    def _change(self, delta: float) -> None:
        self._value = self._clamp_value(round(self._value + delta, 1))
        self._refresh()
        self._on_change(self._value)

    def _refresh(self) -> None:
        sign = "+" if self._value > 0 else ""
        self._lbl_value.set_text(f"{sign}{self._value:.1f}")
        self._btn_down.set_sensitive(self._value > self.MIN_PCT)
        self._btn_up.set_sensitive(self._value < self.MAX_PCT)

    def set_value(self, value: float) -> None:
        """Set the displayed incline, clamped to the supported range."""
        self._value = self._clamp_value(value)
        self._refresh()

    @classmethod
    def _clamp_value(cls, value: float) -> float:
        return max(cls.MIN_PCT, min(cls.MAX_PCT, float(value)))


class TargetGauge(Gtk.DrawingArea):
    """Draw a target band and current-value needle for workout guidance."""

    def __init__(self) -> None:
        super().__init__()
        self.set_content_width(320)
        self.set_content_height(_GAUGE_CONTENT_HEIGHT)
        self.add_css_class("frame")
        self._value = 0.0
        self._headline = "—"
        self._subline = "Target: —"
        self._tgt_lo = 0.0
        self._tgt_hi = 0.0
        self._tgt_mid = 0.0
        self._dom_min = 0.0
        self._dom_max = 1.0
        self.set_draw_func(self._on_draw)

    def band_status(self) -> str:
        """Return the current relationship to the target band."""
        if self._tgt_mid <= 0:
            return "in"
        in_band = self._tgt_lo <= self._value <= self._tgt_hi
        if in_band:
            return "in"
        near = 0.1 * self._tgt_mid
        if self._value < self._tgt_lo:
            return "near" if (self._tgt_lo - self._value) <= near else "low"
        return "near" if (self._value - self._tgt_hi) <= near else "high"

    def set_state(
        self,
        *,
        value: float,
        target_lo: float,
        target_mid: float,
        target_hi: float,
        headline: str,
        subline: str,
        domain_pad: float = 0.5,
    ) -> None:
        """Set target and current values, then redraw the gauge."""
        pad = max(0.1, float(domain_pad))
        self._value = float(value)
        self._tgt_lo = target_lo
        self._tgt_hi = target_hi
        self._tgt_mid = target_mid
        self._headline = headline
        self._subline = subline
        self._dom_min = max(1e-6, target_mid * (1.0 - pad))
        self._dom_max = max(self._dom_min + 1e-6, target_mid * (1.0 + pad))
        self.queue_draw()

    def _angle_for(self, value: float, start: float, end: float) -> float:
        """Map a target value into the gauge's display arc."""
        if self._dom_max <= self._dom_min:
            fraction = 0.0
        else:
            fraction = max(
                0.0,
                min(1.0, (value - self._dom_min) / (self._dom_max - self._dom_min)),
            )
        return start + (end - start) * fraction

    def _draw_target_band(
        self,
        ctx: Context,
        *,
        center_x: float,
        center_y: float,
        radius: float,
        bar_width: float,
        start: float,
        end: float,
        dim: Gdk.RGBA,
        grid: Gdk.RGBA,
    ) -> None:
        """Draw the base arc, target band, and reference marks."""
        ctx.set_line_width(bar_width)
        ctx.set_source_rgba(dim.red, dim.green, dim.blue, dim.alpha)
        ctx.arc(center_x, center_y, radius, start, end)
        ctx.stroke()

        ctx.set_source_rgba(0.20, 0.80, 0.30, 0.95)
        ctx.arc(
            center_x,
            center_y,
            radius,
            self._angle_for(self._tgt_lo, start, end),
            self._angle_for(self._tgt_hi, start, end),
        )
        ctx.stroke()
        ctx.set_line_width(bar_width * 0.45)
        ctx.arc(
            center_x,
            center_y,
            radius,
            self._angle_for(self._tgt_lo, start, end),
            self._angle_for(self._tgt_hi, start, end),
        )
        ctx.stroke()

        ctx.set_line_width(2.6)
        ctx.set_source_rgba(grid.red, grid.green, grid.blue, grid.alpha)
        for fraction in (0.5, 1.0, 1.5):
            angle = self._angle_for(self._tgt_mid * fraction, start, end)
            x0 = center_x + math.cos(angle) * (radius - bar_width * 0.7)
            y0 = center_y + math.sin(angle) * (radius - bar_width * 0.7)
            x1 = center_x + math.cos(angle) * (radius + bar_width * 0.15)
            y1 = center_y + math.sin(angle) * (radius + bar_width * 0.15)
            ctx.move_to(x0, y0)
            ctx.line_to(x1, y1)
            ctx.stroke()

    def _draw_needle(
        self,
        ctx: Context,
        *,
        center_x: float,
        center_y: float,
        radius: float,
        bar_width: float,
        start: float,
        end: float,
    ) -> None:
        """Draw the current-value needle with its target-status color."""
        angle = self._angle_for(self._value, start, end)
        in_band = self._tgt_lo <= self._value <= self._tgt_hi
        near_band = (
            self._value < self._tgt_lo and (self._tgt_lo - self._value) <= 0.1 * self._tgt_mid
        ) or (self._value > self._tgt_hi and (self._value - self._tgt_hi) <= 0.1 * self._tgt_mid)
        if in_band:
            needle_color = (0.20, 0.85, 0.30, 1.0)
        elif near_band:
            needle_color = (0.95, 0.75, 0.20, 1.0)
        else:
            needle_color = (0.95, 0.35, 0.35, 1.0)

        ctx.set_line_width(6.0)
        ctx.set_source_rgba(0, 0, 0, 0.25)
        outer_x = center_x + math.cos(angle) * (radius + bar_width * 0.05 + 1.0)
        outer_y = center_y + math.sin(angle) * (radius + bar_width * 0.05 + 1.0)
        inner_x = center_x + math.cos(angle) * (radius - bar_width * 0.75 + 1.0)
        inner_y = center_y + math.sin(angle) * (radius - bar_width * 0.75 + 1.0)
        ctx.move_to(inner_x, inner_y)
        ctx.line_to(outer_x, outer_y)
        ctx.stroke()

        ctx.set_line_width(4.6)
        ctx.set_source_rgba(*needle_color)
        outer_x = center_x + math.cos(angle) * (radius + bar_width * 0.05)
        outer_y = center_y + math.sin(angle) * (radius + bar_width * 0.05)
        inner_x = center_x + math.cos(angle) * (radius - bar_width * 0.75)
        inner_y = center_y + math.sin(angle) * (radius - bar_width * 0.75)
        ctx.move_to(inner_x, inner_y)
        ctx.line_to(outer_x, outer_y)
        ctx.stroke()

    def _draw_gauge_labels(
        self,
        area: Gtk.DrawingArea,
        ctx: Context,
        *,
        center_x: float,
        center_y: float,
        radius: float,
        fg: Gdk.RGBA,
    ) -> None:
        """Draw the headline and target subline inside the gauge."""
        layout = area.create_pango_layout(self._headline)
        description = (
            layout.get_font_description() or area.get_pango_context().get_font_description()
        )
        description = description.copy()
        description.set_size(int(22 * Pango.SCALE))
        description.set_weight(Pango.Weight.BOLD)
        layout.set_font_description(description)
        text_width, text_height = layout.get_pixel_size()
        ctx.set_source_rgba(fg.red, fg.green, fg.blue, 0.95)
        ctx.move_to(
            center_x - text_width / 2.0,
            center_y - radius * 0.42 - text_height / 2.0,
        )
        PangoCairo.show_layout(ctx, layout)

        subline = area.create_pango_layout(self._subline)
        sub_description = (
            subline.get_font_description() or area.get_pango_context().get_font_description()
        )
        sub_description = sub_description.copy()
        sub_description.set_size(int(13 * Pango.SCALE))
        subline.set_font_description(sub_description)
        sub_width, sub_height = subline.get_pixel_size()
        ctx.set_source_rgba(fg.red, fg.green, fg.blue, 0.8)
        ctx.move_to(
            center_x - sub_width / 2.0,
            center_y - radius * 0.14 - sub_height / 2.0,
        )
        PangoCairo.show_layout(ctx, subline)

    def _on_draw(
        self,
        area: Gtk.DrawingArea,
        ctx: Context,
        width: int,
        height: int,
    ) -> None:
        center_x, center_y = width / 2.0, height - _GAUGE_BOTTOM_PADDING
        # Keep the original gauge radius while removing unused container height.
        radius = min(width, _GAUGE_REFERENCE_HEIGHT) * 0.44
        bar_width = max(10.0, radius * 0.14)
        ctx.set_antialias(cairo.Antialias.GRAY)
        ctx.set_line_cap(cairo.LineCap.ROUND)
        ctx.set_line_join(cairo.LineJoin.ROUND)

        style = area.get_style_context()
        fg = (
            style.lookup_color("theme_fg_color")[1]
            if style.lookup_color("theme_fg_color")[0]
            else Gdk.RGBA(1, 1, 1, 1)
        )
        dim = Gdk.RGBA(fg.red, fg.green, fg.blue, 0.22)
        grid = Gdk.RGBA(fg.red, fg.green, fg.blue, 0.14)
        start = math.radians(195)
        end = math.radians(345)

        self._draw_target_band(
            ctx,
            center_x=center_x,
            center_y=center_y,
            radius=radius,
            bar_width=bar_width,
            start=start,
            end=end,
            dim=dim,
            grid=grid,
        )
        self._draw_needle(
            ctx,
            center_x=center_x,
            center_y=center_y,
            radius=radius,
            bar_width=bar_width,
            start=start,
            end=end,
        )
        self._draw_gauge_labels(
            area,
            ctx,
            center_x=center_x,
            center_y=center_y,
            radius=radius,
            fg=fg,
        )
