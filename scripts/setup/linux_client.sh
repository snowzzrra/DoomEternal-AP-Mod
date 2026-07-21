#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3.11}"
AP_PATH="${1:-}"

if [[ -z "$AP_PATH" ]]; then
    read -r -p "Path to the Archipelago 0.6.8 folder containing CommonClient.py: " AP_PATH
fi
if [[ ! -f "$AP_PATH/CommonClient.py" ]]; then
    echo "CommonClient.py was not found in: $AP_PATH" >&2
    exit 1
fi

"$PYTHON_BIN" -m venv "$SCRIPT_DIR/.venv"
"$SCRIPT_DIR/.venv/bin/python" -m pip install --upgrade pip
"$SCRIPT_DIR/.venv/bin/python" -m pip install \
    -r "$AP_PATH/requirements.txt" \
    -r "$SCRIPT_DIR/requirements.txt"

echo "Client environment created successfully."
echo "Run $SCRIPT_DIR/run_visual_client_linux.sh next."
