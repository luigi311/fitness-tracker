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
import pytest
from fitness_tracker.core.environment import Environment
from fitness_tracker.core.session_state import SessionState
from fitness_tracker.core.sports import SportTypesEnum
from fitness_tracker.hardware.location import LocationState


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

    def remove_all(self) -> None:
        self.items.clear()
        self.active_index = -1

    def get_active_text(self) -> str | None:
        return self.items[self.active_index] if self.active_index >= 0 else None

    def set_active(self, index: int) -> None:  # ty:ignore[invalid-method-override]
        self.active_index = index


class _HeadlessGLib:
    SOURCE_CONTINUE = True
    SOURCE_REMOVE = False

    @staticmethod
    def timeout_add(*_args: object) -> int:
        return 1

    @staticmethod
    def source_remove(_source_id: int) -> None:
        return None


def _load_headless_ui_classes(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[type, type, type]:
    """Import the UI classes while replacing GTK-only dependencies."""
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

    monkeypatch.setattr(gi, "require_versions", lambda _versions: None)
    monkeypatch.setattr(gi.repository, "Adw", fake_adw, raising=False)
    monkeypatch.setattr(gi.repository, "Gdk", types.SimpleNamespace(), raising=False)
    monkeypatch.setattr(gi.repository, "GLib", _HeadlessGLib, raising=False)
    monkeypatch.setattr(gi.repository, "Gtk", fake_gtk, raising=False)
    monkeypatch.setattr(gi.repository, "Pango", types.SimpleNamespace(), raising=False)
    monkeypatch.setitem(sys.modules, module_names[0], mode_module)
    monkeypatch.setitem(sys.modules, widget_module_names[0], widgets_package)
    monkeypatch.setitem(sys.modules, widget_module_names[1], chart_module)
    monkeypatch.setitem(sys.modules, widget_module_names[2], metric_tile_module)
    monkeypatch.setitem(sys.modules, widget_module_names[3], session_controls_module)
    monkeypatch.setitem(sys.modules, widget_module_names[4], timers_module)
    monkeypatch.setitem(sys.modules, widget_module_names[5], trainer_control_module)
    monkeypatch.delitem(sys.modules, module_names[2], raising=False)

    session_path = (
        Path(__file__).parents[1] / "src" / "fitness_tracker" / "ui" / "pages" / "session.py"
    )
    session_spec = importlib.util.spec_from_file_location(module_names[1], session_path)
    if session_spec is None or session_spec.loader is None:
        message = "Unable to load session page for headless tests"
        raise RuntimeError(message)
    session_module = importlib.util.module_from_spec(session_spec)
    monkeypatch.setitem(sys.modules, module_names[1], session_module)

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
    monkeypatch.setitem(sys.modules, section_module_name, section_module)

    session_spec.loader.exec_module(session_module)
    section_spec.loader.exec_module(section_module)
    tracker_module = importlib.import_module(module_names[2])
    monkeypatch.delitem(sys.modules, module_names[2])

    return tracker_module.TrackerPageUI, section_module.LocationSection, session_module.SessionView


@pytest.fixture(scope="module")
def headless_ui_classes():
    """Load headless UI classes under module-scoped dependency patches."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        yield _load_headless_ui_classes(monkeypatch)


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

    def start_recording(self, environment: Environment) -> None:
        self.started.append(environment)


def _tracker_for_location_tests(
    tracker_page_ui: type,
) -> tuple[object, _LocationView, _LocationRecorder, list[str]]:
    view = _LocationView()
    recorder = _LocationRecorder()
    toasts: list[str] = []
    page = tracker_page_ui.__new__(tracker_page_ui)
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


def test_indoor_location_warning_copy_is_present(headless_ui_classes) -> None:
    _tracker_page_ui, location_section, _session_view = headless_ui_classes
    section = location_section(
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


def test_location_status_transitions_do_not_block_session_start(headless_ui_classes) -> None:
    tracker_page_ui, _location_section, _session_view = headless_ui_classes
    page, view, recorder, toasts = _tracker_for_location_tests(tracker_page_ui)

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


def test_outdoor_permission_error_notifies_once(headless_ui_classes) -> None:
    tracker_page_ui, _location_section, _session_view = headless_ui_classes
    page, view, _recorder, toasts = _tracker_for_location_tests(tracker_page_ui)

    for state in (
        LocationState.DENIED,
        LocationState.CANCELLED,
        LocationState.ERROR,
        LocationState.UNAVAILABLE,
    ):
        page.on_location_state(state, None)  # ty:ignore[unresolved-attribute]

    assert toasts == ["Location permission denied"]
    assert view.statuses[-1] == "Location unavailable"


def test_location_status_visibility_follows_resolved_policy(headless_ui_classes) -> None:
    tracker_page_ui, _location_section, _session_view = headless_ui_classes
    page = tracker_page_ui.__new__(tracker_page_ui)
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

    tracker_globals = tracker_page_ui._make_session_view.__globals__
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


def test_session_view_applies_location_status_visibility(headless_ui_classes) -> None:
    _tracker_page_ui, _location_section, session_view = headless_ui_classes

    def build_metric_strip(*, location_enabled: bool) -> bool:
        view = session_view.__new__(session_view)
        view.children = []
        view._build_metric_strip(location_enabled=location_enabled)
        return view.location_status.visible

    assert build_metric_strip(location_enabled=False) is False
    assert build_metric_strip(location_enabled=True) is True


def test_stop_requires_configured_hold_duration_and_cancels_on_focus_loss(
    headless_ui_classes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _tracker_page_ui, _location_section, session_view = headless_ui_classes
    now = [10.0]
    removed_sources: list[int] = []
    stop = Mock()
    button = Mock()
    progress = Mock()
    view = session_view.__new__(session_view)
    view._workout_visible = False
    view._on_stop = stop
    view._stop_hold_started_at = None
    view._stop_hold_source_id = None
    view.btn_stop = button
    view._stop_progress = progress
    method_globals = session_view._begin_stop_hold.__globals__
    hold_seconds = method_globals["_STOP_HOLD_SECONDS"]
    monkeypatch.setitem(method_globals, "monotonic", lambda: now[0])
    monkeypatch.setitem(
        method_globals,
        "GLib",
        SimpleNamespace(
            SOURCE_CONTINUE=True,
            SOURCE_REMOVE=False,
            timeout_add=lambda *_args: 41,
            source_remove=removed_sources.append,
        ),
    )

    view._begin_stop_hold()
    now[0] += hold_seconds - 0.1
    assert view._update_stop_hold() is True
    assert progress.set_fraction.call_args.args[0] == pytest.approx(
        (hold_seconds - 0.1) / hold_seconds,
    )
    view._on_stop_focus_leave(Mock())

    stop.assert_not_called()
    assert removed_sources == [41]
    assert view._stop_hold_started_at is None
    assert view._stop_hold_source_id is None
    assert progress.set_fraction.call_args.args == (0.0,)

    view._begin_stop_hold()
    now[0] += hold_seconds
    assert view._update_stop_hold() is False
    view._end_stop_hold()

    stop.assert_called_once_with()
    assert progress.set_fraction.call_args.args == (1.0,)
    assert button.set_sensitive.call_args.args == (False,)


def test_tracker_averages_exclude_preview_pause_and_reset_each_step(headless_ui_classes) -> None:
    from bleaksport import HeartRateSample  # noqa: PLC0415
    from fitness_tracker.core.live_metrics import LiveMetrics  # noqa: PLC0415
    from fitness_tracker.core.metric_averages import MetricAverages  # noqa: PLC0415
    from fitness_tracker.core.workout_session import WorkoutSession  # noqa: PLC0415
    from fitness_tracker.workout_execution import (  # noqa: PLC0415
        WorkoutDistanceAccumulator,
        WorkoutExecution,
    )
    from workout_parser import TimeDuration, WorkoutStep  # noqa: PLC0415

    tracker_type, _, _ = headless_ui_classes
    page, _, _, _ = _tracker_for_location_tests(tracker_type)
    page.session_view = None
    page._live_metrics = LiveMetrics()
    page._session_averages = MetricAverages()
    page._step_averages = MetricAverages()
    page._append_chart_sample = Mock()
    page.app.pebble_bridge = Mock()
    steps = [WorkoutStep(duration=TimeDuration(seconds=10)) for _ in range(3)]
    page._workout_session = WorkoutSession(
        workout=None,
        steps=steps,
        execution=WorkoutExecution(steps),
        distance_accumulator=WorkoutDistanceAccumulator(),
    )
    page._update_workout_execution(0, render_guidance=False)

    def heart(bpm):
        page.on_sample(HeartRateSample(timestamp_ms=1000, heart_rate_bpm=bpm))

    heart(200)  # Preview readings do not belong to the session.
    assert page._session_averages.values() == {}
    page._session_state = SessionState.RUNNING
    heart(120)
    heart(160)
    page._session_state = SessionState.PAUSED
    heart(200)
    page._session_state = SessionState.RUNNING
    page._publish_pebble_averages()
    page.app.pebble_bridge.update.assert_called_with(averages={"hr": 140})

    page._update_workout_execution(10, render_guidance=False)
    page.app.pebble_bridge.update.assert_called_with(averages={})
    heart(180)
    page._publish_pebble_averages()
    page.app.pebble_bridge.update.assert_called_with(averages={"hr": 180})

    # Manual navigation resets the averaging period even when revisiting a step.
    page._elapsed_display_s = 10
    page._update_workout_guidance = Mock()
    page._skip_step(-1)
    page.app.pebble_bridge.update.assert_called_with(averages={})
    heart(100)
    page._publish_pebble_averages()
    page.app.pebble_bridge.update.assert_called_with(averages={"hr": 100})

    # Finishing a workout reveals the average over the entire recording.
    page._workout_session = None
    page._publish_pebble_averages()
    page.app.pebble_bridge.update.assert_called_with(averages={"hr": 140})


@pytest.mark.parametrize(
    "session_state",
    [SessionState.PREVIEW, SessionState.RUNNING, SessionState.PAUSED],
)
def test_workout_unit_changes_refresh_remaining_and_next_without_advancing(
    headless_ui_classes,
    session_state,
) -> None:
    from fitness_tracker.core.live_metrics import LiveMetrics  # noqa: PLC0415
    from fitness_tracker.core.settings import PersonalSettings  # noqa: PLC0415
    from fitness_tracker.core.units import UnitSystem  # noqa: PLC0415
    from fitness_tracker.core.workout_session import WorkoutSession  # noqa: PLC0415
    from fitness_tracker.workout_execution import (  # noqa: PLC0415
        WorkoutDistanceAccumulator,
        WorkoutExecution,
    )
    from workout_parser import DistanceDuration, PointTarget, WorkoutStep  # noqa: PLC0415

    tracker_type, _, _ = headless_ui_classes
    page, _, _, _ = _tracker_for_location_tests(tracker_type)
    page.app.recorder = None
    page.app.app_settings = SimpleNamespace(personal=PersonalSettings())
    page.app.calculate_hr_zones = dict
    page.mode_view = Mock()
    page.session_view = Mock(sport_type=SportTypesEnum.running)
    page._session_state = session_state
    page._live_metrics = LiveMetrics()
    mile_m = 1609.344
    steps = [
        WorkoutStep(duration=DistanceDuration(meters=meters), speed_mps=PointTarget(value=2.5))
        for meters in (2 * mile_m, mile_m)
    ]
    session = WorkoutSession(
        workout=None,
        steps=steps,
        execution=WorkoutExecution(steps),
        distance_accumulator=WorkoutDistanceAccumulator(),
    )
    snapshot = session.execution.update(0, mile_m)
    session.snapshot = snapshot
    page._workout_session = session

    for unit_system, remaining, next_text in (
        (UnitSystem.IMPERIAL, "1.00 mi", "Next: 10:44 - 10:44 /mi for 1.00 mi"),
        (UnitSystem.METRIC, "1.61 km", "Next: 6:40 - 6:40 /km for 1.61 km"),
        (UnitSystem.IMPERIAL, "1.00 mi", "Next: 10:44 - 10:44 /mi for 1.00 mi"),
    ):
        page.app.unit_system = unit_system

        page.refresh_units()

        page.mode_view.set_unit_system.assert_called_with(unit_system)
        page.session_view.set_step_remaining_text.assert_called_with(remaining)
        guidance = page.session_view.apply_guidance.call_args.args[0]
        assert guidance.next_text == next_text
        assert session.snapshot is snapshot
        assert session.execution.snapshot().remaining_meters == mile_m
        assert page._session_state is session_state


@pytest.mark.usefixtures("headless_ui_classes")
def test_workout_list_summaries_refresh_when_units_change(monkeypatch, tmp_path) -> None:
    from fitness_tracker.core.units import UnitSystem  # noqa: PLC0415
    from workout_parser import DistanceDuration, Workout, WorkoutStep  # noqa: PLC0415

    module_name = "_headless_workout_mode"
    mode_path = Path(__file__).parents[1] / "src" / "fitness_tracker" / "ui" / "pages" / "mode.py"
    spec = importlib.util.spec_from_file_location(module_name, mode_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(gi.repository.Adw, "ActionRow", Mock(side_effect=Mock))
    monkeypatch.setattr(gi.repository.Gtk, "Button", Mock(), raising=False)
    monkeypatch.setattr(gi.repository.Gtk.Align, "CENTER", "center", raising=False)
    workout = Workout(
        name="One mile",
        instructions=(WorkoutStep(duration=DistanceDuration(meters=1609.344)),),
    )
    monkeypatch.setattr(module, "discover_workouts", lambda _directory: [tmp_path / "workout.json"])
    monkeypatch.setattr(module, "load_workout", lambda _path: workout)
    view = module.ModeSelectView.__new__(module.ModeSelectView)
    view._workouts_running_dir = tmp_path
    view._unit_system = UnitSystem.IMPERIAL
    view.sport_type = SportTypesEnum.running
    view._selected_env = Environment.INDOOR
    view._list = []

    view._rebuild_workout_list()

    view._list[0].set_subtitle.assert_called_once_with("1.00 mi")

    view.set_unit_system(UnitSystem.METRIC)

    view._list[0].set_subtitle.assert_called_once_with("1.61 km")

    view.set_unit_system(UnitSystem.IMPERIAL)

    view._list[0].set_subtitle.assert_called_once_with("1.00 mi")


def test_independent_gps_and_bluetooth_samples_preserve_live_values(headless_ui_classes):
    from bleaksport import RunningSample  # noqa: PLC0415
    from fitness_tracker.core.live_metrics import LiveMetrics  # noqa: PLC0415

    tracker_type, _, _ = headless_ui_classes
    page = tracker_type.__new__(tracker_type)
    page._live_metrics = LiveMetrics()
    page._update_live_motion_metrics(
        RunningSample(timestamp_ms=0, cadence_spm=170, power_watts=250),
    )
    page._update_live_motion_metrics(
        RunningSample(timestamp_ms=1000, speed_mps=3, distance_m=10),
    )
    page._update_live_motion_metrics(
        RunningSample(timestamp_ms=1500, cadence_spm=175, power_watts=260),
    )
    assert page._live_metrics == LiveMetrics(
        speed_mps=3,
        distance_m=10,
        cadence_spm=175,
        power_watts=260,
    )
    page._update_live_motion_metrics(RunningSample(timestamp_ms=2000, speed_mps=0))
    assert page._live_metrics.speed_mps == 0


def test_gps_metrics_keep_location_status_visible_with_routes_disabled(headless_ui_classes):
    from fitness_tracker.core.settings import (  # noqa: PLC0415
        AppSettings,
        LocationSettings,
        SensorSettings,
    )

    tracker_type, _, _ = headless_ui_classes
    page = tracker_type.__new__(tracker_type)
    page.app = SimpleNamespace(
        app_settings=AppSettings(
            location=LocationSettings(record_outdoor_routes=False),
            running_sensors=SensorSettings(speed_source="gps"),
        ),
    )
    assert page._location_enabled_for_environment(Environment.OUTDOOR, SportTypesEnum.running)
    assert not page._location_enabled_for_environment(Environment.OUTDOOR, SportTypesEnum.biking)
    assert not page._location_enabled_for_environment(Environment.INDOOR, SportTypesEnum.running)


def test_new_session_drops_old_values_before_merging_gps_samples(headless_ui_classes):
    from collections import deque  # noqa: PLC0415

    from bleaksport import RunningSample  # noqa: PLC0415
    from fitness_tracker.core.live_metrics import LiveMetrics  # noqa: PLC0415

    tracker_type, _, _ = headless_ui_classes
    page, _, _, _ = _tracker_for_location_tests(tracker_type)
    page._live_metrics = LiveMetrics(
        heart_rate_bpm=150,
        speed_mps=3,
        cadence_spm=90,
        distance_m=5000,
        power_watts=250,
    )
    page._times, page._bpms, page._powers = deque([1000]), deque([150]), deque([250])

    page._reset_buffers()
    page._update_live_motion_metrics(RunningSample(timestamp_ms=0, speed_mps=2, distance_m=0))

    assert page._live_metrics == LiveMetrics(speed_mps=2)
    assert page._metrics_dirty
    assert not page._times


def test_start_discards_preview_readings(headless_ui_classes):
    from fitness_tracker.core.live_metrics import LiveMetrics  # noqa: PLC0415

    tracker_type, _, _ = headless_ui_classes
    page, _, _, _ = _tracker_for_location_tests(tracker_type)
    page._live_metrics = LiveMetrics(speed_mps=3, cadence_spm=90, distance_m=5000, power_watts=250)

    page._begin_run_now()

    assert page._live_metrics == LiveMetrics()
    assert page._metrics_dirty


@pytest.mark.parametrize(
    ("motion_connected", "expected_speed"),
    [(False, 0), (True, 3)],
)
def test_sensor_disconnects_clear_values_but_retain_total_distance(
    headless_ui_classes,
    motion_connected,
    expected_speed,
):
    from fitness_tracker.core.live_metrics import LiveMetrics  # noqa: PLC0415

    tracker_type, _, _ = headless_ui_classes
    page, _, _, _ = _tracker_for_location_tests(tracker_type)
    page.app.test_mode = False
    page.app.recorder = Mock(
        hr_connected=False,
        speed_connected=motion_connected,
        cadence_connected=False,
        power_connected=False,
        distance_connected=motion_connected,
    )
    page.session_view = Mock()
    page._live_metrics = LiveMetrics(speed_mps=3, cadence_spm=170, distance_m=10, power_watts=250)
    page._metrics_dirty = False

    page.update_metric_statuses()

    assert page._live_metrics == LiveMetrics(speed_mps=expected_speed, distance_m=10)
    assert page._metrics_dirty


def test_gps_selector_keeps_selection_and_empty_scan_feedback(headless_ui_classes):
    from fitness_tracker.core.settings import SensorSettings  # noqa: PLC0415

    _, location_section, _ = headless_ui_classes
    classes = location_section.__init__.__globals__
    spec = classes["SensorRowSpec"](
        key="speed",
        title="Speed",
        scan_label="Scan",
        scanner=lambda: None,
        scan_group="sensors",
        settings_field="speed_name",
    )
    section = classes["SensorSection"](
        title="Running",
        subtitle="",
        settings=SensorSettings(speed_source="gps"),
        specs=(spec,),
    )
    combo, row = _HeadlessCombo(), _HeadlessWidget()
    section.rows["speed"] = classes["SensorRowWidgets"](
        row=row,
        spinner=Mock(),
        combo=combo,
        scan_button=Mock(),
    )
    section.load()
    assert combo.get_active_text() == "GPS"

    section.apply_scan_result("sensors", {}, "No speed devices found")

    assert row.subtitle == "No speed devices found"
    assert combo.get_active_text() == "GPS"
    assert section.settings_data()["speed_source"] == "gps"

    section.apply_scan_result("sensors", {"Footpod": "AA"}, "No speed devices found")
    combo.set_active(combo.items.index("Footpod"))

    values = section.settings_data()
    assert values["speed_source"] == "ble"
    assert values["speed_address"] == "AA"
