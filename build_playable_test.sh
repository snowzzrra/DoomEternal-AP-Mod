#!/usr/bin/env bash
set -e
exec "$(dirname "$0")/scripts/build/playable_test.sh" "$@"
