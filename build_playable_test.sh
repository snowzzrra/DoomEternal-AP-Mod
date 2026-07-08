#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE="$(cd "$SCRIPT_DIR/.." && pwd)"
TOOLS_DIR="$WORKSPACE/Tools"
GAME_BASE="${DOOM_GAME_BASE:-/run/media/system/Eris/SteamLibrary/steamapps/common/DOOMEternal/base}"
OUTPUT_DIR="${1:-$SCRIPT_DIR/build/playable-test}"
TEMP_DIR="$(mktemp -d /tmp/doom-eap-build.XXXXXX)"
RELEASE_VERSION="v0.1.0-ptb"
PTB_ZIP_NAME="DoomEternalArchipelagoPlayableTest-${RELEASE_VERSION}.zip"

trap 'rm -rf "$TEMP_DIR"' EXIT

if [[ "${AP_PRESERVE_CONFIG:-0}" == "1" && -f "$OUTPUT_DIR/client/ap_config.json" ]]; then
    cp "$OUTPUT_DIR/client/ap_config.json" "$TEMP_DIR/ap_config.json"
fi

extract_and_build() {
    local resource_path="$1"
    local relative_entities_path="$2"
    local config_name="$3"
    local resource_name
    resource_name="$(basename "$resource_path" .resources)"
    local source_resource="$GAME_BASE/$resource_path"
    if [[ -f "${source_resource}.backup" ]]; then
        source_resource="${source_resource}.backup"
    fi

    local extract_dir="$TEMP_DIR/extracted/$config_name"
    local extracted_file="$extract_dir/maps/$relative_entities_path"
    local decompressed_file="$TEMP_DIR/decompressed/$config_name.entities"
    local generated_file="$TEMP_DIR/generated/$config_name.entities"
    local generated_manifest="$TEMP_DIR/manifests/$config_name.json"
    local packaged_file="$OUTPUT_DIR/mod/$resource_name/maps/$relative_entities_path"

    mkdir -p "$extract_dir" "$(dirname "$decompressed_file")" \
        "$(dirname "$generated_file")" "$(dirname "$generated_manifest")" \
        "$(dirname "$packaged_file")"

    "$TOOLS_DIR/EternalResourceExtractor" \
        "$source_resource" "$extract_dir" --quiet --filter='*.entities'

    if [[ ! -f "$extracted_file" ]]; then
        echo "Expected entities file was not extracted: $extracted_file" >&2
        return 1
    fi

    "$TOOLS_DIR/idFileDeCompressor" --decompress \
        "$extracted_file" "$decompressed_file"

    python3 "$SCRIPT_DIR/ap_map_generator.py" \
        --input "$decompressed_file" \
        --output "$generated_file" \
        --config "$SCRIPT_DIR/level_configs/$config_name.json" \
        --manifest "$generated_manifest" \
        --items "$SCRIPT_DIR/data/items.json"

    python3 -c \
        'import json,sys; expected=json.load(open(sys.argv[1])); actual=json.load(open(sys.argv[2])); \
only_expected=sorted(set(expected)-set(actual)); only_actual=sorted(set(actual)-set(expected)); \
value_mismatch=[(k, expected[k], actual[k]) for k in sorted(set(expected)&set(actual)) if expected[k]!=actual[k]]; \
assert expected == actual, f"generated manifest differs: {sys.argv[1]} | only_expected={only_expected} | only_actual={only_actual} | value_mismatch={value_mismatch}"' \
        "$SCRIPT_DIR/manifests/$config_name.json" "$generated_manifest"

    "$TOOLS_DIR/idFileDeCompressor" --compress \
        "$generated_file" "$packaged_file"
}

rm -rf "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR/mod" "$OUTPUT_DIR/client" "$OUTPUT_DIR/apworld/worlds"
cp -R "$SCRIPT_DIR/packaging/mod_assets/." "$OUTPUT_DIR/mod/"

extract_and_build \
    "game/sp/e1m1_intro/e1m1_intro_patch3.resources" \
    "game/sp/e1m1_intro/e1m1_intro.entities" \
    "e1m1_intro"
extract_and_build \
    "game/sp/e1m2_battle/e1m2_battle_patch3.resources" \
    "game/sp/e1m2_battle/e1m2_battle.entities" \
    "e1m2_war"
extract_and_build \
    "game/hub/hub_patch2.resources" \
    "game/hub/hub.entities" \
    "hub"
extract_and_build \
    "game/sp/e1m3_cult/e1m3_cult_patch3.resources" \
    "game/sp/e1m3_cult/e1m3_cult.entities" \
    "e1m3_cult"

cp "$SCRIPT_DIR/packaging/EternalMod.json" "$OUTPUT_DIR/mod/EternalMod.json"
cp "$SCRIPT_DIR/README.md" "$OUTPUT_DIR/README.md"
cat > "$OUTPUT_DIR/RELEASE_MANIFEST.json" <<EOF
{
  "name": "Doom Eternal Archipelago Playable Test",
  "version": "${RELEASE_VERSION}",
  "files": [
    "README.md",
    "RELEASE_MANIFEST.json",
    "DoomEternalArchipelagoPreAlpha.zip",
    "doometernal.apworld",
    "client/ap_client.exe",
    "client/ap_logger.exe",
    "client/bridge_client.py",
    "client/dinput8.dll",
    "client/dxgi.dll",
    "client/version.dll",
    "client/xinput1_4.dll",
    "client/save_death_probe.exe",
    "client/save_decrypt.py",
    "client/run_bridge.sh",
    "client/start_injector_windows.bat",
    "client/validate_runtime_install.sh",
    "client/ap_config.example.json",
    "client/data/items.json",
    "client/data/runtime_locations.json",
    "client/manifests/e1m1_intro.json",
    "client/manifests/e1m2_war.json",
    "client/manifests/e1m3_cult.json",
    "client/manifests/hub.json",
    "client/player_templates/DoomSlayer.yaml",
    "client/player_templates/Marine.yaml"
  ]
}
EOF
cp "$SCRIPT_DIR/ap_client.exe" "$SCRIPT_DIR/ap_logger.exe" \
    "$SCRIPT_DIR/dinput8.dll" "$SCRIPT_DIR/dxgi.dll" \
    "$SCRIPT_DIR/version.dll" "$SCRIPT_DIR/xinput1_4.dll" \
    "$SCRIPT_DIR/save_death_probe.exe" \
    "$SCRIPT_DIR/bridge_client.py" \
    "$SCRIPT_DIR/run_bridge.sh" "$SCRIPT_DIR/save_decrypt.py" \
    "$SCRIPT_DIR/start_injector_windows.bat" \
    "$SCRIPT_DIR/ap_config.example.json" \
    "$SCRIPT_DIR/validate_runtime_install.sh" \
    "$OUTPUT_DIR/client/"
mkdir -p "$OUTPUT_DIR/client/data" "$OUTPUT_DIR/client/manifests"
cp "$SCRIPT_DIR/data/items.json" \
    "$SCRIPT_DIR/data/runtime_locations.json" \
    "$OUTPUT_DIR/client/data/"
cp -R "$SCRIPT_DIR/manifests/." "$OUTPUT_DIR/client/manifests/"
cp -R "$SCRIPT_DIR/player_templates" "$OUTPUT_DIR/client/"
cp -R "$WORKSPACE/Archipelago/worlds/doometernal" \
    "$OUTPUT_DIR/apworld/worlds/doometernal"
find "$OUTPUT_DIR/apworld" -type d -name __pycache__ -prune -exec rm -rf {} +
python3 "$SCRIPT_DIR/build_apworld.py" \
    "$OUTPUT_DIR/apworld/worlds/doometernal" \
    "$OUTPUT_DIR/doometernal.apworld"
chmod +x "$OUTPUT_DIR/client/run_bridge.sh"
chmod +x "$OUTPUT_DIR/client/validate_runtime_install.sh"

(
    cd "$OUTPUT_DIR/mod"
    zip -q -r "$OUTPUT_DIR/DoomEternalArchipelagoPreAlpha.zip" .
)

(
    cd "$OUTPUT_DIR"
    zip -q -r "$PTB_ZIP_NAME" \
        README.md RELEASE_MANIFEST.json client doometernal.apworld \
        DoomEternalArchipelagoPreAlpha.zip
)

echo "Playable test build created at: $OUTPUT_DIR"
echo "Installable mod: $OUTPUT_DIR/DoomEternalArchipelagoPreAlpha.zip"
echo "Linux/Windows test bundle: $OUTPUT_DIR/$PTB_ZIP_NAME"
