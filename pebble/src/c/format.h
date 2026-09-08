#ifndef FITNESS_TRACKER_PEBBLE_FORMAT_H
#define FITNESS_TRACKER_PEBBLE_FORMAT_H

#include <pebble.h>
#include <stddef.h>
#include <stdint.h>

#include "protocol.h"

typedef enum {
  PEBBLE_ZONE_IN = 0,
  PEBBLE_ZONE_NEAR = 1,
  PEBBLE_ZONE_OUT = 2,
} PebbleZone;

// The window the gauge draws, derived from the target band so the band always
// occupies the same readable slice of the arc.
typedef struct {
  float low;   // band lower bound, ordered
  float high;  // band upper bound, ordered
  float min;   // gauge domain minimum
  float max;   // gauge domain maximum
  float near;  // distance outside the band still counted as NEAR
} PebbleGaugeScale;

void pebble_format_distance(char *out, size_t n, KEY_DISTANCE_C_TYPE meters,
                            PebbleUnits units);
void pebble_format_pace_value_only(char *out, size_t n,
                                   const PebbleProtocolState *state);
void pebble_format_pace_from_ms_value_only(char *out, size_t n, float ms,
                                           PebbleUnits units);
void pebble_format_elapsed(char *out, size_t n, KEY_ELAPSED_C_TYPE seconds);

float pebble_current_value_for_kind(const PebbleProtocolState *state);
float pebble_target_value(const PebbleProtocolState *state,
                          KEY_TGT_LO_C_TYPE value);

void pebble_gauge_scale(const PebbleProtocolState *state,
                        PebbleGaugeScale *out);
float pebble_gauge_fraction(const PebbleGaugeScale *scale, float value);

void pebble_format_band_line(char *out, size_t n,
                             const PebbleProtocolState *state);
void pebble_format_remaining_line(char *out, size_t n,
                                  const PebbleProtocolState *state);
void pebble_format_step_hr_line(char *out, size_t n,
                                const PebbleProtocolState *state);

PebbleZone pebble_zone(const PebbleProtocolState *state);
GColor pebble_zone_color(const PebbleProtocolState *state);
const char *pebble_zone_word(const PebbleProtocolState *state);

#endif
