#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ap_root="$(cd "$root/../Archipelago" && pwd)"
ap_python="${ARCHIPELAGO_PYTHON:-$ap_root/.venv/bin/python}"
PYTHONPATH="$root" SKIP_REQUIREMENTS_UPDATE=1 "$ap_python" \
    "$root/tools/content/compile_options_schema.py" \
    --archipelago-root "$ap_root" --check
PYTHONPATH="$root" "$ap_python" -m tools.content.compile_start_inventory_catalog --check
exec "$root/scripts/pipeline.sh" release "$@"
