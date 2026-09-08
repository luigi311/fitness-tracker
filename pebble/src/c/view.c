#include <pebble.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

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

// Screens at least this wide can afford the larger type scale.
#define WIDE_SCREEN_W 180

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

// ---------- UI: status bar ----------
static TextLayer *s_elapsed_value;
static TextLayer *s_link_value;

static char s_buf_elapsed[16];

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

// ---------- UI: workout ----------
static Layer     *s_zone_bar_layer;
static TextLayer *s_info_big;
static TextLayer *s_info_remaining;
static TextLayer *s_info_band;
static TextLayer *s_info_step_hr;

static char s_buf_remaining[20];
static char s_buf_band[32];
static char s_buf_step_hr[32];
static bool s_in_zone_prev = false;
// False until a band has been evaluated for the current step, so entering a
// new step never fires a zone alert on top of the step-change buzz.
static bool s_have_zone_prev = false;

// ---------- Forward declarations ----------
static void render_all(void);
static void layout_layers(Window *w);
static void maybe_haptic_transition(void);
static void view_protocol_updated(void *context);

// ---------- Staleness ----------
// A latched have_* flag only says a value once arrived. Once the link is stale
// nothing on screen is current, so a number would be a claim we cannot back.
static bool metric_live(bool have_flag) {
  return have_flag && !s_protocol.stale;
}

static bool target_metric_live(void) {
  switch (s_protocol.target_kind) {
    case TGT_POWER:      return metric_live(s_protocol.have_power);
    case TGT_PACE:       return metric_live(s_protocol.have_pace);
    case TGT_HEART_RATE: return metric_live(s_protocol.have_hr);
    default:             return false;
  }
}

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

// The zone is read from the whole screen rather than a shape you have to
// focus on, which is the only thing that survives a moving wrist. Without a
// live reading there is no zone to state, so the screen stays plain.
static GColor zone_background(void) {
  if (s_view != VIEW_WORKOUT || !target_metric_live()) {
    return GColorBlack;
  }
#ifdef PBL_COLOR
  switch (pebble_zone(&s_protocol)) {
    case PEBBLE_ZONE_IN:   return GColorGreen;
    case PEBBLE_ZONE_NEAR: return GColorYellow;
    default:               return GColorRed;
  }
#else
  // No colour to spend, so the whole screen inverts when you leave the band.
  return (pebble_zone(&s_protocol) == PEBBLE_ZONE_IN) ? GColorBlack : GColorWhite;
#endif
}

static GColor zone_foreground(void) {
  GColor bg = zone_background();
#ifdef PBL_COLOR
  // Green and yellow are bright enough to take black; red is not.
  if (gcolor_equal(bg, GColorBlack) || gcolor_equal(bg, GColorRed)) {
    return GColorWhite;
  }
  return GColorBlack;
#else
  return gcolor_equal(bg, GColorWhite) ? GColorBlack : GColorWhite;
#endif
}

// ---------- Haptics ----------
// Zone alerts own the short and double pulses, so a step change needs a shape
// of its own: two long buzzes nobody will confuse with either.
static void vibe_step_change(void) {
  static const uint32_t segments[] = { 250, 120, 250 };
  VibePattern pattern = {
    .durations = segments,
    .num_segments = ARRAY_LENGTH(segments),
  };
  vibes_enqueue_custom_pattern(pattern);
}

// Finishing the workout ends on a long buzz, so it reads as a finale rather
// than one more step.
static void vibe_workout_complete(void) {
  static const uint32_t segments[] = { 120, 90, 120, 90, 450 };
  VibePattern pattern = {
    .durations = segments,
    .num_segments = ARRAY_LENGTH(segments),
  };
  vibes_enqueue_custom_pattern(pattern);
}

static void view_protocol_updated(void *context) {
  (void)context;
  if (s_protocol.units_changed) {
    persist_write_int(PKEY_UNITS, (int)s_protocol.units);
    s_protocol.units_changed = false;
  }
  if (s_protocol.step_changed) {
    vibe_step_change();
    // The new step has its own band; re-baseline so the next crossing alerts.
    s_have_zone_prev = false;
    s_protocol.step_changed = false;
  }
  if (s_protocol.workout_ended) {
    vibe_workout_complete();
    s_protocol.workout_ended = false;
  }

  ViewMode next_view = (s_protocol.target_kind == TGT_NONE) ? VIEW_FREE : VIEW_WORKOUT;
  if (next_view != s_view) {
    // Entering a workout no longer raises a step change, so the zone baseline
    // has to be dropped here or the first reading would be compared against
    // the previous workout's band.
    s_have_zone_prev = false;
  }
  s_view = next_view;
  render_all();
}

// ---------- Font helpers ----------
// Bold is reserved for readings that change; a fixed label reads fine at
// regular weight and takes less width, which is what the grid cells need.
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

// Thresholds sit just above each font's own line height. They used to demand
// far more room than the face needs, so a 39px hero box fell back to 34px type
// and a 19px cell fell all the way to 14px.
static GFont pick_font_value(int h, bool is_hero, bool in_focus) {
  (void)in_focus;
  if (is_hero) {
    // BITHAM_42_BOLD rather than a MEDIUM_NUMBERS face: the hero shows "-"
    // when a metric is missing, which a digits-only font cannot draw.
    if (h >= 42) return fonts_get_system_font(FONT_KEY_BITHAM_42_BOLD);
    if (h >= 30) return fonts_get_system_font(FONT_KEY_GOTHIC_28_BOLD);
    return fonts_get_system_font(FONT_KEY_GOTHIC_24_BOLD);
  } else {
    // Grid values
    if (h >= 30) return fonts_get_system_font(FONT_KEY_GOTHIC_28_BOLD);
    if (h >= 24) return fonts_get_system_font(FONT_KEY_GOTHIC_24_BOLD);
    if (h >= 18) return fonts_get_system_font(FONT_KEY_GOTHIC_18_BOLD);
    return fonts_get_system_font(FONT_KEY_GOTHIC_14_BOLD);
  }
}

static GFont pick_font_status(int W, bool bold) {
  if (W >= WIDE_SCREEN_W) {
    return fonts_get_system_font(bold ? FONT_KEY_GOTHIC_24_BOLD : FONT_KEY_GOTHIC_24);
  }
  return fonts_get_system_font(bold ? FONT_KEY_GOTHIC_18_BOLD : FONT_KEY_GOTHIC_18);
}

static int status_bar_height(int W){ return (W >= WIDE_SCREEN_W) ? 28 : 22; }

// ---------- Zone bar ----------
// A flattened gauge: the arc told you the same thing but cost 38% of the
// screen on the 144px watches, which is what forced every other line down to
// 14px type.
#define ZONE_BAR_MARKER_H 6

static void zone_bar_update_proc(Layer *layer, GContext *ctx) {
  if (s_protocol.target_kind == TGT_NONE) return;

  GRect b = layer_get_bounds(layer);
  const GColor fg = zone_foreground();

  int16_t track_h = b.size.h - ZONE_BAR_MARKER_H - 1;
  if (track_h < 4) track_h = 4;
  const GRect track = GRect(b.origin.x + 2, b.origin.y, b.size.w - 4, track_h);

  PebbleGaugeScale scale;
  pebble_gauge_scale(&s_protocol, &scale);

  // Everything is drawn in the foreground colour so the bar stays legible
  // whichever way the zone colours the screen behind it.
  graphics_context_set_stroke_color(ctx, fg);
  graphics_context_set_stroke_width(ctx, 1);
  graphics_draw_rect(ctx, track);

  int16_t x0 = track.origin.x +
    (int16_t)(track.size.w * pebble_gauge_fraction(&scale, scale.low));
  int16_t x1 = track.origin.x +
    (int16_t)(track.size.w * pebble_gauge_fraction(&scale, scale.high));
  if (x1 <= x0) x1 = x0 + 1;
  graphics_context_set_fill_color(ctx, fg);
  graphics_fill_rect(ctx, GRect(x0, track.origin.y + 2,
                                x1 - x0, track.size.h - 4), 0, GCornerNone);

  // No reading means no position to point at.
  if (!target_metric_live()) return;

  float t = pebble_gauge_fraction(&scale,
                                  pebble_current_value_for_kind(&s_protocol));
  int16_t mx = track.origin.x + (int16_t)(track.size.w * t);
  // Pinned inside the track: a reading past either end is exactly when the
  // marker matters most, and half a triangle off-screen reads as nothing.
  int16_t mx_min = track.origin.x + ZONE_BAR_MARKER_H;
  int16_t mx_max = track.origin.x + track.size.w - ZONE_BAR_MARKER_H;
  if (mx < mx_min) mx = mx_min;
  if (mx > mx_max) mx = mx_max;

  // The marker sits below the track pointing up at it, so it can never be lost
  // inside the filled band.
  const int16_t marker_top = track.origin.y + track.size.h + 1;
  for (int i = 0; i < ZONE_BAR_MARKER_H; ++i) {
    int half = i + 1;
    graphics_fill_rect(ctx, GRect(mx - half, marker_top + i, 2 * half + 1, 1),
                       0, GCornerNone);
  }
}

static void maybe_haptic_transition(void) {
  if (s_protocol.target_kind == TGT_NONE || !s_protocol.workout_outdoor) return;
  if (!target_metric_live()) return;

  bool in_zone_now = (pebble_zone(&s_protocol) == PEBBLE_ZONE_IN);

  if (!s_have_zone_prev) {
    // First band evaluation for this step: record it without alerting.
    s_in_zone_prev = in_zone_now;
    s_have_zone_prev = true;
    return;
  }

  if (in_zone_now != s_in_zone_prev) {
    if (in_zone_now) vibes_short_pulse(); else vibes_double_pulse();
    s_in_zone_prev = in_zone_now;
  }
}

// ---------- Layout ----------
static void layout_status_bar(GRect b) {
  const int W = b.size.w;
  const int h = status_bar_height(W);
  const int pad = 4;
  const int half = (W - 2 * pad) / 2;

  layer_set_frame(text_layer_get_layer(s_elapsed_value),
                  GRect(b.origin.x + pad, b.origin.y, half, h));
  text_layer_set_font(s_elapsed_value, pick_font_status(W, /*bold=*/true));
  text_layer_set_text_alignment(s_elapsed_value, GTextAlignmentLeft);

  layer_set_frame(text_layer_get_layer(s_link_value),
                  GRect(b.origin.x + pad + half, b.origin.y, half, h));
  text_layer_set_font(s_link_value, pick_font_status(W, /*bold=*/false));
  text_layer_set_text_alignment(s_link_value, GTextAlignmentRight);
}

static void layout_workout(GRect content) {
  const int W = content.size.w;
  const bool wide = (W >= WIDE_SCREEN_W);

  const int bar_h = wide ? 20 : 16;
  const int big_h = wide ? 56 : 44;
  const int rem_h = wide ? 40 : 30;
  const int line_h = wide ? 28 : 22;

  int y = content.origin.y;

  layer_set_frame(s_zone_bar_layer, GRect(content.origin.x, y, W, bar_h));
  layer_set_hidden(s_zone_bar_layer, false);
  y += bar_h + 2;

  layer_set_frame(text_layer_get_layer(s_info_big),
                  GRect(content.origin.x + 2, y, W - 4, big_h));
  text_layer_set_font(s_info_big, fonts_get_system_font(FONT_KEY_BITHAM_42_BOLD));
  y += big_h;

  // How much of the step is left is the second thing worth reading at speed,
  // so it gets the largest type the system fonts allow after the value.
  layer_set_frame(text_layer_get_layer(s_info_remaining),
                  GRect(content.origin.x + 2, y, W - 4, rem_h));
  text_layer_set_font(s_info_remaining,
                      fonts_get_system_font(FONT_KEY_GOTHIC_28_BOLD));
  y += rem_h;

  layer_set_frame(text_layer_get_layer(s_info_band),
                  GRect(content.origin.x + 2, y, W - 4, line_h));
  // The band is a number you check against the hero above it, so it is read
  // like a value rather than like a label.
  text_layer_set_font(s_info_band,
    fonts_get_system_font(wide ? FONT_KEY_GOTHIC_24_BOLD : FONT_KEY_GOTHIC_18_BOLD));
  y += line_h;

  layer_set_frame(text_layer_get_layer(s_info_step_hr),
                  GRect(content.origin.x + 2, y, W - 4, line_h));
  text_layer_set_font(s_info_step_hr,
    fonts_get_system_font(wide ? FONT_KEY_GOTHIC_24_BOLD : FONT_KEY_GOTHIC_18_BOLD));

  TextLayer *shown[4] = { s_info_big, s_info_remaining, s_info_band, s_info_step_hr };
  for (int i = 0; i < 4; ++i) {
    text_layer_set_text_alignment(shown[i], GTextAlignmentCenter);
    layer_set_hidden(text_layer_get_layer(shown[i]), false);
  }

  // Hide free-run UI
  for (int i = 0; i < 5; ++i) {
    layer_set_hidden(text_layer_get_layer(s_cells[i].label), true);
    layer_set_hidden(text_layer_get_layer(s_cells[i].value), true);
  }
  layer_set_hidden(text_layer_get_layer(s_hero_label), true);
  layer_set_hidden(text_layer_get_layer(s_hero_value), true);
}

static void hide_workout_layers(void) {
  layer_set_hidden(s_zone_bar_layer, true);
  layer_set_hidden(text_layer_get_layer(s_info_big), true);
  layer_set_hidden(text_layer_get_layer(s_info_remaining), true);
  layer_set_hidden(text_layer_get_layer(s_info_band), true);
  layer_set_hidden(text_layer_get_layer(s_info_step_hr), true);
}

static void layout_layers(Window *w) {
  Layer *root = window_get_root_layer(w);
  GRect b = layer_get_unobstructed_bounds(root);

  layout_status_bar(b);

  const int bar_h = status_bar_height(b.size.w);
  GRect content = GRect(b.origin.x, b.origin.y + bar_h,
                        b.size.w, b.size.h - bar_h);

  const int W = content.size.w;
  const int H = content.size.h;

#if PBL_ROUND
  int pad_top = 8;
  int pad_lr  = 10;
#else
  int pad_top = 2;
  int pad_lr  = 6;
#endif

  int pad_mid = (s_focus == FOCUS_GRID) ? 4 : 6;  // tighter spacing in stacked view
  const int pad_bot = 2;

  if (s_view == VIEW_WORKOUT) {
    layout_workout(content);
    return;
  }

  // --- Free run layout (hero + grid) ---
  const bool wide = (W >= WIDE_SCREEN_W);
  // Sized to what the type needs rather than to a share of the screen: at 42%
  // the hero took height the grid needed and still could not fit its own font.
  const int hero_label_h = wide ? 18 : 14;
  const int hero_value_h = 44;  // BITHAM_42_BOLD plus a little air
  int hero_h = (s_focus == FOCUS_HERO_ONLY) ? (H - pad_top - pad_bot)
                                            : (hero_label_h + 2 + hero_value_h);

  // On Focus, give the digits more horizontal room
  if (s_focus == FOCUS_HERO_ONLY) {
    pad_lr = (W >= WIDE_SCREEN_W) ? 6 : 4; // tighter side padding for large digits
  }

  // ---- Hero area ----
  GRect hero = GRect(content.origin.x + pad_lr, content.origin.y + pad_top,
                     W - 2*pad_lr, hero_h);

  int label_h = hero_label_h;

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
    hide_workout_layers();
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
      // Hide the hero's grid twin
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
  int grid_h   = (content.origin.y + H) - (grid_top + pad_bot);
  if (grid_h < 24) grid_h = 24;

  int cols = 2;
  int rows = (n + cols - 1) / cols;

  int cell_w = (W - 2*pad_lr - (cols - 1)*pad_mid) / cols;
  int cell_h = (grid_h - (rows - 1)*pad_mid) / rows;
  if (cell_h < 26) cell_h = 26;

  int cell_label_h = wide ? 18 : 14;
  int cell_value_h = cell_h - cell_label_h - 2;

  // Hide all non-hero grid cells first, then unhide the active ones.
  for (int i = 0; i < 5; ++i) {
    if ( (s_hero == HERO_HR    && s_cells[i].id == CELL_HR) ||
         (s_hero == HERO_PACE  && s_cells[i].id == CELL_PACE) ||
         (s_hero == HERO_POWER && s_cells[i].id == CELL_PWR) ) {
      continue; // hero's grid twin already hidden above
    }
    layer_set_hidden(text_layer_get_layer(s_cells[i].label), true);
    layer_set_hidden(text_layer_get_layer(s_cells[i].value), true);
  }

  for (int i = 0; i < n; ++i) {
    int r = i / cols;
    int c = i % cols;
    int x = content.origin.x + pad_lr + c * (cell_w + pad_mid);
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

  hide_workout_layers();
}

#if PBL_API_EXISTS(unobstructed_area_service_subscribe)
static void unobstructed_change(AnimationProgress progress, void *context) {
  (void)progress;
  layout_layers((Window *)context);
}
#endif

// ---------- Rendering ----------
// Everything on screen takes its colour from the zone, so the text stays
// readable whichever way the background went.
static void apply_zone_colors(void) {
  const GColor bg = zone_background();
  const GColor fg = zone_foreground();

  if (s_win) window_set_background_color(s_win, bg);

  TextLayer *all[] = {
    s_elapsed_value, s_link_value,
    s_hero_label, s_hero_value,
    s_hr_label_grid, s_hr_value_grid,
    s_pace_label, s_pace_value,
    s_cad_label, s_cad_value,
    s_dist_label, s_dist_value,
    s_power_label, s_power_value,
    s_info_big, s_info_remaining, s_info_band, s_info_step_hr,
  };
  for (unsigned i = 0; i < sizeof(all)/sizeof(all[0]); ++i) {
    if (all[i]) text_layer_set_text_color(all[i], fg);
  }
}

static void render_status_bar(void) {
  if (s_protocol.have_elapsed) {
    pebble_format_elapsed(s_buf_elapsed, sizeof(s_buf_elapsed), s_protocol.elapsed_s);
  } else {
    snprintf(s_buf_elapsed, sizeof(s_buf_elapsed), "-:--");
  }
  text_layer_set_text(s_elapsed_value, s_buf_elapsed);

  // Silence means healthy; the slot only speaks when it has something to say.
  // In a workout it carries the zone word, which is what tells NEAR from OUT
  // on a black and white watch where the screen can only invert.
  if (s_protocol.stale) {
    text_layer_set_text(s_link_value, "NO LINK");
  } else if (s_view == VIEW_WORKOUT && target_metric_live()) {
    text_layer_set_text(s_link_value, pebble_zone_word(&s_protocol));
  } else {
    text_layer_set_text(s_link_value, "");
  }
}

static void render_all(void) {
  render_status_bar();

  // If a target is active, always render the workout view
  if (s_protocol.target_kind != TGT_NONE && s_view != VIEW_WORKOUT) {
    s_view = VIEW_WORKOUT;
  }

  if (s_view == VIEW_WORKOUT) {
    // Big value (numeric only)
    static char s_big[12];
    if (!target_metric_live()) {
      snprintf(s_big, sizeof(s_big), "-");
    } else if (s_protocol.target_kind == TGT_POWER) {
      snprintf(s_big, sizeof(s_big), "%u", (unsigned)s_protocol.last_power);
    } else if (s_protocol.target_kind == TGT_PACE) {
      view_format_pace_value_only(s_big, sizeof(s_big)); // m:ss
    } else if (s_protocol.target_kind == TGT_HEART_RATE) {
      snprintf(s_big, sizeof(s_big), "%u", (unsigned)s_protocol.last_hr);
    } else {
      snprintf(s_big, sizeof(s_big), "-");
    }
    text_layer_set_text(s_info_big, s_big);

    pebble_format_remaining_line(s_buf_remaining, sizeof(s_buf_remaining), &s_protocol);
    pebble_format_band_line(s_buf_band, sizeof(s_buf_band), &s_protocol);
    pebble_format_step_hr_line(s_buf_step_hr, sizeof(s_buf_step_hr), &s_protocol);

    text_layer_set_text(s_info_remaining, s_buf_remaining);
    text_layer_set_text(s_info_band, s_buf_band);
    text_layer_set_text(s_info_step_hr, s_buf_step_hr);

    apply_zone_colors();

    if (s_win) {
      layout_layers(s_win);
      layer_mark_dirty(s_zone_bar_layer);
    }

    // Haptic only when crossing the band
    maybe_haptic_transition();

    return;
  }

  // ----- Free-run rendering -----
  static char hr_buf[20], pace_buf[16], cad_buf[16], dist_buf[20], pwr_buf[16];

  if (metric_live(s_protocol.have_hr)) snprintf(hr_buf, sizeof(hr_buf), "%u", (unsigned)s_protocol.last_hr);
  else                                 snprintf(hr_buf, sizeof(hr_buf), "-");

  if (metric_live(s_protocol.have_pace)) view_format_pace(pace_buf, sizeof(pace_buf), s_protocol.last_pace_x100);
  else                                   snprintf(pace_buf, sizeof(pace_buf), "-");

  if (metric_live(s_protocol.have_cad)) snprintf(cad_buf, sizeof(cad_buf), "%u", (unsigned)s_protocol.last_cad);
  else                                  snprintf(cad_buf, sizeof(cad_buf), "-");

  if (metric_live(s_protocol.have_dist)) view_format_distance(dist_buf, sizeof(dist_buf), s_protocol.last_dist_m);
  else                                   snprintf(dist_buf, sizeof(dist_buf), "-");

  if (metric_live(s_protocol.have_power)) snprintf(pwr_buf, sizeof(pwr_buf), "%u", (unsigned)s_protocol.last_power);
  else                                    snprintf(pwr_buf, sizeof(pwr_buf), "-");

  // Hero content
  switch (s_hero) {
    case HERO_HR: {
      text_layer_set_text(s_hero_label, "HEART RATE");
      text_layer_set_text(s_hero_value, hr_buf);
      int vh = layer_get_bounds(text_layer_get_layer(s_hero_value)).size.h;
      text_layer_set_font(s_hero_value,
        pick_font_value(vh, /*is_hero=*/true, /*in_focus=*/(s_focus == FOCUS_HERO_ONLY)));
      break;
    }
    case HERO_POWER: {
      text_layer_set_text(s_hero_label, "POWER/W");
      text_layer_set_text(s_hero_value, pwr_buf);
      int vh = layer_get_bounds(text_layer_get_layer(s_hero_value)).size.h;
      text_layer_set_font(s_hero_value,
        pick_font_value(vh, /*is_hero=*/true, /*in_focus=*/(s_focus == FOCUS_HERO_ONLY)));
      break;
    }
    case HERO_PACE: {
      // Big m:ss only; unit in the label
      static char pace_val[12];
      if (metric_live(s_protocol.have_pace)) view_format_pace_value_only(pace_val, sizeof(pace_val));
      else                                   snprintf(pace_val, sizeof(pace_val), "-");
      text_layer_set_text(s_hero_label, (s_protocol.units == PEBBLE_UNITS_METRIC) ? "PACE/KM" : "PACE/MI");
      text_layer_set_text(s_hero_value, pace_val);
      int vh = layer_get_bounds(text_layer_get_layer(s_hero_value)).size.h;
      text_layer_set_font(s_hero_value,
        pick_font_value(vh, /*is_hero=*/true, /*in_focus=*/(s_focus == FOCUS_HERO_ONLY)));
      break;
    }
  }

  // Grid labels/values (stacked view). Units live in the labels so the values
  // stay pure digits at the largest size the cell allows.
  if (s_focus == FOCUS_GRID) {
    text_layer_set_text(s_hr_label_grid, "HR/BPM");
    text_layer_set_text(s_hr_value_grid, hr_buf);

    static char pace_val_grid[12];
    if (metric_live(s_protocol.have_pace)) view_format_pace_value_only(pace_val_grid, sizeof(pace_val_grid));
    else                                   snprintf(pace_val_grid, sizeof(pace_val_grid), "-");
    text_layer_set_text(s_pace_label, (s_protocol.units == PEBBLE_UNITS_METRIC) ? "PACE/KM" : "PACE/MI");
    text_layer_set_text(s_pace_value, pace_val_grid);

    text_layer_set_text(s_cad_label, "CAD/SPM");
    text_layer_set_text(s_cad_value, cad_buf);

    static char dist_val_grid[20];
    if (metric_live(s_protocol.have_dist)) {
      // The unit is in the label, so trim it from the value.
      view_format_distance(dist_val_grid, sizeof(dist_val_grid), s_protocol.last_dist_m);
      char *space = strchr(dist_val_grid, ' ');
      if (space) *space = '\0';
    } else {
      snprintf(dist_val_grid, sizeof(dist_val_grid), "-");
    }
    text_layer_set_text(s_dist_label,
      (s_protocol.units == PEBBLE_UNITS_METRIC) ? "DIST/KM" : "DIST/MI");
    text_layer_set_text(s_dist_value, dist_val_grid);

    text_layer_set_text(s_power_label, "PWR/W");
    text_layer_set_text(s_power_value, pwr_buf);
  }

  apply_zone_colors();

  if (s_win) layout_layers(s_win);
}

// ---------- Buttons ----------
// No button vibrates: the zone alerts own the haptic channel, and a buzz that
// also means "you pressed something" makes both unreadable on the wrist. Every
// press changes the screen, which is feedback enough.
static void toggle_units(void) {
  s_protocol.units = (s_protocol.units == PEBBLE_UNITS_METRIC) ? PEBBLE_UNITS_IMPERIAL : PEBBLE_UNITS_METRIC;
  persist_write_int(PKEY_UNITS, (int)s_protocol.units);
  render_all();
}

static void next_hero(void) {
  s_hero = (HeroMetric)((s_hero + 1) % 3);
  persist_write_int(PKEY_HERO, (int)s_hero);
  render_all();
}

static void prev_hero(void) {
  s_hero = (HeroMetric)((s_hero + 2) % 3); // wrap backwards
  persist_write_int(PKEY_HERO, (int)s_hero);
  render_all();
}

static void toggle_focus(void) {
  s_focus = (s_focus == FOCUS_GRID) ? FOCUS_HERO_ONLY : FOCUS_GRID;
  persist_write_int(PKEY_FOCUS, (int)s_focus);
  render_all();
}

// The hero and the grid only exist in free run, so in a workout these would
// persist a setting and redraw nothing. Doing nothing at least tells the truth.
static void up_click_handler(ClickRecognizerRef _, void *ctx) {
  (void)_; (void)ctx;
  if (s_view == VIEW_FREE) next_hero();
}

static void down_click_handler(ClickRecognizerRef _, void *ctx) {
  (void)_; (void)ctx;
  if (s_view == VIEW_FREE) prev_hero();
}

static void select_click_handler(ClickRecognizerRef _, void *ctx) {
  (void)_; (void)ctx;
  if (s_view == VIEW_FREE) toggle_focus();
}

// Units are a settings change that silently reinterprets every number on the
// screen, so they need a press you cannot make by brushing a sleeve.
static void select_long_click_handler(ClickRecognizerRef _, void *ctx) {
  (void)_; (void)ctx;
  toggle_units();
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

  // Status bar: elapsed time and link health, shown in both views.
  make_label(&s_elapsed_value);
  make_label(&s_link_value);
  layer_add_child(root, text_layer_get_layer(s_elapsed_value));
  layer_add_child(root, text_layer_get_layer(s_link_value));

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

  // --- Workout bits
  s_zone_bar_layer = layer_create(GRect(0,0,10,10));
  layer_set_update_proc(s_zone_bar_layer, zone_bar_update_proc);
  layer_add_child(root, s_zone_bar_layer);

  make_label(&s_info_big);
  make_label(&s_info_remaining);
  make_label(&s_info_band);
  make_label(&s_info_step_hr);

  TextLayer *info_lines[4] = {
    s_info_big, s_info_remaining, s_info_band, s_info_step_hr,
  };
  for (int i = 0; i < 4; ++i) {
    text_layer_set_text_alignment(info_lines[i], GTextAlignmentCenter);
    // One line each: an overflowing line must shorten, not wrap out of sight.
    text_layer_set_overflow_mode(info_lines[i], GTextOverflowModeTrailingEllipsis);
    layer_add_child(root, text_layer_get_layer(info_lines[i]));
  }
  text_layer_set_font(s_info_big, fonts_get_system_font(FONT_KEY_BITHAM_42_BOLD));

  // Start hidden; layout/render will show them in workout view
  hide_workout_layers();

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
    s_elapsed_value,  s_link_value,
    s_hero_label,     s_hero_value,
    s_hr_label_grid,  s_hr_value_grid,
    s_pace_label,     s_pace_value,
    s_cad_label,      s_cad_value,
    s_dist_label,     s_dist_value,
    s_power_label,    s_power_value,
    s_info_big,       s_info_remaining,
    s_info_band,      s_info_step_hr
  };
  for (unsigned i = 0; i < sizeof(all)/sizeof(all[0]); ++i) {
    if (all[i]) text_layer_destroy(all[i]);
  }
  if (s_zone_bar_layer) layer_destroy(s_zone_bar_layer);
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
