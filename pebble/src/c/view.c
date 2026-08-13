#include <pebble.h>
#include <math.h>
#include <stdio.h>

#include "format.h"
#include "protocol.h"
#include "view.h"

// Persist keys (separate from AppMessage keys)
enum { PKEY_UNITS = 100, PKEY_HERO = 101, PKEY_FOCUS = 102 };

// Which metric is the hero (top, big)
typedef enum { HERO_HR = 0, HERO_PACE = 1, HERO_POWER = 2 } HeroMetric;

// Display density
typedef enum { FOCUS_GRID = 0, FOCUS_HERO_ONLY = 1 } FocusMode;

// View mode: free run vs workout gauge
typedef enum { VIEW_FREE = 0, VIEW_WORKOUT = 1 } ViewMode;

// ---------- View state ----------
static Window *s_win;
static PebbleProtocolState s_protocol;
static HeroMetric s_hero = HERO_HR;
static FocusMode s_focus = FOCUS_GRID;
static ViewMode s_view = VIEW_FREE;

static int clamp_int(int value, int minimum, int maximum) {
  if (value < minimum) return minimum;
  if (value > maximum) return maximum;
  return value;
}

// ---------- UI: hero + grid ----------
static TextLayer *s_hero_value;
static TextLayer *s_hero_label;

typedef struct {
  TextLayer *label;
  TextLayer *value;
  bool *have_flag;
  int id;
} MetricCellID;

enum { CELL_HR=0, CELL_PACE=1, CELL_CAD=2, CELL_DIST=3, CELL_PWR=4 };

static TextLayer *s_hr_label_grid,  *s_hr_value_grid;
static TextLayer *s_pace_label,     *s_pace_value;
static TextLayer *s_cad_label,      *s_cad_value;
static TextLayer *s_dist_label,     *s_dist_value;
static TextLayer *s_power_label,    *s_power_value;

static MetricCellID s_cells[5];

// ---------- UI: workout gauge ----------
static Layer     *s_gauge_layer;
static TextLayer *s_info_current;
static TextLayer *s_info_target;
static TextLayer *s_info_hr;
static TextLayer *s_info_big;
static Layer     *s_underbar_layer;

static char s_buf_current[32];
static char s_buf_target[48];
static char s_buf_hr[24];
static bool s_in_zone_prev = false;

// ---------- Forward declarations ----------
static void render_all(void);
static void layout_layers(Window *w);
static void maybe_haptic_transition(void);
static void view_protocol_updated(void *context);

// ---------- Formatting adapters ----------
static void view_format_distance(char *out, size_t n, KEY_DISTANCE_C_TYPE meters) {
  pebble_format_distance(out, n, meters, s_protocol.units);
}

static void view_format_pace(char *out, size_t n, KEY_PACE_C_TYPE speed_ms_x100) {
  pebble_format_pace(out, n, speed_ms_x100, s_protocol.units);
}

static void view_format_pace_value_only(char *out, size_t n) {
  pebble_format_pace_value_only(out, n, &s_protocol);
}

static float view_current_value_for_kind(void) {
  return pebble_current_value_for_kind(&s_protocol);
}

static float view_target_value(KEY_TGT_LO_C_TYPE value) {
  return pebble_target_value(&s_protocol, value);
}

static void view_gauge_texts(char *current_line, size_t current_n,
                             char *target_line, size_t target_n,
                             char *hr_line, size_t hr_n) {
  pebble_gauge_texts(current_line, current_n, target_line, target_n,
                     hr_line, hr_n, &s_protocol);
}

static GColor view_zone_color(void) {
  return pebble_zone_color(&s_protocol);
}

static const char *view_zone_word(GColor color) {
  return pebble_zone_word(&s_protocol, color);
}

static void view_protocol_updated(void *context) {
  (void)context;
  if (s_protocol.units_changed) {
    persist_write_int(PKEY_UNITS, (int)s_protocol.units);
    s_protocol.units_changed = false;
  }
  if (s_protocol.step_changed) {
    vibes_short_pulse();
    s_protocol.step_changed = false;
  }
  s_view = (s_protocol.target_kind == TGT_NONE) ? VIEW_FREE : VIEW_WORKOUT;
  render_all();
}

// ---------- Font helpers ----------
static GFont pick_font_label(int h, bool is_hero) {
  if (is_hero) {
    if (h >= 22) return fonts_get_system_font(FONT_KEY_GOTHIC_24);
    if (h >= 18) return fonts_get_system_font(FONT_KEY_GOTHIC_18);
    return fonts_get_system_font(FONT_KEY_GOTHIC_14);
  } else {
    // Grid labels
    if (h >= 18) return fonts_get_system_font(FONT_KEY_GOTHIC_18);
    return fonts_get_system_font(FONT_KEY_GOTHIC_14);
  }
}

static GFont pick_font_value(int h, bool is_hero, bool in_focus) {
  if (is_hero) {
    // Hero value uses big numeric fonts; push harder in focus
    if (in_focus) {
      if (h >= 38) return fonts_get_system_font(FONT_KEY_BITHAM_42_BOLD);
      return fonts_get_system_font(FONT_KEY_BITHAM_34_MEDIUM_NUMBERS);
    } else {
      if (h >= 56) return fonts_get_system_font(FONT_KEY_BITHAM_42_BOLD);
      return fonts_get_system_font(FONT_KEY_BITHAM_34_MEDIUM_NUMBERS);
    }
  } else {
    // Grid values
    if (h >= 34) return fonts_get_system_font(FONT_KEY_GOTHIC_28_BOLD);
    if (h >= 26) return fonts_get_system_font(FONT_KEY_GOTHIC_24_BOLD);
    if (h >= 20) return fonts_get_system_font(FONT_KEY_GOTHIC_18_BOLD);
    return fonts_get_system_font(FONT_KEY_GOTHIC_14_BOLD);
  }
}


// ---------- Workout gauge helpers ----------

// Convert [0..1] to trig angle between [start..end]
// Pebble angles: 0=12 o'clock, 90=3 o'clock, 180=6 o'clock, 270=9 o'clock (clockwise positive)
static int32_t angle_of_frac(int32_t start, int32_t end, float t) {
  if (t < 0) t = 0;
  if (t > 1) t = 1;
  return start + (int32_t)((end - start) * t);
}

static inline int32_t trig_from_clock(int32_t clock_ang) {
  // convert Pebble "clock" angle (0°=12 o'clock) to trig (0°=3 o'clock)
  int32_t a = clock_ang - TRIG_MAX_ANGLE * 90 / 360;
  if (a < 0) a += TRIG_MAX_ANGLE;
  return a;
}

// Current numeric value in the target domain

static void gauge_update_proc(Layer *layer, GContext *ctx) {
  if (s_protocol.target_kind == TGT_NONE) return;

  GRect b = layer_get_bounds(layer);
  // Center and size: make it larger & centered
  const int16_t cx = b.origin.x + b.size.w/2;
  const int16_t cy = b.origin.y + (b.size.h*3)/5;   // slightly above center so text fits below
  const int16_t radius = (b.size.w < b.size.h ? b.size.w : b.size.h) * 48 / 100; // bigger
  const int16_t bar = radius * 18 / 100;  // thicker

  // Lower semi-circle from 180° to 360° to avoid “sideways” look
  const int32_t A0 = TRIG_MAX_ANGLE * 270 / 360;  // 270° (9 o'clock)
  const int32_t A1 = TRIG_MAX_ANGLE * 450 / 360;  // 450° (wraps to 90°, 3 o'clock)

#ifndef PBL_COLOR
  // On B/W, simplify: background arc only lightly, needle in white/black
  graphics_context_set_fill_color(ctx, GColorDarkGray);
  graphics_fill_radial(ctx, GRect(cx - radius, cy - radius, 2*radius, 2*radius),
                       GOvalScaleModeFitCircle, bar, A0, A1);
#else
  // Background arc (dim)
  graphics_context_set_fill_color(ctx, GColorDarkGray);
  graphics_fill_radial(ctx, GRect(cx - radius, cy - radius, 2*radius, 2*radius),
                       GOvalScaleModeFitCircle, bar, A0, A1);
#endif

  // Domain mapping around target center ±50%
  float lo = view_target_value(s_protocol.target_lo);
  float hi = view_target_value(s_protocol.target_hi);
  if (hi < lo) { float tmp = lo; lo = hi; hi = tmp; }
  float ctr = 0.5f * (lo + hi);
  float dmin = ctr * 0.5f;
  float dmax = ctr * 1.5f;
  if (dmax <= dmin) { dmax = dmin + 1.0f; }

#ifdef PBL_COLOR
  // Target band arc (green)
  float t0 = (lo - dmin) / (dmax - dmin);
  float t1 = (hi - dmin) / (dmax - dmin);
  if (t0 < 0) t0 = 0;
  if (t0 > 1) t0 = 1;
  if (t1 < 0) t1 = 0;
  if (t1 > 1) t1 = 1;
  int32_t ang0 = angle_of_frac(A0, A1, t0);
  int32_t ang1 = angle_of_frac(A0, A1, t1);
  graphics_context_set_fill_color(ctx, GColorIslamicGreen);
  graphics_fill_radial(ctx, GRect(cx - radius, cy - radius, 2*radius, 2*radius),
                       GOvalScaleModeFitCircle, bar, ang0, ang1);
#endif

  // Tick at the midpoint of the target band
  graphics_context_set_stroke_color(ctx, GColorLightGray);
  graphics_context_set_stroke_width(ctx, 2);
  int32_t ang_ctr_clock = angle_of_frac(A0, A1, 0.5f * ((lo - dmin) / (dmax - dmin) + (hi - dmin) / (dmax - dmin)));
  int32_t ang_ctr = trig_from_clock(ang_ctr_clock);

  int16_t tx0 = cx + (int16_t)(cos_lookup(ang_ctr) * (radius - bar*3/4) / TRIG_MAX_RATIO);
  int16_t ty0 = cy + (int16_t)(sin_lookup(ang_ctr) * (radius - bar*3/4) / TRIG_MAX_RATIO);
  int16_t tx1 = cx + (int16_t)(cos_lookup(ang_ctr) * (radius + bar/6)   / TRIG_MAX_RATIO);
  int16_t ty1 = cy + (int16_t)(sin_lookup(ang_ctr) * (radius + bar/6)   / TRIG_MAX_RATIO);
  graphics_draw_line(ctx, GPoint(tx0,ty0), GPoint(tx1,ty1));

  // Needle
  float cur = view_current_value_for_kind();
  float tv = (cur - dmin) / (dmax - dmin);
  if (tv < 0) tv = 0;
  if (tv > 1) tv = 1;
  int32_t ang = trig_from_clock(angle_of_frac(A0, A1, tv));

  // Needle color
  GColor col = view_zone_color();

  // Shadow
  graphics_context_set_stroke_color(ctx, GColorBlack);
  graphics_context_set_stroke_width(ctx, 6);
  int16_t x0s = cx + (int16_t)(cos_lookup(ang) * (radius - bar*3/4) / TRIG_MAX_RATIO);
  int16_t y0s = cy + (int16_t)(sin_lookup(ang) * (radius - bar*3/4) / TRIG_MAX_RATIO);
  int16_t x1s = cx + (int16_t)(cos_lookup(ang) * (radius + bar/8) / TRIG_MAX_RATIO);
  int16_t y1s = cy + (int16_t)(sin_lookup(ang) * (radius + bar/8) / TRIG_MAX_RATIO);
  graphics_draw_line(ctx, GPoint(x0s,y0s), GPoint(x1s,y1s));

  // Foreground needle
  graphics_context_set_stroke_color(ctx, col);
  graphics_context_set_stroke_width(ctx, 4);
  int16_t x0 = cx + (int16_t)(cos_lookup(ang) * (radius - bar*3/4) / TRIG_MAX_RATIO);
  int16_t y0 = cy + (int16_t)(sin_lookup(ang) * (radius - bar*3/4) / TRIG_MAX_RATIO);
  int16_t x1 = cx + (int16_t)(cos_lookup(ang) * (radius + bar/8) / TRIG_MAX_RATIO);
  int16_t y1 = cy + (int16_t)(sin_lookup(ang) * (radius + bar/8) / TRIG_MAX_RATIO);
  graphics_draw_line(ctx, GPoint(x0,y0), GPoint(x1,y1));

  // Hub
  graphics_context_set_fill_color(ctx, GColorWhite);
  graphics_fill_circle(ctx, GPoint(cx, cy), 5);
}


static void maybe_haptic_transition(void) {
  if (s_protocol.target_kind == TGT_NONE || !s_protocol.workout_outdoor) return;
  float lo = view_target_value(s_protocol.target_lo);
  float hi = view_target_value(s_protocol.target_hi);
  if (hi < lo) { float t=lo; lo=hi; hi=t; }

  float cur = view_current_value_for_kind();
  bool in_zone_now = (cur >= lo && cur <= hi);

  if (in_zone_now != s_in_zone_prev) {
    if (in_zone_now) vibes_short_pulse(); else vibes_double_pulse();
    s_in_zone_prev = in_zone_now;
  }
}


// ---------- Layout ----------
static void layout_layers(Window *w) {
  Layer *root = window_get_root_layer(w);
  GRect b = layer_get_unobstructed_bounds(root);
  const int W = b.size.w;
  const int H = b.size.h;

#if PBL_ROUND
  int pad_top = 8;
  int pad_lr  = 10;
#else
  int pad_top = 4;
  int pad_lr  = 6;
#endif
  int pad_mid = (s_focus == FOCUS_GRID) ? 4 : 6;  // tighter spacing in stacked view
  const int pad_bot = 4;

  // --- Workout view layout (gauge + big value + lines + underbar) ---
  if (s_view == VIEW_WORKOUT) {
    // Gauge occupies ~60% height for more presence
    int gh = (H * 60) / 100;
    layer_set_frame(s_gauge_layer, GRect(b.origin.x, b.origin.y + 2, W, gh));

    // Big value sits just below the gauge
    int big_h = 44;
    int big_y = b.origin.y + gh - big_h - 4;
    layer_set_frame(text_layer_get_layer(s_info_big),
                    GRect(b.origin.x + 4, big_y, W - 8, big_h));
    text_layer_set_font(s_info_big, fonts_get_system_font(FONT_KEY_BITHAM_42_BOLD));
    text_layer_set_text_alignment(s_info_big, GTextAlignmentCenter);
    layer_set_hidden(text_layer_get_layer(s_info_big), false);

    // Underbar just under the big value
    int bar_h = 2;
    int bar_y = big_y + big_h + 0;
    layer_set_frame(s_underbar_layer, GRect(b.origin.x + 12, bar_y, W - 24, bar_h));
    layer_set_hidden(s_underbar_layer, false);

    // Three small lines
    int line_h = 18;
    int y = bar_y + bar_h + 2;

    layer_set_frame(text_layer_get_layer(s_info_current),
                    GRect(b.origin.x + 4, y, W - 8, line_h));
    text_layer_set_text_alignment(s_info_current, GTextAlignmentCenter);
    text_layer_set_font(s_info_current, fonts_get_system_font(FONT_KEY_GOTHIC_18_BOLD));
    y += line_h;

    layer_set_frame(text_layer_get_layer(s_info_target),
                    GRect(b.origin.x + 4, y, W - 8, line_h));
    text_layer_set_text_alignment(s_info_target, GTextAlignmentCenter);
    text_layer_set_font(s_info_target, fonts_get_system_font(FONT_KEY_GOTHIC_18));
    y += line_h;

    layer_set_frame(text_layer_get_layer(s_info_hr),
                    GRect(b.origin.x + 4, y, W - 8, line_h));
    text_layer_set_text_alignment(s_info_hr, GTextAlignmentCenter);
    text_layer_set_font(s_info_hr, fonts_get_system_font(FONT_KEY_GOTHIC_18));

    // Hide free-run UI
    for (int i = 0; i < 5; ++i) {
      layer_set_hidden(text_layer_get_layer(s_cells[i].label), true);
      layer_set_hidden(text_layer_get_layer(s_cells[i].value), true);
    }
    layer_set_hidden(text_layer_get_layer(s_hero_label), true);
    layer_set_hidden(text_layer_get_layer(s_hero_value), true);

    // Show gauge + info
    layer_set_hidden(s_gauge_layer, false);
    layer_set_hidden(text_layer_get_layer(s_info_current), false);
    layer_set_hidden(text_layer_get_layer(s_info_target), false);
    layer_set_hidden(text_layer_get_layer(s_info_hr), false);
    return;
  }

  // --- Free run layout (hero + grid) ---
  int hero_h = (s_focus == FOCUS_HERO_ONLY) ? (H - pad_top - pad_bot) : (H * 42) / 100;
  if (hero_h < 52) hero_h = 52;

  // On Focus, give the digits more horizontal room
  if (s_focus == FOCUS_HERO_ONLY) {
    pad_lr = (W >= 180) ? 6 : 4; // tighter side padding for large digits
  }

  // ---- Hero area ----
  GRect hero = GRect(b.origin.x + pad_lr, b.origin.y + pad_top, W - 2*pad_lr, hero_h);

  int label_h = 18;

  // Let value take the rest; add small gap
  int value_h = hero_h - label_h - 4;
  if (value_h < 24) value_h = 24;

  // Position frames
  layer_set_frame(text_layer_get_layer(s_hero_label),
                  GRect(hero.origin.x, hero.origin.y, hero.size.w, label_h));
  text_layer_set_text_alignment(s_hero_label, GTextAlignmentCenter);

  text_layer_set_font(
    s_hero_label,
    pick_font_label(label_h, /*is_hero=*/true)
  );

  text_layer_set_font(
    s_hero_value,
    pick_font_value(value_h, /*is_hero=*/true, /*in_focus=*/(s_focus == FOCUS_HERO_ONLY))
  );

  layer_set_frame(text_layer_get_layer(s_hero_value),
                  GRect(hero.origin.x, hero.origin.y + label_h + 2, hero.size.w, value_h));
  text_layer_set_text_alignment(s_hero_value, GTextAlignmentCenter);

  // Make sure hero layers are visible in free-run layouts
  layer_set_hidden(text_layer_get_layer(s_hero_label), false);
  layer_set_hidden(text_layer_get_layer(s_hero_value), false);

  // Focus: HERO_ONLY => hide grid and workout bits
  if (s_focus == FOCUS_HERO_ONLY) {
    for (int i = 0; i < 5; ++i) {
      layer_set_hidden(text_layer_get_layer(s_cells[i].label), true);
      layer_set_hidden(text_layer_get_layer(s_cells[i].value), true);
    }
    // Hide workout bits
    layer_set_hidden(s_gauge_layer, true);
    layer_set_hidden(text_layer_get_layer(s_info_current), true);
    layer_set_hidden(text_layer_get_layer(s_info_target), true);
    layer_set_hidden(text_layer_get_layer(s_info_hr), true);
    layer_set_hidden(text_layer_get_layer(s_info_big), true);
    layer_set_hidden(s_underbar_layer, true);

    // Ensure hero is shown in HERO_ONLY
    layer_set_hidden(text_layer_get_layer(s_hero_label), false);
    layer_set_hidden(text_layer_get_layer(s_hero_value), false);
    return;
  }

  // ---- Build active grid list (exclude current hero) ----
  MetricCellID *active[5] = {0};
  int n = 0;

  for (int i = 0; i < 5; ++i) {
    bool is_hero_cell =
      (s_hero == HERO_HR    && s_cells[i].id == CELL_HR) ||
      (s_hero == HERO_PACE  && s_cells[i].id == CELL_PACE) ||
      (s_hero == HERO_POWER && s_cells[i].id == CELL_PWR);

    if (is_hero_cell) {
      // Hide the hero’s grid twin
      layer_set_hidden(text_layer_get_layer(s_cells[i].label), true);
      layer_set_hidden(text_layer_get_layer(s_cells[i].value), true);
      continue;
    }

    if (*(s_cells[i].have_flag)) {
      active[n++] = &s_cells[i];
    }
  }

  // If nothing yet, add placeholders that are not the hero
  if (n == 0) {
    int candidates[3] = { CELL_PACE, CELL_DIST, CELL_CAD };
    for (int k = 0; k < 3 && n < 2; ++k) {
      int id = candidates[k];
      bool is_hero =
        (s_hero == HERO_HR    && id == CELL_HR) ||
        (s_hero == HERO_PACE  && id == CELL_PACE) ||
        (s_hero == HERO_POWER && id == CELL_PWR);
      if (!is_hero) {
        active[n++] = &s_cells[id];
      }
    }
  }

  // Grid geometry
  int gap_hg   = 1;
  int grid_top = hero.origin.y + hero.size.h + gap_hg;
  int grid_h   = H - (grid_top + pad_bot);
  if (grid_h < 24) grid_h = 24;

  int cols = 2;
  int rows = (n + cols - 1) / cols;

  int cell_w = (W - 2*pad_lr - (cols - 1)*pad_mid) / cols;
  int cell_h = (grid_h - (rows - 1)*pad_mid) / rows;
  if (cell_h < 26) cell_h = 26;

  int cell_label_h = 16;
  int cell_value_h = cell_h - cell_label_h - 2;

  // Hide all non-hero grid cells first, then unhide the active ones.
  for (int i = 0; i < 5; ++i) {
    if ( (s_hero == HERO_HR    && s_cells[i].id == CELL_HR) ||
         (s_hero == HERO_PACE  && s_cells[i].id == CELL_PACE) ||
         (s_hero == HERO_POWER && s_cells[i].id == CELL_PWR) ) {
      continue; // hero’s grid twin already hidden above
    }
    layer_set_hidden(text_layer_get_layer(s_cells[i].label), true);
    layer_set_hidden(text_layer_get_layer(s_cells[i].value), true);
  }

  for (int i = 0; i < n; ++i) {
    int r = i / cols;
    int c = i % cols;
    int x = b.origin.x + pad_lr + c * (cell_w + pad_mid);
    int y = grid_top + r * (cell_h + pad_mid);

    layer_set_frame(text_layer_get_layer(active[i]->label),
                GRect(x, y, cell_w, cell_label_h));
    text_layer_set_font(active[i]->label,
      pick_font_label(cell_label_h, /*is_hero=*/false));
    text_layer_set_text_alignment(active[i]->label, GTextAlignmentCenter);
    layer_set_hidden(text_layer_get_layer(active[i]->label), false);

    layer_set_frame(text_layer_get_layer(active[i]->value),
                GRect(x, y + cell_label_h + 2, cell_w, cell_value_h));
    text_layer_set_font(active[i]->value,
      pick_font_value(cell_value_h, /*is_hero=*/false, /*in_focus=*/false));
    text_layer_set_text_alignment(active[i]->value, GTextAlignmentCenter);
    layer_set_hidden(text_layer_get_layer(active[i]->value), false);
  }

  // hide workout bits in free view
  layer_set_hidden(s_gauge_layer, true);
  layer_set_hidden(text_layer_get_layer(s_info_current), true);
  layer_set_hidden(text_layer_get_layer(s_info_target), true);
  layer_set_hidden(text_layer_get_layer(s_info_hr), true);
  layer_set_hidden(text_layer_get_layer(s_info_big), true);
  layer_set_hidden(s_underbar_layer, true);
}

#if PBL_API_EXISTS(unobstructed_area_service_subscribe)
static void unobstructed_change(AnimationProgress progress, void *context) {
  (void)progress;
  layout_layers((Window *)context);
}
#endif

// ---------- Rendering ----------
static void render_all(void) {
  // If a target is active, always render the workout view
  if (s_protocol.target_kind != TGT_NONE && s_view != VIEW_WORKOUT) {
    s_view = VIEW_WORKOUT;
  }

  if (s_view == VIEW_WORKOUT) {
    // Fill persistent info buffers
    view_gauge_texts(s_buf_current, sizeof(s_buf_current),
                s_buf_target,  sizeof(s_buf_target),
                s_buf_hr,      sizeof(s_buf_hr));

    // Big value (numeric only)
    static char s_big[12];
    if (s_protocol.target_kind == TGT_POWER) {
      if (s_protocol.have_power) snprintf(s_big, sizeof(s_big), "%u", (unsigned)s_protocol.last_power);
      else snprintf(s_big, sizeof(s_big), "—");
    } else if (s_protocol.target_kind == TGT_PACE) {
      view_format_pace_value_only(s_big, sizeof(s_big)); // m:ss
    } else if (s_protocol.target_kind == TGT_HEART_RATE) {
      if (s_protocol.have_hr) snprintf(s_big, sizeof(s_big), "%u", (unsigned)s_protocol.last_hr);
      else snprintf(s_big, sizeof(s_big), "—");
    } else {
      snprintf(s_big, sizeof(s_big), "—");
    }
    text_layer_set_text(s_info_big, s_big);

    // Colorize by zone
    GColor zc = view_zone_color();
#ifdef PBL_COLOR
    text_layer_set_text_color(s_info_big, zc);
    text_layer_set_text_color(s_info_current, zc);
    text_layer_set_text_color(s_info_target, GColorWhite);
    text_layer_set_text_color(s_info_hr,     GColorWhite);
#endif

    // Short status word
    text_layer_set_text(s_info_current, view_zone_word(zc));

    // Target / HR lines
    text_layer_set_text(s_info_target, s_buf_target);
    text_layer_set_text(s_info_hr,     s_buf_hr);

    if (s_win) {
      layout_layers(s_win);
      layer_mark_dirty(s_gauge_layer);
      layer_mark_dirty(s_underbar_layer);
    }

    // Haptic only when crossing the band
    maybe_haptic_transition();

    return;
  }

  // ----- Free-run rendering -----
  static char hr_buf[20], pace_buf[16], cad_buf[16], dist_buf[20], pwr_buf[16];

  if (s_protocol.have_hr)      snprintf(hr_buf, sizeof(hr_buf), "%u", (unsigned)s_protocol.last_hr);
  else                snprintf(hr_buf, sizeof(hr_buf), "-");

  if (s_protocol.have_pace)    view_format_pace(pace_buf, sizeof(pace_buf), s_protocol.last_pace_x100);
  else                snprintf(pace_buf, sizeof(pace_buf), "-");

  if (s_protocol.have_cad)     snprintf(cad_buf, sizeof(cad_buf), "%u spm", (unsigned)s_protocol.last_cad);
  else                snprintf(cad_buf, sizeof(cad_buf), "-");

  if (s_protocol.have_dist)    view_format_distance(dist_buf, sizeof(dist_buf), s_protocol.last_dist_m);
  else                snprintf(dist_buf, sizeof(dist_buf), "-");

  if (s_protocol.have_power)   snprintf(pwr_buf, sizeof(pwr_buf), "%u", (unsigned)s_protocol.last_power);
  else                snprintf(pwr_buf, sizeof(pwr_buf), "-");

  // Hero content
  switch (s_hero) {
    case HERO_HR: {
      static char hero_val[20];
      if (s_protocol.have_hr) snprintf(hero_val, sizeof(hero_val), "%u", (unsigned)s_protocol.last_hr);
      else           snprintf(hero_val, sizeof(hero_val), "-");
      text_layer_set_text(s_hero_label, "HEART RATE");
      text_layer_set_text(s_hero_value, hero_val);
      int vh = layer_get_bounds(text_layer_get_layer(s_hero_value)).size.h;
      text_layer_set_font(s_hero_value,
        pick_font_value(vh, /*is_hero=*/true, /*in_focus=*/(s_focus == FOCUS_HERO_ONLY)));
      break;
    }
    case HERO_POWER: {
      static char hero_val[12];
      if (s_protocol.have_power) snprintf(hero_val, sizeof(hero_val), "%u", (unsigned)s_protocol.last_power);
      else              snprintf(hero_val, sizeof(hero_val), "-");
      text_layer_set_text(s_hero_label, "POWER");
      text_layer_set_text(s_hero_value, hero_val);
      int vh = layer_get_bounds(text_layer_get_layer(s_hero_value)).size.h;
      text_layer_set_font(s_hero_value,
        pick_font_value(vh, /*is_hero=*/true, /*in_focus=*/(s_focus == FOCUS_HERO_ONLY)));
      break;
    }
    case HERO_PACE: {
      // Big m:ss only; unit in the label
      static char pace_val[12];
      view_format_pace_value_only(pace_val, sizeof(pace_val)); // m:ss only
      text_layer_set_text(s_hero_label, (s_protocol.units == PEBBLE_UNITS_METRIC) ? "PACE / KM" : "PACE / MI");
      text_layer_set_text(s_hero_value, pace_val);
      int vh = layer_get_bounds(text_layer_get_layer(s_hero_value)).size.h;
      text_layer_set_font(s_hero_value,
        pick_font_value(vh, /*is_hero=*/true, /*in_focus=*/(s_focus == FOCUS_HERO_ONLY)));
      break;
    }
  }

  // Grid labels/values (stacked view)
  if (s_focus == FOCUS_GRID) {
    text_layer_set_text(s_hr_label_grid, "HR");
    text_layer_set_text(s_hr_value_grid, hr_buf);

    // Pace grid: match hero style (value m:ss, unit in label)
    static char pace_val_grid[12];
    view_format_pace_value_only(pace_val_grid, sizeof(pace_val_grid));
    text_layer_set_text(s_pace_label, (s_protocol.units == PEBBLE_UNITS_METRIC) ? "PACE / KM" : "PACE / MI");
    text_layer_set_text(s_pace_value, pace_val_grid);

    text_layer_set_text(s_cad_label, "CAD");
    text_layer_set_text(s_cad_value, cad_buf);

    text_layer_set_text(s_dist_label, "DIST");
    text_layer_set_text(s_dist_value, dist_buf);

    text_layer_set_text(s_power_label, "PWR");
    text_layer_set_text(s_power_value, pwr_buf);
  }

  if (s_win) layout_layers(s_win);
}

// ---------- Underbar ----------
static void underbar_update_proc(Layer *layer, GContext *ctx) {
  if (s_protocol.target_kind == TGT_NONE) return;
  GRect r = layer_get_bounds(layer);
  float lo = view_target_value(s_protocol.target_lo);
  float hi = view_target_value(s_protocol.target_hi);
  if (hi < lo) { float t=lo; lo=hi; hi=t; }

  float ctr = 0.5f*(lo+hi);
  float dmin = ctr*0.5f, dmax = ctr*1.5f;
  if (dmax <= dmin) return;

  float cur = view_current_value_for_kind();
  float t = (cur - dmin) / (dmax - dmin);
  if (t < 0) t = 0;
  if (t > 1) t = 1;

#ifdef PBL_COLOR
  graphics_context_set_fill_color(ctx, view_zone_color());
#else
  graphics_context_set_fill_color(ctx, GColorWhite);
#endif
  int w = (int)(r.size.w * t + 0.5f);
  graphics_fill_rect(ctx, GRect(r.origin.x, r.origin.y, w, r.size.h), 0, GCornerNone);
}


// ---------- Buttons ----------
static void toggle_units(void) {
  s_protocol.units = (s_protocol.units == PEBBLE_UNITS_METRIC) ? PEBBLE_UNITS_IMPERIAL : PEBBLE_UNITS_METRIC;
  persist_write_int(PKEY_UNITS, (int)s_protocol.units);
  vibes_short_pulse();
  render_all();
}

static void next_hero(void) {
  s_hero = (HeroMetric)((s_hero + 1) % 3);
  persist_write_int(PKEY_HERO, (int)s_hero);
  vibes_short_pulse();
  render_all();
}

static void prev_hero(void) {
  s_hero = (HeroMetric)((s_hero + 2) % 3); // wrap backwards
  persist_write_int(PKEY_HERO, (int)s_hero);
  vibes_short_pulse();
  render_all();
}

static void up_click_handler(ClickRecognizerRef _, void *ctx)     { (void)_; (void)ctx; next_hero(); }
static void down_click_handler(ClickRecognizerRef _, void *ctx)   { (void)_; (void)ctx; prev_hero(); }
static void select_click_handler(ClickRecognizerRef _, void *ctx) { (void)_; (void)ctx; toggle_units(); }

// Long-press SELECT toggles focus mode (Grid <-> Hero-only) — only meaningful in Free Run
static void select_long_click_handler(ClickRecognizerRef _, void *ctx) {
  (void)_; (void)ctx;
  s_focus = (s_focus == FOCUS_GRID) ? FOCUS_HERO_ONLY : FOCUS_GRID;
  persist_write_int(PKEY_FOCUS, (int)s_focus);
  vibes_double_pulse();
  render_all();
}

static void click_config_provider(void *ctx) {
  (void)ctx;
  window_single_click_subscribe(BUTTON_ID_UP,     up_click_handler);
  window_single_click_subscribe(BUTTON_ID_DOWN,   down_click_handler);
  window_single_click_subscribe(BUTTON_ID_SELECT, select_click_handler);
  window_long_click_subscribe(BUTTON_ID_SELECT, 500 /*ms*/, select_long_click_handler, NULL);
}

// ---------- Window lifecycle ----------
static void make_label(TextLayer **out) {
  *out = text_layer_create(GRect(0,0,10,10));
  text_layer_set_text(*out, "");
  text_layer_set_text_color(*out, GColorWhite);
  text_layer_set_background_color(*out, GColorClear);
}

static void make_label_and_value(TextLayer **out_label, TextLayer **out_value) {
  *out_label = text_layer_create(GRect(0,0,10,10));
  *out_value = text_layer_create(GRect(0,0,10,10));
  TextLayer *ls[2] = { *out_label, *out_value };
  for (int i = 0; i < 2; ++i) {
    text_layer_set_text(ls[i], "");
    text_layer_set_text_color(ls[i], GColorWhite);
    text_layer_set_background_color(ls[i], GColorClear);
  }
}

static void win_load(Window *w) {
  window_set_background_color(w, GColorBlack);
  Layer *root = window_get_root_layer(w);

  // Hero
  make_label_and_value(&s_hero_label, &s_hero_value);
  layer_add_child(root, text_layer_get_layer(s_hero_label));
  layer_add_child(root, text_layer_get_layer(s_hero_value));

  // Grid cells
  make_label_and_value(&s_hr_label_grid, &s_hr_value_grid);
  make_label_and_value(&s_pace_label,    &s_pace_value);
  make_label_and_value(&s_cad_label,     &s_cad_value);
  make_label_and_value(&s_dist_label,    &s_dist_value);
  make_label_and_value(&s_power_label,   &s_power_value);

  s_cells[0] = (MetricCellID){ .label=s_hr_label_grid,  .value=s_hr_value_grid,  .have_flag=&s_protocol.have_hr,   .id=CELL_HR   };
  s_cells[1] = (MetricCellID){ .label=s_pace_label,     .value=s_pace_value,     .have_flag=&s_protocol.have_pace, .id=CELL_PACE };
  s_cells[2] = (MetricCellID){ .label=s_cad_label,      .value=s_cad_value,      .have_flag=&s_protocol.have_cad,  .id=CELL_CAD  };
  s_cells[3] = (MetricCellID){ .label=s_dist_label,     .value=s_dist_value,     .have_flag=&s_protocol.have_dist, .id=CELL_DIST };
  s_cells[4] = (MetricCellID){ .label=s_power_label,    .value=s_power_value,    .have_flag=&s_protocol.have_power,.id=CELL_PWR  };

  TextLayer *all_grid[] = {
    s_hr_label_grid, s_hr_value_grid,
    s_pace_label,    s_pace_value,
    s_cad_label,     s_cad_value,
    s_dist_label,    s_dist_value,
    s_power_label,   s_power_value
  };
  for (unsigned i = 0; i < sizeof(all_grid)/sizeof(all_grid[0]); ++i) {
    layer_add_child(root, text_layer_get_layer(all_grid[i]));
  }

  // --- Workout gauge bits
  // Gauge layer
  s_gauge_layer = layer_create(GRect(0,0,10,10));
  layer_set_update_proc(s_gauge_layer, gauge_update_proc);
  layer_add_child(root, s_gauge_layer);

  // Info text layers (over gauge)
  make_label(&s_info_current);
  make_label(&s_info_target);
  make_label(&s_info_hr);

  text_layer_set_text_alignment(s_info_current, GTextAlignmentCenter);
  text_layer_set_text_alignment(s_info_target,  GTextAlignmentCenter);
  text_layer_set_text_alignment(s_info_hr,      GTextAlignmentCenter);

  text_layer_set_overflow_mode(s_info_current, GTextOverflowModeWordWrap);
  text_layer_set_overflow_mode(s_info_target,  GTextOverflowModeWordWrap);
  text_layer_set_overflow_mode(s_info_hr,      GTextOverflowModeWordWrap);

#ifdef PBL_COLOR
  text_layer_set_text_color(s_info_current, GColorWhite);
  text_layer_set_text_color(s_info_target,  GColorWhite);
  text_layer_set_text_color(s_info_hr,      GColorWhite);
#endif

  layer_add_child(root, text_layer_get_layer(s_info_current));
  layer_add_child(root, text_layer_get_layer(s_info_target));
  layer_add_child(root, text_layer_get_layer(s_info_hr));

  // Big current value
  make_label(&s_info_big);
  text_layer_set_text_alignment(s_info_big, GTextAlignmentCenter);
  text_layer_set_font(s_info_big, fonts_get_system_font(FONT_KEY_BITHAM_42_BOLD));
  layer_add_child(root, text_layer_get_layer(s_info_big));

  // Underbar
  s_underbar_layer = layer_create(GRect(0,0,10,2));
  layer_set_update_proc(s_underbar_layer, underbar_update_proc);
  layer_add_child(root, s_underbar_layer);

  // Start hidden; layout/render will show them in workout view
  layer_set_hidden(s_gauge_layer, true);
  layer_set_hidden(text_layer_get_layer(s_info_current), true);
  layer_set_hidden(text_layer_get_layer(s_info_target), true);
  layer_set_hidden(text_layer_get_layer(s_info_hr), true);
  layer_set_hidden(text_layer_get_layer(s_info_big), true);
  layer_set_hidden(s_underbar_layer, true);

  // Start protocol after all layers exist.
  pebble_protocol_init(&s_protocol);
  if (persist_exists(PKEY_UNITS)) s_protocol.units = (PebbleUnits)persist_read_int(PKEY_UNITS);
  if (persist_exists(PKEY_HERO)) {
    int hero = persist_read_int(PKEY_HERO);
    s_hero = (HeroMetric)clamp_int(hero, HERO_HR, HERO_POWER);
  }
  if (persist_exists(PKEY_FOCUS)) {
    int focus = persist_read_int(PKEY_FOCUS);
    s_focus = (FocusMode)clamp_int(focus, FOCUS_GRID, FOCUS_HERO_ONLY);
  }

  s_view = (s_protocol.target_kind == TGT_NONE) ? VIEW_FREE : VIEW_WORKOUT;
  pebble_protocol_start(&s_protocol, view_protocol_updated, NULL);

  render_all();

#if PBL_API_EXISTS(unobstructed_area_service_subscribe)
  UnobstructedAreaHandlers h = {
    .will_change = NULL,
    .change = unobstructed_change,
    .did_change = NULL,
  };
  unobstructed_area_service_subscribe(h, w);
#endif
}

static void win_unload(Window *w) {
  (void)w;
  pebble_protocol_stop();
#if PBL_API_EXISTS(unobstructed_area_service_unsubscribe)
  unobstructed_area_service_unsubscribe();
#endif

  TextLayer *all[] = {
    s_hero_label,  s_hero_value,
    s_hr_label_grid,  s_hr_value_grid,
    s_pace_label,     s_pace_value,
    s_cad_label,      s_cad_value,
    s_dist_label,     s_dist_value,
    s_power_label,    s_power_value,
    s_info_current,   s_info_target, s_info_hr,
    s_info_big
  };
  for (unsigned i = 0; i < sizeof(all)/sizeof(all[0]); ++i) {
    if (all[i]) text_layer_destroy(all[i]);
  }
  if (s_gauge_layer) layer_destroy(s_gauge_layer);
  if (s_underbar_layer) layer_destroy(s_underbar_layer);
}



// ---------- App init/deinit ----------
void view_init(void) {
  s_win = window_create();
  window_set_click_config_provider(s_win, click_config_provider);
  window_set_window_handlers(s_win, (WindowHandlers){ .load = win_load, .unload = win_unload });
  window_stack_push(s_win, true);
}

void view_deinit(void) {
  if (s_win) {
    window_destroy(s_win);
    s_win = NULL;
  }
}
