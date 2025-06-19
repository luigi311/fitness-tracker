import threading
from configparser import ConfigParser
from os.path import expanduser
from pathlib import Path
from typing import TYPE_CHECKING

import gi

from fitness_tracker.recorder import Recorder
from fitness_tracker.ui_history import HistoryPageUI
from fitness_tracker.ui_settings import SettingsPageUI
from fitness_tracker.ui_tracker import TrackerPageUI

gi.require_versions({"Gtk": "4.0", "Adw": "1"})
from gi.repository import Adw  # noqa: E402

if TYPE_CHECKING:
    import datetime

Adw.init()

# Determine dark-mode status and define colors
_style_manager = Adw.StyleManager.get_default()
_IS_DARK = _style_manager.get_dark()


class FitnessAppUI(Adw.Application):
    def __init__(self):
        super().__init__(application_id="io.Luigi311.Fitness")

        if _IS_DARK:
            self.DARK_BG = "#2e3436"
            self.DARK_FG = "#ffffff"
            self.DARK_GRID = "#555555"
        else:
            self.DARK_BG = "#f9f9f9"
            self.DARK_FG = "#000000"
            self.DARK_GRID = "#cccccc"

        self.ZONE_COLORS = [
            "#28b0ff",  # Zone 1
            "#a0e0a0",  # Zone 2
            "#edf767",  # Zone 3
            "#ffac2f",  # Zone 4
            "#ff4343",  # Zone 5
        ]

        self.window = None
        self.recorder: Recorder | None = None
        self._times: list[float] = []
        self._bpms: list[int] = []
        self._line = None

        # keep track of which activities the user has ticked
        self.selected_activities: set[int] = set()
        # store each activity’s start-time so we can label the legend
        self.activity_start_times: dict[int, datetime.datetime] = {}

        self.history_filter = "week"

        # Set up application directory
        app_dir = Path(expanduser("~/.local/share/io.Luigi311.Fitness"))
        app_dir.mkdir(parents=True, exist_ok=True)
        self.database = app_dir / "fitness.db"
        self.config_file = app_dir / "config.ini"

        # load existing configuration
        self.cfg = ConfigParser()
        self.database_dsn = ""

        self.device_name = ""
        self.device_address = ""

        self.resting_hr: int = 60
        self.max_hr: int = 180

        if self.config_file.exists():
            self.cfg.read(self.config_file)
            self.database_dsn = self.cfg.get("server", "database_dsn", fallback="")

            self.device_name = self.cfg.get("tracker", "device_name", fallback="")
            self.device_address = self.cfg.get("tracker", "device_address", fallback="")

            self.resting_hr = self.cfg.getint("personal", "resting_hr", fallback=60)
            self.max_hr = self.cfg.getint("personal", "max_hr", fallback=180)

    def show_toast(self, message: str):
        print(message)
        # Create and display a toast on our overlay
        toast = Adw.Toast.new(message)
        self.toast_overlay.add_toast(toast)

    def do_activate(self):
        if not self.window:
            self._build_ui()
            self.recorder = Recorder(
                on_bpm_update=self.tracker.on_bpm,
                database_url=f"sqlite:///{self.database}",
                device_name=self.device_name,
                on_error=self.show_toast,
                device_address=self.device_address or None,
            )
            self.recorder.start()
            # Load history after recorder is initialized
            threading.Thread(target=self.history.load_history, daemon=True).start()
        self.window.present()

    def _build_ui(self):
        self.window = Adw.ApplicationWindow(application=self)
        self.window.set_title("Fitness Tracker")
        self.window.set_default_size(640, 520)
        self.toast_overlay = Adw.ToastOverlay()
        self.window.set_content(self.toast_overlay)

        toolbar_view = Adw.ToolbarView()
        self.toast_overlay.set_child(toolbar_view)

        # Create ViewStack
        self.stack = Adw.ViewStack()
        self.stack.set_vexpand(True)

        # instead of calling your own private _build_* methods, do:
        self.tracker = TrackerPageUI(self)
        self.history = HistoryPageUI(self)
        self.settings = SettingsPageUI(self)

        tracker = self.tracker.build_page()
        history = self.history.build_page()
        settings = self.settings.build_page()

        self.stack.add_titled(tracker, "tracker", "Tracker").set_icon_name(
            "media-playback-start-symbolic",
        )
        self.stack.add_titled(history, "history", "History").set_icon_name("view-list-symbolic")
        self.stack.add_titled(settings, "settings", "Settings").set_icon_name(
            "emblem-system-symbolic",
        )

        switcher_bar = Adw.ViewSwitcherBar()
        switcher_bar.set_stack(self.stack)
        switcher_bar.set_reveal(True)

        toolbar_view.set_content(self.stack)
        toolbar_view.add_bottom_bar(switcher_bar)

    def calculate_hr_zones(self):
        """Returns a mapping of zone names to (lower_bpm, upper_bpm) using Karvonen formula."""
        hr_range = self.max_hr - self.resting_hr
        intensities = [
            ("Zone 1", 0.50, 0.60),
            ("Zone 2", 0.60, 0.70),
            ("Zone 3", 0.70, 0.80),
            ("Zone 4", 0.80, 0.90),
            ("Zone 5", 0.90, 1.00),
        ]
        thresholds = {}
        for name, low_pct, high_pct in intensities:
            low = self.resting_hr + hr_range * low_pct
            high = self.resting_hr + hr_range * high_pct
            thresholds[name] = (low, high)
        return thresholds

    def draw_zones(self, ax):
        """Draw horizontal colored bands on the given Axes for each HR zone."""
        zones = self.calculate_hr_zones()
        colors = self.ZONE_COLORS
        alpha = 0.25
        for (_, (low, high)), color in zip(zones.items(), colors):
            ax.axhspan(low, high, facecolor=color, alpha=alpha)
