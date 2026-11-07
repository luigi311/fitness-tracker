"""Aspect-only interface arrangements and their reference drawing sizes."""

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite

PORTRAIT_ASPECT = 0.8
LANDSCAPE_ASPECT = 1.35
_ASPECT_HYSTERESIS = 0.015


class LayoutFamily(StrEnum):
    """Panel arrangements shared by every display size and zoom setting."""

    PORTRAIT = "portrait"
    SQUARE = "square"
    LANDSCAPE = "landscape"


@dataclass(frozen=True)
class LayoutFrame:
    """Reference allocation and uniform transform for the complete interface."""

    family: LayoutFamily
    width: int
    height: int
    scale: float


def select_layout(
    width: float,
    height: float,
    previous: LayoutFamily | None = None,
) -> LayoutFamily:
    """Choose placement from aspect alone, retaining small rounding fluctuations."""
    if not all(isfinite(value) and value > 0 for value in (width, height)):
        message = "Layout dimensions must be finite and positive"
        raise ValueError(message)
    aspect = width / height
    if previous is LayoutFamily.PORTRAIT and aspect < PORTRAIT_ASPECT + _ASPECT_HYSTERESIS:
        return previous
    if previous is LayoutFamily.SQUARE and (
        PORTRAIT_ASPECT - _ASPECT_HYSTERESIS <= aspect <= LANDSCAPE_ASPECT + _ASPECT_HYSTERESIS
    ):
        return previous
    if previous is LayoutFamily.LANDSCAPE and aspect > LANDSCAPE_ASPECT - _ASPECT_HYSTERESIS:
        return previous
    if aspect < PORTRAIT_ASPECT:
        return LayoutFamily.PORTRAIT
    if aspect <= LANDSCAPE_ASPECT:
        return LayoutFamily.SQUARE
    return LayoutFamily.LANDSCAPE


def fit_layout(
    width: float,
    height: float,
    previous: LayoutFamily | None = None,
) -> LayoutFrame:
    """Fit a fixed arrangement uniformly so proportional zoom preserves its geometry."""
    family = select_layout(width, height, previous)
    if family is LayoutFamily.PORTRAIT:
        reference_width, reference_height = 360, 720
    elif family is LayoutFamily.SQUARE:
        reference_width, reference_height = (540, 620) if width < height else (620, 540)
    else:
        reference_width, reference_height = 720, 360
    scale = min(width / reference_width, height / reference_height)
    return LayoutFrame(family, round(width / scale), round(height / scale), scale)
