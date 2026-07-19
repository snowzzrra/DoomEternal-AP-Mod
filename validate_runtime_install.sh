#!/bin/bash
set -euo pipefail

GAME_DIR="${DOOM_GAME_DIR:-$HOME/.local/share/Steam/steamapps/common/DOOMEternal}"
MOD_ZIP="$GAME_DIR/Mods/DoomEternalArchipelagoAlpha.zip"

if [[ ! -f "$MOD_ZIP" ]]; then
    echo "Missing installed mod: $MOD_ZIP" >&2
    exit 1
fi

mapfile -t override_entities < <(
    find "$GAME_DIR/overrides" -type f -name '*.entities' 2>/dev/null | sort
)
if (( ${#override_entities[@]} > 0 )); then
    echo "Unsafe .entities overrides found; they take precedence over the mod ZIP:" >&2
    printf '  %s\n' "${override_entities[@]}" >&2
    exit 1
fi

if [[ -f "$GAME_DIR/Mods/ap_mod.zip" ]]; then
    echo "Conflicting legacy mod found: $GAME_DIR/Mods/ap_mod.zip" >&2
    exit 1
fi

expected_entries=(
    "e1m1_intro_patch3/maps/game/sp/e1m1_intro/e1m1_intro.entities"
    "e1m2_battle_patch3/maps/game/sp/e1m2_battle/e1m2_battle.entities"
    "hub_patch2/maps/game/hub/hub.entities"
    "e1m3_cult_patch3/maps/game/sp/e1m3_cult/e1m3_cult.entities"
)

resource_archives=(
    "base/game/sp/e1m1_intro/e1m1_intro_patch3.resources"
    "base/game/sp/e1m2_battle/e1m2_battle_patch3.resources"
    "base/game/hub/hub_patch2.resources"
    "base/game/sp/e1m3_cult/e1m3_cult_patch3.resources"
)

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
        echo "Run EternalModInjectorShell.sh before launching the game." >&2
        exit 1
    fi
    if cmp -s "$active" "$backup"; then
        echo "Mod is not injected; active resource is still vanilla: $relative_path" >&2
        echo "Steam may have restored the game files. Run EternalModInjectorShell.sh again." >&2
        exit 1
    fi
    if [[ "$active" -ot "$MOD_ZIP" ]]; then
        echo "Installed ZIP is newer than active resource: $relative_path" >&2
        echo "The ZIP was replaced after injection. Run EternalModInjectorShell.sh again." >&2
        exit 1
    fi
done

echo "Runtime installation layout is valid."
