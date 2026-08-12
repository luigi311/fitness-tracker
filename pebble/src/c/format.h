#ifndef FITNESS_TRACKER_PEBBLE_FORMAT_H
#define FITNESS_TRACKER_PEBBLE_FORMAT_H

#include <pebble.h>
#include <stddef.h>
#include <stdint.h>

#include "protocol.h"

void pebble_format_distance(char *out, size_t n, KEY_DISTANCE_C_TYPE meters,
                            PebbleUnits units);
void pebble_format_pace(char *out, size_t n, KEY_PACE_C_TYPE speed_ms_x100,
                        PebbleUnits units);
void pebble_format_pace_value_only(char *out, size_t n,
                                   const PebbleProtocolState *state);
void pebble_format_pace_from_ms_value_only(char *out, size_t n, float ms,
                                           PebbleUnits units);

float pebble_current_value_for_kind(const PebbleProtocolState *state);
float pebble_target_value(const PebbleProtocolState *state,
                          KEY_TGT_LO_C_TYPE value);
void pebble_gauge_texts(char *current_line, size_t current_n,
                        char *target_line, size_t target_n,
                        char *hr_line, size_t hr_n,
                        const PebbleProtocolState *state);
GColor pebble_zone_color(const PebbleProtocolState *state);
const char *pebble_zone_word(const PebbleProtocolState *state, GColor color);

#endif
