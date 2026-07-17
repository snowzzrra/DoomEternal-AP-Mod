#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE="$(cd "$SCRIPT_DIR/.." && pwd)"
TOOLS_DIR="$WORKSPACE/Tools"
OUTPUT_DIR="${1:-$SCRIPT_DIR/build/release}"
OUTPUT_DIR="$(realpath -m "$OUTPUT_DIR")"
RELEASE_DIR="$(realpath -m "$SCRIPT_DIR/build/release")"
if [[ "$OUTPUT_DIR" != "$RELEASE_DIR" ]]; then
    echo "Playable builds are restricted to $RELEASE_DIR" >&2
    exit 1
fi
TEMP_DIR="$OUTPUT_DIR/.staging"
MAP_SOURCES_FILE="${AP_MAP_SOURCES_FILE:-$SCRIPT_DIR/data/map_sources.json}"
VANILLA_MAPS_DIR="${VANILLA_MAPS_DIR:-$SCRIPT_DIR/vanillamaps}"
PTB_VERSION="v0.3.0-pre-alpha-dev"
RELEASE_VERSION="v${PTB_VERSION#v}"
PTB_ZIP_NAME="DoomEternalArchipelagoPlayableTest-${RELEASE_VERSION}.zip"
AUTOMAP_PROTOTYPE_ONLY="${AP_AUTOMAP_PROTOTYPE_ONLY:-0}"
GENERATED_MAPS_DIR="$OUTPUT_DIR/build/generated-maps"
GENERATED_MANIFESTS_DIR="$TEMP_DIR/manifests"
BUILD_LOG="$OUTPUT_DIR/build/build.log"
CLIENT_BUILD_DIR="$OUTPUT_DIR/build/client"
PACKAGEMAPSPEC="${DOOM_PACKAGEMAPSPEC:-/run/media/system/Eris/SteamLibrary/steamapps/common/DOOMEternal/base/packagemapspec.json}"

trap 'rm -rf "$TEMP_DIR"' EXIT

if [[ "$AUTOMAP_PROTOTYPE_ONLY" != "1" ]]; then
    python3 "$SCRIPT_DIR/tools/audit_scripted_location.py" \
        --contracts "$SCRIPT_DIR/data/scripted_location_contracts.json"
    python3 "$SCRIPT_DIR/validate_data.py"
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
    "$GENERATED_MAPS_DIR" "$TEMP_DIR"
"$SCRIPT_DIR/build_client.sh" "$CLIENT_BUILD_DIR"
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

python3 "$SCRIPT_DIR/automap_native_decl_builder.py" \
    --mod-root "$OUTPUT_DIR/mod" \
    --audit-output "$TEMP_DIR/automap-native-toy-override.json"
python3 "$SCRIPT_DIR/rune_decl_builder.py" \
    --mod-root "$OUTPUT_DIR/mod" \
    --audit-output "$TEMP_DIR/rune-menu-override.json"
python3 "$SCRIPT_DIR/mastery_decl_builder.py" \
    --mod-root "$OUTPUT_DIR/mod" \
    --audit-output "$TEMP_DIR/base-mastery-overrides.json"
python3 "$SCRIPT_DIR/mission_challenge_decl_builder.py" \
    --mod-root "$OUTPUT_DIR/mod" \
    --audit-output "$TEMP_DIR/cultist-mission-challenge-overrides.json"
python3 - "$TEMP_DIR/cultist-mission-challenge-overrides.json" <<'PY'
import json
import sys

audit = json.load(open(sys.argv[1], encoding="utf-8"))
assert "battery_unchanged" not in audit
assert audit["aggregate_reward_suppression"] == {
    "strategy": "child_currencyToGive_num_zero",
    "field": "currencyToGive.num",
    "value": 0,
    "suppressed_native_rewards": [
        "CURRENCY_PRAETOR_UPGRADE",
        "CURRENCY_SENTINEL_BATTERY",
    ],
    "runtime_evidence": "v0.3.0c.1",
}
PY

python3 "$SCRIPT_DIR/tools/audit_scripted_location.py" \
    --contracts "$SCRIPT_DIR/data/scripted_location_contracts.json" \
    --verify-generated-map "$OUTPUT_DIR/build/generated-maps/hub.entities" \
    --location 7770074
python3 "$SCRIPT_DIR/tools/audit_scripted_location.py" \
    --contracts "$SCRIPT_DIR/data/scripted_location_contracts.json" \
    --verify-generated-map "$OUTPUT_DIR/build/generated-maps/e1m3_cult.entities" \
    --location 7770056

ICE_DECL_RELATIVE="generated/decls/logicentity/maps/game/hub/hub/info_logic_hub_from_e1m2.decl"
python3 "$SCRIPT_DIR/logic_decl_patcher.py" \
    --contracts "$SCRIPT_DIR/data/scripted_location_contracts.json" \
    --location 7770074 \
    --output "$OUTPUT_DIR/mod/hub_patch2/$ICE_DECL_RELATIVE" \
    --snapshot "$TEMP_DIR/ice_logic_decl_patch.json"
python3 - "$SCRIPT_DIR/data/snapshots/ice_logic_decl_patch.json" "$TEMP_DIR/ice_logic_decl_patch.json" <<'PY'
import json
import sys
from pathlib import Path

expected = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
actual = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
actual.pop("changed_lines", None)
if actual != expected:
    raise SystemExit(f"Ice logic DECL structural snapshot drift: {actual!r}")
PY

cp "$SCRIPT_DIR/packaging/EternalMod.json" "$OUTPUT_DIR/mod/EternalMod.json"
cp "$SCRIPT_DIR/README.md" "$OUTPUT_DIR/README.md"
cp "$CLIENT_BUILD_DIR/ap_client.exe" "$CLIENT_BUILD_DIR/save_death_probe.exe" \
    "$SCRIPT_DIR/bridge_client.py" "$SCRIPT_DIR/bootstrap_actions.py" \
    "$SCRIPT_DIR/challenge_registry.py" \
    "$SCRIPT_DIR/foundation.py" \
    "$SCRIPT_DIR/run_bridge.sh" "$SCRIPT_DIR/save_decrypt.py" \
    "$SCRIPT_DIR/start_injector_windows.bat" \
    "$SCRIPT_DIR/ap_config.example.json" \
    "$SCRIPT_DIR/validate_runtime_install.sh" \
    "$OUTPUT_DIR/client/"
mkdir -p "$OUTPUT_DIR/client/data" "$OUTPUT_DIR/client/manifests"
cp "$SCRIPT_DIR/data/items.json" \
    "$SCRIPT_DIR/data/challenge_location_registry.json" \
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

python3 - "$OUTPUT_DIR/client/bridge_client.py" "$OUTPUT_DIR/client/bridge_identity.json" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

bridge = Path(sys.argv[1])
identity = {
    "protocol": 3,
    "sha256": hashlib.sha256(bridge.read_bytes()).hexdigest(),
}
identity["revision"] = f"mission-unified-{identity['sha256'][:12]}"
Path(sys.argv[2]).write_text(json.dumps(identity, indent=2) + "\n", encoding="utf-8")
PY

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
import hashlib
import json
import sys
from pathlib import Path

output_dir = Path(sys.argv[1])
release_version = sys.argv[2]
validation_path = Path(sys.argv[3])
toolchain_compiler = sys.argv[4]

validation = json.loads(validation_path.read_text(encoding="utf-8"))
bridge_path = output_dir / "client" / "bridge_client.py"
bridge_sha256 = hashlib.sha256(bridge_path.read_bytes()).hexdigest()

manifest = {
    "name": "DOOM Eternal Archipelago",
    "version": release_version,
    "files": [
        "README.md",
        "RELEASE_MANIFEST.json",
        "DoomEternalArchipelagoPreAlpha.zip",
        "doometernal.apworld",
        "client/ap_client.exe",
        "client/bridge_client.py",
        "client/bridge_identity.json",
        "client/bootstrap_actions.py",
        "client/challenge_registry.py",
        "client/foundation.py",
        "client/save_death_probe.exe",
        "client/save_decrypt.py",
        "client/run_bridge.sh",
        "client/start_injector_windows.bat",
        "client/validate_runtime_install.sh",
        "client/ap_config.example.json",
        "client/data/items.json",
        "client/data/challenge_location_registry.json",
        "client/data/runtime_locations.json",
        "client/manifests/e1m1_intro.json",
        "client/manifests/e1m2_war.json",
        "client/manifests/e1m3_cult.json",
        "client/manifests/hub.json",
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
    if rg -q '^\s*entityDef ap_bootstrap_v[0-9]_' "$generated_map"; then
        echo "Rejected stat-write bootstrap entered the normal build: $generated_map" >&2
        exit 1
    fi
done
if rg -q 'pickups_pickup_weapon_heavy_cannon_1' "$GENERATED_MAPS_DIR/e1m2_war.entities"; then
    echo "Exultia Heavy Cannon fallback reappeared" >&2
    exit 1
fi
if rg -q 'give armor -200|AP_RUNTIME_CHECK_|3_900_000_000|3_800_000_000' \
    "$OUTPUT_DIR/mod" "$GENERATED_MAPS_DIR" "$OUTPUT_DIR/client/data/items.json"; then
    echo "Rejected Armor Drain or watcher architecture entered build" >&2
    exit 1
fi
if rg -q 'Ignoring unexpected goal transition event' "$OUTPUT_DIR/client/bridge_client.py"; then
    echo "Old goal-only transition handler entered build" >&2
    exit 1
fi
mapfile -t MASTERY_OVERRIDE_FILES < <(find "$OUTPUT_DIR/mod" -type f \( \
    -path '*/generated/decls/unlockable/weapon_mastery/*' -o \
    -path '*/generated/decls/perks/perk/player/weapons/*' \
\) | sort)
[[ "${#MASTERY_OVERRIDE_FILES[@]}" == "26" ]] || { echo "Base Mastery override set is incomplete" >&2; exit 1; }
if rg -q 'perkToGive|addStats|STAT_CURRENT_MASTERIES_AQUIRED|MASTERY_EARNED' "${MASTERY_OVERRIDE_FILES[@]}"; then
    echo "Mastery override retains natural reward, completion stat, or global stat" >&2
    exit 1
fi
if ! rg -q 'upgrade/weapons/shotguns/shotgun/pop_rocket_more_bombs' \
    "$OUTPUT_DIR/mod/gameresources/generated/decls/perks/perk/player/weapons/shotgun/pop_rocket_more_bombs.decl"; then
    echo "Sticky AP gameplay upgrade missing" >&2
    exit 1
fi
mapfile -t CHALLENGE_OVERRIDE_FILES < <(find "$OUTPUT_DIR/mod" -type f \
    -path '*/generated/decls/unlockable/mission_challenge/e1m3/challenge_*.decl' | sort)
[[ "${#CHALLENGE_OVERRIDE_FILES[@]}" == "3" ]] || { echo "Cultist Base Mission Challenge override set is incomplete" >&2; exit 1; }
if find "$OUTPUT_DIR/mod" -type f -path '*/generated/decls/unlockable/mission_challenge/*' \
    ! -path '*/generated/decls/unlockable/mission_challenge/e1m3/challenge_1.decl' \
    ! -path '*/generated/decls/unlockable/mission_challenge/e1m3/challenge_2.decl' \
    ! -path '*/generated/decls/unlockable/mission_challenge/e1m3/challenge_3.decl' \
    -print -quit | grep -q .; then
    echo "Unscoped Mission Challenge override entered build" >&2
    exit 1
fi
if rg -q 'CURRENCY_PRAETOR_UPGRADE|CURRENCY_SENTINEL_BATTERY' "${CHALLENGE_OVERRIDE_FILES[@]}"; then
    echo "Cultist Mission Challenge child override contains an unscoped currency name" >&2
    exit 1
fi
for challenge_override in "${CHALLENGE_OVERRIDE_FILES[@]}"; do
    if [[ "$(rg -c 'currencyToGive' "$challenge_override")" != "1" ]] || \
        [[ "$(rg -c 'num = 0;' "$challenge_override")" != "1" ]]; then
        echo "Cultist Mission Challenge reward suppression is missing: $challenge_override" >&2
        exit 1
    fi
done
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
    zip -q -r "$OUTPUT_DIR/DoomEternalArchipelagoPreAlpha.zip" .
)

(
    cd "$OUTPUT_DIR"
    zip -q -r "$PTB_ZIP_NAME" \
        README.md RELEASE_MANIFEST.json client doometernal.apworld \
        DoomEternalArchipelagoPreAlpha.zip
)

if [[ "$AUTOMAP_PROTOTYPE_ONLY" == "1" ]]; then
    rm -rf "$OUTPUT_DIR/build" "$OUTPUT_DIR/client" "$OUTPUT_DIR/mod" \
        "$OUTPUT_DIR/apworld" "$OUTPUT_DIR/doometernal.apworld" \
        "$OUTPUT_DIR/DoomEternalArchipelagoPreAlpha.zip" \
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
unzip -q "$EXTRACTED_AUDIT_DIR/DoomEternalArchipelagoPreAlpha.zip" -d "$MOD_AUDIT_DIR"
if find "$MOD_AUDIT_DIR" -path '*/generated/decls/propitem/propitem/ap*' -o \
    -path '*/generated/decls/propitem/propitem/equipment/ice_bomb.decl' -o \
    -path '*/generated/decls/propitem/propitem/weapon/rocket_launcher/base.decl' -o \
    -path '*/generated/decls/perks/perk/ap/*' -o \
    -path '*/generated/decls/logicentity/ap/*' | grep -q .; then
    echo "Forbidden propitem DECL override found in final mod ZIP" >&2
    exit 1
fi
mapfile -t AUDIT_CHALLENGE_OVERRIDE_FILES < <(find "$MOD_AUDIT_DIR" -type f \
    -path '*/generated/decls/unlockable/mission_challenge/e1m3/challenge_*.decl' | sort)
[[ "${#AUDIT_CHALLENGE_OVERRIDE_FILES[@]}" == "3" ]] || { echo "Final ZIP Cultist Mission Challenge override set drifted" >&2; exit 1; }
if find "$MOD_AUDIT_DIR" -type f -path '*/generated/decls/unlockable/mission_challenge/*' \
    ! -path '*/generated/decls/unlockable/mission_challenge/e1m3/challenge_1.decl' \
    ! -path '*/generated/decls/unlockable/mission_challenge/e1m3/challenge_2.decl' \
    ! -path '*/generated/decls/unlockable/mission_challenge/e1m3/challenge_3.decl' \
    -print -quit | grep -q .; then
    echo "Final ZIP contains an unscoped Mission Challenge override" >&2
    exit 1
fi
if rg -q 'CURRENCY_PRAETOR_UPGRADE|CURRENCY_SENTINEL_BATTERY' "${AUDIT_CHALLENGE_OVERRIDE_FILES[@]}"; then
    echo "Final ZIP Cultist challenge child override contains an unscoped currency name" >&2
    exit 1
fi
for challenge_override in "${AUDIT_CHALLENGE_OVERRIDE_FILES[@]}"; do
    if [[ "$(rg -c 'currencyToGive' "$challenge_override")" != "1" ]] || \
        [[ "$(rg -c 'num = 0;' "$challenge_override")" != "1" ]]; then
        echo "Final ZIP challenge reward suppression drifted: $challenge_override" >&2
        exit 1
    fi
done
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
\) | sort)
[[ "${#AUDIT_MASTERY_OVERRIDE_FILES[@]}" == "26" ]] || { echo "Final ZIP base Mastery override set drifted" >&2; exit 1; }
if rg -q 'perkToGive|addStats|STAT_CURRENT_MASTERIES_AQUIRED|MASTERY_EARNED' "${AUDIT_MASTERY_OVERRIDE_FILES[@]}"; then
    echo "Final ZIP does not isolate Mastery item and location paths" >&2
    exit 1
fi
if rg -q 'give armor -200|AP_RUNTIME_CHECK_|3_900_000_000|3_800_000_000' \
    "$MOD_AUDIT_DIR" "$EXTRACTED_AUDIT_DIR/client/data/items.json"; then
    echo "Final ZIP contains Armor Drain or rejected watcher architecture" >&2
    exit 1
fi
if rg -q 'Ignoring unexpected goal transition event' \
    "$EXTRACTED_AUDIT_DIR/client/bridge_client.py"; then
    echo "Final ZIP contains old goal-only transition handler" >&2
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
python3 "$SCRIPT_DIR/tools/audit_packaged_transition_bridge.py" \
    "$EXTRACTED_AUDIT_DIR/client" \
    "$SCRIPT_DIR/data/challenge_location_registry.json" \
    "$EXTRACTED_AUDIT_DIR/RELEASE_MANIFEST.json" \
    "$EXTRACTED_AUDIT_DIR/doometernal.apworld"
mapfile -t PACKAGE_FILES < <(unzip -Z1 "$OUTPUT_DIR/$PTB_ZIP_NAME" | rg -v '/$' | sort)
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
if printf '%s\n' "${PACKAGE_FILES[@]}" | rg -i -q '(^|/)(playtests?|tests?|build|staging|__pycache__|\.git|todo|session|decisions|pitfalls|architecture)(/|$)|(^|/).*\.log$|(^|/).*\.pid$|(^|/)ap_config\.json$|(^|/)\.local\.env$|(^|/).*-(dev|debug)(\.|/|$)|AP_ICE_DIAG|(^|/).*(condump|seed|cache|output|diagnostic)'; then
    echo "Final ZIP contains a forbidden internal or development artifact" >&2
    exit 1
fi
if find "$OUTPUT_DIR/build" -type f -name '*.txt' -print -quit | grep -q .; then
    echo "Runtime-test .txt files are forbidden in build/release/build" >&2
    exit 1
fi
if unzip -p "$OUTPUT_DIR/$PTB_ZIP_NAME" README.md RELEASE_MANIFEST.json | rg -n -i '(/run/media/system/Eris/|/var/home/guilherme/|[A-Z]:\\\\Users\\\\guilherme\\|ap_ice_diag)' >/dev/null; then
    echo "Final ZIP text contains a personal path or diagnostic marker" >&2
    exit 1
fi
echo "Playable development build created at: $OUTPUT_DIR"
echo "Installable mod: $OUTPUT_DIR/DoomEternalArchipelagoPreAlpha.zip"
echo "Development bundle: $OUTPUT_DIR/$PTB_ZIP_NAME"
echo "Build log: $BUILD_LOG"
