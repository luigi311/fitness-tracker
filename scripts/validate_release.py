"""Validate release metadata for the desktop and Pebble applications."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")


def _version(value: object, source: str) -> str:
    """Validate and return a version string from a metadata source."""
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        message = f"{source} does not contain a supported semantic version: {value!r}"
        raise ValueError(message)
    return value


def _project_version(path: Path) -> str:
    """Read the desktop version from pyproject.toml."""
    metadata = tomllib.loads(path.read_text(encoding="utf-8"))
    return _version(metadata["project"]["version"], str(path))


def _metainfo_version(path: Path) -> str:
    """Read the newest release version from AppStream metadata."""
    root = ET.parse(path).getroot()  # noqa: S314  # local AppStream metadata is trusted
    release = root.find("./releases/release")
    if release is None:
        message = f"{path} does not contain a release entry"
        raise ValueError(message)
    return _version(release.get("version"), f"{path} latest release")


def _pebble_version(path: Path) -> str:
    """Read the independently versioned Pebble app manifest."""
    metadata = json.loads(path.read_text(encoding="utf-8"))
    return _version(metadata["version"], str(path))


def main() -> int:
    """Validate desktop and Pebble release metadata."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, default=Path("pyproject.toml"))
    parser.add_argument(
        "--metainfo",
        type=Path,
        default=Path("data/com.luigi311.fitness-tracker.metainfo.xml"),
    )
    parser.add_argument("--pebble", type=Path, default=Path("pebble/package.json"))
    args = parser.parse_args()

    try:
        project = _project_version(args.project)
        metainfo = _metainfo_version(args.metainfo)
        pebble = _pebble_version(args.pebble)
    except (KeyError, OSError, ValueError, json.JSONDecodeError, tomllib.TOMLDecodeError) as error:
        print(f"Release metadata validation failed: {error}", file=sys.stderr)
        return 1

    if project != metainfo:
        print(
            "Release metadata validation failed: "
            f"pyproject.toml is {project}, but the latest metainfo release is {metainfo}.",
            file=sys.stderr,
        )
        return 1

    print(f"Desktop version: {project} (pyproject.toml and metainfo agree)")
    print(f"Pebble version: {pebble} (independent release stream)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
