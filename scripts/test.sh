#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

case "${1:-fast}" in
  fast)
    exec python -m pytest -m "unit or catalog or generated" -q --maxfail=1
    ;;
  map)
    test -n "${2:-}" || { echo "usage: scripts/test.sh map <map-key>" >&2; exit 2; }
    exec python -m pytest --map "$2" -m "catalog or generated or integration" -q --maxfail=1
    ;;
  changed)
    exec python -m pytest --changed -m "unit or catalog or generated or integration" -q --maxfail=1
    ;;
  integration)
    exec python -m pytest -m integration -q --maxfail=1
    ;;
  full)
    exec python -m pytest -m "integration or apworld or (slow and not legacy_generated)" -q --maxfail=1
    ;;
  *)
    echo "usage: scripts/test.sh {fast|map <key>|changed|integration|full}" >&2
    exit 2
    ;;
esac
