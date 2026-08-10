#!/bin/bash
set -euo pipefail

GAME_DIR="${DOOM_GAME_DIR:-$HOME/.local/share/Steam/steamapps/common/DOOMEternal}"
MOD_ZIP="${DOOM_AP_MOD_ZIP:-}"
INSTALL_RECORD="${DOOM_AP_INSTALL_RECORD:-}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mod-zip)
            [[ $# -ge 2 ]] || { echo "--mod-zip requires a path" >&2; exit 2; }
            MOD_ZIP="$2"
            shift 2
            ;;
        --install-record)
            [[ $# -ge 2 ]] || { echo "--install-record requires a path" >&2; exit 2; }
            INSTALL_RECORD="$2"
            shift 2
            ;;
        *)
            echo "Usage: $0 [--mod-zip PATH | --install-record launcher_setup.json]" >&2
            exit 2
            ;;
    esac
done

if [[ -n "$MOD_ZIP" && -n "$INSTALL_RECORD" ]]; then
    echo "Specify either exact room mod ZIP or launcher install record, not both." >&2
    exit 2
fi
if [[ -n "$INSTALL_RECORD" ]]; then
    MOD_ZIP="$(python3 - "$INSTALL_RECORD" <<'PY'
import hashlib
import json
import sys
import zipfile
from pathlib import Path

record = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
mod_zip = Path(record["staged_mod"]).expanduser().resolve()
if not mod_zip.is_file():
    raise SystemExit(f"recorded room mod is missing: {mod_zip}")
actual = hashlib.sha256(mod_zip.read_bytes()).hexdigest()
if actual != record["staged_sha256"]:
    raise SystemExit("recorded room mod SHA-256 mismatch")
with zipfile.ZipFile(mod_zip) as package:
    manifest = json.loads(package.read("seed_manifest.json"))
if manifest.get("manifest_hash") != record["manifest_hash"]:
    raise SystemExit("install record and room package manifest diverge")
print(mod_zip)
PY
)"
fi
if [[ -z "$MOD_ZIP" ]]; then
    echo "Exact dynamic room ZIP required: use --mod-zip or --install-record." >&2
    exit 2
fi
MOD_ZIP="$(realpath -e "$MOD_ZIP")"
if [[ "$(basename "$MOD_ZIP")" == "DoomEternalArchipelagoBeta.zip" ]]; then
    echo "Obsolete universal mod ZIP is not a valid room package." >&2
    exit 1
fi

python3 - "$MOD_ZIP" <<'PY'
import json
import sys
import zipfile

with zipfile.ZipFile(sys.argv[1]) as package:
    manifest = json.loads(package.read("seed_manifest.json"))
    receipt = json.loads(package.read("seed_receipt.json"))
manifest_hash = manifest.get("manifest_hash")
if not manifest_hash or receipt.get("manifest_hash") != manifest_hash:
    raise SystemExit("room ZIP manifest/receipt identity mismatch")
PY

mapfile -t override_entities < <(
    find "$GAME_DIR/overrides" -type f -name '*.entities' 2>/dev/null | sort
)
if (( ${#override_entities[@]} > 0 )); then
    echo "Unsafe .entities overrides found; they take precedence over room mod ZIP:" >&2
    printf '  %s\n' "${override_entities[@]}" >&2
    exit 1
fi

if [[ -f "$GAME_DIR/Mods/ap_mod.zip" || -f "$GAME_DIR/Mods/DoomEternalArchipelagoBeta.zip" ]]; then
    echo "Conflicting legacy or obsolete mod found in $GAME_DIR/Mods" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/data/map_sources.json" ]]; then
    MAP_SOURCES="$SCRIPT_DIR/data/map_sources.json"
else
    REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
    MAP_SOURCES="$REPO_ROOT/data/map_sources.json"
fi

mapfile -t expected_entries < <(python3 -c '
import json, sys
for entry in json.load(open(sys.argv[1], encoding="utf-8")):
    if not entry.get("release_asset"): continue
    res_name = entry["resource_path"].split("/")[-1].replace(".resources", "")
    print(f"{res_name}/maps/{entry[\"relative_entities_path\"]}")
' "$MAP_SOURCES")

mapfile -t resource_archives < <(python3 -c '
import json, sys
for entry in json.load(open(sys.argv[1], encoding="utf-8")):
    if entry.get("release_asset"): print(entry["resource_path"])
' "$MAP_SOURCES")

archive_entries="$(unzip -Z1 "$MOD_ZIP")"
for entry in "${expected_entries[@]}"; do
    if ! grep -Fxq "$entry" <<<"$archive_entries"; then
        echo "Missing resource-prefixed archive entry: $entry" >&2
        exit 1
    fi
done

unzip -tq "$MOD_ZIP"

for relative_path in "${resource_archives[@]}"; do
    active="$GAME_DIR/$relative_path"
    backup="${active}.backup"
    if [[ ! -f "$active" || ! -f "$backup" ]]; then
        echo "Missing active resource or mod-loader backup: $relative_path" >&2
        echo "Run mod injector before launching game." >&2
        exit 1
    fi
    if cmp -s "$active" "$backup"; then
        echo "Mod is not injected; active resource is still vanilla: $relative_path" >&2
        exit 1
    fi
    if [[ "$active" -ot "$MOD_ZIP" ]]; then
        echo "Room ZIP is newer than active resource: $relative_path" >&2
        echo "Run mod injector again." >&2
        exit 1
    fi
done

echo "Runtime installation layout is valid for: $MOD_ZIP"
