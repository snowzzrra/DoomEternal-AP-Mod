#!/bin/bash
NEW_CMD=()
SKIP_NEXT=0

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
AP_CLIENT_DELAY="${AP_CLIENT_DELAY:-12}"

for arg in "$@"; do
    if [[ $SKIP_NEXT -eq 1 ]]; then
        SKIP_NEXT=0
        continue
    fi

    # Steam executes the launcher first, not x64vk.exe directly
    if [[ "$arg" == *"launcher/idTechLauncher.exe"* || "$arg" == *"DOOMEternalx64vk.exe"* ]]; then
        NEW_CMD+=("$DIR/ap_client.exe" "$PWD")
    elif [[ "$arg" == *"waitforexitandrun"* ]]; then
        # Use 'run' to prevent Steam from blocking on the background client
        NEW_CMD+=("${arg/waitforexitandrun/run}")
    elif [[ "$arg" == "+com_skipSignInManager" || "$arg" == "+com_skipBethesdaMessage" ]]; then
        SKIP_NEXT=1
        continue
    else
        NEW_CMD+=("$arg")
    fi
done

echo "Starting AP Client: ${NEW_CMD[@]}" >> "$DIR/bridge_debug.log"

# Remove injector processes left behind by an earlier game session. They can
# reconnect to a new Meathook server and create multiple competing RPC clients.
pkill -f '[/\\]ap_client\.exe' 2>/dev/null || true

# Let DOOM and Meathook finish their initial startup before opening the RPC
# client. AP_CLIENT_DELAY can be overridden in Steam launch options.
(
    sleep "$AP_CLIENT_DELAY"
    "${NEW_CMD[@]}"
) &

# Start the game immediately.
exec "$@"
