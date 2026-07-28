#!/bin/bash
# apworld_python.sh — Resolve and validate a Python interpreter for APWorld tests.
#
# Sources this file to set APWORLD_PYTHON to a validated interpreter, or prints
# a diagnostic and exits non-zero.
#
# Usage (as sourced library):
#   source scripts/validate/apworld_python.sh
#   # APWORLD_PYTHON is now set and validated
#
# Usage (standalone preflight):
#   bash scripts/validate/apworld_python.sh
#
# Interpreter selection priority:
#   1. APWORLD_PYTHON environment variable (when already set)
#   2. Archipelago/.venv/bin/python
#   3. <workspace>/.venv/bin/python
#   4. python3.13, python3.12, python3.11 on PATH
#
# Requirements:
#   - Python 3.11–3.13
#   - pytest importable

set -euo pipefail

_apworld_python_resolve() {
    local repo_root workspace_root archip_root
    repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
    workspace_root="$(cd "$repo_root/.." && pwd)"
    archip_root="$workspace_root/Archipelago"

    # Already set by caller
    if [[ -n "${APWORLD_PYTHON:-}" ]]; then
        return 0
    fi

    # Candidate list
    local candidates=(
        "$archip_root/.venv/bin/python"
        "$workspace_root/.venv/bin/python"
    )
    local versioned
    for versioned in python3.13 python3.12 python3.11; do
        local found
        found="$(command -v "$versioned" 2>/dev/null || true)"
        if [[ -n "$found" ]]; then
            candidates+=("$found")
        fi
    done

    for candidate in "${candidates[@]}"; do
        if [[ -x "$candidate" ]]; then
            APWORLD_PYTHON="$candidate"
            return 0
        fi
    done

    return 1
}

_apworld_python_validate() {
    local py="$1"

    # Check version range 3.11–3.13
    if ! "$py" -c "
import sys
v = sys.version_info[:2]
assert (3, 11) <= v <= (3, 13), f'Python {v[0]}.{v[1]} is outside 3.11–3.13'
" 2>/dev/null; then
        local ver
        ver="$("$py" -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')" 2>/dev/null || echo "unknown")"
        _apworld_python_fail "$py" "$ver" "Python version $ver is outside required range 3.11–3.13."
        return 1
    fi

    # Check pytest importable
    if ! "$py" -c "import pytest" 2>/dev/null; then
        local ver
        ver="$("$py" -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')" 2>/dev/null || echo "unknown")"
        _apworld_python_fail "$py" "$ver" "pytest is not installed for this interpreter."
        return 1
    fi

    return 0
}

_apworld_python_fail() {
    local py="$1" ver="$2" reason="$3"
    local repo_root
    repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
    local archip_root="$(cd "$repo_root/.." && pwd)/Archipelago"

    cat >&2 <<EOF

APWorld test environment unavailable.
Selected Python: $py
Version found:   $ver
Reason:          $reason
Required:        Python 3.11–3.13 with pytest.

Create a local venv with:
  python3.11 -m venv $archip_root/.venv
  $archip_root/.venv/bin/python -m pip install pytest

Then rerun with:
  APWORLD_PYTHON=$archip_root/.venv/bin/python $0

EOF
}

# --- Main logic ---

if ! _apworld_python_resolve; then
    cat >&2 <<'EOF'

APWorld test environment unavailable.
No suitable Python 3.11–3.13 interpreter found.
Set APWORLD_PYTHON=/path/to/python or create a venv.

EOF
    # If sourced, just fail; if executed directly, exit
    if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
        exit 1
    fi
    return 1 2>/dev/null || exit 1
fi

if ! _apworld_python_validate "$APWORLD_PYTHON"; then
    if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
        exit 1
    fi
    return 1 2>/dev/null || exit 1
fi

export APWORLD_PYTHON

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    echo "APWorld Python OK: $APWORLD_PYTHON ($($APWORLD_PYTHON --version 2>&1))"
fi
