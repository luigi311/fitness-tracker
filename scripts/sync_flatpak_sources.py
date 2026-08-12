"""Keep Flatpak Git source pins synchronized with uv.lock."""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

_MANIFEST_SOURCE = re.compile(
    r"(?m)^(?P<indent>[ \t]+)url:\s*(?P<url>\S+)\s*\n"
    r"(?P=indent)commit:\s*(?P<revision>[0-9a-f]{40})\s*$",
)
_REVISION = re.compile(r"^[0-9a-f]{40}$")


def _base_url(url: str) -> str:
    """Remove uv source query parameters from a repository URL."""
    return url.split("?", maxsplit=1)[0].rstrip("/")


def _lock_sources(path: Path) -> dict[str, str]:
    """Return repository URLs and revisions recorded in uv.lock."""
    lock = tomllib.loads(path.read_text(encoding="utf-8"))
    sources: dict[str, str] = {}
    for package in lock.get("package", []):
        source = package.get("source")
        if not isinstance(source, dict):
            continue
        git_url = source.get("git")
        if not isinstance(git_url, str) or "#" not in git_url:
            continue
        url, revision = git_url.rsplit("#", maxsplit=1)
        if not _REVISION.fullmatch(revision):
            message = f"uv.lock has an invalid Git revision for {_base_url(url)}"
            raise ValueError(message)
        normalized_url = _base_url(url)
        previous = sources.setdefault(normalized_url, revision)
        if previous != revision:
            message = f"uv.lock pins {normalized_url} to multiple revisions"
            raise ValueError(message)
    return sources


def _synchronize(manifest: str, locked_sources: dict[str, str]) -> tuple[str, list[str]]:
    """Return synchronized manifest text and a list of changed repositories."""
    changes: list[str] = []
    replacements: list[tuple[int, int, str]] = []
    for match in _MANIFEST_SOURCE.finditer(manifest):
        url = _base_url(match.group("url"))
        expected = locked_sources.get(url)
        if expected is None:
            message = f"manifest Git source is missing from uv.lock: {url}"
            raise ValueError(message)
        actual = match.group("revision")
        if actual != expected:
            replacements.append((match.start("revision"), match.end("revision"), expected))
            changes.append(f"{url}: {actual} -> {expected}")

    if not changes:
        return manifest, changes

    synchronized = manifest
    for start, end, revision in reversed(replacements):
        synchronized = f"{synchronized[:start]}{revision}{synchronized[end:]}"
    return synchronized, changes


def main() -> int:
    """Synchronize or validate the Flatpak manifest's Git pins."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail instead of updating the manifest",
    )
    parser.add_argument("--lock", type=Path, default=Path("uv.lock"))
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("com.luigi311.fitness-tracker.yaml"),
    )
    args = parser.parse_args()

    try:
        locked_sources = _lock_sources(args.lock)
        manifest = args.manifest.read_text(encoding="utf-8")
        synchronized, changes = _synchronize(manifest, locked_sources)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as error:
        print(f"Flatpak source synchronization failed: {error}", file=sys.stderr)
        return 1

    if not changes:
        print("Flatpak Git source pins match uv.lock.")
        return 0

    if args.check:
        print("Flatpak Git source pins differ from uv.lock:", file=sys.stderr)
        for change in changes:
            print(f"  {change}", file=sys.stderr)
        return 1

    args.manifest.write_text(synchronized, encoding="utf-8")
    print("Updated Flatpak Git source pins:")
    for change in changes:
        print(f"  {change}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
