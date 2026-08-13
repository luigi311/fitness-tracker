"""Cached heart-rate zones and validated chart colours."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

RGB = tuple[float, float, float]
ZoneThresholds = Mapping[str, tuple[float, float]]

_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def _validated_hex_color(value: str) -> str:
    if not _HEX_COLOR.fullmatch(value):
        msg = f"Invalid chart colour: {value!r}; expected #RRGGBB"
        raise ValueError(msg)
    return value


class ChartTheme(BaseModel):
    """Validated colours shared by all matplotlib surfaces."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    background: str
    foreground: str
    grid: str
    zone_colors: tuple[str, ...] = Field(min_length=1)

    @field_validator("background", "foreground", "grid")
    @classmethod
    def _validate_color(cls, value: str) -> str:
        return _validated_hex_color(value)

    @field_validator("zone_colors")
    @classmethod
    def _validate_zone_colors(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            _validated_hex_color(value)
        return values

    @property
    def zone_rgb(self) -> tuple[RGB, ...]:
        """Return normalized RGB values derived from the current zone colours."""
        return tuple(hex_to_rgb(value) for value in self.zone_colors)

    @classmethod
    def for_style(cls, is_dark: bool) -> ChartTheme:  # noqa: FBT001
        """Build the application chart palette for a light or dark style."""
        if is_dark:
            return cls(
                background="#2e3436",
                foreground="#ffffff",
                grid="#555555",
                zone_colors=_ZONE_COLORS,
            )
        return cls(
            background="#f9f9f9",
            foreground="#000000",
            grid="#cccccc",
            zone_colors=_ZONE_COLORS,
        )


def hex_to_rgb(value: str) -> RGB:
    """Convert a validated six-digit color into normalized RGB channels."""
    color = value.removeprefix("#")
    return (
        int(color[0:2], 16) / 255.0,
        int(color[2:4], 16) / 255.0,
        int(color[4:6], 16) / 255.0,
    )


_ZONE_COLORS: tuple[str, ...] = (
    "#28b0ff",
    "#a0e0a0",
    "#edf767",
    "#ffac2f",
    "#ff4343",
)


@dataclass(frozen=True, slots=True)
class HeartRateZones:
    """Calculate Karvonen heart-rate thresholds once per settings snapshot."""

    _INTENSITIES: ClassVar[tuple[tuple[str, float, float], ...]] = (
        ("Zone 1", 0.50, 0.60),
        ("Zone 2", 0.60, 0.70),
        ("Zone 3", 0.70, 0.80),
        ("Zone 4", 0.80, 0.90),
        ("Zone 5", 0.90, 1.00),
    )

    resting_hr: float
    max_hr: float
    _thresholds: ZoneThresholds = field(init=False, repr=False, compare=False, hash=False)

    def __post_init__(self) -> None:
        if self.max_hr <= self.resting_hr:
            message = "max_hr must be greater than resting_hr"
            raise ValueError(message)
        hr_range = self.max_hr - self.resting_hr
        thresholds = {
            name: (
                self.resting_hr + hr_range * low_pct,
                self.resting_hr + hr_range * high_pct,
            )
            for name, low_pct, high_pct in self._INTENSITIES
        }
        object.__setattr__(self, "_thresholds", MappingProxyType(thresholds))

    @property
    def thresholds(self) -> ZoneThresholds:
        """Return the immutable name-to-threshold mapping."""
        return self._thresholds

    def for_heart_rate(self, heart_rate: float) -> tuple[str, float, float, int]:
        """Return the matching zone name, bounds, and zero-based colour index."""
        items = tuple(self._thresholds.items())
        for index, (name, (low, high)) in enumerate(items):
            if low <= heart_rate < high:
                return name, low, high, index
        if heart_rate < items[0][1][0]:
            name, (low, high) = items[0]
            return name, low, high, 0
        name, (low, high) = items[-1]
        return name, low, high, len(items) - 1
