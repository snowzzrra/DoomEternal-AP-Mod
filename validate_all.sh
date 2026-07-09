#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cd "$SCRIPT_DIR"
bash -n build_client.sh build_playable_test.sh validate_all.sh \
    validate_runtime_install.sh
python3 -m py_compile \
    ap_map_generator.py \
    bridge_client.py \
    save_decrypt.py \
    save_inspector.py \
    validate_data.py \
    validate_windows_runtime_deps.py
python3 -m unittest tests.test_ap_map_generator tests.test_check_events
python3 validate_data.py
g++ -std=c++17 tests/test_ap_client_path_utils.cpp ap_client_path_utils.cpp \
    -o /tmp/test_ap_client_path_utils
/tmp/test_ap_client_path_utils

RUNTIME_AUDIT_DIR="$(mktemp -d /tmp/doom-eap-runtime-audit.XXXXXX)"
trap 'rm -rf "$RUNTIME_AUDIT_DIR"' EXIT
cp ap_client.exe ap_logger.exe save_death_probe.exe "$RUNTIME_AUDIT_DIR/"
python3 validate_windows_runtime_deps.py \
    "$RUNTIME_AUDIT_DIR" \
    --forbid-local version.dll \
    --forbid-local dinput8.dll \
    --forbid-local dxgi.dll \
    --forbid-local xinput1_4.dll

distrobox enter doom-cpp -- bash -lc "
    cd '$SCRIPT_DIR/../Archipelago'
    python3.11 Generate.py \
        --player_files_path '$SCRIPT_DIR/player_templates' \
        --outputpath /tmp/doom-eap-validation
"
