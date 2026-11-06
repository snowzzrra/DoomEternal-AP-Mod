"""Project player text into standalone APWorld templates and private Sentinel constants."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from string import Formatter
from textwrap import wrap

from doom_eap.content.option_text import schema_option_texts
from doom_eap.strings import CATALOG_PATH, StringCatalog, StringEntry, read_catalog

MOD_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_APWORLD_ROOT = (
    Path(os.environ.get("ARCHIPELAGO_SOURCE", MOD_ROOT.parent / "Archipelago")) / "worlds/doometernal"
)
DEFAULT_SENTINEL_ROOT = MOD_ROOT.parent / "Sentinel-Core"
NATIVE_FIELDS = {
    "game.controls.ammo_refill": (160, {}),
    "game.controls.toggle_special_weapon": (160, {}),
    "game.controls.group": (160, {}),
    "game.controls.clear": (160, {}),
    "game.controls.unsupported_key": (160, {}),
    "game.controls.key_conflict": (160, {}),
    "game.controls.game_key_conflict": (160, {}),
    "game.controls.save_failed": (160, {}),
    "game.controls.restore_failed": (160, {}),
    "game.controls.input_not_ready": (160, {}),
    "game.controls.invalid_binding": (160, {}),
    "game.controls.binding_error": (160, {"action": ("str", "%s", 0), "error": ("str", "%s", 0)}),
    "game.hud.resource_unknown": (32, {}),
    "game.hud.function_key_toggle": (32, {"number": ("int", "%d", 2)}),
    "game.hud.key_toggle": (32, {"key": ("str", "%s", 15)}),
    "game.hud.charges": (32, {"count": ("int", "%u", 10)}),
    "game.hud.meter": (32, {"percent": ("str", "%.0f", 24)}),
    "game.hud.crucible_selected": (96, {}),
    "game.hud.hammer_selected": (96, {}),
    "game.hud.kills_progress": (80, {"count": ("int", "%llu", 20), "target": ("int", "%llu", 20)}),
    "game.hud.items_found": (64, {"count": ("int", "%u", 10), "total": ("int", "%u", 10)}),
    "game.menu.reward_title": (96, {}),
    "game.menu.reward_category": (96, {}),
    "game.menu.challenge_completed": (None, {"challenge": ("str", "%s", 127)}),
    "game.menu.items_found": (96, {}),
    "game.menu.return_to_fortress": (96, {}),
    "game.menu.resume_checkpoint": (96, {}),
    "game.menu.restart_stage": (96, {}),
    "game.menu.choose_mission": (96, {}),
    "game.menu.mastery_reward": (
        None,
        {"description": ("str", "%s", 16399), "newline": ("str", "%s", 1), "reward": ("str", "%s", 511)},
    ),
    "game.menu.mastery_unknown": (512, {}),
    "game.menu.recovery_pending": (None, {}),
    "game.menu.recovery_finished": (None, {}),
    "game.menu.recovery_no_active": (None, {}),
    "game.menu.recovery_unsupported": (None, {}),
    "game.menu.recovery_changed": (None, {}),
    "game.menu.recovery_unknown": (None, {}),
}
NATIVE_STRING_TEMPLATES = {"game.menu.challenge_completed", "game.menu.mastery_reward"}
NATIVE_CATALOG_KEYS = {
    "game.controls.ammo_refill": "common.ammo_refill",
    "game.controls.clear": "common.clear",
    "game.menu.reward_category": "common.archipelago",
}
NATIVE_CODE = {
    "game.controls.binding_error": "{action} - {error}",
    "game.menu.recovery_pending": "Encounter recovery in progress",
    "game.menu.recovery_finished": "Encounter finished",
    "game.menu.recovery_no_active": "No active encounter",
    "game.menu.recovery_unsupported": "Unsupported encounter",
    "game.menu.recovery_changed": "Encounter changed; request cancelled",
    "game.menu.recovery_unknown": "Recovery outcome unknown",
}


APWORLD_CODE = {
    "apworld.spoiler.access_items": "  Access items: {items}",
    "apworld.spoiler.automatic_adjustments": "Automatic adjustments",
    "apworld.spoiler.combat": "  Combat {stage}: cost {original} -> {actual}; {waves} waves, peak cost {peak}",
    "apworld.spoiler.composition_bands": "Composition bands",
    "apworld.spoiler.early_delivery": "{item} (early delivery, {count})",
    "apworld.spoiler.enemy_preset": "  Enemy preset: {preset}",
    "apworld.spoiler.enemy_seed": "  Enemy generation seed: {seed}",
    "apworld.spoiler.goal": "  Goal: {goal}",
    "apworld.spoiler.item_location": "{item} at {location}",
    "apworld.spoiler.mission_order": "  Mission order: {order}",
    "apworld.spoiler.mission_selection": "  Mission selection: {requested} requested, {included} included; forced goal: {forced_goal}",
    "apworld.spoiler.none": "None",
    "apworld.spoiler.off": "Off",
    "apworld.spoiler.optional_resources": "Optional resources",
    "apworld.spoiler.planning_entries": "  {label}: {entries}",
    "apworld.spoiler.pool_counts": "Pool counts",
    "apworld.spoiler.preparation": "  Preparation: {preparation}",
    "apworld.spoiler.preparation_cr": "  Preparation CR: {initial} -> {final}; effective {effective}; overshoot {overshoot}",
    "apworld.spoiler.progression_bands": "Progression bands",
    "apworld.spoiler.readiness": "  Readiness deficit: {initial} -> {final}; resource budget: {budget}",
    "apworld.spoiler.safety_adjustment": "  Safety adjustment: {details}",
    "apworld.spoiler.sequence": "  Sequence: {sequence}",
    "apworld.spoiler.sequence_unlocks": "None (missions unlock in sequence)",
    "apworld.spoiler.starting_inventory": "{item} (starting inventory)",
    "apworld.spoiler.title": "DOOM Eternal - {player}",
}
APWORLD_CATALOG_KEYS = {"apworld.notices.speed_fixed": "common.enemy_speed_fixed"}


def native_format(entry, label: str, capacity: int | None, arguments: dict) -> str:
    if entry.parameters != arguments.keys():
        raise ValueError(f"{label}: native parameters differ from the destination")
    output = ""
    for locale, template in entry.translations.items():
        if template is None:
            continue
        value = native_translation(template, f"{label}.{locale}", capacity, arguments)
        if locale == "en":
            output = value
    return output


def native_translation(template: str, label: str, capacity: int | None, arguments: dict) -> str:
    try:
        template.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{label}: native field requires UTF-8") from error
    fields = [name for _, name, _, _ in Formatter().parse(template) if name is not None]
    if fields != list(arguments):
        raise ValueError(f"{label}: native parameters must follow destination order")
    size = 0
    parts = []
    for literal, name, _, _ in Formatter().parse(template):
        size += len(literal.encode("utf-8"))
        parts.append(literal.replace("%", "%%") if arguments else literal)
        if name is not None:
            _, spec, width = arguments[name]
            size += width
            parts.append(spec)
    if capacity is not None and size >= capacity:
        raise ValueError(f"{label}: native field requires fewer than {capacity} bytes")
    return "".join(parts)


def native_string_function(name: str, entry) -> str:
    fields = [field for _, field, _, _ in Formatter().parse(entry.translations["en"]) if field is not None]
    parameters = ", ".join(f"const char* {key}" for key in fields)
    parts = ["std::string()"]
    for literal, field, _, _ in Formatter().parse(entry.translations["en"]):
        if literal:
            parts.append(json.dumps(literal))
        if field is not None:
            parts.append(field)
    return f"inline std::string {name}({parameters}) {{ return {' + '.join(parts)}; }}"


def project_sentinel(catalog: StringCatalog, sentinel_root: Path) -> dict[Path, bytes]:
    native = {label: native_entry(catalog, label) for label in NATIVE_FIELDS}
    controls = {
        label.removeprefix("game.controls."): entry
        for label, entry in native.items()
        if label.startswith("game.controls.")
    }
    action_keys = {"ammo_refill", "toggle_special_weapon"}
    error_keys = {
        "unsupported_key",
        "key_conflict",
        "game_key_conflict",
        "save_failed",
        "restore_failed",
        "input_not_ready",
        "invalid_binding",
    }
    action_width = control_width(controls, action_keys)
    error_width = control_width(controls, error_keys)
    lines = [
        "// GENERATED from DoomEternal-AP-Mod/data/strings/en.json; do not edit.",
        "// regenerate in DoomEternal-AP-Mod: python -m tools.content.compile_player_strings",
        "#pragma once",
        "#include <string>",
        "",
        "namespace sentinel::player_strings {",
    ]
    for label, entry in sorted(native.items()):
        lines.append(sentinel_entry(label, entry, action_width, error_width))
    lines.extend(["}", ""])
    return {sentinel_root / "src/player_strings_generated.h": "\n".join(lines).encode("ascii")}


def native_entry(catalog: StringCatalog, label: str) -> StringEntry:
    if label in NATIVE_CODE:
        value = NATIVE_CODE[label]
        fields = frozenset(name for _, name, _, _ in Formatter().parse(value) if name)
        return StringEntry({code: value for code in catalog.languages}, fields)
    category, key = NATIVE_CATALOG_KEYS.get(label, label).rsplit(".", 1)
    return catalog.categories[category][key]


def control_width(controls, keys) -> int:
    return max(
        (
            len(text.encode("utf-8"))
            for key, entry in controls.items()
            if key in keys
            for text in entry.translations.values()
            if text is not None
        ),
        default=0,
    )


def sentinel_entry(label, entry, action_width, error_width) -> str:
    if label not in NATIVE_FIELDS:
        raise ValueError(f"{label}: missing native destination limits")
    capacity, arguments = NATIVE_FIELDS[label]
    if label == "game.controls.binding_error":
        arguments = {"action": ("str", "%s", action_width), "error": ("str", "%s", error_width)}
    value = native_format(entry, label, capacity, arguments)
    name = label.removeprefix("game.").replace(".", "_")
    if label in NATIVE_STRING_TEMPLATES:
        return native_string_function(name, entry)
    return f"inline constexpr char {name}[] = {json.dumps(value)};"


def project_catalog(catalog: StringCatalog, apworld_root: Path = DEFAULT_APWORLD_ROOT) -> dict[Path, bytes]:
    templates = {
        (category, key): entry.translations[catalog.default_locale]
        for category, entries in sorted(catalog.categories.items())
        if category.startswith("apworld.")
        for key, entry in sorted(entries.items())
    }
    for label, value in APWORLD_CODE.items():
        templates[tuple(label.rsplit(".", 1))] = value
    for label, target in APWORLD_CATALOG_KEYS.items():
        category, key = target.rsplit(".", 1)
        templates[tuple(label.rsplit(".", 1))] = catalog.categories[category][key].translations["en"]
    templates = dict(sorted(templates.items()))
    lines = [
        "# GENERATED from data/strings/en.json; regenerate in DoomEternal-AP-Mod:",
        "# python -m tools.content.compile_player_strings",
        '"""APWorld player text; no Mod checkout or runtime catalog is required."""',
        "",
        "TEMPLATES = {",
    ]
    for (category, key), template in templates.items():
        lines.append(f"    ({json.dumps(category)}, {json.dumps(key)}): (")
        lines.extend(
            f"        {json.dumps(part, ensure_ascii=False)}"
            for part in wrap(
                template,
                width=100,
                drop_whitespace=False,
                break_long_words=False,
                break_on_hyphens=False,
            )
        )
        lines.append("    ),")
    lines.extend(
        [
            "}",
            "",
            "",
            "def text(category: str, key: str, **values: str | int) -> str:",
            "    return TEMPLATES[category, key].format(**values)",
            "",
        ]
    )
    return {apworld_root / "player_strings_generated.py": "\n".join(lines).encode("utf-8")}


def compile_player_strings(
    catalog_path: Path = CATALOG_PATH,
    *,
    check: bool = False,
    apworld_root: Path = DEFAULT_APWORLD_ROOT,
    sentinel_root: Path = DEFAULT_SENTINEL_ROOT,
) -> tuple[Path, ...]:
    catalog = read_catalog(catalog_path)
    validate_option_texts(catalog, catalog_path.parent / "options_schema.json")
    changed = []
    projections = project_catalog(catalog, apworld_root) | project_sentinel(catalog, sentinel_root)
    for path, expected in projections.items():
        current = path.read_bytes() if path.is_file() else None
        if current == expected:
            continue
        if check:
            raise ValueError(f"player strings projection missing or stale: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(expected)
        changed.append(path)
    return tuple(changed)


def validate_option_texts(catalog: StringCatalog, schema_path: Path) -> None:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    for key, expected in schema_option_texts(schema).items():
        category, name = key.rsplit(".", 1)
        entry = catalog.categories.get(category, {}).get(name)
        if entry is None or entry.translations["en"] != expected:
            raise ValueError(f"{key}: English text missing or differs from options_schema.json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=CATALOG_PATH)
    parser.add_argument("--apworld-root", type=Path, default=DEFAULT_APWORLD_ROOT)
    parser.add_argument("--sentinel-root", type=Path, default=DEFAULT_SENTINEL_ROOT)
    parser.add_argument("--check", action="store_true", help="validate without writing files")
    args = parser.parse_args(argv)
    try:
        changed = compile_player_strings(
            args.catalog, check=args.check, apworld_root=args.apworld_root, sentinel_root=args.sentinel_root
        )
    except (ValueError, OSError, UnicodeError) as error:
        print(f"player strings: {error}", file=sys.stderr)
        return 1
    print(f"player strings: valid; {len(changed)} projections written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
