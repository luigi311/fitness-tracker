#include <pebble.h>
#include <stdio.h>

// --- AppMessage keys from phone ---
enum {
  KEY_HR       = 1,
  KEY_PACE     = 2, // m/s*100
  KEY_CADENCE  = 3,
  KEY_DISTANCE = 4, // meters
  KEY_STATUS   = 5,
  KEY_UNITS    = 6, // 0=metric, 1=imperial
  KEY_POWER    = 7, // watts
};

typedef enum { UNITS_METRIC = 0, UNITS_IMPERIAL = 1 } Units;

// Persist keys (separate from AppMessage keys)
enum { PKEY_UNITS = 100, PKEY_HERO = 101, PKEY_FOCUS = 102 };

// Which metric is the hero (top, big)
typedef enum { HERO_HR = 0, HERO_PACE = 1, HERO_POWER = 2 } HeroMetric;

// Display density
typedef enum { FOCUS_GRID = 0, FOCUS_HERO_ONLY = 1 } FocusMode;

// ---------- Global state ----------
static Window *s_win;
static Units s_units = UNITS_METRIC;
static HeroMetric s_hero = HERO_HR;
static FocusMode s_focus = FOCUS_GRID;

// Cached values/flags
static bool     s_have_hr, s_have_pace, s_have_cad, s_have_dist, s_have_power;
static uint16_t s_last_hr, s_last_pace_x100, s_last_cad, s_last_power;
static uint32_t s_last_dist_m;

// ---------- UI: hero + grid ----------
static TextLayer *s_hero_value;
static TextLayer *s_hero_label;

// Grid also includes HR so HR can live in grid when not hero
typedef struct {
  TextLayer *label;
  TextLayer *value;
  const char *name;   // short label
  bool *have_flag;    // pointer to “have” bool
  int id;
} MetricCellID;

enum { CELL_HR=0, CELL_PACE=1, CELL_CAD=2, CELL_DIST=3, CELL_PWR=4 };

static TextLayer *s_hr_label_grid,  *s_hr_value_grid;
static TextLayer *s_pace_label,     *s_pace_value;
static TextLayer *s_cad_label,      *s_cad_value;
static TextLayer *s_dist_label,     *s_dist_value;
static TextLayer *s_power_label,    *s_power_value;

static MetricCellID s_cells[5]; // HR, Pace, Cad, Dist, Power

// ---------- Forward decls ----------
static void render_all(void);
static void layout_layers(Window *w);

// ---------- Formatting ----------
static void format_distance(char *out, size_t n, uint32_t meters) {
  if (s_units == UNITS_METRIC) {
    uint32_t km_whole = meters / 1000;
    uint32_t km_frac  = (meters % 1000) / 10; // two decimals
    snprintf(out, n, "%lu.%02lu km", (unsigned long)km_whole, (unsigned long)km_frac);
  } else {
    // miles_x100 = round(meters * 100 / 1609.344)
    uint32_t miles_x100 = (uint32_t)((((uint64_t)meters * 100000ULL) + 804672ULL) / 1609344ULL);
    uint32_t mi_whole   = miles_x100 / 100;
    uint32_t mi_frac    = miles_x100 % 100;
    snprintf(out, n, "%lu.%02lu mi", (unsigned long)mi_whole, (unsigned long)mi_frac);
  }
}

static void format_pace(char *out, size_t n, uint16_t speed_ms_x100) {
  if (speed_ms_x100 <= 1) { snprintf(out, n, "-"); return; }

  float ms = speed_ms_x100 / 100.0f;
  if (ms < 0.01f) { snprintf(out, n, "-"); return; }

  if (s_units == UNITS_METRIC) {
    float min_per_km = (1000.0f / ms) / 60.0f;
    int m = (int)min_per_km;
    int s = (int)((min_per_km - m) * 60.0f + 0.5f);
    if (s == 60) { s = 0; m += 1; }
    snprintf(out, n, "%d'%02d\"/km", m, s); // ASCII ' and "
  } else {
    float min_per_mile = (1609.344f / ms) / 60.0f;
    int m = (int)min_per_mile;
    int s = (int)((min_per_mile - m) * 60.0f + 0.5f);
    if (s == 60) { s = 0; m += 1; }
    snprintf(out, n, "%d'%02d\"/mi", m, s);
  }
}

// For hero/grid Pace value: big numeric "m:ss" only (unit goes in label)
static void format_pace_value_only(char *value_out, size_t vn) {
  if (!s_have_pace) { snprintf(value_out, vn, "-"); return; }
  float ms = s_last_pace_x100 / 100.0f;
  if (ms < 0.01f) { snprintf(value_out, vn, "-"); return; }
  float per_m = (s_units == UNITS_METRIC) ? (1000.0f / ms) : (1609.344f / ms);
  float min = per_m / 60.0f;
  int m = (int)min;
  int s = (int)((min - m) * 60.0f + 0.5f);
  if (s == 60) { s = 0; m += 1; }
  snprintf(value_out, vn, "%d:%02d", m, s); // colon so numeric fonts render cleanly
}

// ---------- Font helpers ----------
static GFont pick_font_label(int h, bool is_hero, bool in_focus) {
  if (is_hero) {
    // Hero label: larger in focus, bold in grid (safe thresholds)
    if (in_focus) {
      if (h >= 22) return fonts_get_system_font(FONT_KEY_GOTHIC_24);
      if (h >= 18) return fonts_get_system_font(FONT_KEY_GOTHIC_18);
      return fonts_get_system_font(FONT_KEY_GOTHIC_14);
    } else {
      if (h >= 22) return fonts_get_system_font(FONT_KEY_GOTHIC_24);
      if (h >= 18) return fonts_get_system_font(FONT_KEY_GOTHIC_18);
      return fonts_get_system_font(FONT_KEY_GOTHIC_14);
    }
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

  // Bigger hero area in Focus mode
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
    pick_font_label(label_h, /*is_hero=*/true, /*in_focus=*/(s_focus == FOCUS_HERO_ONLY))
  );

  text_layer_set_font(
    s_hero_value,
    pick_font_value(value_h, /*is_hero=*/true, /*in_focus=*/(s_focus == FOCUS_HERO_ONLY))
  );

  layer_set_frame(text_layer_get_layer(s_hero_value),
                  GRect(hero.origin.x, hero.origin.y + label_h + 2, hero.size.w, value_h));
  text_layer_set_text_alignment(s_hero_value, GTextAlignmentCenter);

  // ---- If focus mode = HERO_ONLY, hide ALL grid cells and return early ----
  if (s_focus == FOCUS_HERO_ONLY) {
    for (int i = 0; i < 5; ++i) {
      layer_set_hidden(text_layer_get_layer(s_cells[i].label), true);
      layer_set_hidden(text_layer_get_layer(s_cells[i].value), true);
    }
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
      pick_font_label(cell_label_h, /*is_hero=*/false, /*in_focus=*/false));
    text_layer_set_text_alignment(active[i]->label, GTextAlignmentCenter);
    layer_set_hidden(text_layer_get_layer(active[i]->label), false);

    layer_set_frame(text_layer_get_layer(active[i]->value),
                GRect(x, y + cell_label_h + 2, cell_w, cell_value_h));
    text_layer_set_font(active[i]->value,
      pick_font_value(cell_value_h, /*is_hero=*/false, /*in_focus=*/false));
    text_layer_set_text_alignment(active[i]->value, GTextAlignmentCenter);
    layer_set_hidden(text_layer_get_layer(active[i]->value), false);
  }
}

static void unobstructed_change(AnimationProgress progress, void *context) {
  (void)progress;
  layout_layers((Window *)context);
}

// ---------- Rendering ----------
static void render_all(void) {
  static char hr_buf[20], pace_buf[16], cad_buf[16], dist_buf[20], pwr_buf[16];

  if (s_have_hr)      snprintf(hr_buf, sizeof(hr_buf), "%u", (unsigned)s_last_hr);
  else                snprintf(hr_buf, sizeof(hr_buf), "-");

  if (s_have_pace)    format_pace(pace_buf, sizeof(pace_buf), s_last_pace_x100);
  else                snprintf(pace_buf, sizeof(pace_buf), "-");

  if (s_have_cad)     snprintf(cad_buf, sizeof(cad_buf), "%u spm", (unsigned)s_last_cad);
  else                snprintf(cad_buf, sizeof(cad_buf), "-");

  if (s_have_dist)    format_distance(dist_buf, sizeof(dist_buf), s_last_dist_m);
  else                snprintf(dist_buf, sizeof(dist_buf), "-");

  if (s_have_power)   snprintf(pwr_buf, sizeof(pwr_buf), "%u", (unsigned)s_last_power);
  else                snprintf(pwr_buf, sizeof(pwr_buf), "-");

  // Hero content
  switch (s_hero) {
    case HERO_HR: {
      static char hero_val[20];
      if (s_have_hr) snprintf(hero_val, sizeof(hero_val), "%u", (unsigned)s_last_hr);
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
      if (s_have_power) snprintf(hero_val, sizeof(hero_val), "%u", (unsigned)s_last_power);
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
      format_pace_value_only(pace_val, sizeof(pace_val)); // m:ss only
      text_layer_set_text(s_hero_label, (s_units == UNITS_METRIC) ? "PACE / KM" : "PACE / MI");
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
    format_pace_value_only(pace_val_grid, sizeof(pace_val_grid));
    text_layer_set_text(s_pace_label, (s_units == UNITS_METRIC) ? "PACE / KM" : "PACE / MI");
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

// ---------- AppMessage handler ----------
static void inbox_received(DictionaryIterator *iter, void *ctx) {
  Tuple *t;

  if ((t = dict_find(iter, KEY_UNITS))) {
    s_units = (t->value->uint8 == 1) ? UNITS_IMPERIAL : UNITS_METRIC;
    persist_write_int(PKEY_UNITS, (int)s_units);
    render_all();
  }

  if ((t = dict_find(iter, KEY_HR)))       { s_last_hr = t->value->uint16; s_have_hr = true; }
  if ((t = dict_find(iter, KEY_PACE)))     { s_last_pace_x100 = t->value->uint16; s_have_pace = true; }
  if ((t = dict_find(iter, KEY_CADENCE)))  { s_last_cad = t->value->uint16; s_have_cad = true; }
  if ((t = dict_find(iter, KEY_DISTANCE))) { s_last_dist_m = t->value->uint32; s_have_dist = true; }
  if ((t = dict_find(iter, KEY_POWER)))    { s_last_power = t->value->uint16; s_have_power = true; }

  render_all();
}

// ---------- Buttons ----------
static void toggle_units(void) {
  s_units = (s_units == UNITS_METRIC) ? UNITS_IMPERIAL : UNITS_METRIC;
  persist_write_int(PKEY_UNITS, (int)s_units);
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

// Long-press SELECT toggles focus mode (Grid <-> Hero-only)
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

  s_cells[0] = (MetricCellID){ .label=s_hr_label_grid,  .value=s_hr_value_grid,  .name="HR",   .have_flag=&s_have_hr,   .id=CELL_HR   };
  s_cells[1] = (MetricCellID){ .label=s_pace_label,     .value=s_pace_value,     .name="PACE", .have_flag=&s_have_pace, .id=CELL_PACE };
  s_cells[2] = (MetricCellID){ .label=s_cad_label,      .value=s_cad_value,      .name="CAD",  .have_flag=&s_have_cad,  .id=CELL_CAD  };
  s_cells[3] = (MetricCellID){ .label=s_dist_label,     .value=s_dist_value,     .name="DIST", .have_flag=&s_have_dist, .id=CELL_DIST };
  s_cells[4] = (MetricCellID){ .label=s_power_label,    .value=s_power_value,    .name="PWR",  .have_flag=&s_have_power,.id=CELL_PWR  };

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

  // Messaging + persistence
  app_message_register_inbox_received(inbox_received);
  app_message_open(128, 32);

  if (persist_exists(PKEY_UNITS)) s_units = (Units)persist_read_int(PKEY_UNITS);
  if (persist_exists(PKEY_HERO))  s_hero  = (HeroMetric)persist_read_int(PKEY_HERO);
  if (persist_exists(PKEY_FOCUS)) s_focus = (FocusMode)persist_read_int(PKEY_FOCUS);

  render_all();

  UnobstructedAreaHandlers h = { .will_change=NULL, .change=unobstructed_change, .did_change=NULL };
  unobstructed_area_service_subscribe(h, w);
}

static void win_unload(Window *w) {
  (void)w;
  unobstructed_area_service_unsubscribe();
  accel_tap_service_unsubscribe();

  TextLayer *all[] = {
    s_hero_label,  s_hero_value,
    s_hr_label_grid,  s_hr_value_grid,
    s_pace_label,     s_pace_value,
    s_cad_label,      s_cad_value,
    s_dist_label,     s_dist_value,
    s_power_label,    s_power_value
  };
  for (unsigned i = 0; i < sizeof(all)/sizeof(all[0]); ++i) {
    if (all[i]) text_layer_destroy(all[i]);
  }
}

// ---------- App init/deinit ----------
static void init(void) {
  s_win = window_create();
  window_set_click_config_provider(s_win, click_config_provider);
  window_set_window_handlers(s_win, (WindowHandlers){ .load = win_load, .unload = win_unload });
  window_stack_push(s_win, true);
}

static void deinit(void) {
  window_destroy(s_win);
}

int main(void) {
  init();
  app_event_loop();
  deinit();
  return 0;
}
