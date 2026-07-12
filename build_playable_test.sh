#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE="$(cd "$SCRIPT_DIR/.." && pwd)"
TOOLS_DIR="$WORKSPACE/Tools"
OUTPUT_DIR="${1:-$SCRIPT_DIR/build/playable-test}"
TEMP_DIR="$(mktemp -d /tmp/doom-eap-build.XXXXXX)"
MAP_SOURCES_FILE="${AP_MAP_SOURCES_FILE:-$SCRIPT_DIR/data/map_sources.json}"
VANILLA_MAPS_DIR="${VANILLA_MAPS_DIR:-$SCRIPT_DIR/vanillamaps}"
PTB_VERSION="${PTB_VERSION:-v0.2.1-pre-alpha-dev}"
RELEASE_VERSION="v${PTB_VERSION#v}"
PTB_ZIP_NAME="DoomEternalArchipelagoPlayableTest-${RELEASE_VERSION}.zip"
GENERATED_MAPS_DIR="${AP_GENERATED_MAPS_DIR:-$OUTPUT_DIR/build/generated-maps}"
GENERATED_MANIFESTS_DIR="$TEMP_DIR/manifests"
BUILD_LOG="$OUTPUT_DIR/build/build.log"
CLIENT_BUILD_DIR="$SCRIPT_DIR/build/client"

trap 'rm -rf "$TEMP_DIR"' EXIT

if [[ "${AP_PRESERVE_CONFIG:-0}" == "1" && -f "$OUTPUT_DIR/client/ap_config.json" ]]; then
    cp "$OUTPUT_DIR/client/ap_config.json" "$TEMP_DIR/ap_config.json"
fi

extract_and_build() {
    local map_key="$1"
    local source_file="$2"
    local source_sha256="$3"
    local config_path="$4"
    local manifest_path="$5"
    local resource_path="$6"
    local relative_entities_path="$7"
    local supported_game_revision="$8"
    local resource_name
    resource_name="$(basename "$resource_path" .resources)"
    local source_map="$VANILLA_MAPS_DIR/$source_file"
    local generated_file="$GENERATED_MAPS_DIR/$map_key.entities"
    local generated_manifest="$GENERATED_MANIFESTS_DIR/$map_key.json"
    local packaged_file="$OUTPUT_DIR/mod/$resource_name/maps/$relative_entities_path"
    local source_hash_before
    local source_hash_after
    local generated_hash
    local source_size

    mkdir -p "$(dirname "$generated_file")" "$(dirname "$generated_manifest")" \
        "$(dirname "$packaged_file")" "$(dirname "$BUILD_LOG")"

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

    python3 "$SCRIPT_DIR/ap_map_generator.py" \
        --input "$source_map" \
        --output "$generated_file" \
        --config "$SCRIPT_DIR/$config_path" \
        --manifest "$generated_manifest" \
        --items "$SCRIPT_DIR/data/items.json"

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
        "$SCRIPT_DIR/$manifest_path" "$generated_manifest"

    "$TOOLS_DIR/idFileDeCompressor" --compress \
        "$generated_file" "$packaged_file"
}

rm -rf "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR/mod" "$OUTPUT_DIR/client" "$OUTPUT_DIR/apworld/worlds" \
    "$GENERATED_MAPS_DIR"
"$SCRIPT_DIR/build_client.sh"
if [[ ! -f "$CLIENT_BUILD_DIR/ap_client.exe" || ! -f "$CLIENT_BUILD_DIR/save_death_probe.exe" ]]; then
    echo "Fresh client build is missing required executable(s)" >&2
    exit 1
fi
if [[ -f "$SCRIPT_DIR/ap_client.exe" ]]; then
    echo "Refusing to package ap_client.exe from the source tree" >&2
    exit 1
fi
cp -R "$SCRIPT_DIR/packaging/mod_assets/." "$OUTPUT_DIR/mod/"

mapfile -t MAP_ROWS < <(
    python3 - "$MAP_SOURCES_FILE" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as file:
    map_sources = json.load(file).get("maps", {})

for map_key, source in map_sources.items():
    if not source.get("enabled", True):
        continue
    print("\t".join([
        map_key,
        source["source_file"],
        source["source_sha256"],
        source["level_config"],
        source["manifest"],
        source["resource_path"],
        source["relative_entities_path"],
        source["supported_game_revision"],
    ]))
PY
)

for map_row in "${MAP_ROWS[@]}"; do
    IFS=$'\t' read -r map_key source_file source_sha256 config_path manifest_path resource_path relative_entities_path supported_game_revision <<< "$map_row"
    extract_and_build \
        "$map_key" \
        "$source_file" \
        "$source_sha256" \
        "$config_path" \
        "$manifest_path" \
        "$resource_path" \
        "$relative_entities_path" \
        "$supported_game_revision"
done

cp "$SCRIPT_DIR/packaging/EternalMod.json" "$OUTPUT_DIR/mod/EternalMod.json"
cp "$SCRIPT_DIR/README.md" "$OUTPUT_DIR/README.md"
cp "$CLIENT_BUILD_DIR/ap_client.exe" "$CLIENT_BUILD_DIR/save_death_probe.exe" \
    "$SCRIPT_DIR/bridge_client.py" "$SCRIPT_DIR/bootstrap_actions.py" \
    "$SCRIPT_DIR/foundation.py" \
    "$SCRIPT_DIR/run_bridge.sh" "$SCRIPT_DIR/save_decrypt.py" \
    "$SCRIPT_DIR/start_injector_windows.bat" \
    "$SCRIPT_DIR/ap_config.example.json" \
    "$SCRIPT_DIR/validate_runtime_install.sh" \
    "$OUTPUT_DIR/client/"
mkdir -p "$OUTPUT_DIR/client/data" "$OUTPUT_DIR/client/manifests"
cp "$SCRIPT_DIR/data/items.json" \
    "$SCRIPT_DIR/data/runtime_locations.json" \
    "$OUTPUT_DIR/client/data/"
mkdir -p "$OUTPUT_DIR/tools"
cp "$SCRIPT_DIR/tools/test_save_scenarios.py" \
    "$SCRIPT_DIR/tools/generate_foundation_test_plan.py" "$OUTPUT_DIR/tools/"
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

VALIDATION_JSON="$TEMP_DIR/validate-client.json"
python3 "$SCRIPT_DIR/validate_windows_runtime_deps.py" \
    "$OUTPUT_DIR/client" \
    --forbid-local version.dll \
    --forbid-local dinput8.dll \
    --forbid-local dxgi.dll \
    --forbid-local xinput1_4.dll \
    --json-output "$VALIDATION_JSON"

TOOLCHAIN_COMPILER="$(distrobox enter doom-cpp -- x86_64-w64-mingw32-g++ --version | head -n 1)"

python3 - "$OUTPUT_DIR" "$RELEASE_VERSION" "$VALIDATION_JSON" "$TOOLCHAIN_COMPILER" <<'PY'
import json
import sys
from pathlib import Path

output_dir = Path(sys.argv[1])
release_version = sys.argv[2]
validation_path = Path(sys.argv[3])
toolchain_compiler = sys.argv[4]

validation = json.loads(validation_path.read_text(encoding="utf-8"))

manifest = {
    "name": "Doom Eternal Archipelago Playable Test",
    "version": release_version,
    "files": [
        "README.md",
        "RELEASE_MANIFEST.json",
        "DoomEternalArchipelagoPreAlpha.zip",
        "doometernal.apworld",
        "client/ap_client.exe",
        "client/bridge_client.py",
        "client/bootstrap_actions.py",
        "client/foundation.py",
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
        "client/player_templates/Marine.yaml",
        "tools/test_save_scenarios.py",
        "tools/generate_foundation_test_plan.py",
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

for generated_map in "$GENERATED_MAPS_DIR"/*.entities; do
    [[ "$(rg -c '^\s*entityDef ap_bootstrap_v2_' "$generated_map")" == "4" ]] || {
        echo "Normal build lacks four v2 bootstrap controls: $generated_map" >&2
        exit 1
    }
    if rg -q 'ap_bootstrap_v[13]_' "$generated_map"; then
        echo "Generated map contains stale bootstrap revision: $generated_map" >&2
        exit 1
    fi
done

PACKAGED_CLIENT_SHA256="$(sha256sum "$OUTPUT_DIR/client/ap_client.exe" | awk '{print $1}')"
FRESH_CLIENT_SHA256="$(sha256sum "$CLIENT_BUILD_DIR/ap_client.exe" | awk '{print $1}')"
[[ "$PACKAGED_CLIENT_SHA256" == "$FRESH_CLIENT_SHA256" ]] || { echo "Packaged ap_client.exe is not the fresh build" >&2; exit 1; }

(
    cd "$OUTPUT_DIR/mod"
    zip -q -r "$OUTPUT_DIR/DoomEternalArchipelagoPreAlpha.zip" .
)

(
    cd "$OUTPUT_DIR"
    zip -q -r "$PTB_ZIP_NAME" \
        README.md RELEASE_MANIFEST.json client tools doometernal.apworld \
        DoomEternalArchipelagoPreAlpha.zip
)

EXTRACTED_AUDIT_DIR="$TEMP_DIR/extracted-final"
mkdir -p "$EXTRACTED_AUDIT_DIR"
unzip -q "$OUTPUT_DIR/$PTB_ZIP_NAME" -d "$EXTRACTED_AUDIT_DIR"
MOD_AUDIT_DIR="$TEMP_DIR/extracted-mod"
mkdir -p "$MOD_AUDIT_DIR"
unzip -q "$EXTRACTED_AUDIT_DIR/DoomEternalArchipelagoPreAlpha.zip" -d "$MOD_AUDIT_DIR"
if find "$MOD_AUDIT_DIR" -path '*/generated/decls/propitem/propitem/ap*' -o \
    -path '*/generated/decls/propitem/propitem/equipment/ice_bomb.decl' -o \
    -path '*/generated/decls/propitem/propitem/weapon/rocket_launcher/base.decl' | grep -q .; then
    echo "Forbidden propitem DECL override found in final mod ZIP" >&2
    exit 1
fi
python3 "$SCRIPT_DIR/validate_windows_runtime_deps.py" \
    "$EXTRACTED_AUDIT_DIR/client" \
    --forbid-local version.dll \
    --forbid-local dinput8.dll \
    --forbid-local dxgi.dll \
    --forbid-local xinput1_4.dll
[[ "$(find "$EXTRACTED_AUDIT_DIR" -name ap_client.exe -type f | wc -l)" == "1" ]] || { echo "Final ZIP must contain exactly one ap_client.exe" >&2; exit 1; }
[[ "$(sha256sum "$EXTRACTED_AUDIT_DIR/client/ap_client.exe" | awk '{print $1}')" == "$FRESH_CLIENT_SHA256" ]] || { echo "ZIP ap_client.exe hash mismatch" >&2; exit 1; }
if unzip -Z1 "$OUTPUT_DIR/$PTB_ZIP_NAME" | rg -q '(^|/)(build|staging|__pycache__|.*\.log)$|^/|^[A-Za-z]:/'; then
    echo "Final ZIP contains a generated/runtime artifact or absolute path" >&2
    exit 1
fi
echo "Playable test build created at: $OUTPUT_DIR"
echo "Installable mod: $OUTPUT_DIR/DoomEternalArchipelagoPreAlpha.zip"
echo "Linux/Windows test bundle: $OUTPUT_DIR/$PTB_ZIP_NAME"
echo "Build log: $BUILD_LOG"
