"""Generated from pebble/protocol.toml; do not edit manually."""

from typing import Final

KEY_HR: Final = 1
KEY_PACE: Final = 2
KEY_CADENCE: Final = 3
KEY_DISTANCE: Final = 4
KEY_UNITS: Final = 6
KEY_POWER: Final = 7
KEY_TGT_KIND: Final = 8
KEY_TGT_LO: Final = 9
KEY_TGT_HI: Final = 10
KEY_WORKOUT_OUTDOOR: Final = 11
KEY_WORKOUT_STEP: Final = 12
KEY_SYNC_REQUEST: Final = 13

TGT_NONE: Final = 0
TGT_POWER: Final = 1
TGT_PACE: Final = 2
TGT_HEART_RATE: Final = 3

KEY_HR_SCALE: Final = 1
KEY_PACE_SCALE: Final = 100
KEY_CADENCE_SCALE: Final = 1
KEY_DISTANCE_SCALE: Final = 1
KEY_UNITS_SCALE: Final = 1
KEY_POWER_SCALE: Final = 1
KEY_TGT_KIND_SCALE: Final = 1
KEY_TGT_LO_SCALE: Final = "target_kind"
KEY_TGT_HI_SCALE: Final = "target_kind"
KEY_WORKOUT_OUTDOOR_SCALE: Final = 1
KEY_WORKOUT_STEP_SCALE: Final = 1
KEY_SYNC_REQUEST_SCALE: Final = 1

KEY_WIDTHS: Final = {
    KEY_HR: 16,
    KEY_PACE: 16,
    KEY_CADENCE: 16,
    KEY_DISTANCE: 32,
    KEY_UNITS: 8,
    KEY_POWER: 16,
    KEY_TGT_KIND: 8,
    KEY_TGT_LO: 16,
    KEY_TGT_HI: 16,
    KEY_WORKOUT_OUTDOOR: 8,
    KEY_WORKOUT_STEP: 16,
    KEY_SYNC_REQUEST: 8,
}

KEY_SCALES: Final = {
    KEY_HR: 1,
    KEY_PACE: 100,
    KEY_CADENCE: 1,
    KEY_DISTANCE: 1,
    KEY_UNITS: 1,
    KEY_POWER: 1,
    KEY_TGT_KIND: 1,
    KEY_TGT_LO: "target_kind",
    KEY_TGT_HI: "target_kind",
    KEY_WORKOUT_OUTDOOR: 1,
    KEY_WORKOUT_STEP: 1,
    KEY_SYNC_REQUEST: 1,
}

TARGET_KIND_SCALE: Final = {
    TGT_NONE: 1,
    TGT_POWER: 1,
    TGT_PACE: 100,
    TGT_HEART_RATE: 1,
}
