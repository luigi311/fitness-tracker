#!/usr/bin/env bash
# Regenerate builders/pip-sources.json from uv.lock.
# Run this whenever dependencies change (after `uv lock`).
# Output into the flatpak directory
#
# Requires: uv
#   - uv:                   https://docs.astral.sh/uv/
#
# Usage:
#   ./regenerate_flatpak_deps.sh "PATH"

set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

uv sync --dev --frozen --group flatpak

# Export from uv.lock → requirements.txt.
#    --no-emit-project     : skip fitness-tracker itself
#    --no-hashes           : flatpak-pip-generator adds its own sha256
#    --no-dev              : skip pytest/ruff/ty/vulture
#    --no-group            : skip flatpak group
#    --no-emit-package X   : skip packages we handle elsewhere
uv export \
    --frozen \
    --format requirements-txt \
    --no-hashes \
    --no-emit-project \
    --no-dev \
    --no-group flatpak \
    --no-emit-package pygobject \
    --no-emit-package bleaksport \
    --no-emit-package libpebble2 \
    --no-emit-package cobble-client \
    --no-emit-package pyftms \
    --no-emit-package workout-parser \
    > requirements.raw.txt

# Remove non linux packages
grep -vE "sys_platform == '(darwin|win32)'" requirements.raw.txt | \
grep -vE "platform_system == '(Darwin|Windows)'" | \
sed -E "s/ ;.*$//" \
    > requirements.txt


# Inject build tools as some deps require them
{
    echo "hatchling==1.29.0"
} > builder-requirements.txt


DIR="${1%/}"
OUT_BUILDERS="${DIR}/builders.json"
OUT_PIP="${DIR}/pip-sources.json"

# For some reason doesnt work with uv run and only works if ran directly.
"$PROJECT_DIR/.venv/bin/python" -m flatpak_pip_generator \
    --runtime='org.gnome.Sdk//50' \
    --requirements-file=builder-requirements.txt \
    --output="$OUT_BUILDERS"

# Match target platforms to python version in the gnome sdk
"$PROJECT_DIR/.venv/bin/python" -m req2flatpak --requirements-file requirements.txt --target-platforms 313-x86_64 313-aarch64 > "$OUT_PIP"

# Notify that files were written
echo "Wrote $OUT_BUILDERS"
echo "Wrote $OUT_PIP"

# Keep the manually maintained Git source section synchronized with uv.lock.
"$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/scripts/sync_flatpak_sources.py"
