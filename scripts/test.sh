#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
case "${1:-fast}" in
    fast|changed|integration)
        exec "$root/scripts/pipeline.sh" "${1:-fast}" "${@:2}"
        ;;
    map)
        exec "$root/scripts/pipeline.sh" map "${@:2}"
        ;;
    full|release)
        exec "$root/scripts/pipeline.sh" release "${@:2}"
        ;;
    *)
        echo "usage: scripts/test.sh {fast|map <key>|changed|integration|full}" >&2
        exit 2
        ;;
esac
