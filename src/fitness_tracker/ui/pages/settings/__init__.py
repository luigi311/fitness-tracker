"""Settings page and its composable preference sections."""

from fitness_tracker.ui.pages.settings.page import SettingsPageUI
from fitness_tracker.ui.pages.settings.sections import (
    NONE_LABEL,
    TRAINER_SUPPLIED_HR_LABEL,
    SensorRowSpec,
    SensorRowWidgets,
    SensorSection,
)

__all__ = [
    "NONE_LABEL",
    "TRAINER_SUPPLIED_HR_LABEL",
    "SensorRowSpec",
    "SensorRowWidgets",
    "SensorSection",
    "SettingsPageUI",
]
