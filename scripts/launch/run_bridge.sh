#!/bin/bash
set -u

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
AP_CLIENT_DELAY="${AP_CLIENT_DELAY:-12}"
client_pid=""
game_pid=""
cleanup() {
    if [[ -n "$client_pid" ]]; then
        kill "$client_pid" 2>/dev/null || true
        wait "$client_pid" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

if [[ -x "$DIR/ap_client.exe" ]]; then
    (
        sleep "$AP_CLIENT_DELAY"
        exec "$DIR/ap_client.exe" "$PWD"
    ) &
    client_pid=$!
fi
"$@" &
game_pid=$!
wait "$game_pid"
status=$?
trap - EXIT INT TERM
cleanup
exit "$status"
