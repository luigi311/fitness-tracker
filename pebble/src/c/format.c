#include <math.h>
#include <stdio.h>
#include <string.h>

#include "format.h"

// A fixed +/-50% window around the band centre made the gauge unreadable for
// every target except power: a 10 bpm heart-rate band came out as 6% of the
// arc, so the needle never visibly crossed it. Sizing the window from the band
// instead keeps the band at a constant, legible third of the sweep whatever
// the target is.
#define GAUGE_BAND_HALF_WIDTHS 3.0f
// Floor for a point target, where the band has no width of its own.
#define GAUGE_MIN_HALF_FRACTION 0.08f
// NEAR margin, as a fraction of the half domain. A third puts it one half-band
// outside the target, which splits the sweep evenly into in / near / out; any
// tighter and heart-rate noise alone jumps the amber warning entirely.
#define GAUGE_NEAR_FRACTION (1.0f / 3.0f)

#define METRES_PER_KM 1000u

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

void pebble_format_elapsed(char *out, size_t n, KEY_ELAPSED_C_TYPE seconds) {
  uint32_t total = (uint32_t)seconds;
  uint32_t hours = total / 3600;
  uint32_t minutes = (total % 3600) / 60;
  uint32_t secs = total % 60;
  if (hours > 0) {
    snprintf(out, n, "%lu:%02lu:%02lu", (unsigned long)hours,
             (unsigned long)minutes, (unsigned long)secs);
  } else {
    snprintf(out, n, "%lu:%02lu", (unsigned long)minutes, (unsigned long)secs);
  }
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

void pebble_gauge_scale(const PebbleProtocolState *state,
                        PebbleGaugeScale *out) {
  float low = pebble_target_value(state, state->target_lo);
  float high = pebble_target_value(state, state->target_hi);
  if (high < low) {
    float temp = low;
    low = high;
    high = temp;
  }

  float centre = 0.5f * (low + high);
  float half_band = 0.5f * (high - low);
  float half = GAUGE_BAND_HALF_WIDTHS * half_band;
  float floor_half = GAUGE_MIN_HALF_FRACTION * centre;
  if (half < floor_half) {
    half = floor_half;
  }
  if (half <= 0.f) {
    // Degenerate target: keep the domain non-empty so the mapping stays sane.
    half = 1.f;
  }

  out->low = low;
  out->high = high;
  out->min = centre - half;
  out->max = centre + half;
  out->near = GAUGE_NEAR_FRACTION * half;
}

float pebble_gauge_fraction(const PebbleGaugeScale *scale, float value) {
  float span = scale->max - scale->min;
  if (span <= 0.f) {
    return 0.5f;
  }
  float t = (value - scale->min) / span;
  if (t < 0.f) {
    t = 0.f;
  }
  if (t > 1.f) {
    t = 1.f;
  }
  return t;
}

PebbleZone pebble_zone(const PebbleProtocolState *state) {
  if (state->target_kind == TGT_NONE) {
    return PEBBLE_ZONE_IN;
  }

  PebbleGaugeScale scale;
  pebble_gauge_scale(state, &scale);
  float current = pebble_current_value_for_kind(state);

  if (current >= scale.low && current <= scale.high) {
    return PEBBLE_ZONE_IN;
  }
  if ((current < scale.low && scale.low - current <= scale.near) ||
      (current > scale.high && current - scale.high <= scale.near)) {
    return PEBBLE_ZONE_NEAR;
  }
  return PEBBLE_ZONE_OUT;
}

GColor pebble_zone_color(const PebbleProtocolState *state) {
#ifdef PBL_COLOR
  switch (pebble_zone(state)) {
    case PEBBLE_ZONE_IN:
      return GColorGreen;
    case PEBBLE_ZONE_NEAR:
      return GColorPastelYellow;
    default:
      return GColorRed;
  }
#else
  (void)state;
  return GColorWhite;
#endif
}

// What to do, not which side you are on. For a pace target the bar is held in
// speed while the value is shown as pace, so the faster end is the one with the
// smaller number and no arrow or edge label can say that without ambiguity; an
// instruction can. It reads the same way for every target kind: below the band
// always means do more, above it means do less.
const char *pebble_zone_word(const PebbleProtocolState *state) {
  if (state->target_kind == TGT_NONE) {
    return "";
  }

  PebbleGaugeScale scale;
  pebble_gauge_scale(state, &scale);
  float current = pebble_current_value_for_kind(state);

  if (current >= scale.low && current <= scale.high) {
    return "IN";
  }
  return (current < scale.low) ? "PUSH" : "EASE";
}

// "150-160 bpm" — the zone word is gone from here; the screen colour and the
// bar carry the state, so this line is only the reference you are aiming at.
void pebble_format_band_line(char *out, size_t n,
                             const PebbleProtocolState *state) {
  if (state->target_kind == TGT_NONE) {
    snprintf(out, n, "-");
    return;
  }

  PebbleGaugeScale scale;
  pebble_gauge_scale(state, &scale);

  if (state->target_kind == TGT_POWER) {
    snprintf(out, n, "%d-%d W", (int)scale.low, (int)scale.high);
  } else if (state->target_kind == TGT_HEART_RATE) {
    snprintf(out, n, "%d-%d bpm", (int)scale.low, (int)scale.high);
  } else {
    // The band is held as speed but shown as pace, so the faster edge is the
    // smaller number: print the high speed first to keep the range ascending.
    char fast[16];
    char slow[16];
    pebble_format_pace_from_ms_value_only(fast, sizeof(fast), scale.high,
                                          state->units);
    pebble_format_pace_from_ms_value_only(slow, sizeof(slow), scale.low,
                                          state->units);
    snprintf(out, n, "%s-%s/%s", fast, slow,
             state->units == PEBBLE_UNITS_METRIC ? "km" : "mi");
  }
}

// "2:40" or "450 m" — how much of this step is left, the second thing worth
// reading at speed after the value itself.
void pebble_format_remaining_line(char *out, size_t n,
                                  const PebbleProtocolState *state) {
  if (state->step_remaining_kind == PEBBLE_STEP_REMAINING_SECONDS) {
    pebble_format_elapsed(out, n, state->step_remaining);
    return;
  }
  if (state->step_remaining_kind == PEBBLE_STEP_REMAINING_METERS) {
    uint32_t meters = (uint32_t)state->step_remaining;
    if (state->units == PEBBLE_UNITS_METRIC && meters < METRES_PER_KM) {
      // Metres beat two decimals of a kilometre once the finish is close.
      snprintf(out, n, "%lu m", (unsigned long)meters);
    } else {
      pebble_format_distance(out, n, meters, state->units);
    }
    return;
  }
  snprintf(out, n, "-");
}

// "3/8   HR 148" — both are secondary, so they share a row.
void pebble_format_step_hr_line(char *out, size_t n,
                                const PebbleProtocolState *state) {
  char position[16];
  position[0] = '\0';
  if (state->have_workout_step && state->workout_step_count > 0) {
    snprintf(position, sizeof(position), "%u/%u",
             (unsigned)(state->workout_step + 1),
             (unsigned)state->workout_step_count);
  }

  char heart[16];
  heart[0] = '\0';
  // A heart-rate target already shows the number as the hero above.
  if (state->target_kind != TGT_HEART_RATE) {
    if (state->have_hr && !state->stale) {
      snprintf(heart, sizeof(heart), "HR %u", (unsigned)state->last_hr);
    } else {
      snprintf(heart, sizeof(heart), "HR -");
    }
  }

  if (position[0] != '\0' && heart[0] != '\0') {
    snprintf(out, n, "%s   %s", position, heart);
  } else if (position[0] != '\0') {
    snprintf(out, n, "%s", position);
  } else if (heart[0] != '\0') {
    snprintf(out, n, "%s", heart);
  } else {
    snprintf(out, n, "-");
  }
}
