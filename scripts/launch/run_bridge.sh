#!/bin/bash
NEW_CMD=()
GAME_CMD=()
SKIP_NEXT=0

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
AP_CLIENT_DELAY="${AP_CLIENT_DELAY:-12}"

for arg in "$@"; do
    # the prepared AP session keeps wineserver alive before the game starts
    if [[ "$arg" == "waitforexitandrun" || "$arg" == "--verb=waitforexitandrun" ]]; then
        arg="${arg/waitforexitandrun/run}"
    fi
    GAME_CMD+=("$arg")
    if [[ $SKIP_NEXT -eq 1 ]]; then
        SKIP_NEXT=0
        continue
    fi

    # Steam executes the launcher first, not x64vk.exe directly
    if [[ "$arg" == *"launcher/idTechLauncher.exe"* || "$arg" == *"DOOMEternalx64vk.exe"* ]]; then
        NEW_CMD+=("$DIR/ap_client.exe" "$PWD")
    elif [[ "$arg" == "+com_skipSignInManager" || "$arg" == "+com_skipBethesdaMessage" ]]; then
        SKIP_NEXT=1
        continue
    else
        NEW_CMD+=("$arg")
    fi
done

echo "Starting AP Client: ${NEW_CMD[@]}" >> "$DIR/bridge_debug.log"

# allow Core to start before the client connects; Steam can override AP_CLIENT_DELAY.
(
    sleep "$AP_CLIENT_DELAY"
    "${NEW_CMD[@]}"
) &

exec "${GAME_CMD[@]}"
