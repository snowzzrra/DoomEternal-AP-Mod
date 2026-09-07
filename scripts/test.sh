#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
case "${1:-fast}" in
    fast|affected|changed|integration|package-preflight|playtest|seed-smoke)
        exec "$root/scripts/pipeline.sh" "${1:-fast}" "${@:2}"
        ;;
    map)
        exec "$root/scripts/pipeline.sh" map "${@:2}"
        ;;
    package)
        exec "$root/scripts/pipeline.sh" package "${@:2}"
        ;;
    full|release)
        exec "$root/scripts/pipeline.sh" "$1" "${@:2}"
        ;;
    *)
        echo "usage: scripts/test.sh {fast|affected|map <key>|package-preflight|playtest|full|release|seed-smoke}" >&2
        exit 2
        ;;
esac
