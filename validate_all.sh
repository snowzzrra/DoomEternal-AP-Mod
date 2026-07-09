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
    validate_data.py
python3 -m unittest tests.test_ap_map_generator tests.test_check_events
python3 validate_data.py
g++ -std=c++17 tests/test_ap_client_path_utils.cpp ap_client_path_utils.cpp \
    -o /tmp/test_ap_client_path_utils
/tmp/test_ap_client_path_utils

distrobox enter doom-cpp -- bash -lc "
    cd '$SCRIPT_DIR/../Archipelago'
    python3.11 Generate.py \
        --player_files_path '$SCRIPT_DIR/player_templates' \
        --outputpath /tmp/doom-eap-validation
"
