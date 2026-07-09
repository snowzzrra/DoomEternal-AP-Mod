#!/usr/bin/env python3
"""Validate APWorld IDs, bridge commands, level configs, and manifests."""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

from ap_map_generator import (
    EVENT_ENTITY_PREFIX,
    RPC_ENTITY_PREFIX,
    generate_check_event,
    generate_event_relay,
    generate_pickup_notification,
    generate_rpc_command_entities,
    generate_target_relay,
)


ROOT = Path(__file__).resolve().parent
APWORLD = ROOT.parent / "Archipelago" / "worlds" / "doometernal"
MAP_SOURCES_PATH = ROOT / "data" / "map_sources.json"


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def extract_namedtuple_table(path: Path, variable: str) -> dict[str, int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == variable
            and isinstance(node.value, ast.Dict)
        ):
            return {
                ast.literal_eval(key): ast.literal_eval(value.args[0])
                for key, value in zip(node.value.keys, node.value.values)
                if ast.literal_eval(value.args[0]) is not None
            }
    raise RuntimeError(f"Could not find {variable} in {path}")


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []

    item_ids = extract_namedtuple_table(APWORLD / "items.py", "item_data_table")
    location_ids = extract_namedtuple_table(APWORLD / "locations.py", "location_data_table")
    commands = {int(key): value for key, value in read_json(ROOT / "data" / "items.json").items()}
    runtime_locations = set(
        read_json(ROOT / "data" / "runtime_locations.json").values()
    )
    map_sources = read_json(MAP_SOURCES_PATH).get("maps", {})

    manifests: dict[str, int] = {}
    for path in sorted((ROOT / "manifests").glob("*.json")):
        for declaration, location_id in read_json(path).items():
            if declaration in manifests:
                errors.append(f"Duplicate manifest declaration: {declaration}")
            if location_id in manifests.values():
                errors.append(f"Duplicate manifest location ID: {location_id}")
            manifests[declaration] = location_id

    for path in sorted((ROOT / "level_configs").glob("*.json")):
        config_data = read_json(path)
        config = dict(config_data.get("entities", {}))
        for encounter in config_data.get("secret_encounters", []):
            config[encounter["ap_check"]] = encounter["location_id"]
        manifest_path = ROOT / "manifests" / path.name
        if not manifest_path.exists():
            errors.append(f"Missing manifest for {path.name}")
            continue
        manifest = read_json(manifest_path)
        if config != manifest:
            errors.append(f"Config/manifest mismatch: {path.name}")

    enabled_map_sources = {
        map_key: source
        for map_key, source in map_sources.items()
        if source.get("enabled", True)
    }
    expected_level_configs = {
        Path(source["level_config"]).name for source in enabled_map_sources.values()
    }
    if expected_level_configs != {path.name for path in (ROOT / "level_configs").glob("*.json")}:
        errors.append("Enabled map sources are not aligned with level_configs/*.json")

    for map_key, source in enabled_map_sources.items():
        for required_key in (
            "source_file",
            "source_sha256",
            "level_config",
            "manifest",
            "resource_path",
            "relative_entities_path",
            "supported_game_revision",
        ):
            if not source.get(required_key):
                errors.append(f"Map source {map_key} is missing {required_key}")
        source_path = ROOT / "vanillamaps" / source["source_file"]
        if not source_path.exists():
            errors.append(f"Missing vanilla source for {map_key}: {source_path}")

    missing_commands = sorted(set(item_ids.values()) - set(commands))
    extra_commands = sorted(set(commands) - set(item_ids.values()))
    declared_runtime_locations = runtime_locations & set(location_ids.values())
    missing_locations = sorted(set(manifests.values()) - set(location_ids.values()))
    unmanifested_locations = sorted(
        set(location_ids.values()) - set(manifests.values()) - runtime_locations
    )

    if missing_commands:
        errors.append(f"AP item IDs without commands: {missing_commands}")
    if extra_commands:
        warnings.append(f"Commands without AP items: {extra_commands}")
    if missing_locations:
        errors.append(f"Manifest IDs absent from APWorld: {missing_locations}")
    if unmanifested_locations:
        errors.append(f"APWorld location IDs absent from manifests: {unmanifested_locations}")
    if declared_runtime_locations != runtime_locations:
        errors.append(
            "Runtime location IDs absent from APWorld: "
            f"{sorted(runtime_locations - declared_runtime_locations)}"
        )
    overlap = runtime_locations & set(manifests.values())
    if overlap:
        errors.append(f"Runtime location IDs also present in map manifests: {sorted(overlap)}")

    generated_commands = generate_rpc_command_entities(
        {
            "1": {
                "type": "progressive_perk",
                "perks": ["perk/player/argent/health_capacity_0"],
            },
            "2": {
                "type": "currency",
                "currency": "CURRENCY_PRAETOR_UPGRADE",
                "count": 1,
            },
            "3": {
                "type": "perk",
                "perk": "perk/player/suit/fundamentals/weapon_change_speed",
            },
            "4": ["give first", "give second"],
        }
    )
    if (
        f"entityDef {RPC_ENTITY_PREFIX}_1_0" not in generated_commands
        or "givePlayerPerk perk/player/argent/health_capacity_0;"
        not in generated_commands
        or "activatePlayerPerk perk/player/argent/health_capacity_0"
        not in generated_commands
    ):
        errors.append("Progressive perks are not generated as one ordered command entity")
    if "SGT_NO_SAVE" not in generated_commands or "SGS_NONE" not in generated_commands:
        errors.append("Currency command entities must not persist activation state")
    if (
        f"entityDef {RPC_ENTITY_PREFIX}_3" not in generated_commands
        or "givePlayerPerk perk/player/suit/fundamentals/weapon_change_speed;"
        not in generated_commands
        or "activatePlayerPerk perk/player/suit/fundamentals/weapon_change_speed"
        not in generated_commands
    ):
        errors.append("Suit perks are not generated as one ordered command entity")
    if (
        f"entityDef {RPC_ENTITY_PREFIX}_4" not in generated_commands
        or 'inherit = "target/relay";' not in generated_commands
        or 'class = "idTarget_Count";' not in generated_commands
        or "count = 1;" not in generated_commands
        or f'item[0] = "{RPC_ENTITY_PREFIX}_4_0";' not in generated_commands
        or f'item[1] = "{RPC_ENTITY_PREFIX}_4_1";' not in generated_commands
        or 'commandText = "give first";' not in generated_commands
        or 'commandText = "give second";' not in generated_commands
        or 'class = "idTarget_Relay";' in generated_commands
    ):
        errors.append("Multi-command items do not use the validated target/count relay")

    relay = generate_target_relay("AP_CHECK_VALIDATION", 7770999, "")
    secret_relay = generate_event_relay(
        "AP_CHECK_SECRET_VALIDATION", 7770998, "", include_notification=False
    )
    event = generate_check_event(7770999)
    notification = generate_pickup_notification("AP_CHECK_VALIDATION")
    if (
        'item[0] = "ap_notify_AP_CHECK_VALIDATION";' not in relay
        or f'item[1] = "{EVENT_ENTITY_PREFIX}7770999";' not in relay
        or 'class = "idTarget_Notification";' not in notification
        or 'notificationType = "HUD_NOTIFY_SECRET_FOUND";' not in notification
        or 'header = "#str_swf_notification_secret_found";' not in notification
    ):
        errors.append("AP checks are not connected to native pickup notifications")
    if (
        f"entityDef {EVENT_ENTITY_PREFIX}7770999" not in event
        or "echo AP_CHECK_EVENT_7770999; condump ap_event_7770999.txt"
        not in event
    ):
        errors.append("AP checks do not emit the expected native event file")
    if (
        f'item[0] = "{EVENT_ENTITY_PREFIX}7770998";' not in secret_relay
        or 'class = "idTarget_Count";' not in secret_relay
        or 'class = "idTarget_Relay";' in secret_relay
        or "ap_notify_AP_CHECK_SECRET_VALIDATION" in secret_relay
    ):
        errors.append("Secret encounter checks do not use the validated event-only relay")

    for item_id, command_value in commands.items():
        if isinstance(command_value, dict):
            command_type = command_value.get("type")
            if command_type == "no_op":
                continue
            if command_type == "progressive_perk":
                perks = command_value.get("perks")
                if (
                    not isinstance(perks, list)
                    or not perks
                    or len(set(perks)) != len(perks)
                    or not all(
                        isinstance(perk, str)
                        and perk.startswith("perk/player/")
                        for perk in perks
                    )
                ):
                    errors.append(
                        f"Progressive perk command {item_id} must define unique player perks"
                    )
                if item_id in {7770017, 7770088, 7770092} and (
                    not isinstance(perks, list)
                    or len(perks) != 4
                    or not all(
                        isinstance(perk, str)
                        and perk.startswith("perk/player/argent/")
                        for perk in perks
                    )
                ):
                    errors.append(
                        f"Capacity command {item_id} must define four Argent perks"
                    )
                continue
            if command_type == "perk":
                perk = command_value.get("perk")
                if not isinstance(perk, str) or not perk.startswith("perk/player/"):
                    errors.append(f"Perk command {item_id} has invalid path: {perk}")
                continue
            if command_type != "currency":
                errors.append(f"Entity command {item_id} has unsupported type: {command_value.get('type')}")
                continue
            currency = command_value.get("currency")
            count = command_value.get("count")
            if not isinstance(currency, str) or not currency.startswith("CURRENCY_"):
                errors.append(f"Currency command {item_id} has invalid currency: {currency}")
            if not isinstance(count, int) or count <= 0:
                errors.append(f"Currency command {item_id} must have a positive integer count")
            continue

        command_list = command_value if isinstance(command_value, list) else [command_value]
        if not command_list or not all(isinstance(command, str) and command.strip() for command in command_list):
            errors.append(f"Command {item_id} must be a string or non-empty list of strings")
            continue
        for command in command_list:
            if ";" in command:
                errors.append(f"Command {item_id} contains unsupported semicolon chaining")
            if command in {
                "give weapon/player/plasma_rifle_secondary_aoe",
                "give weapon/player/plasma_rifle_secondary_microwave",
                "give weapon/player/rocket_launcher_lock_mod",
            }:
                errors.append(
                    f"Command {item_id} uses a direct weapon-mod grant that corrupts weapon inventory"
                )
            perk_match = re.search(r"givePlayerPerk\s+(perk/player/\S+)", command)
            if perk_match and any(token in perk_match.group(1) for token in ("gauss_rifle", "energy_shield", "remote_detonate")):
                errors.append(f"Command {item_id} uses a known non-canonical perk path: {perk_match.group(1)}")

    for message in warnings:
        print(f"WARNING: {message}")
    for message in errors:
        print(f"ERROR: {message}")

    print(
        f"Validated {len(item_ids)} AP items, {len(commands)} commands, "
        f"{len(location_ids)} locations, {len(manifests)} map checks, "
        f"{len(enabled_map_sources)} enabled map sources, "
        f"and {len(runtime_locations)} runtime checks."
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
