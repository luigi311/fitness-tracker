"""Shared allocation, event-loop, and screenshot helpers for opt-in GTK tests."""

import os
import time
from pathlib import Path

import gi

gi.require_versions({"Gtk": "4.0", "Graphene": "1.0"})
from gi.repository import GLib, Graphene, Gtk  # noqa: E402  # ty:ignore[unresolved-import]


def settle():
    """Process GTK work long enough for allocation and queued redraws to finish."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + 0.12
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(may_block=False)
        time.sleep(0.005)


def assert_inside(widget, container):
    """Require a mapped widget to fit within its visible container."""
    success, bounds = widget.compute_bounds(container)
    assert success
    assert widget.get_mapped()
    assert bounds.get_x() >= -1
    assert bounds.get_y() >= -1
    assert bounds.get_x() + bounds.get_width() <= container.get_width() + 1
    assert bounds.get_y() + bounds.get_height() <= container.get_height() + 1


def descendants(widget, kind):
    """Yield widgets of a chosen type throughout a widget tree."""
    if isinstance(widget, kind):
        yield widget
    for child in widget:
        yield from descendants(child, kind)


def descendant_labels(widget):
    """Yield all labels in a widget tree for text and visibility checks."""
    yield from descendants(widget, Gtk.Label)


def save_screenshot(window, name):
    """Save a painted frame when screenshot output is explicitly enabled."""
    directory = os.environ.get("FITNESS_TRACKER_SCREENSHOTS")
    if not directory:
        return
    destination = Path(directory)
    destination.mkdir(parents=True, exist_ok=True)
    node = None
    for _ in range(5):
        snapshot = Gtk.Snapshot()
        Gtk.WidgetPaintable.new(window).snapshot(snapshot, window.get_width(), window.get_height())
        node = snapshot.to_node()
        if node is not None:
            break
        window.queue_draw()
        settle()
    assert node is not None, "Window did not produce a painted frame"
    bounds = Graphene.Rect().init(0, 0, window.get_width(), window.get_height())
    texture = window.get_renderer().render_texture(node, bounds)
    assert texture.save_to_png(str(destination / f"{name}.png"))
