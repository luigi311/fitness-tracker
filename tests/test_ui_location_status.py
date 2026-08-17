"""Headless coverage for location settings and tracker status feedback."""

# ruff: noqa: PLR0915, SLF001

from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import gi
import gi.repository
from fitness_tracker.core.environment import Environment
from fitness_tracker.core.session_state import SessionState
from fitness_tracker.core.sports import SportTypesEnum
from fitness_tracker.hardware.location import LocationState

_MISSING = object()
_GI_NAMES = ("Adw", "Gdk", "GLib", "Gtk", "Pango")


class _HeadlessWidget:
    def __init__(self) -> None:
        self.title = ""
        self.subtitle = ""
        self.active = False
        self.suffixes: list[object] = []
        self.connections: list[tuple[object, object]] = []

    def set_title(self, value: str) -> None:
        self.title = value

    def set_subtitle(self, value: str) -> None:
        self.subtitle = value

    def set_active(self, value: object) -> None:
        self.active = bool(value)

    def get_active(self) -> bool:
        return self.active

    def connect(self, signal: object, callback: object) -> None:
        self.connections.append((signal, callback))

    def add_suffix(self, widget: object) -> None:
        self.suffixes.append(widget)

    def set_sensitive(self, _value: object) -> None:
        return None


class _HeadlessBox(_HeadlessWidget):
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        super().__init__()
        self.children: list[object] = []

    def append(self, child: object) -> None:
        self.children.append(child)


class _HeadlessFlowBox(_HeadlessBox):
    def insert(self, child: object, _position: int) -> None:
        self.children.append(child)

    def set_selection_mode(self, _mode: object) -> None:
        return None

    def set_valign(self, _align: object) -> None:
        return None

    def set_halign(self, _align: object) -> None:
        return None

    def set_homogeneous(self, _value: object) -> None:
        return None

    def set_column_spacing(self, _value: int) -> None:
        return None

    def set_row_spacing(self, _value: int) -> None:
        return None


class _HeadlessLabel(_HeadlessWidget):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__()
        self.label = kwargs.get("label", args[0] if args else "")
        self.visible = True

    def add_css_class(self, _css_class: str) -> None:
        return None

    def set_wrap(self, _value: object) -> None:
        return None

    def set_margin_start(self, _value: int) -> None:
        return None

    def set_margin_end(self, _value: int) -> None:
        return None

    def set_visible(self, value: object) -> None:
        self.visible = bool(value)


class _HeadlessMetricTile:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        return None


class _HeadlessGroup(_HeadlessWidget):
    def __init__(self) -> None:
        super().__init__()
        self.children: list[object] = []

    def add(self, child: object) -> None:
        self.children.append(child)


class _HeadlessCombo(_HeadlessWidget):
    def __init__(self) -> None:
        super().__init__()
        self.items: list[str] = []
        self.active_index = -1

    def append_text(self, value: str) -> None:
        self.items.append(value)

    def set_active(self, index: int) -> None:  # ty:ignore[invalid-method-override]
        self.active_index = index


class _HeadlessGLib:
    @staticmethod
    def timeout_add(*_args: object) -> int:
        return 1


def _load_headless_ui_classes() -> tuple[type, type, type]:
    """Import the UI classes while replacing GTK-only dependencies."""
    original_require_versions = gi.require_versions
    original_gi_attributes = {name: getattr(gi.repository, name, _MISSING) for name in _GI_NAMES}
    module_names = (
        "fitness_tracker.ui.pages.mode",
        "fitness_tracker.ui.pages.session",
        "fitness_tracker.ui.pages.tracker",
    )
    widget_module_names = (
        "fitness_tracker.ui.widgets",
        "fitness_tracker.ui.widgets.chart",
        "fitness_tracker.ui.widgets.metric_tile",
        "fitness_tracker.ui.widgets.session_controls",
        "fitness_tracker.ui.widgets.timers",
        "fitness_tracker.ui.widgets.trainer_control",
    )
    original_modules = {
        name: sys.modules.get(name, _MISSING) for name in (*module_names, *widget_module_names)
    }

    mode_module = types.ModuleType(module_names[0])
    mode_module.ModeSelectView = object  # ty:ignore[unresolved-attribute]
    widgets_package = types.ModuleType(widget_module_names[0])
    widgets_package.__path__ = []
    chart_module = types.ModuleType(widget_module_names[1])
    chart_module.LiveChart = object  # ty:ignore[unresolved-attribute]
    metric_tile_module = types.ModuleType(widget_module_names[2])
    metric_tile_module.MetricTile = _HeadlessMetricTile  # ty:ignore[unresolved-attribute]
    session_controls_module = types.ModuleType(widget_module_names[3])
    session_controls_module.InclineControl = object  # ty:ignore[unresolved-attribute]
    session_controls_module.TargetGauge = object  # ty:ignore[unresolved-attribute]
    timers_module = types.ModuleType(widget_module_names[4])
    timers_module.SessionTimer = object  # ty:ignore[unresolved-attribute]
    trainer_control_module = types.ModuleType(widget_module_names[5])
    trainer_control_module.TrainerTargetControl = object  # ty:ignore[unresolved-attribute]

    fake_adw = types.SimpleNamespace(
        ActionRow=_HeadlessWidget,
        PreferencesGroup=_HeadlessGroup,
        SwitchRow=_HeadlessWidget,
    )
    fake_gtk = types.SimpleNamespace(
        Align=types.SimpleNamespace(FILL="fill"),
        Box=_HeadlessBox,
        ComboBoxText=_HeadlessCombo,
        FlowBox=_HeadlessFlowBox,
        Label=_HeadlessLabel,
        SelectionMode=types.SimpleNamespace(NONE="none"),
    )

    gi.require_versions = lambda _versions: None  # ty:ignore[invalid-assignment]
    gi.repository.Adw = fake_adw  # ty:ignore[unresolved-attribute]
    gi.repository.Gdk = types.SimpleNamespace()  # ty:ignore[unresolved-attribute]
    gi.repository.GLib = _HeadlessGLib  # ty:ignore[unresolved-attribute]
    gi.repository.Gtk = fake_gtk  # ty:ignore[unresolved-attribute]
    gi.repository.Pango = types.SimpleNamespace()  # ty:ignore[unresolved-attribute]
    sys.modules[module_names[0]] = mode_module
    sys.modules[widget_module_names[0]] = widgets_package
    sys.modules[widget_module_names[1]] = chart_module
    sys.modules[widget_module_names[2]] = metric_tile_module
    sys.modules[widget_module_names[3]] = session_controls_module
    sys.modules[widget_module_names[4]] = timers_module
    sys.modules[widget_module_names[5]] = trainer_control_module
    sys.modules.pop(module_names[2], None)

    session_path = (
        Path(__file__).parents[1] / "src" / "fitness_tracker" / "ui" / "pages" / "session.py"
    )
    session_spec = importlib.util.spec_from_file_location(module_names[1], session_path)
    if session_spec is None or session_spec.loader is None:
        message = "Unable to load session page for headless tests"
        raise RuntimeError(message)
    session_module = importlib.util.module_from_spec(session_spec)
    sys.modules[module_names[1]] = session_module

    section_module_name = "_headless_location_sections"
    section_path = (
        Path(__file__).parents[1]
        / "src"
        / "fitness_tracker"
        / "ui"
        / "pages"
        / "settings"
        / "sections.py"
    )
    section_spec = importlib.util.spec_from_file_location(section_module_name, section_path)
    if section_spec is None or section_spec.loader is None:
        message = "Unable to load settings sections for headless tests"
        raise RuntimeError(message)
    section_module = importlib.util.module_from_spec(section_spec)
    sys.modules[section_module_name] = section_module

    try:
        session_spec.loader.exec_module(session_module)
        section_spec.loader.exec_module(section_module)
        tracker_module = importlib.import_module(module_names[2])
    finally:
        sys.modules.pop(section_module_name, None)
        gi.require_versions = original_require_versions
        for name, original in original_gi_attributes.items():
            if original is _MISSING:
                delattr(gi.repository, name)
            else:
                setattr(gi.repository, name, original)
        for name, original in original_modules.items():
            if original is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original  # ty:ignore[invalid-assignment]

    return tracker_module.TrackerPageUI, section_module.LocationSection, session_module.SessionView


TrackerPageUI, LocationSection, SessionView = _load_headless_ui_classes()


class _LocationView:
    profile_ready = True

    def __init__(self) -> None:
        self.statuses: list[str] = []
        self.states: list[SessionState] = []

    def set_location_status(self, status: str) -> None:
        self.statuses.append(status)

    def set_state(self, state: SessionState) -> None:
        self.states.append(state)

    def set_profile_ready(self, *, ready: bool) -> None:
        self.profile_ready = ready


class _LocationRecorder:
    finalization_in_progress = False
    finalization_pending = False
    distance_connected = True

    def __init__(self) -> None:
        self.started: list[Environment] = []

    def start_recording(self, *, environment: Environment) -> None:
        self.started.append(environment)


def _tracker_for_location_tests() -> tuple[object, _LocationView, _LocationRecorder, list[str]]:
    view = _LocationView()
    recorder = _LocationRecorder()
    toasts: list[str] = []
    page = TrackerPageUI.__new__(TrackerPageUI)  # ty:ignore[no-matching-overload]
    page.app = SimpleNamespace(
        recorder=recorder,
        pebble_bridge=None,
        show_toast=toasts.append,
        test_mode=True,
    )
    page.session_view = view
    page._session_state = SessionState.PREVIEW
    page._active_environment = Environment.OUTDOOR
    page._location_had_tracking = False
    page._location_error_notified = False
    page._start_requested = False
    page._workout_session = None
    page._timer_source_id = None
    page._trainer_target_throttle = Mock()
    return page, view, recorder, toasts


def test_indoor_location_warning_copy_is_present() -> None:
    section = LocationSection(
        SimpleNamespace(
            record_outdoor_routes=True,
            record_indoor_anchor=False,
            indoor_accuracy="neighborhood",
        ),
    )

    group = section.build()
    indoor_row = next(
        row for row in group.children if row.title == "Store an indoor/trainer location"
    )

    assert "may reveal your home location" in indoor_row.subtitle


def test_location_status_transitions_do_not_block_session_start() -> None:
    page, view, recorder, toasts = _tracker_for_location_tests()

    for state in (
        LocationState.STARTING,
        LocationState.ACQUIRING,
        LocationState.TRACKING,
    ):
        page.on_location_state(state, None)  # ty:ignore[unresolved-attribute]
    page._begin_run_now()  # ty:ignore[unresolved-attribute]

    assert recorder.started == [Environment.OUTDOOR]
    assert page._session_state is SessionState.RUNNING  # ty:ignore[unresolved-attribute]
    assert view.states == [SessionState.RUNNING]
    assert toasts == []


def test_outdoor_permission_error_notifies_once() -> None:
    page, view, _recorder, toasts = _tracker_for_location_tests()

    for state in (
        LocationState.DENIED,
        LocationState.CANCELLED,
        LocationState.ERROR,
        LocationState.UNAVAILABLE,
    ):
        page.on_location_state(state, None)  # ty:ignore[unresolved-attribute]

    assert toasts == ["Location permission denied"]
    assert view.statuses[-1] == "Location unavailable"


def test_location_status_visibility_follows_resolved_policy() -> None:
    page = TrackerPageUI.__new__(TrackerPageUI)  # ty:ignore[no-matching-overload]
    page.app = SimpleNamespace(
        app_settings=SimpleNamespace(
            location=SimpleNamespace(
                record_outdoor_routes=True,
                record_indoor_anchor=False,
                indoor_accuracy="neighborhood",
            ),
        ),
    )

    class _CapturingSessionView:
        def __init__(self, **kwargs: object) -> None:
            self.location_enabled = kwargs["location_enabled"]

    tracker_globals = TrackerPageUI._make_session_view.__globals__  # ty:ignore[unresolved-attribute]
    original_session_view = tracker_globals["SessionView"]
    tracker_globals["SessionView"] = _CapturingSessionView
    try:
        indoor_view = page._make_session_view(
            SportTypesEnum.running,
            Environment.INDOOR,
            title="Run",
            workout=False,
        )
        outdoor_view = page._make_session_view(
            SportTypesEnum.running,
            Environment.OUTDOOR,
            title="Run",
            workout=False,
        )
    finally:
        tracker_globals["SessionView"] = original_session_view

    assert indoor_view.location_enabled is False
    assert outdoor_view.location_enabled is True


def test_session_view_applies_location_status_visibility() -> None:
    def build_metric_strip(*, location_enabled: bool) -> bool:
        view = SessionView.__new__(SessionView)  # ty:ignore[no-matching-overload]
        view.children = []
        view._build_metric_strip(location_enabled=location_enabled)
        return view.location_status.visible

    assert build_metric_strip(location_enabled=False) is False
    assert build_metric_strip(location_enabled=True) is True
