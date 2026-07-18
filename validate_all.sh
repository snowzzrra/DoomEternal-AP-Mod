#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VALIDATION_BUILD_DIR="$SCRIPT_DIR/build/release/build/validation"

cd "$SCRIPT_DIR"
mkdir -p "$VALIDATION_BUILD_DIR"
bash -n build_client.sh build_playable_test.sh validate_all.sh \
    validate_runtime_install.sh
python3 -m py_compile \
    ap_map_generator.py \
    bootstrap_actions.py \
    bridge_client.py \
    challenge_registry.py \
    foundation.py \
    logic_decl_patcher.py \
    mission_complete_map_patcher.py \
    mastery_decl_builder.py \
    mission_challenge_decl_builder.py \
    rune_decl_builder.py \
    save_decrypt.py \
    save_inspector.py \
    validate_data.py \
    validate_windows_runtime_deps.py \
    tools/test_save_scenarios.py \
    tools/generate_foundation_test_plan.py \
    tools/audit_scripted_location.py \
    tools/audit_packaged_transition_bridge.py
python3 tools/audit_scripted_location.py --contracts data/scripted_location_contracts.json
python3 -m unittest \
    tests.test_check_events \
    tests.test_challenge_locations \
    tests.test_ap_map_generator \
    tests.test_validate_data \
      tests.test_foundation \
      tests.test_logic_decl_patcher \
      tests.test_mission_complete_map_patcher \
      tests.test_scripted_location_contracts \
      tests.test_save_scenarios
python3 validate_data.py
g++ -std=c++17 tests/test_ap_client_path_utils.cpp ap_client_path_utils.cpp \
    -o "$VALIDATION_BUILD_DIR/test_ap_client_path_utils"
"$VALIDATION_BUILD_DIR/test_ap_client_path_utils"

./build_client.sh
python3 -m unittest \
    tests.test_check_events.StickySaveMetricTests.test_parser_accepts_provided_24_and_25_snapshots
python3 validate_windows_runtime_deps.py \
    "$SCRIPT_DIR/build/release/build/client" \
    --forbid-local version.dll \
    --forbid-local dinput8.dll \
    --forbid-local dxgi.dll \
    --forbid-local xinput1_4.dll

distrobox enter doom-cpp -- bash -lc "
    cd '$SCRIPT_DIR/../Archipelago'
    python3.11 -m unittest discover \
        -s worlds/doometernal/test -p 'test_*.py'
"

distrobox enter doom-cpp -- bash -lc "
    cd '$SCRIPT_DIR/../Archipelago'
    python3.11 Generate.py \
        --player_files_path '$SCRIPT_DIR/player_templates' \
        --outputpath /tmp/doom-eap-validation
"
