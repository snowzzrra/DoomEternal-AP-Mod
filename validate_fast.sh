#!/bin/bash
set -euo pipefail

# validate_fast.sh — Hermetic/rapid validation for DOOM Eternal AP Mod.
#
# Runs checks that do NOT depend on local proprietary assets:
#   vanillamaps/, vanilla_decls/, packagemapspec.json, Windows toolchain.
#
# Exits 0 on full PASS, 1 on any failure.
#
# For asset-dependent validation, run validate_all.sh with all assets present.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PASS=0
FAIL=0

pass() { PASS=$((PASS+1)); }
fail() { FAIL=$((FAIL+1)); echo "FAIL: $*" >&2; }

cd "$SCRIPT_DIR"

echo "=== validate_fast: hermetic validation ==="

# ----- 1. Shell syntax -----
echo "--- Shell syntax ---"
bash -n build_client.sh build_playable_test.sh validate_all.sh validate_fast.sh \
    validate_runtime_install.sh && pass || fail "shell syntax"

# ----- 2. Python compilation -----
echo "--- Python py_compile ---"
python3 -m py_compile \
    ap_map_generator.py \
    bootstrap_actions.py \
    bridge_client.py \
    challenge_registry.py \
    foundation.py \
    hub_diff_guard.py \
    item_reconciliation.py \
    map_registry.py \
    map_preflight.py \
    map_semantic_baseline.py \
    logic_decl_patcher.py \
    mission_complete_map_patcher.py \
    mastery_decl_builder.py \
    mission_challenge_decl_builder.py \
    rune_decl_builder.py \
    devinv_builder.py \
    validate_challenge_overrides.py \
    save_decrypt.py \
    save_inspector.py \
    validate_data.py \
    validate_windows_runtime_deps.py \
    tools/test_save_scenarios.py \
    tools/generate_foundation_test_plan.py \
    tools/audit_scripted_location.py \
    tools/audit_packaged_transition_bridge.py && pass || fail "py_compile"

# ----- 3. Registry/contract structural validation -----
echo "--- Registry validation ---"
python3 tools/audit_scripted_location.py --contracts data/scripted_location_contracts.json \
    && pass || fail "scripted location contracts"

# ----- 4. Unit tests (hermetic subset — no vanillamaps/vanilla_decls needed) -----
echo "--- Unit tests (hermetic) ---"
HERMETIC_TEST_MODULES=(
    tests.test_check_events
    tests.test_validate_data
    tests.test_foundation
    tests.test_item_reconciliation
    tests.test_logic_decl_patcher
    tests.test_scripted_location_contracts
    tests.test_save_scenarios
    tests.test_devinv_builder
)
for mod in "${HERMETIC_TEST_MODULES[@]}"; do
    if python3 -m unittest "$mod" 2>/dev/null; then
        pass
    else
        # If test fails due to missing assets (vanillamaps/vanilla_decls), skip with warning
        fail "$mod"
    fi
done

# ----- 5. Hermetic C++ test (if toolchain available) -----
echo "--- C++ path utils test ---"
if command -v g++ &>/dev/null; then
    VALIDATION_BUILD_DIR="$SCRIPT_DIR/build/release/build/validation"
    mkdir -p "$VALIDATION_BUILD_DIR"
    g++ -std=c++17 tests/test_ap_client_path_utils.cpp ap_client_path_utils.cpp \
        -o "$VALIDATION_BUILD_DIR/test_ap_client_path_utils" 2>/dev/null \
        && "$VALIDATION_BUILD_DIR/test_ap_client_path_utils" && pass \
        || fail "C++ path utils test"
else
    echo "  SKIP: g++ not available for C++ test"
fi

# ----- 6. Asset-dependent tests — clear skip messages -----
echo "--- Asset-dependent checks (SKIPPED in validate_fast) ---"
echo "  SKIP: validate_data.py (needs vanillamaps/, packagemapspec.json)"
echo "  SKIP: test_challenge_locations (needs vanilla_decls/)"
echo "  SKIP: test_ap_map_generator (needs vanillamaps/)"
echo "  SKIP: test_mission_complete_map_patcher (needs vanillamaps/)"
echo "  SKIP: build_client.sh (needs MinGW toolchain)"
echo "  SKIP: APWorld tests (needs distrobox/Archipelago setup)"
echo "  SKIP: seed generation (needs distrobox/Archipelago setup)"

# ----- Summary -----
echo "=== validate_fast complete: ${PASS} passed, ${FAIL} failed ==="
exit $FAIL
