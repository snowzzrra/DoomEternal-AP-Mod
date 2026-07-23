#!/bin/bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
export PYTHONPATH="$REPO_ROOT"
WORKSPACE="$(cd "$REPO_ROOT/.." && pwd)"
TOOLS_DIR="$WORKSPACE/Tools"
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
MAP_SOURCES_FILE="${AP_MAP_SOURCES_FILE:-$REPO_ROOT/data/map_sources.json}"
VANILLA_MAPS_DIR="${VANILLA_MAPS_DIR:-$REPO_ROOT/vanillamaps}"
RELEASE_VERSION="v0.3.1-alpha"
PTB_ZIP_NAME="DoomEternalArchipelagoPlayableTest-${RELEASE_VERSION}.zip"
STALE_DEV_ZIP="$OUTPUT_DIR/DoomEternalArchipelagoPlayableTest-v0.3.0-pre-alpha-dev.zip"
AUTOMAP_PROTOTYPE_ONLY="${AP_AUTOMAP_PROTOTYPE_ONLY:-0}"
GENERATED_MAPS_DIR="$OUTPUT_DIR/build/generated-maps"
GENERATED_MANIFESTS_DIR="$TEMP_DIR/manifests"
BUILD_LOG="$OUTPUT_DIR/build/build.log"
CLIENT_BUILD_DIR="$OUTPUT_DIR/build/client"
PACKAGEMAPSPEC="${DOOM_PACKAGEMAPSPEC:-/run/media/system/Eris/SteamLibrary/steamapps/common/DOOMEternal/base/packagemapspec.json}"

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

trap 'rm -rf "$TEMP_DIR"' EXIT
trap 'report_build_failure "$LINENO" "$BASH_COMMAND"' ERR

mkdir -p "$(dirname "$BUILD_LOG")"
: > "$BUILD_LOG"
exec > >(tee -a "$BUILD_LOG") 2>&1

if [[ "$AUTOMAP_PROTOTYPE_ONLY" != "1" ]]; then
    run_build_step "scripted location contract validation" \
        python3 "$REPO_ROOT/tools/validation/audit_scripted_location.py" \
        --contracts "$REPO_ROOT/data/scripted_location_contracts.json"
    run_build_step "release data validation" \
        python3 "$REPO_ROOT/tools/validation/validate_data.py"
fi

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
    local packaged_file="$OUTPUT_DIR/mod/$resource_name/maps/$relative_entities_path"
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

    python3 "$REPO_ROOT/tools/maps/ap_map_generator.py" \
        --input "$source_map" \
        --output "$generated_file" \
        --config "$REPO_ROOT/$config_path" \
        --manifest "$generated_manifest" \
        --items "$REPO_ROOT/data/items.json" \
        "${GENERATOR_ARGS[@]}"

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

rm -rf "$OUTPUT_DIR/mod" "$OUTPUT_DIR/client" "$OUTPUT_DIR/apworld" \
    "$OUTPUT_DIR/DoomEternalArchipelagoAlpha.zip" \
    "$OUTPUT_DIR/doometernal.apworld" "$OUTPUT_DIR/README.md" \
    "$OUTPUT_DIR/RELEASE_MANIFEST.json" "$OUTPUT_DIR/$PTB_ZIP_NAME" \
    "$STALE_DEV_ZIP" \
    "$OUTPUT_DIR/DoomEternalArchipelago-v0.3.0-pre-alpha.zip" \
    "$OUTPUT_DIR/DoomEternalArchipelagoPreAlpha.zip"
find "$OUTPUT_DIR/build" -mindepth 1 -maxdepth 1 ! -name build.log -exec rm -rf -- {} +
mkdir -p "$OUTPUT_DIR/mod" "$OUTPUT_DIR/client" "$OUTPUT_DIR/apworld/worlds" \
    "$GENERATED_MAPS_DIR" "$TEMP_DIR"
echo "Build log: $BUILD_LOG"
if [[ "$ENABLE_ITEM_NOTIFICATIONS" == "1" ]]; then
    echo "ITEM_NOTIFICATIONS=enabled"
else
    echo "ITEM_NOTIFICATIONS=disabled"
fi
if [[ "${AP_NOTIFICATION_LAB:-0}" == "1" ]]; then
    echo "NOTIFICATION_LAB=enabled"
else
    echo "NOTIFICATION_LAB=disabled"
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

cp -R "$REPO_ROOT/packaging/mod_assets/." "$OUTPUT_DIR/mod/"

mapfile -t MAP_ROWS < <(
    python3 "$REPO_ROOT/map_registry.py" release-rows --registry "$MAP_SOURCES_FILE"
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
    --output "$OUTPUT_DIR/mod/gameresources_patch1/EternalMod/strings/english.json"
python3 "$REPO_ROOT/tools/release/build_string_table.py" \
    --items "$REPO_ROOT/data/items.json" \
    --item-replay-policies "$REPO_ROOT/data/item_replay_policies.json" \
    --location-names "$REPO_ROOT/data/location_names.json" \
    --maps-dir "$GENERATED_MAPS_DIR" \
    --output "$OUTPUT_DIR/mod/gameresources_patch1/EternalMod/strings/portuguese.json"

python3 "$REPO_ROOT/tools/maps/mission_complete_map_patcher.py" \
    --contracts "$REPO_ROOT/data/mission_complete_map_contracts.json" \
    "${MISSION_MAP_ARGS[@]}" \
    --mod-root "$OUTPUT_DIR/mod" \
    --audit-output "$TEMP_DIR/mission-complete-map-patch.json"
python3 - "$TEMP_DIR/mission-complete-map-patch.json" <<'PY'
import json
import sys

audit = json.load(open(sys.argv[1], encoding="utf-8"))
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
assert audit["fortress_visit_3_goal"]["after_targets"] == [
    "ap_goal_fortress_visit_3",
]
assert audit["fortress_visit_3_goal"]["terminal"]["nextMapName"] == (
    "maps/game/sp/e2m1_nest/e2m1_nest.map"
)
PY

for map_row in "${MAP_ROWS[@]}"; do
    IFS=$'\t' read -r map_key _ _ _ _ generated_output resource_path relative_entities_path _ <<< "$map_row"
    resource_name="$(basename "$resource_path" .resources)"
    "$TOOLS_DIR/idFileDeCompressor" --compress \
        "$GENERATED_MAPS_DIR/$generated_output" \
        "$OUTPUT_DIR/mod/$resource_name/maps/$relative_entities_path"
done

TEMP_DIR=$(mktemp -d)
python3 "$REPO_ROOT/tools/maps/automap_native_decl_builder.py" \
    --mod-root "$OUTPUT_DIR/mod" \
    --audit-output "$TEMP_DIR/automap-native-toy-override.json"
python3 "$REPO_ROOT/tools/decls/rune_decl_builder.py" \
    --mod-root "$OUTPUT_DIR/mod" \
    --audit-output "$TEMP_DIR/rune-menu-override.json"
python3 "$REPO_ROOT/tools/decls/mastery_decl_builder.py" \
    --mod-root "$OUTPUT_DIR/mod" \
    --audit-output "$TEMP_DIR/base-mastery-overrides.json"
python3 "$REPO_ROOT/tools/decls/mission_challenge_decl_builder.py" \
    --mod-root "$OUTPUT_DIR/mod" \
    --audit-output "$TEMP_DIR/mission-challenge-overrides.json"
python3 "$REPO_ROOT/tools/decls/devinv_builder.py" \
    --mod-root "$OUTPUT_DIR/mod" \
    --map-registry "$MAP_SOURCES_FILE" \
    --audit-output "$TEMP_DIR/devinv-override.json"
python3 "$REPO_ROOT/tools/validation/validate_challenge_overrides.py" \
    --registry "$REPO_ROOT/data/challenge_location_registry.json" \
    --mod-root "$OUTPUT_DIR/mod"

python3 "$REPO_ROOT/tools/validation/audit_scripted_location.py" \
    --contracts "$REPO_ROOT/data/scripted_location_contracts.json" \
    --verify-generated-map "$OUTPUT_DIR/build/generated-maps/hub.entities" \
    --location 7770074
python3 "$REPO_ROOT/tools/validation/audit_scripted_location.py" \
    --contracts "$REPO_ROOT/data/scripted_location_contracts.json" \
    --verify-generated-map "$OUTPUT_DIR/build/generated-maps/e1m3_cult.entities" \
    --location 7770056

ICE_DECL_RELATIVE="generated/decls/logicentity/maps/game/hub/hub/info_logic_hub_from_e1m2.decl"
python3 "$REPO_ROOT/tools/maps/logic_decl_patcher.py" \
    --contracts "$REPO_ROOT/data/scripted_location_contracts.json" \
    --location 7770074 \
    --output "$OUTPUT_DIR/mod/hub_patch2/$ICE_DECL_RELATIVE" \
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

cp "$REPO_ROOT/packaging/EternalMod.json" "$OUTPUT_DIR/mod/EternalMod.json"
cp "$REPO_ROOT/README.md" "$OUTPUT_DIR/README.md"
cp "$CLIENT_BUILD_DIR/ap_client.exe" "$CLIENT_BUILD_DIR/save_death_probe.exe" \
    "$REPO_ROOT/bridge_client.py" "$REPO_ROOT/bootstrap_actions.py" \
    "$REPO_ROOT/challenge_registry.py" \
    "$REPO_ROOT/foundation.py" \
    "$REPO_ROOT/item_classification.py" \
    "$REPO_ROOT/item_reconciliation.py" \
    "$REPO_ROOT/map_registry.py" \
    "$REPO_ROOT/scripts/launch/run_bridge.sh" "$REPO_ROOT/save_decrypt.py" \
    "$REPO_ROOT/scripts/launch/start_injector_windows.bat" \
    "$REPO_ROOT/packaging/client/ap_config.example.json" \
    "$REPO_ROOT/scripts/validate/runtime_install.sh" \
    "$OUTPUT_DIR/client/"
mkdir -p "$OUTPUT_DIR/client/data" "$OUTPUT_DIR/client/manifests"
cp "$REPO_ROOT/data/items.json" \
    "$REPO_ROOT/data/item_classifications.json" \
    "$REPO_ROOT/data/item_replay_policies.json" \
    "$REPO_ROOT/data/location_names.json" \
    "$REPO_ROOT/data/challenge_location_registry.json" \
    "$REPO_ROOT/data/runtime_locations.json" \
    "$REPO_ROOT/data/map_sources.json" \
    "$OUTPUT_DIR/client/data/"
for map_row in "${MAP_ROWS[@]}"; do
    IFS=$'\t' read -r _ _ _ _ manifest_path _ <<< "$map_row"
    cp "$REPO_ROOT/$manifest_path" "$OUTPUT_DIR/client/manifests/"
done
cp -R "$REPO_ROOT/player_templates" "$OUTPUT_DIR/client/"
cp -R "$WORKSPACE/Archipelago/worlds/doometernal" \
    "$OUTPUT_DIR/apworld/worlds/doometernal"
find "$OUTPUT_DIR/apworld" -type d -name __pycache__ -prune -exec rm -rf {} +
python3 "$REPO_ROOT/tools/release/build_apworld.py" \
    "$OUTPUT_DIR/apworld/worlds/doometernal" \
    "$OUTPUT_DIR/doometernal.apworld"
chmod +x "$OUTPUT_DIR/client/run_bridge.sh"

cp "$REPO_ROOT/scripts/validate/runtime_install.sh" "$OUTPUT_DIR/client/validate_runtime_install.sh"
chmod +x "$OUTPUT_DIR/client/validate_runtime_install.sh"

python3 - "$OUTPUT_DIR/client/bridge_client.py" "$OUTPUT_DIR/client/bridge_identity.json" "$ENABLE_ITEM_NOTIFICATIONS" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

bridge = Path(sys.argv[1])
identity = {
    "protocol": 3,
    "sha256": hashlib.sha256(bridge.read_bytes()).hexdigest(),
    "item_notifications": {
        "enabled": sys.argv[3] == "1",
        "revision": 1,
        "experimental": False,
    },
}
identity["revision"] = f"mission-unified-{identity['sha256'][:12]}"
Path(sys.argv[2]).write_text(json.dumps(identity, indent=2) + "\n", encoding="utf-8")
PY

VALIDATION_JSON="$TEMP_DIR/validate-client.json"
python3 "$REPO_ROOT/tools/validation/validate_windows_runtime_deps.py" \
    "$OUTPUT_DIR/client" \
    --forbid-local version.dll \
    --forbid-local dinput8.dll \
    --forbid-local dxgi.dll \
    --forbid-local xinput1_4.dll \
    --json-output "$VALIDATION_JSON"

TOOLCHAIN_COMPILER="$(distrobox enter doom-cpp -- x86_64-w64-mingw32-g++ --version | head -n 1)"

python3 - "$OUTPUT_DIR" "$RELEASE_VERSION" "$VALIDATION_JSON" "$TOOLCHAIN_COMPILER" "$REPO_ROOT" "$MAP_SOURCES_FILE" "$ENABLE_ITEM_NOTIFICATIONS" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

output_dir = Path(sys.argv[1])
release_version = sys.argv[2]
validation_path = Path(sys.argv[3])
toolchain_compiler = sys.argv[4]
sys.path.insert(0, sys.argv[5])
from map_registry import load_map_registry, release_plan
map_manifest_files = [
    plan.client_manifest for plan in release_plan(load_map_registry(Path(sys.argv[6])))
]

validation = json.loads(validation_path.read_text(encoding="utf-8"))
bridge_path = output_dir / "client" / "bridge_client.py"
bridge_sha256 = hashlib.sha256(bridge_path.read_bytes()).hexdigest()
item_notifications_enabled = sys.argv[7] == "1"

manifest = {
    "name": "DOOM Eternal Archipelago",
    "version": release_version,
    "files": [
        "README.md",
        "RELEASE_MANIFEST.json",
        "DoomEternalArchipelagoAlpha.zip",
        "doometernal.apworld",
        "client/ap_client.exe",
        "client/bridge_client.py",
        "client/bridge_identity.json",
        "client/bootstrap_actions.py",
        "client/challenge_registry.py",
        "client/foundation.py",
        "client/item_classification.py",
        "client/item_reconciliation.py",
        "client/map_registry.py",
        "client/save_death_probe.exe",
        "client/save_decrypt.py",
        "client/run_bridge.sh",
        "client/start_injector_windows.bat",
        "client/runtime_install.sh",
        "client/validate_runtime_install.sh",
        "client/ap_config.example.json",
        "client/data/items.json",
        "client/data/item_classifications.json",
        "client/data/item_replay_policies.json",
        "client/data/location_names.json",
        "client/data/challenge_location_registry.json",
        "client/data/runtime_locations.json",
        "client/data/map_sources.json",
        *map_manifest_files,
        "client/player_templates/DoomSlayer.yaml",
        "client/player_templates/Marine.yaml",
    ],
    "ap_client": {
        "sha256": validation["exe_sha256"],
        "size": validation["exe_size"],
        "direct_imports": validation["exe_direct_imports"],
        "compiler": toolchain_compiler,
        "linker_flags": [
            "-static",
            "-static-libgcc",
            "-static-libstdc++",
            "-lversion",
        ],
    },
    "mission_bridge": {
        "protocol": 3,
        "sha256": bridge_sha256,
        "revision": f"mission-unified-{bridge_sha256[:12]}",
        "transition_handler": "unified",
    },
    "item_notifications": {
        "enabled": item_notifications_enabled,
        "revision": 1,
        "experimental": False,
    },
    "validator": {
        "status": validation["status"],
        "errors": validation["errors"],
        "forbidden_local_dlls_absent": [
            "version.dll",
            "dinput8.dll",
            "dxgi.dll",
            "xinput1_4.dll",
        ],
    },
}

(output_dir / "RELEASE_MANIFEST.json").write_text(
    json.dumps(manifest, indent=2) + "\n",
    encoding="utf-8",
)
PY

if [[ "$AUTOMAP_PROTOTYPE_ONLY" != "1" ]]; then
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
    "$OUTPUT_DIR/mod" "$GENERATED_MAPS_DIR" "$OUTPUT_DIR/client/data/items.json"; then
    echo "Rejected Armor Drain or watcher architecture entered build" >&2
    exit 1
fi
if grep -q 'Ignoring unexpected goal transition event' "$OUTPUT_DIR/client/bridge_client.py"; then
    echo "Old goal-only transition handler entered build" >&2
    exit 1
fi
mapfile -t MASTERY_OVERRIDE_FILES < <(find "$OUTPUT_DIR/mod" -type f \( \
    -path '*/generated/decls/unlockable/weapon_mastery/*' -o \
    -path '*/generated/decls/perks/perk/player/weapons/*' \
\) | LC_ALL=C sort)
[[ "${#MASTERY_OVERRIDE_FILES[@]}" == "26" ]] || { echo "Base Mastery override set is incomplete" >&2; exit 1; }
if grep -q 'perkToGive|addStats|STAT_CURRENT_MASTERIES_AQUIRED|MASTERY_EARNED' "${MASTERY_OVERRIDE_FILES[@]}"; then
    echo "Mastery override retains natural reward, completion stat, or global stat" >&2
    exit 1
fi
if ! grep -q 'upgrade/weapons/shotguns/shotgun/pop_rocket_more_bombs' \
    "$OUTPUT_DIR/mod/gameresources/generated/decls/perks/perk/player/weapons/shotgun/pop_rocket_more_bombs.decl"; then
    echo "Sticky AP gameplay upgrade missing" >&2
    exit 1
fi
python3 "$REPO_ROOT/tools/validation/validate_challenge_overrides.py" \
    --registry "$REPO_ROOT/data/challenge_location_registry.json" \
    --mod-root "$OUTPUT_DIR/mod"
python3 "$REPO_ROOT/tools/validation/validate_devinvloadout_package.py" \
    --mod-root "$OUTPUT_DIR/mod" \
    --map-registry "$MAP_SOURCES_FILE" \
    --generated-map "$GENERATED_MAPS_DIR/e1m1_intro.entities"
python3 "$REPO_ROOT/tools/validation/validate_item_notification_package.py" \
    --enabled "$ENABLE_ITEM_NOTIFICATIONS" \
    --maps-dir "$GENERATED_MAPS_DIR" \
    --mod-root "$OUTPUT_DIR/mod" \
    --client-dir "$OUTPUT_DIR/client" \
    --release-manifest "$OUTPUT_DIR/RELEASE_MANIFEST.json"
python3 "$REPO_ROOT/tools/validation/audit_item_notification_release.py" \
    --enabled "$ENABLE_ITEM_NOTIFICATIONS" \
    --generated-maps "$GENERATED_MAPS_DIR" \
    --mod-root "$OUTPUT_DIR/mod" \
    --client-dir "$OUTPUT_DIR/client" \
    --release-manifest "$OUTPUT_DIR/RELEASE_MANIFEST.json" \
    --map-registry "$MAP_SOURCES_FILE" \
    --decompressor "$TOOLS_DIR/idFileDeCompressor" \
    --update-manifest
if find "$OUTPUT_DIR/mod" \( \
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

(
    cd "$OUTPUT_DIR/mod"
    zip -q -r "$OUTPUT_DIR/DoomEternalArchipelagoAlpha.zip" .
)

(
    cd "$OUTPUT_DIR"
    zip -q -r "$PTB_ZIP_NAME" \
        README.md RELEASE_MANIFEST.json client doometernal.apworld \
        DoomEternalArchipelagoAlpha.zip
)

if [[ "$AUTOMAP_PROTOTYPE_ONLY" == "1" ]]; then
    rm -rf "$OUTPUT_DIR/build" "$OUTPUT_DIR/client" "$OUTPUT_DIR/mod" \
        "$OUTPUT_DIR/apworld" "$OUTPUT_DIR/doometernal.apworld" \
        "$OUTPUT_DIR/DoomEternalArchipelagoAlpha.zip" \
        "$OUTPUT_DIR/README.md" "$OUTPUT_DIR/RELEASE_MANIFEST.json"
    echo "Automap prototype ZIP created at: $OUTPUT_DIR/$PTB_ZIP_NAME"
    exit 0
fi

EXTRACTED_AUDIT_DIR="$TEMP_DIR/extracted-final"
mkdir -p "$EXTRACTED_AUDIT_DIR"
unzip -q "$OUTPUT_DIR/$PTB_ZIP_NAME" -d "$EXTRACTED_AUDIT_DIR"
python3 - "$EXTRACTED_AUDIT_DIR/client/data/items.json" <<'PY'
import json
import sys

items = json.load(open(sys.argv[1], encoding="utf-8"))
assert len(items) == 116
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
MOD_AUDIT_DIR="$TEMP_DIR/extracted-mod"
mkdir -p "$MOD_AUDIT_DIR"
unzip -q "$EXTRACTED_AUDIT_DIR/DoomEternalArchipelagoAlpha.zip" -d "$MOD_AUDIT_DIR"
if find "$MOD_AUDIT_DIR" -path '*/generated/decls/propitem/propitem/ap*' -o \
    -path '*/generated/decls/propitem/propitem/equipment/ice_bomb.decl' -o \
    -path '*/generated/decls/propitem/propitem/weapon/rocket_launcher/base.decl' -o \
    -path '*/generated/decls/perks/perk/ap/*' -o \
    -path '*/generated/decls/logicentity/ap/*' | grep -q .; then
    echo "Forbidden propitem DECL override found in final mod ZIP" >&2
    exit 1
fi
python3 "$REPO_ROOT/tools/validation/validate_challenge_overrides.py" \
    --registry "$REPO_ROOT/data/challenge_location_registry.json" \
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
python3 "$REPO_ROOT/tools/validation/validate_windows_runtime_deps.py" \
    "$EXTRACTED_AUDIT_DIR/client" \
    --forbid-local version.dll \
    --forbid-local dinput8.dll \
    --forbid-local dxgi.dll \
    --forbid-local xinput1_4.dll
[[ "$(find "$EXTRACTED_AUDIT_DIR" -name ap_client.exe -type f | wc -l)" == "1" ]] || { echo "Final ZIP must contain exactly one ap_client.exe" >&2; exit 1; }
[[ "$(sha256sum "$EXTRACTED_AUDIT_DIR/client/ap_client.exe" | awk '{print $1}')" == "$FRESH_CLIENT_SHA256" ]] || { echo "ZIP ap_client.exe hash mismatch" >&2; exit 1; }
python3 "$REPO_ROOT/tools/validation/audit_packaged_transition_bridge.py" \
    "$EXTRACTED_AUDIT_DIR/client" \
    "$REPO_ROOT/data/challenge_location_registry.json" \
    "$EXTRACTED_AUDIT_DIR/RELEASE_MANIFEST.json" \
    "$EXTRACTED_AUDIT_DIR/doometernal.apworld"
mapfile -t PACKAGE_FILES < <(unzip -Z1 "$OUTPUT_DIR/$PTB_ZIP_NAME" | grep -v '/$' | LC_ALL=C sort)
mapfile -t ALLOWED_FILES < <(python3 - "$EXTRACTED_AUDIT_DIR/RELEASE_MANIFEST.json" <<'PY'
import json
import sys

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
for name in sorted(set(manifest["files"] + ["doometernal.apworld"])):
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
if find "$OUTPUT_DIR/build" -type f -name '*.txt' -print -quit | grep -q .; then
    echo "Runtime-test .txt files are forbidden in build/release/build" >&2
    exit 1
fi
if unzip -p "$OUTPUT_DIR/$PTB_ZIP_NAME" README.md RELEASE_MANIFEST.json | grep -E -n -i '(/run/media/system/Eris/|/var/home/guilherme/|[A-Z]:\\\\Users\\\\guilherme\\|ap_ice_diag)' >/dev/null; then
    echo "Final ZIP text contains a personal path or diagnostic marker" >&2
    exit 1
fi
if [[ "$(find "$OUTPUT_DIR" -maxdepth 1 -type f -name 'DoomEternalArchipelago*dev*.zip' | wc -l)" != "1" ]]; then
    echo "Development ZIP count in build/release is not exactly one" >&2
    exit 1
fi
echo "Playable development build created at: $OUTPUT_DIR"
echo "Installable mod: $OUTPUT_DIR/DoomEternalArchipelagoAlpha.zip"
echo "Development bundle: $OUTPUT_DIR/$PTB_ZIP_NAME"
echo "Build log: $BUILD_LOG"
