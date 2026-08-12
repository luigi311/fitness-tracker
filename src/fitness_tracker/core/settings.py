"""Validated, persistence-facing application settings."""

import contextlib
import json
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Self, cast
from urllib.parse import urlparse
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_file_settings import FileSettings
from pydantic_settings import SettingsConfigDict

from fitness_tracker.core.file_permissions import secure_directory, secure_file
from fitness_tracker.core.units import UnitSystem

SETTINGS_SCHEMA_VERSION = 1
TRAINER_SUPPLIED_HR_LABEL = "Trainer supplied"
_SETTINGS_RECOVERY_PREFIX = "settings.json.rejected-"


def _fsync_directory(path: Path) -> None:
    """Flush a directory entry on platforms that support directory fsync."""
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class SettingsModel(BaseModel):
    """Common validation policy for nested settings models."""

    model_config = ConfigDict(validate_assignment=True, extra="ignore")

    @field_validator("*", mode="before")
    @classmethod
    def _normalize_strings(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value


class PersonalSettings(SettingsModel):
    """Personal measurements and training thresholds."""

    weight_kg: float = Field(default=80.0, ge=30.0, le=225.0)
    resting_hr: int = Field(default=60, ge=30, le=120)
    max_hr: int = Field(default=200, ge=100, le=250)
    lthr_bpm: int | None = Field(default=None, ge=1, le=250)
    ftp_watts: int = Field(default=150, ge=50, le=2000)

    @field_validator("lthr_bpm", mode="before")
    @classmethod
    def _zero_lthr_is_unset(cls, value: object) -> object:
        return None if value == 0 else value

    @model_validator(mode="after")
    def _validate_heart_rate_relationships(self) -> Self:
        if self.resting_hr >= self.max_hr:
            message = "resting_hr must be lower than max_hr"
            raise ValueError(message)
        if self.lthr_bpm is not None and not self.resting_hr < self.lthr_bpm < self.max_hr:
            message = "lthr_bpm must be between resting_hr and max_hr"
            raise ValueError(message)
        return self


class HeartRateSource(SettingsModel):
    """A selectable heart-rate source shared by sensor profiles."""

    hr_name: str | None = None
    hr_address: str | None = None


class SensorSettings(HeartRateSource):
    """BLE sensors used by a non-trainer profile."""

    speed_name: str | None = None
    speed_address: str | None = None
    cadence_name: str | None = None
    cadence_address: str | None = None
    power_name: str | None = None
    power_address: str | None = None


class TrainerSettings(HeartRateSource):
    """FTMS trainer and its optional heart-rate source."""

    trainer_name: str | None = None
    trainer_address: str | None = None
    # bleaksport.MachineType is a Flag. Keep its numeric value here so
    # the core settings module does not import the BLE integration package.
    trainer_machine_type: int | None = None
    trainer_supplied_hr: bool = False

    @model_validator(mode="before")
    @classmethod
    def _migrate_hr_sentinel(cls, data: object) -> object:
        if isinstance(data, dict) and data.get("hr_name") == TRAINER_SUPPLIED_HR_LABEL:
            return {
                **data,
                "hr_name": None,
                "hr_address": None,
                "trainer_supplied_hr": True,
            }
        return data


class PebbleSettings(SettingsModel):
    """Pebble bridge configuration."""

    enable: bool = False
    uuid: str = "f4fcdac7-f58e-4d22-96bd-48cf98e25d09"
    use_emulator: bool = False
    port: int = Field(default=47527, ge=1, le=65535)
    name: str | None = None
    address: str | None = None

    @field_validator("uuid")
    @classmethod
    def _validate_uuid(cls, value: str) -> str:
        try:
            return str(UUID(value))
        except ValueError as exc:
            message = "uuid must be a valid UUID"
            raise ValueError(message) from exc


class IntervalsIcuAPI(SettingsModel):
    """Intervals.icu credentials, validated when the provider is enabled."""

    athlete_id: str | None = None
    api_key: str | None = None


class DatabaseSettings(SettingsModel):
    """Optional remote database connection settings."""

    dsn: str | None = None

    @field_validator("dsn")
    @classmethod
    def _validate_dsn_scheme(cls, value: str | None) -> str | None:
        if value is None:
            return None
        scheme = urlparse(value).scheme.lower()
        if scheme not in {"sqlite", "postgres", "postgresql"} and not (
            scheme.startswith(("sqlite+", "postgresql+"))
        ):
            message = "dsn must use a sqlite or postgresql scheme"
            raise ValueError(message)
        return value


class DisplaySettings(SettingsModel):
    """User-facing display preferences."""

    unit_system: UnitSystem = UnitSystem.IMPERIAL


class AppSettings(FileSettings):
    """Application settings persisted as a versioned JSON document."""

    model_config = SettingsConfigDict(
        env_prefix="FITNESS_TRACKER_",
        nested_model_default_partial_update=True,
        validate_assignment=True,
        extra="ignore",
    )

    CURRENT_SCHEMA_VERSION: ClassVar[int] = SETTINGS_SCHEMA_VERSION
    schema_version: int = Field(default=SETTINGS_SCHEMA_VERSION, ge=1, le=SETTINGS_SCHEMA_VERSION)
    personal: PersonalSettings = Field(default_factory=PersonalSettings)
    running_sensors: SensorSettings = Field(default_factory=SensorSettings)
    cycling_sensors: SensorSettings = Field(default_factory=SensorSettings)
    trainer_running: TrainerSettings = Field(default_factory=TrainerSettings)
    trainer_cycling: TrainerSettings = Field(default_factory=TrainerSettings)
    pebble: PebbleSettings = Field(default_factory=PebbleSettings)
    display: DisplaySettings = Field(default_factory=DisplaySettings)
    icu: IntervalsIcuAPI = Field(default_factory=IntervalsIcuAPI)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)

    _recovery_message: str | None = PrivateAttr(default=None)

    @model_validator(mode="before")
    @classmethod
    def _migrate_schema(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data

        values = dict(data)
        version = values.get("schema_version", 0)
        if isinstance(version, int) and version > cls.CURRENT_SCHEMA_VERSION:
            message = (
                f"schema_version {version} is newer than supported version "
                f"{cls.CURRENT_SCHEMA_VERSION}"
            )
            raise ValueError(message)

        # v0 was the unversioned v4.x representation. The nested models perform
        # their own migrations, including the trainer HR sentinel conversion.
        values["schema_version"] = cls.CURRENT_SCHEMA_VERSION
        return values

    @property
    def recovery_message(self) -> str | None:
        """Return a diagnostic when invalid source data was recovered."""
        return self._recovery_message

    @classmethod
    def load(
        cls,
        settings_dir: str | Path,
        create_if_missing: bool = False,  # noqa: FBT001, FBT002 - preserve FileSettings.load override compatibility
    ) -> Self:
        """Load settings, recovering valid fields when the source is partially invalid."""
        settings_dir = Path(settings_dir).resolve()
        secure_directory(settings_dir)
        cls._secure_rejected_files(settings_dir)
        settings_path = settings_dir / cls.__FILENAME__
        if not settings_path.exists():
            return super().load(settings_dir, create_if_missing=create_if_missing)

        try:
            secure_file(settings_path)
            raw_text = settings_path.read_text(encoding="utf8")
        except OSError as exc:
            message = f"Unable to read private settings file {settings_path}: {exc}"
            raise ValueError(message) from exc
        try:
            settings = cls.model_validate_json(raw_text)
        except (ValidationError, ValueError) as exc:
            backup_path, backup_error = cls._backup_rejected_file(settings_path)
            try:
                raw_data: object = json.loads(raw_text)
            except ValueError:
                raw_data = {}
            settings = _recover_settings_model(cls, raw_data)
            settings._settings_dir = settings_dir  # noqa: SLF001 - initialize inherited FileSettings state
            if backup_path is not None:
                backup_message = f"Rejected file preserved as {backup_path.name}."
            else:
                backup_message = f"Could not preserve rejected settings: {backup_error}."
            settings._recovery_message = (  # noqa: SLF001 - set recovery diagnostics on the loaded model
                f"Invalid settings; valid fields recovered. {backup_message} {exc}"
            )
            return settings

        settings._settings_dir = settings_dir  # noqa: SLF001 - initialize inherited FileSettings state
        return settings

    def save(self) -> None:
        """Persist settings using user-only directory and file permissions."""
        settings_dir = self._settings_dir
        settings_path = settings_dir / self.__FILENAME__
        descriptor: int | None = None
        temporary_path: Path | None = None
        try:
            secure_directory(settings_dir)
            secure_file(settings_path)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.__FILENAME__}.",
                dir=settings_dir,
            )
            temporary_path = Path(temporary_name)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf8") as stream:
                descriptor = None
                stream.write(self.model_dump_json(indent=2))
                stream.flush()
                os.fsync(stream.fileno())
            secure_file(temporary_path)
            temporary_path.replace(settings_path)
            temporary_path = None
            # The replacement was created with mode 0600. Directory fsync is
            # best-effort so a successful replacement is never reported as a
            # failed save when the filesystem cannot flush the directory.
            with contextlib.suppress(OSError):
                _fsync_directory(settings_dir)
        except OSError as exc:
            message = f"Failed to save settings to {settings_path}: {exc}"
            raise OSError(message) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary_path is not None:
                with contextlib.suppress(OSError):
                    temporary_path.unlink()

    @staticmethod
    def _secure_rejected_files(settings_dir: Path) -> None:
        for backup_path in settings_dir.glob(f"{_SETTINGS_RECOVERY_PREFIX}*"):
            if backup_path.is_file():
                secure_file(backup_path)

    @staticmethod
    def _backup_rejected_file(settings_path: Path) -> tuple[Path | None, str | None]:
        try:
            source_bytes = settings_path.read_bytes()
            for backup_path in sorted(
                settings_path.parent.glob(f"{_SETTINGS_RECOVERY_PREFIX}*"),
            ):
                try:
                    if backup_path.is_file() and backup_path.read_bytes() == source_bytes:
                        secure_file(backup_path)
                        return backup_path, None
                except OSError:
                    continue

            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            backup_path = settings_path.with_name(f"{_SETTINGS_RECOVERY_PREFIX}{timestamp}")
            shutil.copy2(settings_path, backup_path)
            secure_file(backup_path)
        except OSError as exc:
            return None, str(exc)
        return backup_path, None

    @classmethod
    def recover(cls, settings_dir: str | Path) -> Self:
        """Return defaults associated with ``settings_dir`` after load failure."""
        secure_directory(Path(settings_dir).resolve())
        settings = cls()
        settings._settings_dir = Path(settings_dir).resolve()
        return settings


def _recover_settings_model[SettingsModelT: BaseModel](
    model_cls: type[SettingsModelT],
    raw_data: object,
) -> SettingsModelT:
    """Recover valid fields recursively while restoring invalid fields to defaults."""
    defaults = model_cls()
    source = raw_data if isinstance(raw_data, dict) else {}
    recovered: dict[str, Any] = {}

    for field_name in model_cls.model_fields:
        if field_name not in source:
            continue

        raw_value = source[field_name]
        default_value = getattr(defaults, field_name)
        if isinstance(default_value, BaseModel):
            if isinstance(raw_value, dict):
                recovered[field_name] = _recover_settings_model(type(default_value), raw_value)
            continue

        try:
            partial = model_cls.model_validate({field_name: raw_value})
        except ValidationError:
            continue
        recovered[field_name] = getattr(partial, field_name)

    try:
        return model_cls.model_validate(recovered)
    except ValidationError:
        if model_cls is PersonalSettings:
            return cast("SettingsModelT", _recover_personal_settings(recovered))
        return defaults


def _recover_personal_settings(values: dict[str, Any]) -> PersonalSettings:
    """Repair cross-field heart-rate constraints after scalar recovery."""
    defaults = PersonalSettings()
    repaired = dict(values)
    resting_hr = repaired.get("resting_hr")
    max_hr = repaired.get("max_hr")
    lthr_bpm = repaired.get("lthr_bpm")

    if isinstance(resting_hr, int) and isinstance(max_hr, int) and resting_hr >= max_hr:
        repaired["resting_hr"] = defaults.resting_hr
        resting_hr = defaults.resting_hr
    if (
        isinstance(resting_hr, int)
        and isinstance(max_hr, int)
        and isinstance(lthr_bpm, int)
        and not resting_hr < lthr_bpm < max_hr
    ):
        repaired["lthr_bpm"] = defaults.lthr_bpm

    try:
        return PersonalSettings.model_validate(repaired)
    except ValidationError:
        return defaults
