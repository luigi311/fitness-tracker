# ruff: noqa: SLF001
"""Focused coverage for UI review regressions."""

import datetime
from types import SimpleNamespace
from unittest.mock import Mock

from fitness_tracker.core.settings import (
    TRAINER_SUPPLIED_HR_LABEL,
    PebbleSettings,
    TrainerSettings,
)
from fitness_tracker.core.sports import SportTypesEnum
from fitness_tracker.core.units import UnitSystem
from fitness_tracker.services.jobs import CancellationToken, DuplicateJobError
from fitness_tracker.ui.pages import history as history_module
from fitness_tracker.ui.pages.history import HistoryPageUI
from fitness_tracker.ui.pages.settings import page as settings_page_module
from fitness_tracker.ui.pages.settings.sections import (
    PebbleSection,
    SensorRowSpec,
    SensorRowWidgets,
    SensorSection,
)
from fitness_tracker.ui.widgets.session_controls import InclineControl

EXPECTED_COALESCED_SUBMISSIONS = 2


class _Combo:
    def __init__(self, active_text: str = "") -> None:
        self.active_text = active_text
        self.items: list[str] = []
        self.active_index = -1

    def get_active_text(self) -> str:
        return self.active_text

    def remove_all(self) -> None:
        self.items.clear()

    def append_text(self, value: str) -> None:
        self.items.append(value)

    def set_active(self, index: int) -> None:
        self.active_index = index


def test_pebble_duplicate_display_name_persists_advertised_name() -> None:
    section = PebbleSection(PebbleSettings(), on_scan=lambda: None)
    section.enable_row = SimpleNamespace(get_active=lambda: True)
    section.emu_switch = SimpleNamespace(get_active=lambda: False)
    section.combo = _Combo("Pebble (2)")
    section.device_map = section._unique_display_names(
        [("Pebble", "AA:AA"), ("Pebble", "BB:BB")],
    )

    values = section.settings_data()

    assert values["name"] == "Pebble"
    assert values["address"] == "BB:BB"


def test_empty_trainer_hr_scan_keeps_supplied_option_and_empty_status() -> None:
    spec = SensorRowSpec(
        key="hr",
        title="Heart rate",
        scan_label="Scan",
        scanner=lambda: None,
        scan_group="trainer_hr",
        settings_field="hr_name",
    )
    section = SensorSection(
        title="Trainer",
        subtitle="",
        settings=TrainerSettings(),
        specs=(spec,),
    )
    row = Mock()
    combo = _Combo()
    section.rows["hr"] = SensorRowWidgets(
        row=row,
        spinner=Mock(),
        combo=combo,
        scan_button=Mock(),
    )

    section.apply_scan_result("trainer_hr", {}, "No HRM found")

    row.set_subtitle.assert_called_once_with("No HRM found")
    assert TRAINER_SUPPLIED_HR_LABEL in combo.items


def test_history_ascent_uses_selected_unit_system() -> None:
    page = HistoryPageUI.__new__(HistoryPageUI)
    page.app = SimpleNamespace(unit_system=UnitSystem.IMPERIAL)  # ty:ignore[invalid-assignment]
    stats = SimpleNamespace(
        duration_s=60,
        distance_m=1000,
        avg_speed_mps=None,
        avg_bpm=None,
        max_bpm=None,
        avg_cadence=None,
        avg_power_watts=None,
        total_ascent_m=1000,
    )

    parts = page._activity_metric_parts(stats, SportTypesEnum.running)

    assert "↑ 0.62 mi" in parts


def test_history_activity_title_can_shrink_in_narrow_layout() -> None:
    page = HistoryPageUI.__new__(HistoryPageUI)
    page.selected_ids = set()
    page._on_export_clicked = Mock()
    page._on_select_toggle = Mock()
    stats = SimpleNamespace(
        activity_id=7,
        start_time=datetime.datetime(2026, 8, 15, 11, 36, tzinfo=datetime.UTC),
    )
    box = history_module.Gtk.Box()

    page._append_activity_header(box, stats)

    header = box.get_first_child()
    title = header.get_first_child()
    assert title.get_single_line_mode()
    assert title.get_ellipsize() == history_module.Pango.EllipsizeMode.END


def test_history_backfill_discard_and_duplicate_release_reload_guard() -> None:
    page = HistoryPageUI.__new__(HistoryPageUI)
    jobs = Mock()
    page.app = SimpleNamespace(  # ty:ignore[invalid-assignment]
        database=SimpleNamespace(stat_calc=Mock()),
        jobs=jobs,
    )
    page._stats_backfill_in_progress = False
    page._stats_reload_pending = False
    page.filter_id = "week"

    assert page._reload_everything() is False
    assert page._stats_backfill_in_progress
    jobs.submit.call_args.kwargs["on_discard"]()
    assert not page._stats_backfill_in_progress

    jobs.submit.reset_mock(side_effect=True)
    jobs.submit.side_effect = DuplicateJobError
    assert page._reload_everything() is False
    assert not page._stats_backfill_in_progress


def test_history_backfill_batches_heart_rates_in_worker_result() -> None:
    page = HistoryPageUI.__new__(HistoryPageUI)
    jobs = Mock()
    repository = Mock()
    repository.list_heart_rate_series.return_value = {7: [(0, 120)]}
    stats = SimpleNamespace(
        activity_id=7,
        start_time=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
    )
    page.app = SimpleNamespace(  # ty:ignore[invalid-assignment]
        database=SimpleNamespace(stat_calc=Mock()),
        jobs=jobs,
    )
    page.sort_id = "date_desc"
    page.filter_id = "week"
    page._stats_backfill_in_progress = False
    page._stats_reload_pending = False
    page._fetch_stats_rows = Mock(return_value=[stats])
    page._get_repository = Mock(return_value=repository)

    page._reload_everything()
    work = jobs.submit.call_args.args[1]
    result = work(CancellationToken())

    page._fetch_stats_rows.assert_called_once_with("week")
    repository.list_heart_rate_series.assert_called_once_with([7])
    assert result.filter_id == "week"
    assert result.heart_rate_series == {7: [(0, 120)]}


def test_history_backfill_coalesces_reload_for_current_filter() -> None:
    page = HistoryPageUI.__new__(HistoryPageUI)
    jobs = Mock()
    repository = Mock()
    repository.list_heart_rate_series.return_value = {}
    page.app = SimpleNamespace(  # ty:ignore[invalid-assignment]
        database=SimpleNamespace(stat_calc=Mock()),
        jobs=jobs,
    )
    page.filter_id = "week"
    page.sort_id = "date_desc"
    page._stats_backfill_in_progress = False
    page._stats_reload_pending = False
    page._fetch_stats_rows = Mock(return_value=[])
    page._get_repository = Mock(return_value=repository)
    page._bind_summary = Mock()
    page._bind_list = Mock()
    page._redraw_compare_chart = Mock()

    page._reload_everything()
    first_job = jobs.submit.call_args
    page.filter_id = "month"
    page._reload_everything()
    assert jobs.submit.call_count == 1

    first_result = first_job.args[1](CancellationToken())
    first_job.kwargs["on_success"](first_result)
    page._bind_summary.assert_not_called()
    first_job.kwargs["on_finally"]()
    assert jobs.submit.call_count == EXPECTED_COALESCED_SUBMISSIONS

    second_job = jobs.submit.call_args
    second_result = second_job.args[1](CancellationToken())
    second_job.kwargs["on_success"](second_result)
    second_job.kwargs["on_finally"]()

    assert page._fetch_stats_rows.call_args_list == [
        (("week",),),
        (("month",),),
    ]
    page._bind_summary.assert_called_once_with([])
    assert not page._stats_backfill_in_progress
    assert not page._stats_reload_pending


def test_history_export_reads_and_conversion_run_in_submitted_job(monkeypatch) -> None:
    page = HistoryPageUI.__new__(HistoryPageUI)
    jobs = Mock()
    repository = Mock()
    activity = SimpleNamespace(
        start_time=datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC),
    )
    repository.get_activity.return_value = activity
    repository.list_heart_rates.return_value = []
    repository.list_running_metrics.return_value = []
    repository.list_cycling_metrics.return_value = []
    repository.get_activity_stats.return_value = SimpleNamespace(
        sport_type_id=SportTypesEnum.running.value,
    )
    page.app = SimpleNamespace(jobs=jobs, show_toast=Mock())  # ty:ignore[invalid-assignment]
    page._get_repository = Mock(return_value=repository)
    exporter = Mock(return_value=b"tcx")
    monkeypatch.setattr("fitness_tracker.ui.pages.history.activity_to_tcx", exporter)

    page._on_export_clicked(7)

    repository.get_activity.assert_not_called()
    work = jobs.submit.call_args.args[1]
    result = work(CancellationToken())
    repository.get_activity.assert_called_once_with(7)
    repository.list_heart_rates.assert_called_once_with(7)
    repository.list_running_metrics.assert_called_once_with(7)
    repository.list_cycling_metrics.assert_called_once_with(7)
    repository.get_activity_stats.assert_called_once_with(7)
    assert result.tcx_bytes == b"tcx"


def test_history_export_writes_selected_gio_target_asynchronously(monkeypatch) -> None:
    page = HistoryPageUI.__new__(HistoryPageUI)
    page.app = SimpleNamespace(window=object(), show_toast=Mock())  # ty:ignore[invalid-assignment]
    selected = Mock()
    selected.get_basename.return_value = "activity"
    parent = Mock()
    selected.get_parent.return_value = parent
    target = Mock()
    target.get_parse_name.return_value = "sftp://example/activity.tcx"
    parent.get_child.return_value = target
    write_response = object()

    def replace_async(*_args, **kwargs) -> None:
        kwargs["callback"](target, write_response)

    target.replace_contents_async.side_effect = replace_async
    dialog = Mock()
    dialog.save_finish.return_value = selected
    dialog.save.side_effect = lambda _window, _cancellable, callback: callback(dialog, object())
    file_factory = Mock()
    file_filter = Mock()
    fake_gtk = SimpleNamespace(
        FileDialog=SimpleNamespace(new=Mock(return_value=dialog)),
        FileFilter=Mock(return_value=file_filter),
    )
    fake_gio = SimpleNamespace(
        File=SimpleNamespace(new_for_path=file_factory),
        FileCreateFlags=SimpleNamespace(REPLACE_DESTINATION=object()),
    )
    monkeypatch.setattr(history_module, "Gtk", fake_gtk)
    monkeypatch.setattr(history_module, "Gio", fake_gio)

    page._show_export_dialog(
        history_module._HistoryExportResult("activity.tcx", b"tcx"),
    )

    parent.get_child.assert_called_once_with("activity.tcx")
    target.replace_contents_async.assert_called_once()
    target.replace_contents_finish.assert_called_once_with(write_response)
    page.app.show_toast.assert_called_once_with("Saved: sftp://example/activity.tcx")


def test_idle_once_callback_explicitly_removes_source(monkeypatch) -> None:
    scheduled: list[object] = []
    callback = Mock()
    monkeypatch.setattr(
        settings_page_module.GLib,
        "idle_add",
        scheduled.append,
    )

    settings_page_module._idle_once(callback)

    assert len(scheduled) == 1
    assert scheduled[0]() is False
    callback.assert_called_once_with()


def test_incline_clamp_applies_to_initial_values() -> None:
    assert InclineControl._clamp_value(25) == InclineControl.MAX_PCT
    assert InclineControl._clamp_value(-25) == InclineControl.MIN_PCT
