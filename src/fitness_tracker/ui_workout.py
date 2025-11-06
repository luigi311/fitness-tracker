from __future__ import annotations

import math

import gi

gi.require_versions({"Gtk": "4.0", "Adw": "1"})
from gi.repository import Gtk, Adw, Gdk, Pango, PangoCairo


# --------- TargetGauge: semi-circle with target band + needle --------- #
class TargetGauge(Gtk.DrawingArea):
    """
    A semi-circular gauge that shows:
      - A target band (lo..hi) highlighted on the arc
      - A needle for the current value
      - Tick marks at 50%, 100%, 150% of target-center
    Works for power or pace (we only care about the numeric domain here).
    """

    def __init__(self) -> None:
        super().__init__()
        self.set_content_width(320)
        self.set_content_height(200)
        self.add_css_class("frame")
        # State
        self._kind = "POWER"  # or "PACE"
        self._value = 0.0
        self._units = "W"
        self._headline = "—"
        self._subline = "Target: —"

        # Target model
        self._tgt_lo = 0.0
        self._tgt_hi = 0.0
        self._tgt_ctr = 0.0

        # Display domain (we map [min,max] -> [start,end] of the arc)
        self._dom_min = 0.0
        self._dom_max = 1.0

        # Draw
        self.set_draw_func(self._on_draw)

    # --- helpers exposed to parent view ---
    def band_status(self) -> str:
        """Return 'in', 'near', 'low', or 'high' relative to target band."""
        if self._tgt_ctr <= 0:
            return "in"
        in_band = self._tgt_lo <= self._value <= self._tgt_hi
        if in_band:
            return "in"
        near = 0.1 * self._tgt_ctr
        if self._value < self._tgt_lo:
            return "near" if (self._tgt_lo - self._value) <= near else "low"
        return "near" if (self._value - self._tgt_hi) <= near else "high"

    def set_state(
        self,
        *,
        kind: str,
        value: float,
        units: str,
        target_lo: float,
        target_hi: float,
        headline: str,
        subline: str,
        domain_pad: float = 0.5,
    ) -> None:
        """
        kind: "power" or "pace" (case-insensitive)
        value: current numeric (watts or m/s)
        units: e.g. "W" or "min/mi"
        target_lo/hi: numeric range in the same unit as `value`
        headline/subline: text to render under the arc
        domain_pad: fraction around center for min/max (0.5 == ±50%).
        """
        lo = float(min(target_lo, target_hi))
        hi = float(max(target_lo, target_hi))
        ctr = 0.5 * (lo + hi)
        pad = max(0.1, float(domain_pad))

        self._kind = "PACE" if (kind or "").lower().startswith("pace") else "POWER"
        self._value = float(value)
        self._units = units or ""
        self._tgt_lo = lo
        self._tgt_hi = hi
        self._tgt_ctr = ctr
        self._headline = headline
        self._subline = subline

        # Domain is ctr*(1-pad) .. ctr*(1+pad) — avoid zero/neg ranges
        dmin = max(1e-6, ctr * (1.0 - pad))
        dmax = max(dmin + 1e-6, ctr * (1.0 + pad))
        self._dom_min = dmin
        self._dom_max = dmax

        self.queue_draw()

    # ---- Drawing ----
    def _on_draw(self, area: Gtk.DrawingArea, ctx, w: int, h: int) -> None:
        cx, cy = w / 2.0, h * 0.70
        radius = min(w, h) * 0.44
        bar_w = max(10.0, radius * 0.14)

        # antialias
        ctx.set_antialias(1)
        ctx.set_line_cap(1)
        ctx.set_line_join(1)

        # theme colors
        style = area.get_style_context()
        fg = (
            style.lookup_color("theme_fg_color")[1]
            if style.lookup_color("theme_fg_color")[0]
            else Gdk.RGBA(1, 1, 1, 1)
        )
        dim = Gdk.RGBA(fg.red, fg.green, fg.blue, 0.22)
        grid = Gdk.RGBA(fg.red, fg.green, fg.blue, 0.14)

        # arc geometry
        start = math.radians(195)  # a bit past 180 for nicer base
        end = math.radians(345)

        # helpers
        def lerp(a, b, t):
            return a + (b - a) * t

        def clamp01(x):
            return max(0.0, min(1.0, x))

        def t_of(v: float) -> float:
            if self._dom_max <= self._dom_min:
                return 0.0
            return clamp01((v - self._dom_min) / (self._dom_max - self._dom_min))

        def ang_of_value(v: float) -> float:
            return lerp(start, end, t_of(v))

        # --- background arc
        ctx.set_line_width(bar_w)
        ctx.set_source_rgba(dim.red, dim.green, dim.blue, dim.alpha)
        ctx.arc(cx, cy, radius, start, end)
        ctx.stroke()

        # --- target band arc
        t0 = ang_of_value(self._tgt_lo)
        t1 = ang_of_value(self._tgt_hi)

        # band color (green-ish)
        band = (0.20, 0.80, 0.30, 0.95)
        ctx.set_source_rgba(*band)
        # Draw band with slight “glow”: two strokes, wide + narrow
        ctx.set_line_width(bar_w)
        ctx.arc(cx, cy, radius, t0, t1)
        ctx.stroke()
        ctx.set_line_width(bar_w * 0.45)
        ctx.arc(cx, cy, radius, t0, t1)
        ctx.stroke()

        # --- tick marks (50 / 100 / 150 % of target center)
        ctx.set_line_width(2.6)
        ctx.set_source_rgba(grid.red, grid.green, grid.blue, grid.alpha)
        for frac in (0.5, 1.0, 1.5):
            ang = ang_of_value(self._tgt_ctr * frac)
            x0 = cx + math.cos(ang) * (radius - bar_w * 0.7)
            y0 = cy + math.sin(ang) * (radius - bar_w * 0.7)
            x1 = cx + math.cos(ang) * (radius + bar_w * 0.15)
            y1 = cy + math.sin(ang) * (radius + bar_w * 0.15)
            ctx.move_to(x0, y0)
            ctx.line_to(x1, y1)
            ctx.stroke()

        # --- needle at current value
        ang_v = ang_of_value(self._value)
        # color by in/out of band
        in_band = self._tgt_lo <= self._value <= self._tgt_hi
        near_band = (
            self._value < self._tgt_lo and (self._tgt_lo - self._value) <= 0.1 * self._tgt_ctr
        ) or (self._value > self._tgt_hi and (self._value - self._tgt_hi) <= 0.1 * self._tgt_ctr)
        if in_band:
            needle_col = (0.20, 0.85, 0.30, 1.0)
        elif near_band:
            needle_col = (0.95, 0.75, 0.20, 1.0)
        else:
            needle_col = (0.95, 0.35, 0.35, 1.0)

        # needle “shadow” for readability
        ctx.set_line_width(6.0)
        ctx.set_source_rgba(0, 0, 0, 0.25)
        xn = cx + math.cos(ang_v) * (radius + bar_w * 0.05 + 1.0)
        yn = cy + math.sin(ang_v) * (radius + bar_w * 0.05 + 1.0)
        xm = cx + math.cos(ang_v) * (radius - bar_w * 0.75 + 1.0)
        ym = cy + math.sin(ang_v) * (radius - bar_w * 0.75 + 1.0)
        ctx.move_to(xm, ym)
        ctx.line_to(xn, yn)
        ctx.stroke()

        # actual needle
        ctx.set_line_width(4.6)
        ctx.set_source_rgba(*needle_col)
        xn = cx + math.cos(ang_v) * (radius + bar_w * 0.05)
        yn = cy + math.sin(ang_v) * (radius + bar_w * 0.05)
        xm = cx + math.cos(ang_v) * (radius - bar_w * 0.75)
        ym = cy + math.sin(ang_v) * (radius - bar_w * 0.75)
        ctx.move_to(xm, ym)
        ctx.line_to(xn, yn)
        ctx.stroke()

        # --- text
        # Headline (large)
        layout = area.create_pango_layout(self._headline)
        desc = layout.get_font_description() or area.get_pango_context().get_font_description()
        desc = desc.copy()
        desc.set_size(int(22 * Pango.SCALE))
        desc.set_weight(Pango.Weight.BOLD)
        layout.set_font_description(desc)
        wtxt, htxt = layout.get_pixel_size()
        ctx.set_source_rgba(fg.red, fg.green, fg.blue, 0.95)
        ctx.move_to(cx - wtxt / 2.0, cy - radius * 0.42 - htxt / 2.0)
        PangoCairo.show_layout(ctx, layout)

        # Subline
        layout2 = area.create_pango_layout(self._subline)
        d2 = layout2.get_font_description() or area.get_pango_context().get_font_description()
        d2 = d2.copy()
        d2.set_size(int(13 * Pango.SCALE))
        layout2.set_font_description(d2)
        w2, h2 = layout2.get_pixel_size()
        ctx.set_source_rgba(fg.red, fg.green, fg.blue, 0.8)
        ctx.move_to(cx - w2 / 2.0, cy - radius * 0.14 - h2 / 2.0)
        PangoCairo.show_layout(ctx, layout2)


# ---------------- small metric widgets ---------------- #
class _MetricPill(Gtk.Box):
    """
    Big, glanceable metric block with a value and label.
    Optionally shows a tiny connection dot on the right.
    """

    def __init__(self, label: str, unit: str = "", wide: bool = False) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.add_css_class("card")
        self.set_hexpand(True)
        if wide:
            self.set_halign(Gtk.Align.FILL)

        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        inner.set_margin_top(8)
        inner.set_margin_bottom(8)
        inner.set_margin_start(10)
        inner.set_margin_end(10)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.value = Gtk.Label()
        self.value.add_css_class("title-1")
        self.value.add_css_class("numeric")
        self.value.set_xalign(0.0)

        sub = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.label = Gtk.Label(label=label, xalign=0.0)
        self.label.add_css_class("dim-label")
        self.unit = Gtk.Label(label=unit, xalign=0.0)
        self.unit.add_css_class("dim-label")
        sub.append(self.label)
        if unit:
            sub.append(self.unit)

        vbox.append(self.value)
        vbox.append(sub)

        self.dot = Gtk.Label(label="⚫")
        self.dot.set_xalign(1.0)
        self.dot.set_opacity(0.55)

        inner.append(vbox)
        inner.append(Gtk.Box(hexpand=True))
        inner.append(self.dot)

        self.append(inner)
        self.set_hexpand(True)
        self.set_size_request(120, -1)

    def set_value(self, text: str) -> None:
        self.value.set_text(text)

    def set_unit(self, text: str) -> None:
        self.unit.set_text(text)

    def set_connected(self, ok: bool) -> None:
        self.dot.set_text("🟢" if ok else "⚫")
        self.value.set_opacity(1.0 if ok else 0.6)


class _TimerBig(Gtk.Box):
    """Large timers for step remaining & elapsed."""

    def __init__(self, title: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.set_hexpand(True)
        self.add_css_class("card")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(10)
        box.set_margin_end(10)

        self.caption = Gtk.Label(label=title, xalign=0.0)
        self.caption.add_css_class("dim-label")

        self.value = Gtk.Label(label="00:00", xalign=0.0)
        self.value.add_css_class("title-1")
        self.value.add_css_class("numeric")

        box.append(self.caption)
        box.append(self.value)
        self.append(box)

    def set_text(self, text: str) -> None:
        self.value.set_text(text or "00:00")


# ---------------- Workout View ---------------- #
class WorkoutView(Gtk.Box):
    """
    A focused workout helper page:
      - Title
      - Step timers (Remaining / Elapsed)
      - Gauge (current vs target band) + compliance pill
      - Target / Next labels
      - Thick progress bar for current step
      - Metric strip (HR, Cadence, Speed, Distance, pace, power)
      - Prev / Next / Start / Stop with large touch targets.
    """

    def __init__(self, *, title: str, on_prev, on_next, on_stop, on_start_record) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(self, f"set_margin_{m}")(12)

        self.type = "run"

        clamp = Adw.Clamp(maximum_size=820, tightening_threshold=680)
        self.append(clamp)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        clamp.set_child(content)

        # Header
        self.title_label = Gtk.Label(xalign=0.0)
        self.title_label.add_css_class("title-2")
        self.title_label.set_text(title)
        content.append(self.title_label)

        # Timers row (Remaining / Elapsed)
        timers = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.timer_elapsed = _TimerBig("Elapsed")
        self.timer_remaining = _TimerBig("Remaining")
        timers.append(self.timer_elapsed)
        timers.append(self.timer_remaining)
        content.append(timers)

        # Gauge + compliance pill
        self.gauge = TargetGauge()
        content.append(self.gauge)

        pill_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        pill_bar.set_halign(Gtk.Align.CENTER)
        self.compliance = Gtk.Label()
        self.compliance.add_css_class("pill")
        self.compliance.set_name("compliance-pill")
        pill_bar.append(self.compliance)
        content.append(pill_bar)

        # Target / Next rows
        self.lbl_target = Gtk.Label(xalign=0.0, wrap=True)
        self.lbl_target.add_css_class("heading")
        self.lbl_target.set_wrap_mode(Pango.WrapMode.WORD_CHAR)

        self.lbl_next = Gtk.Label(xalign=0.0, wrap=True)
        self.lbl_next.set_wrap_mode(Pango.WrapMode.WORD_CHAR)

        content.append(self.lbl_target)
        content.append(self.lbl_next)

        # Progress (thicker, with margins)
        self.step_progress = Gtk.ProgressBar()
        self.step_progress.set_hexpand(True)
        self.step_progress.set_margin_top(4)
        self.step_progress.set_margin_bottom(2)
        self.step_progress.set_css_classes(["osd"])  # higher contrast theme style
        content.append(self.step_progress)

        # Metric strip (glanceable)
        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_valign(Gtk.Align.FILL)
        flow.set_halign(Gtk.Align.FILL)
        flow.set_homogeneous(True)  # all cells same size
        flow.set_column_spacing(12)
        flow.set_row_spacing(12)
        # Optional: keep items short so more fit per row
        # (each pill already hexpands; that’s okay)
        self.card_hr = _MetricPill("Heart Rate", "bpm")
        self.card_pace = _MetricPill("Pace", "min/mi")
        self.card_pwr = _MetricPill("Power", "W")
        self.card_cad = _MetricPill("Cadence", "spm")
        self.card_spd = _MetricPill("Speed", "mph")
        self.card_dst = _MetricPill("Distance", "mi")
        # Gauge readout pill (value mirrors the gauge target — power or pace)
        self.card_pp = _MetricPill("Gauge", "")  # unit set dynamically

        for w in (
            self.card_hr,
            self.card_cad,
            self.card_spd,
            self.card_dst,
            self.card_pace,
            self.card_pwr,
        ):
            flow.insert(w, -1)

        content.append(flow)

        # Buttons
        self.btn_prev = Gtk.Button.new_with_label("◀︎ Prev")
        self.btn_prev.add_css_class("pill")
        self.btn_prev.set_size_request(90, -1)

        self.btn_next = Gtk.Button.new_with_label("Next ▶︎")
        self.btn_next.add_css_class("pill")
        self.btn_next.set_size_request(90, -1)

        self.btn_start = Gtk.Button.new_with_label("Start")
        self.btn_start.add_css_class("suggested-action")
        self.btn_start.add_css_class("pill")
        self.btn_start.set_size_request(120, 44)

        self.btn_stop = Gtk.Button.new_with_label("Stop")
        self.btn_stop.add_css_class("destructive-action")
        self.btn_stop.add_css_class("pill")
        self.btn_stop.set_size_request(120, 44)

        self.btn_prev.connect("clicked", lambda *_: on_prev())
        self.btn_next.connect("clicked", lambda *_: on_next())
        self.btn_start.connect("clicked", lambda *_: on_start_record())
        self.btn_stop.connect("clicked", lambda *_: on_stop())

        # --- Pair boxes so they wrap as units and keep equal inner widths ---
        nav_pair = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        nav_pair.set_homogeneous(True)  # Prev/Next same width
        nav_pair.set_hexpand(True)
        nav_pair.append(self.btn_prev)
        nav_pair.append(self.btn_next)

        act_pair = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        act_pair.set_homogeneous(True)  # Start/Stop same width
        act_pair.set_hexpand(True)
        act_pair.append(self.btn_stop)
        act_pair.append(self.btn_start)

        # --- FlowBox containing just the two pairs ---
        controls = Gtk.FlowBox()
        controls.set_selection_mode(Gtk.SelectionMode.NONE)
        controls.set_homogeneous(True)  # nav_pair and act_pair get equal cell widths
        controls.set_column_spacing(8)
        controls.set_row_spacing(8)
        controls.set_min_children_per_line(1)  # stacks on very narrow screens
        controls.set_max_children_per_line(2)  # side-by-side when there’s room

        controls.insert(nav_pair, -1)
        controls.insert(act_pair, -1)

        content.append(controls)

        self.set_recording(False)
        self._update_compliance_pill()  # initialize

    # --- External setters used by controller (ui_tracker.py) ---
    def set_title(self, title: str) -> None:
        self.title_label.set_text(title or "Workout")

    def set_target_text(self, text: str) -> None:
        self.lbl_target.set_text(text)
        self._update_compliance_pill()

    def set_next_text(self, text: str) -> None:
        self.lbl_next.set_text(text)

    def set_progress(self, frac: float) -> None:
        self.step_progress.set_fraction(max(0.0, min(1.0, float(frac))))

    def set_recording(self, recording: bool) -> None:
        self.btn_start.set_sensitive(not recording)
        self.btn_stop.set_sensitive(True)

    # Timers (optional helpers)
    def set_elapsed_text(self, text: str) -> None:
        self.timer_elapsed.set_text(text)

    def set_step_remaining_text(self, text: str) -> None:
        self.timer_remaining.set_text(text)

    # Metrics (optional helpers)
    def set_metrics(
        self,
        *,
        bpm: int | None = None,
        pace: str | None = None,
        cadence_spm: int | None = None,
        speed_mph: float | None = None,
        dist_mi: float | None = None,
        power_watts: float | None = None,
        is_power: bool | None = None,
    ) -> None:
        """Update metric strip. is_power=True switches center card label to 'Power'."""
        if bpm is not None:
            self.card_hr.set_value(str(int(bpm)))
        if pace is not None:
            self.card_pace.set_value(pace)
        if cadence_spm is not None:
            # Running is double the cadence
            out = int(cadence_spm * 2) if self.type == "run" else int(cadence_spm)
            self.card_cad.set_value(str(out))
        if speed_mph is not None:
            self.card_spd.set_value(f"{float(speed_mph):.1f}")
        if dist_mi is not None:
            self.card_dst.set_value(f"{float(dist_mi):.2f}")
        if power_watts is not None:
            self.card_pwr.set_value(str(int(power_watts)))

    def set_statuses(self, *, hr_ok: bool, cad_ok: bool, spd_ok: bool, pow_ok: bool) -> None:
        self.card_hr.set_connected(hr_ok)
        # pace/power card dot represents the active target domain:
        # if power is the workout driver, use pow_ok; else use spd_ok
        # (It’s fine if both toggle—dot is just a hint.)
        self.card_pace.set_connected(spd_ok)
        self.card_cad.set_connected(cad_ok)
        self.card_spd.set_connected(spd_ok)
        self.card_dst.set_connected(True)
        self.card_pwr.set_connected(pow_ok)

    # -------- Gauge helpers (power) --------
    def set_gauge_power(
        self,
        *,
        current_w: float | int,
        target_w: float | int,
        target_w_lo: float | int | None = None,
        target_w_hi: float | int | None = None,
    ) -> None:
        """
        Show a power band; if you don't have a range, pass only target_w and
        we'll synthesize a ±5% band for readability.
        """
        cur = float(current_w)
        tgt = float(target_w)
        if target_w_lo is None or target_w_hi is None:
            lo = tgt * 0.95
            hi = tgt * 1.05
        else:
            lo = float(min(target_w_lo, target_w_hi))
            hi = float(max(target_w_lo, target_w_hi))

        headline = f"{round(cur)} W"
        subline = f"Target: {round(lo)}–{round(hi)} W"
        self.gauge.set_state(
            kind="power",
            value=cur,
            units="W",
            target_lo=lo,
            target_hi=hi,
            headline=headline,
            subline=subline,
            domain_pad=0.5,  # ±50% around center
        )
        # Gauge pill mirrors the gauge; keep its label static as "Gauge"
        self.card_pp.set_unit("W")
        self.card_pp.set_value(f"{round(cur)}")
        self._update_compliance_pill()

    # -------- Gauge helpers (pace) --------
    def set_gauge_pace(
        self,
        *,
        current_mps: float,
        target_mps: float,
        current_pace_text: str,
        target_pace_text: str,
        target_mps_lo: float | None = None,
        target_mps_hi: float | None = None,
    ) -> None:
        """
        Show a pace band (internally speed m/s).
        If no range provided, uses ±3% to make the band visible.
        """
        cur_v = float(current_mps)
        tgt_v = float(target_mps)
        if target_mps_lo is None or target_mps_hi is None:
            lo_v = tgt_v * 0.97
            hi_v = tgt_v * 1.03
        else:
            lo_v = float(min(target_mps_lo, target_mps_hi))
            hi_v = float(max(target_mps_lo, target_mps_hi))

        headline = f"{current_pace_text} /mi"
        subline = f"Target: {target_pace_text} /mi"
        self.gauge.set_state(
            kind="pace",
            value=cur_v,
            units="min/mi",
            target_lo=lo_v,
            target_hi=hi_v,
            headline=headline,
            subline=subline,
            domain_pad=0.5,
        )
        # Gauge pill mirrors the gauge; label stays "Gauge"
        self.card_pp.set_unit("min/mi")
        # Show pace without the "/mi" suffix in the value line (unit already says min/mi)
        self.card_pp.set_value(current_pace_text)
        self._update_compliance_pill()

    # --- internal ---
    def _update_compliance_pill(self) -> None:
        status = self.gauge.band_status()
        for c in ("pill-in", "pill-near", "pill-low", "pill-high"):
            self.compliance.remove_css_class(c)

        if status == "in":
            self.compliance.add_css_class("pill-in")
            txt = "In Target"
        elif status == "near":
            self.compliance.add_css_class("pill-near")
            txt = "Close to Target"
        elif status == "low":
            self.compliance.add_css_class("pill-low")
            txt = "Below Target"
        else:
            self.compliance.add_css_class("pill-high")
            txt = "Above Target"

        self.compliance.set_text(txt)
