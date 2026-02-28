from matplotlib.artist import get
import contextlib
import socket
from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import gi
from bleaksport import MachineType
from loguru import logger
from pebble_bridge import PebbleBridge
from xdg_base_dirs import (
    xdg_config_home,
    xdg_data_home,
)

from fitness_tracker.database import SportTypesEnum
from fitness_tracker.recorder import Recorder
from fitness_tracker.ui_history import HistoryPageUI
from fitness_tracker.ui_settings import SettingsPageUI
from fitness_tracker.ui_tracker import TrackerPageUI

gi.require_versions({"Gtk": "4.0", "Adw": "1"})

from gi.repository import Adw, Gdk, Gtk  # noqa: E402

if TYPE_CHECKING:
    import datetime

Adw.init()

# Determine dark-mode status and define colors
_style_manager = Adw.StyleManager.get_default()
_IS_DARK = _style_manager.get_dark()

_PROV = Gtk.CssProvider()
_PROV.load_from_data(b"""
.pill { padding: 4px 10px; border-radius: 9999px; color: white; }
.pill-in   { background-color: rgba(51,204,77,0.95); }   /* #33CC4D-ish */
.pill-near { background-color: rgba(242,191,51,0.95); }  /* amber */
.pill-low  { background-color: rgba(242,140,51,0.95); }  /* orange */
.pill-high { background-color: rgba(242,89,89,0.95); }   /* red */
""")
Gtk.StyleContext.add_provider_for_display(
    Gdk.Display.get_default(), _PROV, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
)


@dataclass(frozen=True)
class SensorProfile:
    # Common HRM (optional)
    hr_name: str = ""
    hr_address: str = ""

    # “speed/cadence/power” addresses (meaning depends on profile)
    speed_name: str = ""
    speed_address: str = ""
    cadence_name: str = ""
    cadence_address: str = ""
    power_name: str = ""
    power_address: str = ""

    # Trainer-only (FTMS)
    trainer_name: str = ""
    trainer_address: str = ""
    trainer_machine_type: MachineType | None = None

class FitnessAppUI(Adw.Application):
    def __init__(self, test_mode: bool = False):
        super().__init__(application_id="io.Luigi311.Fitness")
        self.test_mode = test_mode

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
        data_dir = Path(xdg_data_home()) / "fitness_tracker"
        config_dir = Path(xdg_config_home()) / "fitness_tracker"
        data_dir.mkdir(parents=True, exist_ok=True)
        config_dir.mkdir(parents=True, exist_ok=True)

        self.database = data_dir / "fitness.db"
        self.config_file = config_dir / "config.ini"
        self.workouts_running_dir = data_dir / "workouts" / "running"
        self.workouts_running_dir.mkdir(parents=True, exist_ok=True)
        self.workouts_cycling_dir = data_dir / "workouts" / "cycling"
        self.workouts_cycling_dir.mkdir(parents=True, exist_ok=True)

        # load existing configuration
        self.cfg = ConfigParser()
        self.database_dsn = ""

        # Granular sensors (each may point to the same physical device)
        # Running sensors
        self.hr_name = ""
        self.hr_address = ""
        self.speed_name = ""
        self.speed_address = ""
        self.cadence_name = ""
        self.cadence_address = ""
        self.power_name = ""
        self.power_address = ""

        # Cycling sensors
        self.cycling_hr_name = ""
        self.cycling_hr_address = ""
        self.cycling_speed_name = ""
        self.cycling_speed_address = ""
        self.cycling_cadence_name = ""
        self.cycling_cadence_address = ""
        self.cycling_power_name = ""
        self.cycling_power_address = ""

        # Trainer (FTMS) configuration (separated)
        self.trainer_running_hr_address: str = ""
        self.trainer_running_hr_name: str = ""
        self.trainer_running_name: str = ""
        self.trainer_running_address: str = ""
        self.trainer_running_machine_type: MachineType | None = None

        self.trainer_cycling_hr_name: str = ""
        self.trainer_cycling_hr_address: str = ""
        self.trainer_cycling_name: str = ""
        self.trainer_cycling_address: str = ""
        self.trainer_cycling_machine_type: MachineType | None = None

        self.resting_hr: int = 60
        self.max_hr: int = 180
        self.ftp_watts: int = 150

        # Pebble Settings
        self.pebble_enable = True
        self.pebble_use_emulator = True
        self.pebble_uuid = "f4fcdac7-f58e-4d22-96bd-48cf98e25d09"  # UUID of pebble app
        self.pebble_name = None
        self.pebble_address = None
        self.pebble_bridge = None
        self.pebble_port = 47527

        # Intervals.icu config
        self.icu_athlete_id: str = ""
        self.icu_api_key: str = ""

        if self.config_file.exists():
            self.cfg.read(self.config_file)
            self.database_dsn = self.cfg.get("server", "database_dsn", fallback="")

            # # Sensors Running
            self.hr_name = self.cfg.get("sensors_running", "hr_name", fallback="")
            self.hr_address = self.cfg.get("sensors_running", "hr_address", fallback="")
            self.speed_name = self.cfg.get("sensors_running", "speed_name", fallback="")
            self.speed_address = self.cfg.get("sensors_running", "speed_address", fallback="")
            self.cadence_name = self.cfg.get("sensors_running", "cadence_name", fallback="")
            self.cadence_address = self.cfg.get("sensors_running", "cadence_address", fallback="")
            self.power_name = self.cfg.get("sensors_running", "power_name", fallback="")
            self.power_address = self.cfg.get("sensors_running", "power_address", fallback="")

            # Sensors Cycling
            self.cycling_hr_name = self.cfg.get("sensors_cycling", "hr_name", fallback="")
            self.cycling_hr_address = self.cfg.get("sensors_cycling", "hr_address", fallback="")
            self.cycling_speed_name = self.cfg.get("sensors_cycling", "speed_name", fallback="")
            self.cycling_speed_address = self.cfg.get("sensors_cycling", "speed_address", fallback="")
            self.cycling_cadence_name = self.cfg.get("sensors_cycling", "cadence_name", fallback="")
            self.cycling_cadence_address = self.cfg.get("sensors_cycling", "cadence_address", fallback="")
            self.cycling_power_name = self.cfg.get("sensors_cycling", "power_name", fallback="")
            self.cycling_power_address = self.cfg.get("sensors_cycling", "power_address", fallback="")

            # Trainer (FTMS)
            self.trainer_running_hr_name = self.cfg.get("sensors_trainer_running", "hr_name", fallback="")
            self.trainer_running_hr_address = self.cfg.get("sensors_trainer_running", "hr_address", fallback="")
            self.trainer_running_name = self.cfg.get("sensors_trainer_running", "trainer_name", fallback="")
            self.trainer_running_address = self.cfg.get("sensors_trainer_running", "trainer_address", fallback="")

            self.trainer_running_machine_type = None
            trainer_running_machine_type = self.cfg.get("sensors_trainer_running", "trainer_machine_type", fallback="")
            if trainer_running_machine_type:
                self.trainer_running_machine_type = MachineType(int(trainer_running_machine_type))


            self.trainer_cycling_hr_name = self.cfg.get("sensors_trainer_cycling", "hr_name", fallback="")
            self.trainer_cycling_hr_address = self.cfg.get("sensors_trainer_cycling", "hr_address", fallback="")
            self.trainer_cycling_name = self.cfg.get("sensors_trainer_cycling", "trainer_name", fallback="")
            self.trainer_cycling_address = self.cfg.get("sensors_trainer_cycling", "trainer_address", fallback="")

            self.trainer_cycling_machine_type = None
            trainer_cycling_machine_type = self.cfg.get("sensors_trainer_cycling", "trainer_machine_type", fallback="")
            if trainer_cycling_machine_type:
                self.trainer_cycling_machine_type = MachineType(int(trainer_cycling_machine_type))

            self.resting_hr = self.cfg.getint("personal", "resting_hr", fallback=60)
            self.max_hr = self.cfg.getint("personal", "max_hr", fallback=180)
            self.ftp_watts = self.cfg.getint("personal", "ftp_watts", fallback=150)

            # Pebble device
            self.pebble_enable = self.cfg.getboolean(
                "pebble",
                "enable",
                fallback=self.pebble_enable,
            )
            self.pebble_use_emulator = self.cfg.getboolean(
                "pebble",
                "use_emulator",
                fallback=self.pebble_use_emulator,
            )
            self.pebble_name = self.cfg.get("pebble", "name", fallback=None)
            self.pebble_address = self.cfg.get("pebble", "mac", fallback=None)
            self.pebble_port = self.cfg.getint("pebble", "port", fallback=47527)

            # Intervals.icu
            self.icu_athlete_id = self.cfg.get("intervals_icu", "athlete_id", fallback="")
            self.icu_api_key = self.cfg.get("intervals_icu", "api_key", fallback="")


    def show_toast(self, message: str) -> None:
        print(message)
        # Create and display a toast on our overlay
        toast = Adw.Toast.new(message)
        self.toast_overlay.add_toast(toast)

    def apply_pebble_settings(self) -> None:
        if self.pebble_bridge:
            # Skip teardown and recreation if no settings change
            if (
                self.pebble_address ==  self.pebble_bridge.mac
                and self.pebble_use_emulator == self.pebble_bridge.use_emulator
                and self.pebble_port == self.pebble_bridge.port
            ):
                return

            with contextlib.suppress(Exception):
                self.pebble_bridge.stop()
        self.pebble_bridge = None

        if not self.pebble_enable:
            print("Pebble Disabled")
            return

        try:
            if not self.pebble_use_emulator and not hasattr(socket, "AF_BLUETOOTH"):
                # Check if python sock has AF_BLUETOOTH support
                # Do not attempt to start the bridge if no Bluetooth support
                # Clear out connection info
                self.pebble_address = None

                msg = "No Bluetooth support in Python socket module"
                print(msg)
                self.show_toast(msg)
                return


            self.pebble_bridge = PebbleBridge(
                app_uuid=self.pebble_uuid,
                mac=self.pebble_address,
                send_hz=2.0,
                use_emulator=self.pebble_use_emulator,
                port=self.pebble_port,
            )

            self.pebble_bridge.start()
            mode = "Emulator" if self.pebble_use_emulator else "Watch"
            print(f"Pebble bridge started ({mode})")
        except Exception as e:
            self.pebble_bridge = None
            print(e)

    def _profile_from_sport_type(self, sport_type: SportTypesEnum, trainer: bool = False) -> SensorProfile:
        """Convert a SportTypesEnum to a SensorProfile object."""
        if trainer:
            if sport_type == SportTypesEnum.running:
                logger.debug("Using trainer running profile")
                return SensorProfile(
                    hr_name=self.trainer_running_hr_name,
                    hr_address=self.trainer_running_hr_address,
                    speed_name="",  # trainer doesn't use speed/cad/power sensors
                    speed_address="",
                    cadence_name="",
                    cadence_address="",
                    power_name="",
                    power_address="",
                    trainer_name=self.trainer_running_name,
                    trainer_address=self.trainer_running_address,
                    trainer_machine_type=self.trainer_running_machine_type,
                )
            if sport_type == SportTypesEnum.biking:
                logger.debug("Using trainer biking profile")
                return SensorProfile(
                    hr_name=self.trainer_cycling_hr_name,
                    hr_address=self.trainer_cycling_hr_address,
                    speed_name="",  # trainer doesn't use speed/cad/power sensors
                    speed_address="",
                    cadence_name="",
                    cadence_address="",
                    power_name="",
                    power_address="",
                    trainer_name=self.trainer_cycling_name,
                    trainer_address=self.trainer_cycling_address,
                    trainer_machine_type=self.trainer_cycling_machine_type,
                )

            logger.error(f"Unknown profile '{sport_type}' for trainer. Defaulting to empty profile.")
            return SensorProfile()

        if sport_type == SportTypesEnum.biking:
            logger.debug("Using biking profile")
            return SensorProfile(
                hr_name=self.cycling_hr_name,
                hr_address=self.cycling_hr_address,
                speed_name=self.cycling_speed_name,
                speed_address=self.cycling_speed_address,
                cadence_name=self.cycling_cadence_name,
                cadence_address=self.cycling_cadence_address,
                power_name=self.cycling_power_name,
                power_address=self.cycling_power_address,
            )
        if sport_type == SportTypesEnum.running:
            logger.debug("Using running profile")
            return SensorProfile(
                hr_name=self.hr_name,
                hr_address=self.hr_address,
                speed_name=self.speed_name,
                speed_address=self.speed_address,
                cadence_name=self.cadence_name,
                cadence_address=self.cadence_address,
                power_name=self.power_name,
                power_address=self.power_address,
            )

        logger.error(f"Unknown profile '{sport_type}'. Defaulting to empty profile.")
        return SensorProfile()


    def apply_sensor_settings(self, sport_type: SportTypesEnum = SportTypesEnum.running, trainer: bool = False) -> None:
        desired = self._profile_from_sport_type(sport_type, trainer=trainer)
        try:
            if self.recorder:
                if getattr(self.recorder, "sport_type", None) == sport_type:
                    same = True
                    same &= (desired.hr_address or "") == (getattr(self.recorder, "hr_address", "") or "")
                    same &= (desired.speed_address or "") == (getattr(self.recorder, "speed_address", "") or "")
                    same &= (desired.cadence_address or "") == (getattr(self.recorder, "cadence_address", "") or "")
                    same &= (desired.power_address or "") == (getattr(self.recorder, "power_address", "") or "")
                    same &= (desired.trainer_address or "") == (getattr(self.recorder, "trainer_address", "") or "")
                    same &= (desired.trainer_machine_type or "") == (getattr(self.recorder, "trainer_machine_type", "") or "")
                    if same:
                        logger.debug("Recorder already matches desired profile. Skipping rebuild.")
                        return

                with contextlib.suppress(Exception):
                    self.recorder.shutdown()
        except Exception as e:
            print(e)

        # Build recorder with sensors
        logger.debug(f"Applying sensor settings for profile '{sport_type}': {desired}")
        self.recorder = Recorder(
            sport_type=sport_type,
            on_bpm_update=self.tracker.on_bpm,
            on_sample_update=self.tracker.on_sample,
            database_url=f"sqlite:///{self.database}",
            hr_name=desired.hr_name,
            hr_address=desired.hr_address,
            speed_name=desired.speed_name,
            speed_address=desired.speed_address,
            cadence_name=desired.cadence_name,
            cadence_address=desired.cadence_address,
            power_name=desired.power_name,
            power_address=desired.power_address,
            trainer_name=desired.trainer_name,
            trainer_address=desired.trainer_address,
            trainer_machine_type=desired.trainer_machine_type,
            on_error=self.show_toast,
            test_mode=self.test_mode,
        )
        if not self.test_mode:
            # Only spin BLE loops when not in test mode
            self.recorder.start()

        self.tracker.update_metric_statuses()

    def do_activate(self):
        if not self.window:
            self._build_ui()

            # Start/stop Pebble according to config
            self.apply_pebble_settings()

            # Start/stop recorder with sensors
            self.apply_sensor_settings(sport_type=SportTypesEnum.running, trainer=False)

        self.window.present()

    def _build_ui(self):
        self.window = Adw.ApplicationWindow(application=self)
        self.window.connect("close-request", lambda *a: (self.quit(), False)[1])
        self.window.set_title("Fitness Tracker")
        self.window.set_default_size(720, 1280)
        self.window.set_resizable(True)
        self.toast_overlay = Adw.ToastOverlay()
        self.window.set_content(self.toast_overlay)

        toolbar_view = Adw.ToolbarView()
        self.toast_overlay.set_child(toolbar_view)

        # Create HeaderBar for desktop usage
        header_bar = Adw.HeaderBar()
        header_bar.set_show_title(True)

        # Add header bar to the top of toolbar
        header_bar = Adw.HeaderBar()
        header_bar.set_show_title(True)

        self.header_revealer = Gtk.Revealer()
        self.header_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.header_revealer.set_reveal_child(False)  # default to hidden (mobile)

        self.header_revealer.set_child(header_bar)
        toolbar_view.add_top_bar(self.header_revealer)

        # Create ViewStack
        self.stack = Adw.ViewStack()
        self.stack.set_vexpand(True)

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

        cond = Adw.BreakpointCondition.parse("min-width: 700sp")
        bp = Adw.Breakpoint.new(cond)
        bp.add_setter(self.header_revealer, "reveal-child", True)
        self.window.add_breakpoint(bp)

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
        for (_, (low, high)), color in zip(zones.items(), colors, strict=True):
            ax.axhspan(low, high, facecolor=color, alpha=alpha)

    def do_shutdown(self):
        # Cleanly stop recorder/BLE loop before app teardown
        try:
            if self.recorder:
                with contextlib.suppress(Exception):
                    self.recorder.stop_recording()
                self.recorder.shutdown()

            if self.pebble_bridge:
                with contextlib.suppress(Exception):
                    self.pebble_bridge.stop()
        finally:
            # IMPORTANT: chain up by calling the base class with self
            Adw.Application.do_shutdown(self)
