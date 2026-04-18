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

uv sync --dev --frozen --group flatpak

# Export from uv.lock → requirements.txt.
#    --no-emit-project     : skip fitness-tracker itself
#    --no-hashes           : flatpak-pip-generator adds its own sha256
#    --no-dev              : skip pytest/ruff/ty/vulture
#    --no-emit-package X   : skip packages we handle elsewhere
uv export \
    --format requirements-txt \
    --no-hashes \
    --no-emit-project \
    --no-dev \
    --no-emit-package pygobject \
    --no-emit-package bleaksport \
    --no-emit-package libpebble2 \
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
.venv/bin/python -m flatpak_pip_generator \
    --runtime='org.gnome.Sdk//50' \
    --requirements-file=builder-requirements.txt \
    --output="$OUT_BUILDERS"

# Match target platforms to python version in the gnome sdk
req2flatpak --requirements-file requirements.txt --target-platforms 313-x86_64 313-aarch64 > "$OUT_PIP"

# Notify that files were written
echo "Wrote $OUT_BUILDERS"
echo "Wrote $OUT_PIP"

# Sanity: verify the commits in the manifest match uv.lock.
echo
echo "Git commits in uv.lock — confirm these match your .yaml manifest:"
grep -E '^\s*source = \{ git = ' uv.lock | sed 's/.*"\(http[^"]*\)".*/\1/'
