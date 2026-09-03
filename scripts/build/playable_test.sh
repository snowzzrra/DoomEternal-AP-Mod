#!/bin/bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
if [[ -z "${AP_PIPELINE_RECEIPT:-}" ]]; then
    exec "$REPO_ROOT/scripts/pipeline.sh" playtest "$@"
fi
export PYTHONPATH="$REPO_ROOT"
WORKSPACE="$(cd "$REPO_ROOT/.." && pwd)"
TOOLS_DIR="$WORKSPACE/Tools"
ARCHIPELAGO_PYTHON="${ARCHIPELAGO_PYTHON:-$WORKSPACE/Archipelago/.venv/bin/python}"
if [[ ! -x "$ARCHIPELAGO_PYTHON" ]]; then
    echo "Archipelago Python is not executable: $ARCHIPELAGO_PYTHON" >&2
    exit 1
fi
OUTPUT_DIR=""
ENABLE_ITEM_NOTIFICATIONS=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --disable-item-notifications)
            ENABLE_ITEM_NOTIFICATIONS=0
            shift
            ;;
        *)
            if [[ -z "$OUTPUT_DIR" ]]; then
                OUTPUT_DIR="$1"
            else
                echo "Unknown argument: $1" >&2
                exit 1
            fi
            shift
            ;;
    esac
done

OUTPUT_DIR="${OUTPUT_DIR:-$REPO_ROOT/build/release}"
OUTPUT_DIR="$(realpath -m "$OUTPUT_DIR")"
RELEASE_DIR="$(realpath -m "$REPO_ROOT/build/release")"
if [[ "$OUTPUT_DIR" != "$RELEASE_DIR" ]]; then
    echo "Playable builds are restricted to $RELEASE_DIR" >&2
    exit 1
fi
TEMP_DIR="$OUTPUT_DIR/.staging"
MOD_STAGING_DIR="$TEMP_DIR/mod"
MAP_SOURCES_FILE="${AP_MAP_SOURCES_FILE:-$REPO_ROOT/data/map_sources.json}"
VANILLA_MAPS_DIR="${VANILLA_MAPS_DIR:-$REPO_ROOT/vanillamaps}"
RELEASE_VERSION="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["release_version"])' "$REPO_ROOT/data/content_identity.json")"
PTB_ZIP_NAME="DoomEternalArchipelago-${RELEASE_VERSION}.zip"
STALE_DEV_ZIP="$OUTPUT_DIR/DoomEternalArchipelagoPlayableTest-v0.3.0-pre-alpha-dev.zip"
AUTOMAP_PROTOTYPE_ONLY="${AP_AUTOMAP_PROTOTYPE_ONLY:-0}"
DEEP_AUDIT="${AP_PIPELINE_DEEP_AUDIT:-0}"
GENERATED_MAPS_DIR="$OUTPUT_DIR/build/generated-maps"
GENERATED_MANIFESTS_DIR="$TEMP_DIR/manifests"
BUILD_LOG="$OUTPUT_DIR/build/build.log"
CLIENT_BUILD_DIR="$OUTPUT_DIR/build/client"
PACKAGEMAPSPEC="${DOOM_PACKAGEMAPSPEC:-/run/media/system/Eris/SteamLibrary/steamapps/common/DOOMEternal/base/packagemapspec.json}"

mkdir -p "$TEMP_DIR"
SKIP_REQUIREMENTS_UPDATE=1 "$ARCHIPELAGO_PYTHON" \
    -m tools.content.compile_options_schema
python3 - "$REPO_ROOT" "$TEMP_DIR" <<'PY'
import json
import hashlib
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from doom_eap.contracts.challenge_registry import challenge_registry_document
from doom_eap.content.content_catalog import load_content_catalog
from doom_eap.contracts.publisher_contracts import publisher_contracts_document
catalog = load_content_catalog(Path(sys.argv[1]))
target = Path(sys.argv[2])
(target / "challenge_location_registry.json").write_text(
    json.dumps(challenge_registry_document(catalog), indent=2) + "\n",
    encoding="utf-8",
)
(target / "publisher_contracts.json").write_text(
    json.dumps(publisher_contracts_document(catalog.publishers), indent=2) + "\n",
    encoding="utf-8",
)
PY

report_build_failure() {
    local status=$?
    local line_number="$1"
    local command="$2"
    printf 'BUILD_FAILED status=%s line=%s command=%q log=%s\n' \
        "$status" "$line_number" "$command" "$BUILD_LOG" >&2
    return "$status"
}

run_build_step() {
    local step="$1"
    shift
    printf 'BUILD_STEP %s\n' "$step"
    if "$@"; then
        return 0
    else
        local status=$?
        printf 'BUILD_FAILED status=%s step=%s log=%s\n' \
            "$status" "$step" "$BUILD_LOG" >&2
        return "$status"
    fi
}

cleanup_build() {
    local status=$?
    rm -rf "$TEMP_DIR"
    if [[ "$status" -ne 0 ]]; then
        rm -f "$OUTPUT_DIR/$PTB_ZIP_NAME" "${OUTPUT_DIR}/${PTB_ZIP_NAME}.tmp"
    fi
    return "$status"
}
trap cleanup_build EXIT
trap 'report_build_failure "$LINENO" "$BASH_COMMAND"' ERR

mkdir -p "$(dirname "$BUILD_LOG")"
: > "$BUILD_LOG"
exec > >(tee -a "$BUILD_LOG") 2>&1

if [[ "${AP_PRESERVE_CONFIG:-0}" == "1" && -f "$OUTPUT_DIR/client/ap_config.json" ]]; then
    cp "$OUTPUT_DIR/client/ap_config.json" "$TEMP_DIR/ap_config.json"
fi

extract_and_build() {
    local map_key="$1"
    local source_file="$2"
    local source_sha256="$3"
    local config_path="$4"
    local manifest_path="$5"
    local generated_output="$6"
    local resource_path="$7"
    local relative_entities_path="$8"
    local supported_game_revision="$9"
    local resource_name
    resource_name="$(basename "$resource_path" .resources)"
    local source_map="$VANILLA_MAPS_DIR/$source_file"
    local generated_file="$GENERATED_MAPS_DIR/$generated_output"
    local generated_manifest="$GENERATED_MANIFESTS_DIR/$map_key.json"
    local packaged_file="$MOD_STAGING_DIR/$resource_name/maps/$relative_entities_path"
    local source_hash_before
    local source_hash_after
    local generated_hash
    local source_size

    mkdir -p "$(dirname "$generated_file")" "$(dirname "$generated_manifest")" \
        "$(dirname "$packaged_file")" "$(dirname "$BUILD_LOG")"

    # Resolves the repository root relative to the script location
    REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

    if [[ ! -f "$source_map" ]]; then
        echo "Missing vanilla source for $map_key: $source_map" >&2
        return 1
    fi

    source_hash_before="$(sha256sum "$source_map" | awk '{print $1}')"
    source_size="$(stat -c %s "$source_map")"
    if [[ "$source_hash_before" != "$source_sha256" ]]; then
        echo "Vanilla source hash mismatch for $map_key: expected $source_sha256, got $source_hash_before. Supported revision: $supported_game_revision" >&2
        return 1
    fi

    echo "[$map_key] source=$source_map size=$source_size sha256=$source_hash_before revision=$supported_game_revision" | tee -a "$BUILD_LOG"

    local GENERATOR_ARGS=()
    if [[ "$ENABLE_ITEM_NOTIFICATIONS" != "1" ]]; then
        GENERATOR_ARGS+=(--disable-item-notifications)
    fi

    if [[ -n "${AP_PIPELINE_RECEIPT:-}" ]]; then
        python3 - "$AP_PIPELINE_RECEIPT" "$map_key" "$generated_file" "$generated_manifest" <<'PYEOF'
import hashlib
import json
import shutil
import sys
from pathlib import Path

receipt_path = Path(sys.argv[1])
map_key = sys.argv[2]
entities_dest = Path(sys.argv[3])
manifest_dest = Path(sys.argv[4])

receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
if map_key not in receipt["maps"]:
    raise SystemExit(f"Receipt missing map={map_key}")

entry = receipt["maps"][map_key]
entities_source = Path(entry["output_source"])
manifest_source = Path(entry["manifest_source"])

entities_dest.parent.mkdir(parents=True, exist_ok=True)
manifest_dest.parent.mkdir(parents=True, exist_ok=True)

shutil.copy2(str(entities_source), str(entities_dest))
actual_hash = hashlib.sha256(entities_dest.read_bytes()).hexdigest()
if actual_hash != entry["output_sha256"]:
    raise SystemExit(f"Entities hash mismatch map={map_key} expected={entry['output_sha256']} got={actual_hash}")

shutil.copy2(str(manifest_source), str(manifest_dest))
actual_hash = hashlib.sha256(manifest_dest.read_bytes()).hexdigest()
if actual_hash != entry["manifest_sha256"]:
    raise SystemExit(f"Manifest hash mismatch map={map_key} expected={entry['manifest_sha256']} got={actual_hash}")
PYEOF
    elif [[ -n "${AP_PIPELINE_ARTIFACT_ROOT:-}" ]]; then
        cp "$AP_PIPELINE_ARTIFACT_ROOT/maps/$generated_output" "$generated_file"
        cp "$AP_PIPELINE_ARTIFACT_ROOT/manifests/$map_key.json" "$generated_manifest"
    else
        python3 "$REPO_ROOT/tools/maps/ap_map_generator.py" \
            --input "$source_map" \
            --output "$generated_file" \
            --config "$REPO_ROOT/$config_path" \
            --manifest "$generated_manifest" \
            --items "$REPO_ROOT/data/items.json" \
            "${GENERATOR_ARGS[@]}"
    fi

    source_hash_after="$(sha256sum "$source_map" | awk '{print $1}')"
    if [[ "$source_hash_after" != "$source_hash_before" ]]; then
        echo "Vanilla source was modified during build for $map_key: $source_map" >&2
        return 1
    fi

    generated_hash="$(sha256sum "$generated_file" | awk '{print $1}')"
    echo "[$map_key] generated=$generated_file sha256=$generated_hash" | tee -a "$BUILD_LOG"

    python3 -c \
        'import json,sys; expected=json.load(open(sys.argv[1])); actual=json.load(open(sys.argv[2])); \
only_expected=sorted(set(expected)-set(actual)); only_actual=sorted(set(actual)-set(expected)); \
value_mismatch=[(k, expected[k], actual[k]) for k in sorted(set(expected)&set(actual)) if expected[k]!=actual[k]]; \
assert expected == actual, f"generated manifest differs: {sys.argv[1]} | only_expected={only_expected} | only_actual={only_actual} | value_mismatch={value_mismatch}"' \
        "$REPO_ROOT/$manifest_path" "$generated_manifest"

}

rm -rf "$OUTPUT_DIR/client" "$OUTPUT_DIR/apworld" "$OUTPUT_DIR/licenses" \
    "$OUTPUT_DIR/DoomEternalArchipelagoLauncher" \
    "$OUTPUT_DIR/DoomEternalArchipelagoLauncher.exe" \
    "$OUTPUT_DIR/DoomEternalArchipelagoBeta.zip" \
    "$OUTPUT_DIR/doometernal.apworld" "$OUTPUT_DIR/README.md" "$OUTPUT_DIR/INSTALL.md" "$OUTPUT_DIR/LICENSE" \
    "$OUTPUT_DIR/RELEASE_MANIFEST.json" "$OUTPUT_DIR/$PTB_ZIP_NAME" \
    "${OUTPUT_DIR}/${PTB_ZIP_NAME}.tmp" \
    "$STALE_DEV_ZIP" \
    "$OUTPUT_DIR/DoomEternalArchipelago-v0.3.0-pre-alpha.zip" \
    "$OUTPUT_DIR/DoomEternalArchipelagoPreAlpha.zip"
find "$OUTPUT_DIR/build" -mindepth 1 -maxdepth 1 ! -name build.log -exec rm -rf -- {} +
mkdir -p "$MOD_STAGING_DIR" "$OUTPUT_DIR/client" "$GENERATED_MAPS_DIR" "$TEMP_DIR"
echo "Build log: $BUILD_LOG"
if [[ "$ENABLE_ITEM_NOTIFICATIONS" == "1" ]]; then
    echo "ITEM_NOTIFICATIONS=enabled"
else
    echo "ITEM_NOTIFICATIONS=disabled"
fi
"$REPO_ROOT/scripts/build/client.sh" "$CLIENT_BUILD_DIR"
if [[ ! -f "$CLIENT_BUILD_DIR/ap_client.exe" || ! -f "$CLIENT_BUILD_DIR/save_death_probe.exe" ]]; then
    echo "Fresh client build is missing required executable(s)" >&2
    exit 1
fi
if [[ -f "$SCRIPT_DIR/ap_client.exe" ]]; then
    echo "Refusing to package ap_client.exe from the source tree" >&2
    exit 1
fi

cp -R "$REPO_ROOT/packaging/mod_assets/." "$MOD_STAGING_DIR/"

mapfile -t MAP_ROWS < <(
    python3 -m doom_eap.content.map_registry release-rows --authorial --registry "$MAP_SOURCES_FILE"
)
MISSION_MAP_ARGS=()

for map_row in "${MAP_ROWS[@]}"; do
    IFS=$'\t' read -r map_key source_file source_sha256 config_path manifest_path generated_output resource_path relative_entities_path supported_game_revision <<< "$map_row"
    extract_and_build \
        "$map_key" \
        "$source_file" \
        "$source_sha256" \
        "$config_path" \
        "$manifest_path" \
        "$generated_output" \
        "$resource_path" \
        "$relative_entities_path" \
        "$supported_game_revision"
    MISSION_MAP_ARGS+=(--generated-map "$map_key=$GENERATED_MAPS_DIR/$generated_output")
done

python3 "$REPO_ROOT/tools/release/build_string_table.py" \
    --items "$REPO_ROOT/data/items.json" \
    --item-replay-policies "$REPO_ROOT/data/item_replay_policies.json" \
    --location-names "$REPO_ROOT/data/location_names.json" \
    --maps-dir "$GENERATED_MAPS_DIR" \
    --output "$MOD_STAGING_DIR/gameresources_patch1/EternalMod/strings/english.json"
python3 "$REPO_ROOT/tools/release/build_string_table.py" \
    --items "$REPO_ROOT/data/items.json" \
    --item-replay-policies "$REPO_ROOT/data/item_replay_policies.json" \
    --location-names "$REPO_ROOT/data/location_names.json" \
    --maps-dir "$GENERATED_MAPS_DIR" \
    --output "$MOD_STAGING_DIR/gameresources_patch1/EternalMod/strings/portuguese.json"
if [[ -n "${AP_PIPELINE_ARTIFACT_ROOT:-}" ]]; then
    cp -R "$AP_PIPELINE_ARTIFACT_ROOT/mod/." "$MOD_STAGING_DIR/"
else
python3 "$REPO_ROOT/tools/maps/mission_complete_map_patcher.py" \
    --contracts "$REPO_ROOT/data/mission_complete_map_contracts.json" \
    "${MISSION_MAP_ARGS[@]}" \
    --mod-root "$MOD_STAGING_DIR" \
    --audit-output "$TEMP_DIR/mission-complete-map-patch.json"
python3 - "$TEMP_DIR/mission-complete-map-patch.json" "$REPO_ROOT/data/campaign_goal_contract.json" <<'PY'
import json
import sys

audit = json.load(open(sys.argv[1], encoding="utf-8"))
goal_contract = json.load(open(sys.argv[2], encoding="utf-8"))
assert audit["unrelated_generated_entity_diff_count"] == 0
assert audit["hell_on_earth"]["after_targets"] == [
    "AP_CHECK_MISSION_COMPLETE_HELL_ON_EARTH",
    "citadel_target_level_transition_3",
]
assert audit["exultia"]["after_targets"] == [
    "AP_CHECK_MISSION_COMPLETE_EXULTIA",
    "extraction_target_level_transition_1",
]
assert audit["doom_hunter_base"]["after_targets"] == [
    "AP_CHECK_MISSION_COMPLETE_DOOM_HUNTER_BASE",
    "checkpoints_target_level_transition_1",
]
assert audit["campaign_goal"]["owner"] == goal_contract["owner"]
assert audit["campaign_goal"]["runtime_map"] == goal_contract["runtime_map"]
assert audit["campaign_goal"]["destination_map"] == goal_contract["destination_map"]
assert audit["campaign_goal"]["event_file"] == goal_contract["event_filename"]
assert audit["campaign_goal"]["marker"] == goal_contract["marker"]
publishers = audit["campaign_goal"]["publishers"]
assert set(publishers) == {
    "sentinel_prime_mission_complete",
    "sentinel_prime_campaign_goal",
}
assert audit["campaign_goal"]["after_targets"] == [
    publishers["sentinel_prime_campaign_goal"]["relay"],
    publishers["sentinel_prime_mission_complete"]["relay"],
    "ap_publisher_preserved_e2m4_endoflevel_transition_native_relay",
]
assert audit["campaign_goal"]["preserved_native_targets"] == [
    "e2m4_endoflevel_transition_native"
]
PY
fi

python3 - "$MOD_STAGING_DIR/gameresources_patch1/EternalMod/strings/english.json" \
    "$MOD_STAGING_DIR/gameresources_patch1/EternalMod/strings/portuguese.json" <<'PY'
import json
import sys
from pathlib import Path

for raw_path in sys.argv[1:]:
    path = Path(raw_path)
    table = json.loads(path.read_text(encoding="utf-8"))
    entries = {
        entry["name"]: entry
        for entry in table["strings"]
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    }
    entries["#str_code__GHOST31004"] = {
        "name": "#str_code__GHOST31004",
        "text": "DOOM ETERNAL ARCHIPELAGO",
    }
    entries["#str_code_mainmenu_campaign_name"] = {
        "name": "#str_code_mainmenu_campaign_name",
        "text": "DOOM ETERNAL ARCHIPELAGO",
    }
    table["strings"] = [entries[name] for name in sorted(entries)]
    path.write_text(json.dumps(table, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
PY

mkdir -p "$MOD_STAGING_DIR/shell/EternalMod/assetsinfo"
cp "$REPO_ROOT/packaging/shell_menu_assetsinfo.json" \
    "$MOD_STAGING_DIR/shell/EternalMod/assetsinfo/shell.json"

mkdir -p "$MOD_STAGING_DIR/hub_patch2/EternalMod/assetsinfo"
cp "$REPO_ROOT/packaging/hub_world_text_assetsinfo.json" \
    "$MOD_STAGING_DIR/hub_patch2/EternalMod/assetsinfo/hub.json"

for map_row in "${MAP_ROWS[@]}"; do
    IFS=$'\t' read -r map_key _ _ _ _ generated_output resource_path relative_entities_path _ <<< "$map_row"
    resource_name="$(basename "$resource_path" .resources)"
    "$TOOLS_DIR/idFileDeCompressor" --compress \
        "$GENERATED_MAPS_DIR/$generated_output" \
        "$MOD_STAGING_DIR/$resource_name/maps/$relative_entities_path"
done

python3 "$REPO_ROOT/tools/maps/shell_menu_visual.py" \
    --source "$VANILLA_MAPS_DIR/shell.map" \
    --output "$MOD_STAGING_DIR/shell/maps/game/shell/shell.entities"
"$TOOLS_DIR/idFileDeCompressor" --compress \
    "$MOD_STAGING_DIR/shell/maps/game/shell/shell.entities" \
    "$MOD_STAGING_DIR/shell/maps/game/shell/shell.entities.compressed"
mv "$MOD_STAGING_DIR/shell/maps/game/shell/shell.entities.compressed" \
   "$MOD_STAGING_DIR/shell/maps/game/shell/shell.entities"

python3 "$REPO_ROOT/tools/maps/automap_native_decl_builder.py" \
    --mod-root "$MOD_STAGING_DIR" \
    --audit-output "$TEMP_DIR/automap-native-toy-override.json"
python3 "$REPO_ROOT/tools/decls/rune_decl_builder.py" \
    --mod-root "$MOD_STAGING_DIR" \
    --audit-output "$TEMP_DIR/rune-menu-override.json"
python3 "$REPO_ROOT/tools/decls/rune_slot_builder.py" \
    --mod-root "$MOD_STAGING_DIR" \
    --audit-output "$TEMP_DIR/rune-slot-override.json"
python3 "$REPO_ROOT/tools/decls/mastery_decl_builder.py" \
    --mod-root "$MOD_STAGING_DIR" \
    --audit-output "$TEMP_DIR/base-mastery-overrides.json"
python3 "$REPO_ROOT/tools/decls/mission_challenge_decl_builder.py" \
    --mod-root "$MOD_STAGING_DIR" \
    --audit-output "$TEMP_DIR/mission-challenge-overrides.json"
python3 "$REPO_ROOT/tools/decls/weapon_stripping_builder.py" \
    --mod-root "$MOD_STAGING_DIR" \
    --audit-output "$TEMP_DIR/weapon-stripping-overrides.json"
python3 "$REPO_ROOT/tools/decls/devinv_builder.py" \
    --mod-root "$MOD_STAGING_DIR" \
    --map-registry "$MAP_SOURCES_FILE" \
    --audit-output "$TEMP_DIR/devinv-override.json"
if [[ "$DEEP_AUDIT" == "1" ]]; then
    python3 "$REPO_ROOT/tools/validation/validate_challenge_overrides.py" \
        --registry "$TEMP_DIR/challenge_location_registry.json" \
        --mod-root "$MOD_STAGING_DIR"
    python3 "$REPO_ROOT/tools/validation/audit_scripted_location.py" \
        --contracts "$REPO_ROOT/data/scripted_location_contracts.json" \
        --verify-generated-map "$OUTPUT_DIR/build/generated-maps/hub.entities" \
        --location 7770074
    python3 "$REPO_ROOT/tools/validation/audit_scripted_location.py" \
        --contracts "$REPO_ROOT/data/scripted_location_contracts.json" \
        --verify-generated-map "$OUTPUT_DIR/build/generated-maps/e1m3_cult.entities" \
        --location 7770056
fi

ICE_DECL_RELATIVE="generated/decls/logicentity/maps/game/hub/hub/info_logic_hub_from_e1m2.decl"
python3 "$REPO_ROOT/tools/maps/logic_decl_patcher.py" \
    --contracts "$REPO_ROOT/data/scripted_location_contracts.json" \
    --location 7770074 \
    --output "$MOD_STAGING_DIR/hub_patch2/$ICE_DECL_RELATIVE" \
    --snapshot "$TEMP_DIR/ice_logic_decl_patch.json"
python3 - "$REPO_ROOT/data/snapshots/ice_logic_decl_patch.json" "$TEMP_DIR/ice_logic_decl_patch.json" <<'PY'
import json
import sys
from pathlib import Path

expected = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
actual = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
actual.pop("changed_lines", None)
if actual != expected:
    raise SystemExit(f"Ice logic DECL structural snapshot drift: {actual!r}")
PY

cp "$REPO_ROOT/packaging/EternalMod.json" "$MOD_STAGING_DIR/EternalMod.json"
cp "$REPO_ROOT/README.md" "$OUTPUT_DIR/README.md"
cp "$REPO_ROOT/docs/INSTALL.md" "$OUTPUT_DIR/INSTALL.md"
cp "$REPO_ROOT/docs/LICENSE" "$OUTPUT_DIR/LICENSE"
cp "$CLIENT_BUILD_DIR/ap_client.exe" "$CLIENT_BUILD_DIR/save_death_probe.exe" "$OUTPUT_DIR/client/"
cp "$REPO_ROOT/doom_eap/runtime/bridge_client.py" "$OUTPUT_DIR/client/bridge_client.py"
cp "$REPO_ROOT/doom_eap/runtime/bootstrap_actions.py" "$OUTPUT_DIR/client/bootstrap_actions.py"
cp "$REPO_ROOT/doom_eap/runtime/deathlink_receive.py" "$OUTPUT_DIR/client/deathlink_receive.py"
cp "$REPO_ROOT/doom_eap/contracts/campaign_goal_contract.py" "$OUTPUT_DIR/client/campaign_goal_contract.py"
cp "$REPO_ROOT/doom_eap/contracts/challenge_registry.py" "$OUTPUT_DIR/client/challenge_registry.py"
cp "$REPO_ROOT/doom_eap/content/content_catalog.py" "$OUTPUT_DIR/client/content_catalog.py"
cp "$REPO_ROOT/doom_eap/content/automap_visual_registry.py" "$OUTPUT_DIR/client/automap_visual_registry.py"
cp "$REPO_ROOT/doom_eap/contracts/ap_visual_contract.py" "$OUTPUT_DIR/client/ap_visual_contract.py"
cp "$REPO_ROOT/doom_eap/contracts/foundation.py" "$OUTPUT_DIR/client/foundation.py"
cp "$REPO_ROOT/doom_eap/content/item_classification.py" "$OUTPUT_DIR/client/item_classification.py"
cp "$REPO_ROOT/doom_eap/contracts/item_contracts.py" "$OUTPUT_DIR/client/item_contracts.py"
cp "$REPO_ROOT/doom_eap/runtime/item_reconciliation.py" "$OUTPUT_DIR/client/item_reconciliation.py"
cp "$REPO_ROOT/doom_eap/runtime/rune_reconciliation.py" "$OUTPUT_DIR/client/rune_reconciliation.py"
cp "$REPO_ROOT/doom_eap/launcher/launcher_app.py" "$OUTPUT_DIR/client/launcher_app.py"
cp "$REPO_ROOT/doom_eap/launcher/launcher_controller.py" "$OUTPUT_DIR/client/launcher_controller.py"
cp "$REPO_ROOT/doom_eap/launcher/launcher_core.py" "$OUTPUT_DIR/client/launcher_core.py"
cp "$REPO_ROOT/doom_eap/launcher/launcher_integration.py" "$OUTPUT_DIR/client/launcher_integration.py"
cp "$REPO_ROOT/doom_eap/launcher/launcher_platform.py" "$OUTPUT_DIR/client/launcher_platform.py"
cp "$REPO_ROOT/doom_eap/launcher/launcher_supervisor.py" "$OUTPUT_DIR/client/launcher_supervisor.py"
cp "$REPO_ROOT/doom_eap/launcher/launcher_ui.py" "$OUTPUT_DIR/client/launcher_ui.py"
cp "$REPO_ROOT/doom_eap/launcher/launcher_native_health.py" "$OUTPUT_DIR/client/launcher_native_health.py"
cp "$REPO_ROOT/doom_eap/launcher/launcher_doctor.py" "$OUTPUT_DIR/client/launcher_doctor.py"
cp "$REPO_ROOT/doom_eap/content/options_foundation.py" "$OUTPUT_DIR/client/options_foundation.py"
cp "$REPO_ROOT/doom_eap/content/map_registry.py" "$OUTPUT_DIR/client/map_registry.py"
cp "$REPO_ROOT/doom_eap/runtime/observer_lifecycle.py" "$OUTPUT_DIR/client/observer_lifecycle.py"
cp "$REPO_ROOT/doom_eap/contracts/publisher_contracts.py" "$OUTPUT_DIR/client/publisher_contracts.py"
cp "$REPO_ROOT/doom_eap/runtime/publisher_runtime.py" "$OUTPUT_DIR/client/publisher_runtime.py"
cp "$REPO_ROOT/doom_eap/runtime/save_decrypt.py" "$OUTPUT_DIR/client/save_decrypt.py"
cp "$REPO_ROOT/doom_eap/presentation.py" "$OUTPUT_DIR/client/presentation.py"
cp "$REPO_ROOT/scripts/launch/run_bridge.sh" \
    "$REPO_ROOT/packaging/client/ap_config.example.json" \
    "$REPO_ROOT/scripts/validate/runtime_install.sh" \
    "$OUTPUT_DIR/client/"
mkdir -p "$OUTPUT_DIR/client/doom_eap/launcher" "$OUTPUT_DIR/client/doom_eap/runtime" \
    "$OUTPUT_DIR/client/doom_eap/content" "$OUTPUT_DIR/client/doom_eap/contracts"
cp "$REPO_ROOT/doom_eap/"*.py "$OUTPUT_DIR/client/doom_eap/"
cp "$REPO_ROOT/doom_eap/launcher/"*.py "$OUTPUT_DIR/client/doom_eap/launcher/"
cp "$REPO_ROOT/doom_eap/launcher/launcher_cli.py" "$OUTPUT_DIR/client/launcher_cli.py"
cp "$REPO_ROOT/doom_eap/runtime/"*.py "$OUTPUT_DIR/client/doom_eap/runtime/"
cp "$REPO_ROOT/doom_eap/content/"*.py "$OUTPUT_DIR/client/doom_eap/content/"
cp "$REPO_ROOT/doom_eap/contracts/"*.py "$OUTPUT_DIR/client/doom_eap/contracts/"
LAUNCHER_PYTHON="${LAUNCHER_PYTHON:-$ARCHIPELAGO_PYTHON}"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$OUTPUT_DIR/client" "$LAUNCHER_PYTHON" -B -c "
import doom_eap.presentation
import presentation
from doom_eap.launcher.launcher_core import RoomCompiler
from doom_eap.launcher.launcher_ui import LauncherUI
from doom_eap.content.content_catalog import load_content_catalog
" || { echo "Packaged Python client import closure failed" >&2; exit 1; }
mkdir -p "$OUTPUT_DIR/client/tools"
cp "$REPO_ROOT/tools/__init__.py" "$OUTPUT_DIR/client/tools/__init__.py"
mkdir -p "$OUTPUT_DIR/client/tools/release"
cp "$REPO_ROOT/tools/release/__init__.py" \
   "$REPO_ROOT/tools/release/room_payloads.py" \
   "$OUTPUT_DIR/client/tools/release/"
mkdir -p "$OUTPUT_DIR/client/tools/decls"
cp "$REPO_ROOT/tools/decls/"*.py "$OUTPUT_DIR/client/tools/decls/"
mkdir -p "$OUTPUT_DIR/client/tools/maps"
cp "$REPO_ROOT/tools/maps/"*.py "$OUTPUT_DIR/client/tools/maps/"
cp "$WORKSPACE/Archipelago/worlds/doometernal/doom_logo.png" \
    "$OUTPUT_DIR/client/doom_logo.png"
mkdir -p "$OUTPUT_DIR/client/data" "$OUTPUT_DIR/client/manifests"
python3 -m tools.content.compile_content_catalog --output-root "$OUTPUT_DIR/client/data"
python3 -m tools.content.compile_start_inventory_catalog
cp -R "$REPO_ROOT/data"/* "$OUTPUT_DIR/client/data/"
cp "$TEMP_DIR/challenge_location_registry.json" \
   "$TEMP_DIR/publisher_contracts.json" \
   "$OUTPUT_DIR/client/data/"
for map_row in "${MAP_ROWS[@]}"; do
    IFS=$'\t' read -r _ _ _ _ manifest_path _ <<< "$map_row"
    cp "$REPO_ROOT/$manifest_path" "$OUTPUT_DIR/client/manifests/"
done
cp -R "$REPO_ROOT/player_templates" "$OUTPUT_DIR/client/"
cp -R "$REPO_ROOT/content" "$OUTPUT_DIR/client/"
SKIP_REQUIREMENTS_UPDATE=1 "$ARCHIPELAGO_PYTHON" -m tools.release.apworld_cache \
    --output "$OUTPUT_DIR/doometernal.apworld" \
    --archipelago-source "$WORKSPACE/Archipelago" \
    --archipelago-python "$ARCHIPELAGO_PYTHON"
chmod +x "$OUTPUT_DIR/client/run_bridge.sh"

LAUNCHER_PYTHON="${LAUNCHER_PYTHON:-$ARCHIPELAGO_PYTHON}"
"$LAUNCHER_PYTHON" "$REPO_ROOT/tools/release/build_launcher.py" \
    --output-dir "$OUTPUT_DIR" \
    --archipelago-source "$WORKSPACE/Archipelago"

cp "$REPO_ROOT/scripts/validate/runtime_install.sh" "$OUTPUT_DIR/client/validate_runtime_install.sh"
chmod +x "$OUTPUT_DIR/client/validate_runtime_install.sh"

python3 - "$OUTPUT_DIR/client/bridge_client.py" "$OUTPUT_DIR/client/bridge_identity.json" \
    "$ENABLE_ITEM_NOTIFICATIONS" "$REPO_ROOT/data/content_identity.json" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

bridge = Path(sys.argv[1])
content_identity = json.loads(Path(sys.argv[4]).read_text(encoding="utf-8"))
identity = {
    "protocol": content_identity["bridge_protocol_version"],
    "game": "DOOM Eternal",
    "sha256": hashlib.sha256(bridge.read_bytes()).hexdigest(),
    "item_notifications": {
        "enabled": sys.argv[3] == "1",
        "revision": 2,
        "experimental": False,
    },
}
identity["revision"] = f"mission-unified-{identity['sha256'][:12]}"
Path(sys.argv[2]).write_text(json.dumps(identity, indent=2) + "\n", encoding="utf-8")
PY

mkdir -p "$OUTPUT_DIR/client/resources"
ROOM_STAGE="$TEMP_DIR/room-payloads"
ROOM_PAYLOAD_CACHE_DIR="${DOOMEAP_ROOM_PAYLOAD_CACHE:-$WORKSPACE/.cache/doomeap/room-payloads}"
mkdir -p "$ROOM_STAGE"
python3 - "$REPO_ROOT" "$MOD_STAGING_DIR" "$ROOM_STAGE" "$TOOLS_DIR/idFileDeCompressor" "$MAP_SOURCES_FILE" "$OUTPUT_DIR/client/resources" "$ROOM_PAYLOAD_CACHE_DIR" <<'PY'
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

root, staged, room_root, compressor, map_sources_path, resources, cache_root = map(Path, sys.argv[1:])
sys.path.insert(0, str(root))
from doom_eap.launcher.launcher_core import ModCompiler, SeedManifest
from doom_eap.content.physical_options import (
    PHYSICAL_OPTION_KEYS,
    map_physical_option_keys,
)
from tools.release.room_payloads import (
    BASE_RESOURCE_NAME, ROOM_PAYLOAD_MANIFEST_NAME, ROOM_PAYLOAD_RESOURCE_NAME,
    canonical_json, plan_map_local_states, resource_metadata, validate_room_payload_manifest,
    write_deterministic_zip, zip_directory,
)
from tools.maps.mission_complete_map_patcher import (
    patch_mission_complete_maps,
    patch_generated_map_text,
    find_entity_block_bounds,
)
from tools.maps.ap_map_generator import generate_context_marker_overlay
from doom_eap.content.content_catalog import load_content_catalog
from doom_eap.runtime.context_registry import dlc_contexts

compiler = ModCompiler(root)
map_sources = json.loads(map_sources_path.read_text(encoding="utf-8"))["maps"]
catalog = load_content_catalog(root)
release_map_specs = tuple(catalog.enabled_maps())
release_map_keys = tuple(spec.key for spec in release_map_specs)
maps = {
    map_key: (
        source["resource_path"],
        source["relative_entities_path"],
        root / "vanillamaps" / source["source_file"],
    )
    for map_key, source in map_sources.items()
    if map_key in release_map_keys and source["enabled"]
}
if set(maps) != set(release_map_keys):
    raise SystemExit("Room payload map set does not match map contract")


def sha256_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_files(paths):
    digest = hashlib.sha256()
    for path in sorted(set(paths), key=lambda value: value.as_posix()):
        if not path.is_file():
            raise SystemExit(f"Missing room payload cache input: {path}")
        digest.update(path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


generator_path = root / "tools/maps/ap_map_generator.py"
generator_identity = sha256_file(generator_path)
compiler_identity = sha256_files([
    root / "doom_eap/launcher/launcher_core.py",
    root / "doom_eap/content/physical_options.py",
    root / "doom_eap/content/content_catalog.py",
    root / "tools/maps/mission_complete_map_patcher.py",
    root / "data/items.json",
    root / "data/item_classifications.json",
    root / "data/location_names.json",
    root / "data/mission_complete_map_contracts.json",
    root / "data/publisher_contracts.json",
    map_sources_path,
])
compressor_identity = sha256_file(compressor)
cache_root.mkdir(parents=True, exist_ok=True)


def cache_material(map_key, vanilla, options, local_identity):
    return {
        "schema": 1,
        "map_key": map_key,
        "source_identity": sha256_file(vanilla),
        "generator_identity": generator_identity,
        "compiler_identity": compiler_identity,
        "compressor_identity": compressor_identity,
        "local_identity": local_identity,
        "state": dict(sorted(options.items())),
    }


def cache_paths(material):
    key = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    directory = cache_root / key[:2]
    return key, directory / f"{key}.json", directory / f"{key}.entities", directory / f"{key}.packed"


def read_cached_state(material):
    key, metadata_path, entities_path, packed_path = cache_paths(material)
    if not all(path.is_file() for path in (metadata_path, entities_path, packed_path)):
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        entities = entities_path.read_bytes()
        packed = packed_path.read_bytes()
    except (OSError, ValueError):
        return None
    if (
        metadata.get("key") != key
        or metadata.get("material") != material
        or metadata.get("entities_sha256") != hashlib.sha256(entities).hexdigest()
        or metadata.get("packed_sha256") != hashlib.sha256(packed).hexdigest()
    ):
        return None
    return entities, packed


def write_cached_state(material, entities, packed):
    key, metadata_path, entities_path, packed_path = cache_paths(material)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    for path, content in ((entities_path, entities), (packed_path, packed)):
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_bytes(content)
        temporary.replace(path)
    metadata = {
        "key": key,
        "material": material,
        "entities_sha256": hashlib.sha256(entities).hexdigest(),
        "packed_sha256": hashlib.sha256(packed).hexdigest(),
    }
    temporary = metadata_path.with_name(f".{metadata_path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(metadata_path)


def compile_cached_state(map_key, vanilla, options, local_identity, entities, packed):
    material = cache_material(map_key, vanilla, options, local_identity)
    cached = read_cached_state(material)
    if cached is not None:
        entities.write_bytes(cached[0])
        packed.write_bytes(cached[1])
        return
    compile_options = dict(canonical_options)
    compile_options.update(options)
    manifest = SeedManifest.create(
        seed_name=f"room-payload-{map_key}-state", team=0, slot=1,
        options=options,
        active_location_ids=compiler.active_location_ids(compile_options),
        static_precompile=True,
    )
    compiler.compile_map(manifest, vanilla, entities, map_key)
    with tempfile.TemporaryDirectory(prefix=f"room-payload-{map_key}-patch-") as patch_root:
        audit = patch_mission_complete_maps(
            root / "data/mission_complete_map_contracts.json", {map_key: entities}, Path(patch_root)
        )
    if audit["unrelated_generated_entity_diff_count"] != 0:
        raise SystemExit(f"Room payload patch changed unrelated entities: {map_key}")
    subprocess.run([str(compressor), "--compress", str(entities), str(packed)], check=True)
    write_cached_state(material, entities.read_bytes(), packed.read_bytes())


canonical_options = {key: False for key in PHYSICAL_OPTION_KEYS}
state_plans = {
    plan.map_key: plan
    for plan in plan_map_local_states(tuple(maps))
}

local_identities = {}
for map_key, source in map_sources.items():
    local_files = [
        root / value for value in source.values()
        if isinstance(value, str) and (root / value).is_file()
    ]
    local_identities[map_key] = sha256_files(local_files)

for map_key in maps:
    resource_path, relative, vanilla = maps[map_key]
    target = staged / f"{Path(resource_path).stem}/maps/{relative}"
    plan = state_plans[map_key]
    if not plan.compile_base:
        if not target.is_file():
            raise SystemExit(f"Canonical staged base is missing: {map_key}/{target}")
        continue
    entities = room_root / f"{map_key}-base.entities"
    target.parent.mkdir(parents=True, exist_ok=True)
    compile_cached_state(
        map_key, vanilla, plan.base_options, local_identities[map_key], entities, target
    )
for context in dlc_contexts():
    map_key = context.map_keys[0]
    spec = catalog.maps[map_key]
    target = staged / (
        f"{Path(spec.resource_path).stem}/maps/{context.runtime_maps[0]}.entities"
    )
    if target.is_file():
        continue
    source = (root / "vanillamaps" / spec.source_file).read_text(encoding="utf-8")
    overlay_text = generate_context_marker_overlay(map_key, context.runtime_maps[0])
    full_text = source.rstrip() + "\n" + overlay_text.lstrip()
    entities = room_root / f"{map_key}-context.entities"
    entities.write_text(full_text, encoding="utf-8", newline="")
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(compressor), "--compress", str(entities), str(target)], check=True)
base_members = zip_directory(staged, resources / BASE_RESOURCE_NAME)
payload_files = {}
map_records = {}
for map_key, (resource_path, relative, vanilla) in maps.items():
    plan = state_plans[map_key]
    target_member = f"{Path(resource_path).stem}/maps/{relative}"
    states = []
    for options in plan.states:
        if not any(options.values()):
            states.append({
                "options": options,
                "source": "base",
                "member": None,
                "sha256": base_members[target_member],
            })
            continue
        state_labels = [
            key.removeprefix("randomize_")
            for key in map_physical_option_keys(map_key)
            if options[key]
        ]
        state_name = "-".join(state_labels)
        entities = room_root / f"{map_key}-{state_name}.entities"
        packed = room_root / f"{map_key}-{state_name}.packed"
        compile_cached_state(
            map_key, vanilla, options, local_identities[map_key], entities, packed
        )
        member = f"replacements/{map_key}/{state_name}.entities"
        payload_files[member] = packed.read_bytes()
        states.append({
            "options": options, "source": "replacement", "member": member,
            "sha256": hashlib.sha256(packed.read_bytes()).hexdigest(),
        })
    map_records[map_key] = {
        "option_keys": list(plan.option_keys),
        "target_member": target_member,
        "states": states,
    }
    map_records[map_key]["state_policy"] = plan.state_policy
payload_manifest = {
    "schema_version": 1,
    "model": "dependent_map_payloads",
    "physical_option_keys": list(PHYSICAL_OPTION_KEYS),
    "base_members": sorted(base_members),
    "maps": map_records,
    "context_targets": {
        context.identity: (
            f"{Path(catalog.maps[context.map_keys[0]].resource_path).stem}"
            f"/maps/{context.runtime_maps[0]}.entities"
        )
        for context in dlc_contexts()
    },
}
validate_room_payload_manifest(
    payload_manifest,
    known_maps={record_key: record["target_member"] for record_key, record in map_records.items()},
)
write_deterministic_zip(payload_files, resources / ROOM_PAYLOAD_RESOURCE_NAME)
(resources / ROOM_PAYLOAD_MANIFEST_NAME).write_bytes(canonical_json(payload_manifest))
PY

find "$OUTPUT_DIR/client" -type d -name __pycache__ -prune -exec rm -rf {} +

python3 - "$OUTPUT_DIR" "$RELEASE_VERSION" "$REPO_ROOT" "$MAP_SOURCES_FILE" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

output_dir = Path(sys.argv[1])
release_version = sys.argv[2]
sys.path.insert(0, sys.argv[3])
from doom_eap.content.map_registry import load_map_registry, release_plan
from tools.validation.release_layout import expected_release_roots
plans = release_plan(load_map_registry(Path(sys.argv[4]), authorial=True))
map_manifest_files = [plan.client_manifest for plan in plans]
generated_map_hashes = {
    plan.map_key: hashlib.sha256(
        (output_dir / "build" / "generated-maps" / plan.generated_output).read_bytes()
    ).hexdigest()
    for plan in plans
}
visual_registry_path = output_dir / "client/data/checked_location_visuals.json"
launcher_executable = (
    "DoomEternalArchipelagoLauncher.exe"
    if (output_dir / "DoomEternalArchipelagoLauncher.exe").is_file()
    else "DoomEternalArchipelagoLauncher"
)
from tools.validation.release_layout import public_file_members
client_files = sorted(f"client/{path.relative_to(output_dir / 'client').as_posix()}" for path in (output_dir / "client").rglob("*") if path.is_file())
public_files = sorted(
    list(expected_release_roots(launcher_executable) - {"client", launcher_executable})
    + client_files
    + [launcher_executable]
)

from tools.release.release_manifest import build_release_manifest, write_release_manifest
write_release_manifest(
    output_dir / "RELEASE_MANIFEST.json",
    build_release_manifest(
        Path(sys.argv[3]),
        generated_maps=output_dir / "build/generated-maps",
        public_files=public_files,
        release_version=release_version,
        room_resources=output_dir / "client/resources",
    ),
)
PY

if [[ "$DEEP_AUDIT" == "1" && "$AUTOMAP_PROTOTYPE_ONLY" != "1" ]]; then
for generated_map in "$GENERATED_MAPS_DIR"/*.entities; do
    if grep -q '^\s*entityDef ap_bootstrap_v[0-9]_' "$generated_map"; then
        echo "Rejected stat-write bootstrap entered the normal build: $generated_map" >&2
        exit 1
    fi
done
if grep -q 'pickups_pickup_weapon_heavy_cannon_1' "$GENERATED_MAPS_DIR/e1m2_war.entities"; then
    echo "Exultia Heavy Cannon fallback reappeared" >&2
    exit 1
fi
if grep -q 'give armor -200|AP_RUNTIME_CHECK_|3_900_000_000|3_800_000_000' \
    "$MOD_STAGING_DIR" "$GENERATED_MAPS_DIR" "$OUTPUT_DIR/client/data/items.json"; then
    echo "Rejected Armor Drain or watcher architecture entered build" >&2
    exit 1
fi
if grep -q 'Ignoring unexpected goal transition event' "$OUTPUT_DIR/client/bridge_client.py"; then
    echo "Old goal-only transition handler entered build" >&2
    exit 1
fi
CLIENT_STRINGS_FILE="$TEMP_DIR/ap-client.strings"
strings "$CLIENT_BUILD_DIR/ap_client.exe" > "$CLIENT_STRINGS_FILE"
if grep -qE 'v0\.3\.(8|9)-alpha' "$CLIENT_STRINGS_FILE"; then
    echo "Stale alpha product label found in ap_client.exe" >&2
    exit 1
fi
if ! grep -Fq "$RELEASE_VERSION" "$CLIENT_STRINGS_FILE"; then
    echo "Current $RELEASE_VERSION product label missing from ap_client.exe" >&2
    exit 1
fi
mapfile -t MASTERY_OVERRIDE_FILES < <(find "$MOD_STAGING_DIR" -type f \( \
    -path '*/generated/decls/unlockable/weapon_mastery/*' -o \
    -path '*/generated/decls/perks/perk/player/weapons/*' \
\) | LC_ALL=C sort)
[[ "${#MASTERY_OVERRIDE_FILES[@]}" == "26" ]] || { echo "Base Mastery override set is incomplete" >&2; exit 1; }
if grep -q 'perkToGive|addStats|STAT_CURRENT_MASTERIES_AQUIRED|MASTERY_EARNED' "${MASTERY_OVERRIDE_FILES[@]}"; then
    echo "Mastery override retains natural reward, completion stat, or global stat" >&2
    exit 1
fi
if ! grep -q 'upgrade/weapons/shotguns/shotgun/pop_rocket_more_bombs' \
    "$MOD_STAGING_DIR/gameresources/generated/decls/perks/perk/player/weapons/shotgun/pop_rocket_more_bombs.decl"; then
    echo "Sticky AP gameplay upgrade missing" >&2
    exit 1
fi
python3 "$REPO_ROOT/tools/validation/validate_challenge_overrides.py" \
    --registry "$TEMP_DIR/challenge_location_registry.json" \
    --mod-root "$MOD_STAGING_DIR"
python3 "$REPO_ROOT/tools/validation/validate_devinvloadout_package.py" \
    --mod-root "$MOD_STAGING_DIR" \
    --map-registry "$MAP_SOURCES_FILE" \
    --generated-map "$GENERATED_MAPS_DIR/e1m1_intro.entities"
python3 "$REPO_ROOT/tools/validation/validate_item_notification_package.py" \
    --enabled "$ENABLE_ITEM_NOTIFICATIONS" \
    --maps-dir "$GENERATED_MAPS_DIR" \
    --mod-root "$MOD_STAGING_DIR" \
    --client-dir "$OUTPUT_DIR/client" \
    --release-manifest "$OUTPUT_DIR/RELEASE_MANIFEST.json"
if find "$MOD_STAGING_DIR" \( \
    -path '*/generated/decls/perks/perk/ap/*' -o \
    -path '*/generated/decls/logicentity/ap/*' \
\) -print -quit | grep -q .; then
    echo "Rejected watcher DECL override entered build" >&2
    exit 1
fi
fi

PACKAGED_CLIENT_SHA256="$(sha256sum "$OUTPUT_DIR/client/ap_client.exe" | awk '{print $1}')"
FRESH_CLIENT_SHA256="$(sha256sum "$CLIENT_BUILD_DIR/ap_client.exe" | awk '{print $1}')"
[[ "$PACKAGED_CLIENT_SHA256" == "$FRESH_CLIENT_SHA256" ]] || { echo "Packaged ap_client.exe is not the fresh build" >&2; exit 1; }

if [[ "$DEEP_AUDIT" == "1" ]]; then
    python3 "$REPO_ROOT/tools/validation/audit_resource_packages.py" \
        --asset-root "$REPO_ROOT/packaging/mod_assets" \
        --mod-root "$MOD_STAGING_DIR" \
        --generated-maps "$GENERATED_MAPS_DIR" \
        --source-map-root "$REPO_ROOT/vanillamaps"
fi

if [[ -e "$OUTPUT_DIR/DoomEternalArchipelagoBeta.zip" ]]; then
    echo "Obsolete universal mod ZIP exists in public release root" >&2
    exit 1
fi
if [[ -e "$OUTPUT_DIR/client/DoomEternalArchipelagoLauncher" || \
      -e "$OUTPUT_DIR/client/DoomEternalArchipelagoLauncher.exe" ]]; then
    echo "Launcher must not be packaged under client/" >&2
    exit 1
fi
if [[ ! -f "$OUTPUT_DIR/DoomEternalArchipelagoLauncher" && \
      ! -f "$OUTPUT_DIR/DoomEternalArchipelagoLauncher.exe" ]]; then
    echo "Public root launcher is missing" >&2
    exit 1
fi
if [[ -f "$OUTPUT_DIR/DoomEternalArchipelagoLauncher.exe" ]]; then
    LAUNCHER_EXECUTABLE="DoomEternalArchipelagoLauncher.exe"
else
    LAUNCHER_EXECUTABLE="DoomEternalArchipelagoLauncher"
fi

(
    cd "$OUTPUT_DIR"
    zip -q -r "${PTB_ZIP_NAME}.tmp" \
        README.md INSTALL.md LICENSE RELEASE_MANIFEST.json client doometernal.apworld \
        "$LAUNCHER_EXECUTABLE"
)
mv "${OUTPUT_DIR}/${PTB_ZIP_NAME}.tmp" "${OUTPUT_DIR}/${PTB_ZIP_NAME}"

if [[ "$AUTOMAP_PROTOTYPE_ONLY" == "1" ]]; then
    rm -rf "$OUTPUT_DIR/build" "$OUTPUT_DIR/client" \
        "$OUTPUT_DIR/apworld" "$OUTPUT_DIR/doometernal.apworld" \
        "$OUTPUT_DIR/DoomEternalArchipelagoLauncher" \
        "$OUTPUT_DIR/DoomEternalArchipelagoLauncher.exe" \
        "$OUTPUT_DIR/README.md" "$OUTPUT_DIR/INSTALL.md" "$OUTPUT_DIR/RELEASE_MANIFEST.json"
    echo "Automap prototype ZIP created at: $OUTPUT_DIR/$PTB_ZIP_NAME"
    exit 0
fi

EXTRACTED_AUDIT_DIR="$TEMP_DIR/extracted-final"
mkdir -p "$EXTRACTED_AUDIT_DIR"
unzip -q "$OUTPUT_DIR/$PTB_ZIP_NAME" -d "$EXTRACTED_AUDIT_DIR"
PYTHONPATH="$REPO_ROOT" python3 - "$EXTRACTED_AUDIT_DIR" "$LAUNCHER_EXECUTABLE" <<'PY'
import sys
import zipfile
from pathlib import Path

from tools.release.release_manifest import load_release_manifest

root = Path(sys.argv[1])
required = (
    root / sys.argv[2],
    root / "client" / "ap_client.exe",
    root / "client" / "save_death_probe.exe",
    root / "doometernal.apworld",
)
if any(not path.is_file() for path in required):
    raise SystemExit("final artifact required public member is missing")
with zipfile.ZipFile(root / "doometernal.apworld") as archive:
    if archive.testzip() is not None:
        raise SystemExit("final artifact APWorld member is corrupt")
load_release_manifest(root / "RELEASE_MANIFEST.json", package_root=root)
PY
if [[ "$DEEP_AUDIT" == "1" ]]; then
python3 - \
    "$EXTRACTED_AUDIT_DIR/client/data/items.json" \
    "$REPO_ROOT/data/items.json" <<'PY'
import json
import sys

items = json.load(open(sys.argv[1], encoding="utf-8"))
canonical_items = json.load(open(sys.argv[2], encoding="utf-8"))
assert items == canonical_items
assert items["7770016"] == {
    "type": "currency", "currency": "CURRENCY_SENTINEL_BATTERY", "count": 1,
}
assert items["7770142"] == {
    "type": "currency", "currency": "CURRENCY_SENTINEL_BATTERY", "count": 2,
}
assert not any(
    isinstance(value, dict) and value.get("currency") == "CURRENCY_WEAPON_UPGRADE"
    for value in items.values()
)
PY
fi
MOD_AUDIT_DIR="$TEMP_DIR/extracted-mod"
mkdir -p "$MOD_AUDIT_DIR"
MOD_AUDIT_ZIP="$TEMP_DIR/assembled-room-mod.zip"
python3 - "$EXTRACTED_AUDIT_DIR/client/resources" "$MOD_AUDIT_DIR" "$MOD_AUDIT_ZIP" <<'PY'
import sys
from pathlib import Path
from tools.release.room_payloads import (
    BASE_RESOURCE_NAME, ROOM_PAYLOAD_MANIFEST_NAME, ROOM_PAYLOAD_RESOURCE_NAME,
    assemble_room_files, load_room_payload_manifest, write_deterministic_zip,
)

resources, mod_root, archive = map(Path, sys.argv[1:])
manifest = load_room_payload_manifest(resources / ROOM_PAYLOAD_MANIFEST_NAME)
assembled, _ = assemble_room_files(
    resources / BASE_RESOURCE_NAME,
    resources / ROOM_PAYLOAD_RESOURCE_NAME,
    manifest,
    {
        "randomize_chainsaw": True,
        "randomize_dash": True,
        "randomize_first_battery": True,
    },
)
for member, content in assembled.items():
    target = mod_root / member
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
write_deterministic_zip(assembled, archive)
PY
if [[ "$DEEP_AUDIT" == "1" ]]; then
python3 "$REPO_ROOT/tools/validation/audit_resource_packages.py" \
    --asset-root "$REPO_ROOT/packaging/mod_assets" \
    --mod-root "$MOD_AUDIT_DIR" \
    --generated-maps "$GENERATED_MAPS_DIR" \
    --source-map-root "$REPO_ROOT/vanillamaps" \
    --zip "$MOD_AUDIT_ZIP"
if find "$MOD_AUDIT_DIR" -path '*/generated/decls/propitem/propitem/ap*' -o \
    -path '*/generated/decls/propitem/propitem/equipment/ice_bomb.decl' -o \
    -path '*/generated/decls/propitem/propitem/weapon/rocket_launcher/base.decl' -o \
    -path '*/generated/decls/perks/perk/ap/*' -o \
    -path '*/generated/decls/logicentity/ap/*' | grep -q .; then
    echo "Forbidden propitem DECL override found in final mod ZIP" >&2
    exit 1
fi
python3 "$REPO_ROOT/tools/validation/validate_challenge_overrides.py" \
    --registry "$TEMP_DIR/challenge_location_registry.json" \
    --mod-root "$MOD_AUDIT_DIR"
python3 "$REPO_ROOT/tools/validation/validate_devinvloadout_package.py" \
    --mod-root "$MOD_AUDIT_DIR" \
    --map-registry "$MAP_SOURCES_FILE" \
    --generated-map "$GENERATED_MAPS_DIR/e1m1_intro.entities"
python3 "$REPO_ROOT/tools/validation/validate_item_notification_package.py" \
    --enabled "$ENABLE_ITEM_NOTIFICATIONS" \
    --maps-dir "$GENERATED_MAPS_DIR" \
    --mod-root "$MOD_AUDIT_DIR" \
    --client-dir "$EXTRACTED_AUDIT_DIR/client" \
    --release-manifest "$EXTRACTED_AUDIT_DIR/RELEASE_MANIFEST.json"
python3 "$REPO_ROOT/tools/validation/audit_item_notification_release.py" \
    --enabled "$ENABLE_ITEM_NOTIFICATIONS" \
    --generated-maps "$GENERATED_MAPS_DIR" \
    --playable-zip "$OUTPUT_DIR/$PTB_ZIP_NAME" \
    --map-registry "$MAP_SOURCES_FILE" \
    --decompressor "$TOOLS_DIR/idFileDeCompressor"
if find "$MOD_AUDIT_DIR" \( \
    -path '*/generated/decls/perks/perk/ap/*' -o \
    -path '*/generated/decls/logicentity/ap/*' \
\) -print -quit | grep -q .; then
    echo "Final ZIP contains rejected watcher DECL override" >&2
    exit 1
fi
mapfile -t AUDIT_MASTERY_OVERRIDE_FILES < <(find "$MOD_AUDIT_DIR" -type f \( \
    -path '*/generated/decls/unlockable/weapon_mastery/*' -o \
    -path '*/generated/decls/perks/perk/player/weapons/*' \
\) | LC_ALL=C sort)
[[ "${#AUDIT_MASTERY_OVERRIDE_FILES[@]}" == "26" ]] || { echo "Final ZIP base Mastery override set drifted" >&2; exit 1; }
if grep -q 'perkToGive|addStats|STAT_CURRENT_MASTERIES_AQUIRED|MASTERY_EARNED' "${AUDIT_MASTERY_OVERRIDE_FILES[@]}"; then
    echo "Final ZIP does not isolate Mastery item and location paths" >&2
    exit 1
fi
if grep -q 'give armor -200|AP_RUNTIME_CHECK_|3_900_000_000|3_800_000_000' \
    "$MOD_AUDIT_DIR" "$EXTRACTED_AUDIT_DIR/client/data/items.json"; then
    echo "Final ZIP contains Armor Drain or rejected watcher architecture" >&2
    exit 1
fi
if grep -q 'Ignoring unexpected goal transition event' \
    "$EXTRACTED_AUDIT_DIR/client/bridge_client.py"; then
    echo "Final ZIP contains old goal-only transition handler" >&2
    exit 1
fi
[[ "$(find "$EXTRACTED_AUDIT_DIR" -name ap_client.exe -type f | wc -l)" == "1" ]] || { echo "Final ZIP must contain exactly one ap_client.exe" >&2; exit 1; }
[[ "$(sha256sum "$EXTRACTED_AUDIT_DIR/client/ap_client.exe" | awk '{print $1}')" == "$FRESH_CLIENT_SHA256" ]] || { echo "ZIP ap_client.exe hash mismatch" >&2; exit 1; }
python3 "$REPO_ROOT/tools/validation/audit_packaged_transition_bridge.py" \
    "$EXTRACTED_AUDIT_DIR/client" \
    "$TEMP_DIR/challenge_location_registry.json" \
    "$EXTRACTED_AUDIT_DIR/RELEASE_MANIFEST.json" \
    "$EXTRACTED_AUDIT_DIR/doometernal.apworld"
fi
mapfile -t PACKAGE_FILES < <(unzip -Z1 "$OUTPUT_DIR/$PTB_ZIP_NAME" | grep -v '/$' | LC_ALL=C sort)
PACKAGE_ROOTS=()
for package_file in "${PACKAGE_FILES[@]}"; do
    package_root="${package_file%%/*}"
    if [[ ! " ${PACKAGE_ROOTS[*]} " =~ " ${package_root} " ]]; then
        PACKAGE_ROOTS+=("$package_root")
    fi
done
mapfile -t PACKAGE_ROOTS < <(printf '%s\n' "${PACKAGE_ROOTS[@]}" | LC_ALL=C sort)
mapfile -t EXPECTED_ROOTS < <(PYTHONPATH="$REPO_ROOT" python3 - "$LAUNCHER_EXECUTABLE" <<'PY'
import sys
from tools.validation.release_layout import expected_release_roots

print("\n".join(sorted(expected_release_roots(sys.argv[1]))))
PY
)
if [[ "${PACKAGE_ROOTS[*]}" != "${EXPECTED_ROOTS[*]}" ]]; then
    echo "Final ZIP root layout is not exact" >&2
    printf 'actual:\n%s\nexpected:\n%s\n' "${PACKAGE_ROOTS[*]}" "${EXPECTED_ROOTS[*]}" >&2
    exit 1
fi
if printf '%s\n' "${PACKAGE_FILES[@]}" | grep -E -i -q \
    '(^|/)(XINPUT1_3\.dll|EternalModManager[^/]*|EternalModInjectorShell[^/]*|Meathook[^/]*)(/|$)'; then
    echo "Final ZIP contains a prohibited external dependency" >&2
    exit 1
fi
mapfile -t ALLOWED_FILES < <(python3 - "$EXTRACTED_AUDIT_DIR/RELEASE_MANIFEST.json" <<'PY'
import json
import sys

files = json.load(open(sys.argv[1], encoding="utf-8"))["public_files"]
for name in sorted(set(files + ["doometernal.apworld"])):
    print(name)
PY
)
if [[ "${PACKAGE_FILES[*]}" != "${ALLOWED_FILES[*]}" ]]; then
    echo "Final ZIP violates the public package allowlist" >&2
    printf 'actual:\n%s\nallowed:\n%s\n' "${PACKAGE_FILES[*]}" "${ALLOWED_FILES[*]}" >&2
    exit 1
fi
if printf '%s\n' "${PACKAGE_FILES[@]}" | grep -E -i -q '(^|/)(playtests?|tests?|build|staging|__pycache__|\.git|todo|session|decisions|pitfalls|architecture)(/|$)|(^|/).*\.log$|(^|/).*\.pid$|(^|/)ap_config\.json$|(^|/)\.local\.env$|(^|/).*-(dev|debug)(\.|/|$)|AP_ICE_DIAG|(^|/).*(condump|seed|cache|output|diagnostic)'; then
    echo "Final ZIP contains a forbidden internal or development artifact" >&2
    exit 1
fi
if find "$OUTPUT_DIR/build" \
    -path "$OUTPUT_DIR/build/launcher/work" -prune -o \
    -type f -name '*.txt' -print -quit | grep -q .; then
    echo "Runtime-test .txt files are forbidden in build/release/build" >&2
    exit 1
fi
if unzip -p "$OUTPUT_DIR/$PTB_ZIP_NAME" README.md RELEASE_MANIFEST.json | grep -E -n -i '(/run/media/system/Eris/|/var/home/guilherme/|[A-Z]:\\\\Users\\\\guilherme\\|ap_ice_diag)' >/dev/null; then
    echo "Final ZIP text contains a personal path or diagnostic marker" >&2
    exit 1
fi
if [[ ! -f "$OUTPUT_DIR/$PTB_ZIP_NAME" ]] || \
   [[ "$(find "$OUTPUT_DIR" -maxdepth 1 -type f -name "$PTB_ZIP_NAME" | wc -l)" != "1" ]]; then
    echo "Playable development ZIP is missing or not unique in build/release" >&2
    exit 1
fi
echo "Playable development build created at: $OUTPUT_DIR"
echo "Room compiler resources: $OUTPUT_DIR/client/resources/base_mod.zip, room_payloads.zip, room_payload_manifest.json"
echo "Development bundle: $OUTPUT_DIR/$PTB_ZIP_NAME"
echo "Build log: $BUILD_LOG"
