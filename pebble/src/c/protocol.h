#ifndef FITNESS_TRACKER_PEBBLE_PROTOCOL_STATE_H
#define FITNESS_TRACKER_PEBBLE_PROTOCOL_STATE_H

#include <stdbool.h>
#include <stdint.h>

#include "generated_protocol.h"

typedef enum {
  PEBBLE_UNITS_METRIC = 0,
  PEBBLE_UNITS_IMPERIAL = 1,
} PebbleUnits;

// Mirrors STEP_REMAINING_* in src/pebble_bridge/pebble_bridge.py.
typedef enum {
  PEBBLE_STEP_REMAINING_NONE = 0,
  PEBBLE_STEP_REMAINING_SECONDS = 1,
  PEBBLE_STEP_REMAINING_METERS = 2,
} PebbleStepRemainingKind;

typedef struct {
  PebbleUnits units;
  bool units_changed;

  bool have_hr;
  bool have_pace;
  bool have_cad;
  bool have_dist;
  bool have_power;
  KEY_HR_C_TYPE last_hr;
  KEY_PACE_C_TYPE last_pace_x100;
  KEY_CADENCE_C_TYPE last_cad;
  KEY_POWER_C_TYPE last_power;
  KEY_DISTANCE_C_TYPE last_dist_m;

  bool have_elapsed;
  KEY_ELAPSED_C_TYPE elapsed_s;

  PebbleTargetKind target_kind;
  KEY_TGT_LO_C_TYPE target_lo;
  KEY_TGT_HI_C_TYPE target_hi;
  bool workout_outdoor;
  bool have_workout_step;
  KEY_WORKOUT_STEP_C_TYPE workout_step;
  KEY_WORKOUT_STEP_COUNT_C_TYPE workout_step_count;
  PebbleStepRemainingKind step_remaining_kind;
  KEY_STEP_REMAINING_C_TYPE step_remaining;
  bool step_changed;
  bool workout_ended;

  // True once the phone has been quiet for longer than a session update can
  // plausibly take: every readout derived from a sensor is then unknown, not
  // merely unchanged.
  bool stale;
} PebbleProtocolState;

typedef void (*PebbleProtocolUpdate)(void *context);

void pebble_protocol_init(PebbleProtocolState *state);
void pebble_protocol_start(PebbleProtocolState *state,
                           PebbleProtocolUpdate update,
                           void *context);
void pebble_protocol_stop(void);

#endif
