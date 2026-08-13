#include <math.h>
#include <stdio.h>
#include <string.h>

#include "format.h"

void pebble_format_distance(char *out, size_t n, KEY_DISTANCE_C_TYPE meters,
                            PebbleUnits units) {
  if (units == PEBBLE_UNITS_METRIC) {
    uint32_t km_whole = meters / 1000;
    uint32_t km_frac = (meters % 1000) / 10;
    snprintf(out, n, "%lu.%02lu km", (unsigned long)km_whole,
             (unsigned long)km_frac);
  } else {
    uint32_t miles_x100 =
      (uint32_t)((((uint64_t)meters * 100000ULL) + 804672ULL) / 1609344ULL);
    uint32_t mi_whole = miles_x100 / 100;
    uint32_t mi_frac = miles_x100 % 100;
    snprintf(out, n, "%lu.%02lu mi", (unsigned long)mi_whole,
             (unsigned long)mi_frac);
  }
}

void pebble_format_pace(char *out, size_t n, KEY_PACE_C_TYPE speed_ms_x100,
                        PebbleUnits units) {
  if (speed_ms_x100 <= 1) {
    snprintf(out, n, "-");
    return;
  }

  float ms = speed_ms_x100 / (float)KEY_PACE_SCALE;
  char value[16];
  pebble_format_pace_from_ms_value_only(value, sizeof(value), ms, units);
  if (value[0] == '-') {
    snprintf(out, n, "-");
    return;
  }

  char *separator = strchr(value, ':');
  if (separator == NULL) {
    snprintf(out, n, "-");
    return;
  }
  *separator = '\0';
  snprintf(out, n, "%s'%s\"/%s", value, separator + 1,
           units == PEBBLE_UNITS_METRIC ? "km" : "mi");
}

void pebble_format_pace_value_only(char *out, size_t n,
                                   const PebbleProtocolState *state) {
  if (!state->have_pace) {
    snprintf(out, n, "-");
    return;
  }

  float ms = state->last_pace_x100 / (float)KEY_PACE_SCALE;
  pebble_format_pace_from_ms_value_only(out, n, ms, state->units);
}

void pebble_format_pace_from_ms_value_only(char *out, size_t n, float ms,
                                           PebbleUnits units) {
  if (ms < 0.01f) {
    snprintf(out, n, "-");
    return;
  }

  float minutes_per_unit =
    (units == PEBBLE_UNITS_METRIC ? 1000.0f : 1609.344f) / ms / 60.0f;
  int minutes = (int)minutes_per_unit;
  int seconds = (int)((minutes_per_unit - minutes) * 60.0f + 0.5f);
  if (seconds == 60) {
    seconds = 0;
    minutes += 1;
  }
  snprintf(out, n, "%d:%02d", minutes, seconds);
}

float pebble_current_value_for_kind(const PebbleProtocolState *state) {
  if (state->target_kind == TGT_POWER) {
    return state->have_power ? (float)state->last_power : 0.f;
  }
  if (state->target_kind == TGT_PACE) {
    return state->have_pace
      ? (float)state->last_pace_x100 / (float)KEY_PACE_SCALE
      : 0.f;
  }
  if (state->target_kind == TGT_HEART_RATE) {
    return state->have_hr ? (float)state->last_hr : 0.f;
  }
  return 0.f;
}

float pebble_target_value(const PebbleProtocolState *state,
                          KEY_TGT_LO_C_TYPE value) {
  return state->target_kind == TGT_PACE
    ? (float)value / (float)TGT_PACE_SCALE
    : (float)value;
}

void pebble_gauge_texts(char *current_line, size_t current_n,
                        char *target_line, size_t target_n,
                        char *hr_line, size_t hr_n,
                        const PebbleProtocolState *state) {
  if (state->target_kind == TGT_POWER) {
    int current = state->have_power ? (int)state->last_power : 0;
    snprintf(current_line, current_n, "%d W", current);
  } else if (state->target_kind == TGT_PACE) {
    char current_pace[16];
    pebble_format_pace_value_only(current_pace, sizeof(current_pace), state);
    snprintf(current_line, current_n, "%s %s", current_pace,
             state->units == PEBBLE_UNITS_METRIC ? "/km" : "/mi");
  } else if (state->target_kind == TGT_HEART_RATE) {
    int current = state->have_hr ? (int)state->last_hr : 0;
    snprintf(current_line, current_n, "%d bpm", current);
  } else {
    snprintf(current_line, current_n, "—");
  }

  if (state->target_kind == TGT_POWER) {
    int low = (int)state->target_lo;
    int high = (int)state->target_hi;
    if (high < low) {
      int temp = low;
      low = high;
      high = temp;
    }
    snprintf(target_line, target_n, "Target: %d–%d W", low, high);
  } else if (state->target_kind == TGT_PACE) {
    float low_ms = state->target_lo / (float)TGT_PACE_SCALE;
    float high_ms = state->target_hi / (float)TGT_PACE_SCALE;
    if (high_ms < low_ms) {
      float temp = low_ms;
      low_ms = high_ms;
      high_ms = temp;
    }
    char low_text[16];
    char high_text[16];
    pebble_format_pace_from_ms_value_only(low_text, sizeof(low_text), low_ms,
                                          state->units);
    pebble_format_pace_from_ms_value_only(high_text, sizeof(high_text), high_ms,
                                          state->units);
    snprintf(target_line, target_n, "Target: %s–%s %s", high_text, low_text,
             state->units == PEBBLE_UNITS_METRIC ? "/km" : "/mi");
  } else if (state->target_kind == TGT_HEART_RATE) {
    int low = (int)state->target_lo;
    int high = (int)state->target_hi;
    if (high < low) {
      int temp = low;
      low = high;
      high = temp;
    }
    snprintf(target_line, target_n, "Target: %d–%d bpm", low, high);
  } else {
    snprintf(target_line, target_n, "Target: —");
  }

  if (state->target_kind == TGT_HEART_RATE) {
    hr_line[0] = '\0';
  } else if (state->have_hr) {
    snprintf(hr_line, hr_n, "HR: %u bpm", (unsigned)state->last_hr);
  } else {
    snprintf(hr_line, hr_n, "HR: —");
  }
}

GColor pebble_zone_color(const PebbleProtocolState *state) {
  if (state->target_kind == TGT_NONE) {
    return GColorWhite;
  }

  float low = pebble_target_value(state, state->target_lo);
  float high = pebble_target_value(state, state->target_hi);
  if (high < low) {
    float temp = low;
    low = high;
    high = temp;
  }
  float center = 0.5f * (low + high);
  float current = pebble_current_value_for_kind(state);

#ifdef PBL_COLOR
  if (current >= low && current <= high) {
    return GColorGreen;
  }
  float near = 0.10f * center;
  if ((current < low && low - current <= near) ||
      (current > high && current - high <= near)) {
    return GColorPastelYellow;
  }
  return GColorRed;
#else
  (void)center;
  (void)low;
  (void)high;
  (void)current;
  return GColorWhite;
#endif
}

const char *pebble_zone_word(const PebbleProtocolState *state, GColor color) {
#ifdef PBL_COLOR
  (void)state;
  return (color.argb == GColorGreen.argb) ? "IN" :
         (color.argb == GColorPastelYellow.argb) ? "NEAR" : "OUT";
#else
  (void)color;
  float low = pebble_target_value(state, state->target_lo);
  float high = pebble_target_value(state, state->target_hi);
  if (high < low) {
    float temp = low;
    low = high;
    high = temp;
  }
  float current = pebble_current_value_for_kind(state);
  if (current >= low && current <= high) {
    return "IN";
  }
  float center = 0.5f * (low + high);
  float near = 0.10f * center;
  if ((current < low && low - current <= near) ||
      (current > high && current - high <= near)) {
    return "NEAR";
  }
  return "OUT";
#endif
}
