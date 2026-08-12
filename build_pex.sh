#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$SCRIPT_DIR"

rm -rf -- "$SCRIPT_DIR/dist"

uv export \
    --format requirements-txt \
    --no-emit-project \
    --no-dev \
    --frozen \
    --no-hashes \
    -o "$SCRIPT_DIR/dist/requirements.txt"

uv build

uvx --python .venv/bin/python pex \
    -r "$SCRIPT_DIR/dist/requirements.txt" \
    "$SCRIPT_DIR"/dist/fitness_tracker-*.whl \
    -e fitness_tracker.main:main \
    -o "$SCRIPT_DIR/dist/fitness_tracker.pex" \
    --python-shebang '#!/usr/bin/env python3' \
    --scie eager \
    --scie-pbs-stripped
