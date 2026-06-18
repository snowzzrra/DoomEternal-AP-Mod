#!/bin/bash
NEW_CMD=()
SKIP_NEXT=0

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"

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

# 1. Start the C++ background client in the same Proton prefix
"${NEW_CMD[@]}" &

# 2. Start the game
exec "$@"
