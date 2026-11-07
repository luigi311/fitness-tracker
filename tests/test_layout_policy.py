"""Aspect selection and whole-interface scaling are independent of device zoom."""

from math import inf, nan

import pytest
from fitness_tracker.core.layout import LayoutFamily, fit_layout, select_layout


@pytest.mark.parametrize(
    ("width", "height", "family"),
    [
        (360, 720, LayoutFamily.PORTRAIT),
        (720, 360, LayoutFamily.LANDSCAPE),
        (540, 620, LayoutFamily.SQUARE),
        (620, 540, LayoutFamily.SQUARE),
        (800, 1280, LayoutFamily.PORTRAIT),
        (1280, 800, LayoutFamily.LANDSCAPE),
        (800, 1000, LayoutFamily.SQUARE),
        (1350, 1000, LayoutFamily.SQUARE),
        (1351, 1000, LayoutFamily.LANDSCAPE),
    ],
)
def test_layout_selection_depends_only_on_aspect(width, height, family):
    """Use the same arrangement for tiny and large windows of identical shape."""
    for factor in (0.25, 0.5, 1, 2, 4):
        assert select_layout(width * factor, height * factor) is family


@pytest.mark.parametrize(
    ("width", "height"),
    [(720, 1440), (1440, 720), (1080, 1240), (1240, 1080), (800, 1280), (1600, 900)],
)
def test_all_zoom_percentages_keep_reference_geometry(width, height):
    """Preserve layout and physical component sizing through every 100-300% zoom."""
    reference = fit_layout(width, height)
    previous = reference.family
    for percent in range(100, 301):
        zoom = percent / 100
        frame = fit_layout(round(width / zoom), round(height / zoom), previous)
        assert frame.family is reference.family
        assert frame.width == pytest.approx(reference.width, abs=2)
        assert frame.height == pytest.approx(reference.height, abs=2)
        assert frame.scale * zoom == pytest.approx(reference.scale, rel=0.01)
        previous = frame.family


@pytest.mark.parametrize("boundary", [0.8, 1.35])
def test_rounding_near_a_boundary_keeps_the_previous_family(boundary):
    """Avoid moving panels for small allocation-rounding changes near a threshold."""
    previous = select_layout(boundary * 1000, 1000)
    for error in (-1, 0, 1):
        assert select_layout(boundary * 1000 + error, 1000, previous) is previous
    assert select_layout(500, 1000, previous) is LayoutFamily.PORTRAIT
    assert select_layout(2000, 1000, previous) is LayoutFamily.LANDSCAPE


@pytest.mark.parametrize(
    ("width", "height"),
    [(0, 1), (1, 0), (-1, 1), (1, -1), (inf, 1), (1, nan)],
)
def test_invalid_layout_dimensions_are_rejected(width, height):
    """Reject allocations that cannot produce a finite, positive GTK transform."""
    with pytest.raises(ValueError, match="finite and positive"):
        fit_layout(width, height)
