"""Open adaptive preview with one top-level application page attached."""

from __future__ import annotations

import argparse

import gi

gi.require_versions({"Adw": "1", "Gio": "2.0", "GLib": "2.0", "Gtk": "4.0"})
from fitness_tracker.ui.app import FitnessAppUI  # noqa: E402
from gi.repository import Gio, GLib, Gtk  # noqa: E402  # ty:ignore[unresolved-import]


def _non_negative_int(value: str) -> int:
    """Parse an integer duration that is safe to pass to GLib timeouts."""
    duration = int(value)
    if duration < 0:
        message = "duration must be non-negative"
        raise argparse.ArgumentTypeError(message)
    return duration


class AdaptiveWidthDiagnosticApp(FitnessAppUI):
    """Run the real adaptive preview with all but one application page removed."""

    def __init__(self, page_name: str, preview_seconds: int) -> None:
        super().__init__(test_mode=True)
        self._page_name = page_name
        self._preview_seconds = preview_seconds

    def do_activate(self) -> None:
        """Build the UI, isolate one page, and open adaptive preview briefly."""
        if self.window is None:
            self._build_ui()
            self._isolate_page()

        self.window.present()
        GLib.idle_add(self._open_preview)

    def _isolate_page(self) -> None:
        if self._page_name == "all":
            return

        pages = self.stack.get_pages()
        selected_child = None
        for index in range(pages.get_n_items()):
            page = pages.get_item(index)
            if page.get_name() == self._page_name:
                selected_child = page.get_child()

        if selected_child is None:
            message = f"Unknown application page: {self._page_name}"
            raise ValueError(message)

        for index in range(pages.get_n_items() - 1, -1, -1):
            child = pages.get_item(index).get_child()
            if child is not selected_child:
                self.stack.remove(child)
        self.stack.set_visible_child(selected_child)

    def _open_preview(self) -> bool:
        print(f"Adaptive preview page: {self._page_name}", flush=True)
        self.window.set_adaptive_preview(True)
        GLib.timeout_add(250, self._report_widths)
        GLib.timeout_add_seconds(self._preview_seconds, self._close_preview)
        return False

    def _report_widths(self) -> bool:
        """Print the widest descendants after adaptive preview allocates them."""
        rows: list[tuple[int, int, int, str, str]] = []

        def visit(widget: Gtk.Widget, path: str) -> None:
            minimum, natural, _minimum_baseline, _natural_baseline = widget.measure(
                Gtk.Orientation.HORIZONTAL,
                -1,
            )
            detail = ""
            if isinstance(widget, Gtk.Label):
                detail = widget.get_text()
            elif isinstance(widget, Gtk.Button):
                detail = widget.get_label() or ""
            rows.append((minimum, natural, widget.get_width(), path, detail))

            child = widget.get_first_child()
            index = 0
            while child is not None:
                visit(child, f"{path}/{type(child).__name__}[{index}]")
                child = child.get_next_sibling()
                index += 1

        visit(self.toast_overlay, type(self.toast_overlay).__name__)
        print("minimum natural allocated widget", flush=True)
        for minimum, natural, allocated, path, detail in sorted(rows, reverse=True)[:25]:
            print(
                f"{minimum:7} {natural:7} {allocated:9} {path} {detail!r}",
                flush=True,
            )
        return False

    def _close_preview(self) -> bool:
        self.window.set_adaptive_preview(False)
        self.window.destroy()
        self.quit()
        return False


def main() -> None:
    """Parse the page selection and run the diagnostic application."""
    parser = argparse.ArgumentParser()
    parser.add_argument("page", choices=("all", "tracker", "history", "settings"))
    parser.add_argument("--seconds", type=_non_negative_int, default=2)
    args = parser.parse_args()

    app = AdaptiveWidthDiagnosticApp(args.page, args.seconds)
    app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
    raise SystemExit(app.run([]))


if __name__ == "__main__":
    main()
