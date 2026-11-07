"""Find the shared layout context without importing native widget classes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fitness_tracker.core.layout import LayoutFrame, fit_layout

if TYPE_CHECKING:
    from gi.repository import Gtk  # ty:ignore[unresolved-import]


def layout_owner(widget: Gtk.Widget) -> Gtk.Widget | None:
    """Find the ancestor that owns the complete interface's scale and arrangement."""
    current = widget
    while current is not None:
        if isinstance(getattr(current, "layout_frame", None), LayoutFrame):
            return current
        current = current.get_parent()
    return None


def layout_frame(widget: Gtk.Widget) -> LayoutFrame:
    """Return the shared reference allocation, including before the first map."""
    owner = layout_owner(widget)
    if owner is not None:
        return owner.layout_frame
    root = widget.get_root()
    if root is None:
        return fit_layout(360, 720)
    return fit_layout(max(1, root.get_width()), max(1, root.get_height()))
