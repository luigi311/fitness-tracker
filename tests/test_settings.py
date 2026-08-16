"""Settings migration, validation, recovery, and persistence contracts."""

import json
from pathlib import Path

import pytest
from fitness_tracker.core import settings as settings_module
from fitness_tracker.core.settings import AppSettings
from fitness_tracker.core.units import UnitSystem
from pydantic import ValidationError

PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
VALID_WEIGHT_KG = 72.0
REOPENED_SETTINGS_MESSAGE = "settings path was reopened"


def test_legacy_values_are_migrated_and_optional_strings_normalized() -> None:
    settings = AppSettings.model_validate(
        {
            "trainer_running": {
                "hr_name": "Trainer supplied",
                "hr_address": "old-address",
            },
            "running_sensors": {"hr_name": "  ", "hr_address": "  "},
            "icu": {"athlete_id": "  ", "api_key": " key "},
            "database": {"dsn": " sqlite:///:memory: "},
            "display": {"unit_system": "metric"},
        },
    )

    assert settings.schema_version == 1
    assert settings.trainer_running.hr_name is None
    assert settings.trainer_running.hr_address is None
    assert settings.trainer_running.trainer_supplied_hr is True
    assert settings.running_sensors.hr_name is None
    assert settings.icu.athlete_id is None
    assert settings.icu.api_key == "key"
    assert settings.database.dsn == "sqlite:///:memory:"
    assert settings.display.unit_system is UnitSystem.METRIC
    assert settings.model_dump()["display"]["unit_system"] == "metric"


def test_location_settings_defaults_and_round_trip(tmp_path: Path) -> None:
    defaults = AppSettings.model_validate({})

    assert defaults.location.record_outdoor_routes is True
    assert defaults.location.record_indoor_anchor is False
    assert defaults.location.indoor_accuracy == "neighborhood"

    settings = AppSettings.load(tmp_path, create_if_missing=True)
    settings.location.record_outdoor_routes = False
    settings.location.record_indoor_anchor = True
    settings.location.indoor_accuracy = "street"
    settings.save()

    loaded = AppSettings.load(tmp_path)

    assert loaded.location.record_outdoor_routes is False
    assert loaded.location.record_indoor_anchor is True
    assert loaded.location.indoor_accuracy == "street"
    assert loaded.schema_version == AppSettings.CURRENT_SCHEMA_VERSION


def test_invalid_domain_values_identify_the_rejected_field() -> None:
    invalid_values = [
        ({"personal": {"weight_kg": 1}}, "weight_kg"),
        ({"personal": {"resting_hr": 100, "max_hr": 100}}, "resting_hr"),
        ({"pebble": {"port": 0}}, "port"),
        ({"pebble": {"uuid": "not-a-uuid"}}, "uuid"),
        ({"database": {"dsn": "mysql://localhost/example"}}, "dsn"),
        ({"schema_version": 2}, "schema_version"),
    ]

    for payload, field in invalid_values:
        with pytest.raises(ValidationError, match=field):
            AppSettings.model_validate(payload)


def test_invalid_settings_preserve_the_original_and_recover_valid_fields(tmp_path: Path) -> None:
    payload = {
        "personal": {"weight_kg": VALID_WEIGHT_KG, "ftp_watts": 0},
        "running_sensors": {"hr_name": "Chest strap", "hr_address": "AA:BB"},
        "icu": {"api_key": "secret"},
    }
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps(payload), encoding="utf8")

    settings = AppSettings.load(tmp_path)

    assert settings.personal.weight_kg == VALID_WEIGHT_KG
    assert settings.personal.ftp_watts == AppSettings().personal.ftp_watts
    assert settings.running_sensors.hr_address == "AA:BB"
    assert settings.icu.api_key == "secret"
    assert settings.recovery_message is not None
    rejected = list(tmp_path.glob("settings.json.rejected-*"))
    assert len(rejected) == 1
    assert json.loads(rejected[0].read_text(encoding="utf8")) == payload


def test_newer_settings_schema_is_preserved_and_rejected(tmp_path: Path) -> None:
    payload = {"schema_version": AppSettings.CURRENT_SCHEMA_VERSION + 1}
    settings_path = tmp_path / "settings.json"
    source_bytes = json.dumps(payload).encode()
    settings_path.write_bytes(source_bytes)

    with pytest.raises(ValidationError, match="newer than supported"):
        AppSettings.load(tmp_path)

    rejected = list(tmp_path.glob("settings.json.rejected-*"))
    assert len(rejected) == 1
    assert rejected[0].read_bytes() == source_bytes


def test_recovery_does_not_reopen_the_validated_settings_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_path = tmp_path / "settings.json"
    source_bytes = b'{"personal":{"ftp_watts":0}}'
    settings_path.write_bytes(source_bytes)
    original_read_bytes = Path.read_bytes
    original_read_text = Path.read_text

    def guarded_read_bytes(path: Path) -> bytes:
        if path == settings_path:
            raise AssertionError(REOPENED_SETTINGS_MESSAGE)
        return original_read_bytes(path)

    def guarded_read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path == settings_path:
            raise AssertionError(REOPENED_SETTINGS_MESSAGE)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    settings = AppSettings.load(tmp_path)

    assert settings.personal.ftp_watts == AppSettings().personal.ftp_watts
    rejected = list(tmp_path.glob("settings.json.rejected-*"))
    assert len(rejected) == 1
    assert original_read_bytes(rejected[0]) == source_bytes


def test_settings_paths_are_created_and_repaired_with_private_permissions(tmp_path: Path) -> None:
    settings_dir = tmp_path / "config"
    settings_dir.mkdir(mode=0o755)

    settings = AppSettings.load(settings_dir, create_if_missing=True)
    settings_path = settings_dir / "settings.json"
    rejected_path = settings_dir / "settings.json.rejected-old"
    rejected_path.write_text("rejected", encoding="utf8")
    rejected_path.chmod(0o644)
    settings_dir.chmod(0o755)
    settings_path.chmod(0o644)
    settings = AppSettings.load(settings_dir)
    settings.save()

    assert settings_dir.stat().st_mode & 0o777 == PRIVATE_DIRECTORY_MODE
    assert settings_path.stat().st_mode & 0o777 == PRIVATE_FILE_MODE
    assert rejected_path.stat().st_mode & 0o777 == PRIVATE_FILE_MODE


def test_atomic_save_keeps_the_original_when_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AppSettings.load(tmp_path, create_if_missing=True)
    settings_path = tmp_path / "settings.json"
    original = settings_path.read_bytes()
    settings.icu.api_key = "new-secret"

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError

    monkeypatch.setattr(settings_module.Path, "replace", fail_replace)

    with pytest.raises(OSError, match="Failed to save settings"):
        settings.save()

    assert settings_path.read_bytes() == original
    assert not list(tmp_path.glob(".settings.json.*"))
