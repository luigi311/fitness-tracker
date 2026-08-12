# ruff: noqa: SLF001
"""Regression coverage for release and Flatpak metadata validation."""

import sys
from pathlib import Path

import pytest
from scripts import sync_flatpak_sources, validate_release

_REVISION = "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.parametrize(
    "version",
    [
        "0.0.0",
        "1.2.3",
        "1.2.3-rc.1+build.5",
        "1.2.3-alpha-1+build.01",
    ],
)
def test_validate_release_accepts_semver_versions(version: str) -> None:
    assert validate_release._version(version, "test") == version


@pytest.mark.parametrize(
    "version",
    [
        "01.2.3",
        "1.02.3",
        "1.2.03",
        "1.2.3-01",
        "1.2.3-rc..1",
        "1.2.3+build..5",
    ],
)
def test_validate_release_rejects_invalid_semver_versions(version: str) -> None:
    with pytest.raises(ValueError, match="supported semantic version"):
        validate_release._version(version, "test")


@pytest.mark.parametrize("revision", ["abc123", "g" * 40])
def test_sync_check_rejects_invalid_manifest_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    revision: str,
) -> None:
    lock_path = tmp_path / "uv.lock"
    lock_path.write_text(
        "[[package]]\n"
        'name = "example"\n'
        'version = "1.0.0"\n'
        f'source = {{ git = "https://example.com/example#{_REVISION}" }}\n',
        encoding="utf-8",
    )
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        "sources:\n"
        "  - type: git\n"
        "    url: https://example.com/example\n"
        f"    commit: {revision}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "sync_flatpak_sources.py",
            "--check",
            "--lock",
            str(lock_path),
            "--manifest",
            str(manifest_path),
        ],
    )

    assert sync_flatpak_sources.main() == 1
    assert "invalid Git revision" in capsys.readouterr().err
