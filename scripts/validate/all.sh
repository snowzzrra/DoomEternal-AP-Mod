#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT_DIR="$REPO_ROOT/scripts/validate"
VALIDATION_BUILD_DIR="$REPO_ROOT/build/release/build/validation"

cd "$REPO_ROOT"
mkdir -p "$VALIDATION_BUILD_DIR"
bash -n scripts/build/client.sh scripts/build/playable_test.sh scripts/validate/all.sh \
    scripts/validate/runtime_install.sh

export PYTHONPATH="$REPO_ROOT"

python3 -m py_compile \
    tools/maps/ap_map_generator.py \
    bootstrap_actions.py \
    bridge_client.py \
    challenge_registry.py \
    foundation.py \
    tools/maps/hub_diff_guard.py \
    item_reconciliation.py \
    map_registry.py \
    tools/maps/map_preflight.py \
    tools/maps/map_semantic_baseline.py \
    tools/maps/logic_decl_patcher.py \
    tools/maps/mission_complete_map_patcher.py \
    tools/decls/mastery_decl_builder.py \
    tools/decls/mission_challenge_decl_builder.py \
    tools/decls/rune_decl_builder.py \
    tools/decls/devinv_builder.py \
    tools/validation/validate_challenge_overrides.py \
    tools/diagnostics/save_inspector.py \
    tools/validation/validate_data.py \
    tools/validation/validate_windows_runtime_deps.py \
    tools/diagnostics/save_scenarios.py \
    tools/release/generate_foundation_test_plan.py \
    tools/validation/audit_scripted_location.py \
    tools/validation/audit_packaged_transition_bridge.py

python3 tools/validation/audit_scripted_location.py --contracts data/scripted_location_contracts.json

for test_module in \
    tests.unit.test_check_events \
    tests.unit.test_challenge_locations \
    tests.unit.test_ap_map_generator \
    tests.unit.test_validate_data \
    tests.unit.test_foundation \
    tests.unit.test_item_reconciliation \
    tests.unit.test_logic_decl_patcher \
    tests.unit.test_mission_complete_map_patcher \
    tests.unit.test_scripted_location_contracts \
    tests.unit.test_save_scenarios \
    tests.unit.test_devinv_builder
do
    python3 -m unittest "$test_module"
done

python3 tools/validation/validate_data.py
g++ -std=c++17 tests/native/test_ap_client_path_utils.cpp native/client/ap_client_path_utils.cpp \
    -o "$VALIDATION_BUILD_DIR/test_ap_client_path_utils"
"$VALIDATION_BUILD_DIR/test_ap_client_path_utils"

./scripts/build/client.sh
python3 -m unittest \
    tests.unit.test_check_events.StickySaveMetricTests.test_parser_accepts_provided_24_and_25_snapshots

python3 tools/validation/validate_windows_runtime_deps.py \
    "$REPO_ROOT/build/release/build/client" \
    --forbid-local version.dll \
    --forbid-local dinput8.dll \
    --forbid-local dxgi.dll \
    --forbid-local xinput1_4.dll

distrobox enter doom-cpp -- bash -lc "
    cd '$REPO_ROOT/../Archipelago'
    python3.11 -m unittest discover \
        -s worlds/doometernal/test -p 'test_*.py'
"

distrobox enter doom-cpp -- bash -lc "
    cd '$REPO_ROOT/../Archipelago'
    python3.11 Generate.py \
        --player_files_path '$REPO_ROOT/player_templates' \
        --outputpath /tmp/doom-eap-validation
"
