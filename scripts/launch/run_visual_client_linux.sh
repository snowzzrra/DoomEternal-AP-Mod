#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python"

if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "Run setup_linux_client.sh first." >&2
    exit 1
fi

cd "$SCRIPT_DIR"
exec "$PYTHON_BIN" bridge_client.py "$@"
