#ifndef FITNESS_TRACKER_PEBBLE_PROTOCOL_H
#define FITNESS_TRACKER_PEBBLE_PROTOCOL_H

#include <stdint.h>

// Generated from pebble/protocol.toml; do not edit manually.
#define PEBBLE_PROTOCOL_SCALE_TARGET_KIND (-1)

enum {
  KEY_HR = 1,
  KEY_PACE = 2,
  KEY_CADENCE = 3,
  KEY_DISTANCE = 4,
  KEY_UNITS = 6,
  KEY_POWER = 7,
  KEY_TGT_KIND = 8,
  KEY_TGT_LO = 9,
  KEY_TGT_HI = 10,
  KEY_WORKOUT_OUTDOOR = 11,
  KEY_WORKOUT_STEP = 12,
  KEY_SYNC_REQUEST = 13,
};

#define KEY_HR_WIDTH 16
#define KEY_HR_C_TYPE uint16_t
#define KEY_HR_SCALE 1
#define KEY_HR_TUPLE_VALUE(tuple) ((tuple)->value->uint16)
#define KEY_HR_WRITE(iter, value) dict_write_uint16((iter), KEY_HR, (value))

#define KEY_PACE_WIDTH 16
#define KEY_PACE_C_TYPE uint16_t
#define KEY_PACE_SCALE 100
#define KEY_PACE_TUPLE_VALUE(tuple) ((tuple)->value->uint16)
#define KEY_PACE_WRITE(iter, value) dict_write_uint16((iter), KEY_PACE, (value))

#define KEY_CADENCE_WIDTH 16
#define KEY_CADENCE_C_TYPE uint16_t
#define KEY_CADENCE_SCALE 1
#define KEY_CADENCE_TUPLE_VALUE(tuple) ((tuple)->value->uint16)
#define KEY_CADENCE_WRITE(iter, value) dict_write_uint16((iter), KEY_CADENCE, (value))

#define KEY_DISTANCE_WIDTH 32
#define KEY_DISTANCE_C_TYPE uint32_t
#define KEY_DISTANCE_SCALE 1
#define KEY_DISTANCE_TUPLE_VALUE(tuple) ((tuple)->value->uint32)
#define KEY_DISTANCE_WRITE(iter, value) dict_write_uint32((iter), KEY_DISTANCE, (value))

#define KEY_UNITS_WIDTH 8
#define KEY_UNITS_C_TYPE uint8_t
#define KEY_UNITS_SCALE 1
#define KEY_UNITS_TUPLE_VALUE(tuple) ((tuple)->value->uint8)
#define KEY_UNITS_WRITE(iter, value) dict_write_uint8((iter), KEY_UNITS, (value))

#define KEY_POWER_WIDTH 16
#define KEY_POWER_C_TYPE uint16_t
#define KEY_POWER_SCALE 1
#define KEY_POWER_TUPLE_VALUE(tuple) ((tuple)->value->uint16)
#define KEY_POWER_WRITE(iter, value) dict_write_uint16((iter), KEY_POWER, (value))

#define KEY_TGT_KIND_WIDTH 8
#define KEY_TGT_KIND_C_TYPE uint8_t
#define KEY_TGT_KIND_SCALE 1
#define KEY_TGT_KIND_TUPLE_VALUE(tuple) ((tuple)->value->uint8)
#define KEY_TGT_KIND_WRITE(iter, value) dict_write_uint8((iter), KEY_TGT_KIND, (value))

#define KEY_TGT_LO_WIDTH 16
#define KEY_TGT_LO_C_TYPE uint16_t
#define KEY_TGT_LO_SCALE PEBBLE_PROTOCOL_SCALE_TARGET_KIND
#define KEY_TGT_LO_TUPLE_VALUE(tuple) ((tuple)->value->uint16)
#define KEY_TGT_LO_WRITE(iter, value) dict_write_uint16((iter), KEY_TGT_LO, (value))

#define KEY_TGT_HI_WIDTH 16
#define KEY_TGT_HI_C_TYPE uint16_t
#define KEY_TGT_HI_SCALE PEBBLE_PROTOCOL_SCALE_TARGET_KIND
#define KEY_TGT_HI_TUPLE_VALUE(tuple) ((tuple)->value->uint16)
#define KEY_TGT_HI_WRITE(iter, value) dict_write_uint16((iter), KEY_TGT_HI, (value))

#define KEY_WORKOUT_OUTDOOR_WIDTH 8
#define KEY_WORKOUT_OUTDOOR_C_TYPE uint8_t
#define KEY_WORKOUT_OUTDOOR_SCALE 1
#define KEY_WORKOUT_OUTDOOR_TUPLE_VALUE(tuple) ((tuple)->value->uint8)
#define KEY_WORKOUT_OUTDOOR_WRITE(iter, value) dict_write_uint8((iter), KEY_WORKOUT_OUTDOOR, (value))

#define KEY_WORKOUT_STEP_WIDTH 16
#define KEY_WORKOUT_STEP_C_TYPE uint16_t
#define KEY_WORKOUT_STEP_SCALE 1
#define KEY_WORKOUT_STEP_TUPLE_VALUE(tuple) ((tuple)->value->uint16)
#define KEY_WORKOUT_STEP_WRITE(iter, value) dict_write_uint16((iter), KEY_WORKOUT_STEP, (value))

#define KEY_SYNC_REQUEST_WIDTH 8
#define KEY_SYNC_REQUEST_C_TYPE uint8_t
#define KEY_SYNC_REQUEST_SCALE 1
#define KEY_SYNC_REQUEST_TUPLE_VALUE(tuple) ((tuple)->value->uint8)
#define KEY_SYNC_REQUEST_WRITE(iter, value) dict_write_uint8((iter), KEY_SYNC_REQUEST, (value))

typedef enum {
  TGT_NONE = 0,
  TGT_POWER = 1,
  TGT_PACE = 2,
  TGT_HEART_RATE = 3,
} PebbleTargetKind;

#define TGT_NONE_SCALE 1
#define TGT_POWER_SCALE 1
#define TGT_PACE_SCALE 100
#define TGT_HEART_RATE_SCALE 1

#endif
