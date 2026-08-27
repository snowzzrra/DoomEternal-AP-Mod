#!/usr/bin/env python3
"""Validate APWorld IDs, bridge commands, level configs, and manifests."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
import tempfile
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from doom_eap.runtime.bootstrap_actions import BOOTSTRAP_ENTITY_PREFIXES
from doom_eap.content.automap_visual_registry import build_authorial_registry, load_automap_visual_registry
from doom_eap.contracts.challenge_registry import all_location_entries, load_challenge_registry
from doom_eap.contracts.foundation import (
    compile_all_item_plans,
    compile_item_delivery_plan,
    load_foundation_contracts,
    load_primitive_registry,
    validate_primitive_registry,
)
from doom_eap.content.item_classification import load_item_classification_identity
from doom_eap.content.map_registry import load_map_registry, validation_plan
from tools.decls.devinv_builder import load_devinv_mapping
from tools.content.compile_start_inventory_catalog import compile_catalog
from tools.maps import ap_map_generator
from tools.maps.ap_map_generator import (
    EVENT_ENTITY_PREFIX,
    RPC_ENTITY_PREFIX,
    command_requires_map_side_rpc,
    extract_target_names,
    find_entity_block_bounds,
    generate_bootstrap_entities,
    generate_check_event,
    generate_event_relay,
    generate_map,
    generate_pickup_notification,
    generate_rpc_command_entities,
    generate_target_relay,
    validate_target_policies,
)
from tools.maps.automap_baseline_guard import assert_separate_automap_helper_guard
from tools.maps.hub_diff_guard import assert_hub_diff_classified
from tools.maps.map_semantic_baseline import assert_frozen_map_baselines

ROOT = Path(__file__).resolve().parent.parent.parent
APWORLD = ROOT.parent / "Archipelago" / "worlds" / "doometernal"
ITEM_CLASSIFICATION_SOURCE = "Archipelago/worlds/doometernal/items.py"
MAP_SOURCES_PATH = ROOT / "data" / "map_sources.json"
AUTOMAP_FAMILY_REGISTRY_PATH = ROOT / "data" / "automap_family_registry.json"
BATTERY_LOCATIONS = {
    "Exultia - Sentinel Battery - King Novik Return Path": 7770084,
    "Cultist Base - Sentinel Battery - First Arena Blue-Vent Ledge": 7770057,
    "Cultist Base - Sentinel Battery - Post-Arena Interior": 7770069,
    "Cultist Base - Sentinel Battery - Moving Elevator Shaft": 7770070,
    "Doom Hunter Base - Sentinel Battery - First Combat Room Vent": 7770148,
    "Doom Hunter Base - Sentinel Battery - Above the Coffin-Wall Platform": 7770151,
}
BATTERY_ITEM_COMMANDS = {
    7770016: 1,
    7770142: 2,
}


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
                for key, value in zip(node.value.keys, node.value.values, strict=True)
                if ast.literal_eval(value.args[0]) is not None
            }
    if variable == "location_data_table":
        generated = path.with_name("generated_content.py")
        generated_tree = ast.parse(generated.read_text(encoding="utf-8"), filename=str(generated))
        for node in generated_tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "LOCATION_ROWS"
                for target in node.targets
            ):
                rows = ast.literal_eval(node.value)
                return {name: code for name, code, _ in rows if code is not None}
    raise RuntimeError(f"Could not find {variable} in {path}")


def extract_item_classifications(path: Path) -> dict[int, int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    classification_bits = {
        "filler": 0,
        "progression": 1,
        "useful": 2,
        "trap": 4,
    }

    def classification_value(node: ast.AST) -> int:
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "ItemClassification"
            and node.attr in classification_bits
        ):
            return classification_bits[node.attr]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return classification_value(node.left) | classification_value(node.right)
        raise ValueError("APWorld item has unsupported classification expression")

    for node in tree.body:
        if not (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "item_data_table"
            and isinstance(node.value, ast.Dict)
        ):
            continue
        result: dict[int, int] = {}
        for value in node.value.values:
            code = ast.literal_eval(value.args[0])
            if code is not None:
                result[code] = classification_value(value.args[1])
        return result
    raise RuntimeError("Could not find item_data_table in APWorld")


def extract_frozenset_constant(path: Path, variable: str) -> set[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == variable for target in node.targets)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "frozenset"
            and len(node.value.args) == 1
        ):
            return set(ast.literal_eval(node.value.args[0]))
    raise RuntimeError(f"Could not find {variable} in {path}")


def extract_string_set_constant(path: Path, variable: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == variable for target in node.targets)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "frozenset"
        ):
            return set(ast.literal_eval(node.value.args[0]))
    raise RuntimeError(f"Could not find {variable} in {path}")


def validate_devinv_mapping(item_ids: dict[str, int], commands: dict[int, object]) -> list[str]:
    errors: list[str] = []
    try:
        mapping = load_devinv_mapping()
        legal_names = extract_string_set_constant(APWORLD / "items.py", "DEVINV_START_INVENTORY_ITEM_NAMES")
    except (OSError, ValueError, SyntaxError, TypeError, RuntimeError, AttributeError) as exc:
        return [f"DevInv start mapping invalid: {exc}"]
    mapped_names = {entry["name"] for entry in mapping.values()}
    if mapped_names != legal_names:
        errors.append(
            "DevInv start mapping coverage drift: "
            f"missing={sorted(legal_names - mapped_names)}, extra={sorted(mapped_names - legal_names)}"
        )
    for item_id, entry in mapping.items():
        if item_id not in commands:
            errors.append(f"DevInv mapping ID {item_id} missing from data/items.json")
        if item_ids.get(entry["name"]) != item_id:
            errors.append(
                f"DevInv mapping identity drift for {entry['name']}: "
                f"mapping={item_id}, APWorld={item_ids.get(entry['name'])}"
            )
    return errors


def validate_start_inventory_catalog() -> list[str]:
    try:
        expected = compile_catalog()
        actual = read_json(ROOT / "data" / "start_inventory_catalog.json")
    except (OSError, ValueError, SyntaxError, TypeError, RuntimeError, AttributeError) as exc:
        return [f"Starting Inventory catalog invalid: {exc}"]
    if actual != expected:
        return ["Starting Inventory catalog diverges from canonical DevInv legality"]
    return []


def validate_support_rune_foundation(
    item_ids: dict[str, int], commands: dict[int, object]
) -> list[str]:
    errors: list[str] = []
    expected = {
        "Break Blast": 7770145,
        "Desperate Punch": 7770146,
        "Take Back": 7770147,
    }
    expected_paths = {
        7770145: "perk/player/runes/dlc/weakpoint_concussive_blast",
        7770146: "perk/player/runes/dlc/blood_punch_low_health_bonus_damage",
        7770147: "perk/player/runes/dlc/extra_life_refund",
    }
    try:
        contracts = read_json(ROOT / "data" / "item_runtime_contracts.json")
        contract_items = contracts["persistent_ownership"]["items"]
        policies = read_json(ROOT / "data" / "item_replay_policies.json")["items"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return [f"Support Rune contract data invalid: {exc}"]
    for name, item_id in expected.items():
        if item_ids.get(name) != item_id:
            errors.append(f"Support Rune identity drifted: {name}={item_ids.get(name)}")
            continue
        definition = commands.get(item_id)
        expected_command = expected_paths[item_id]
        if definition != f"ai_ScriptCmdEnt player1 givePlayerPerk {expected_command}":
            errors.append(f"Support Rune {item_id} direct perk mapping drifted")
        try:
            plan = compile_item_delivery_plan(item_id, commands, receipt=False)
            if not plan.commands:
                errors.append(f"Support Rune {item_id} generated no native delivery")
        except ValueError as exc:
            errors.append(f"Support Rune {item_id} failed compile probe: {exc}")
        contract = contract_items.get(str(item_id), {})
        if contract.get("family") != "runes":
            errors.append(f"Support Rune {item_id} contract family drifted")
        if contract.get("ownership_primitive") != "devinv_is_rune":
            errors.append(f"Support Rune {item_id} ownership primitive drifted")
        if contract.get("runtime_primitive") != "context_deferred":
            errors.append(f"Support Rune {item_id} context deferral drifted")
        if contract.get("reconcile") != "tag_context_only":
            errors.append(f"Support Rune {item_id} context reconciliation drifted")
        if policies.get(str(item_id), {}).get("policy") != "replay_manual_only":
            errors.append(f"Support Rune {item_id} replay policy drifted")
    return errors


def validate_replay_registry(item_ids: dict[str, int], commands: dict[int, object]) -> list[str]:
    errors: list[str] = []
    try:
        registry = read_json(ROOT / "data" / "item_replay_policies.json").get("items")
        if not isinstance(registry, dict):
            raise ValueError("items must be an object")
        policy_ids = {int(item_id) for item_id in registry}
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return [f"Item replay registry invalid: {exc}"]
    command_ids = set(commands)
    if policy_ids != command_ids:
        errors.append(
            "Item replay registry coverage drift: "
            f"missing={sorted(command_ids - policy_ids)}, extra={sorted(policy_ids - command_ids)}"
        )
    expected_names = {item_id: name for name, item_id in item_ids.items()}
    for raw_id, entry in registry.items():
        item_id = int(raw_id)
        if not isinstance(entry, dict) or entry.get("name") != expected_names.get(item_id):
            errors.append(f"Item replay registry identity drift for {item_id}")
    return errors


def collect_duplicate_ids(values: dict[str, int]) -> dict[int, list[str]]:
    grouped: dict[int, list[str]] = {}
    for name, value in values.items():
        grouped.setdefault(value, []).append(name)
    return {
        value: names for value, names in grouped.items() if len(names) > 1
    }


def validate_id_namespaces(
    item_ids: dict[str, int], location_ids: dict[str, int]
) -> list[str]:
    errors: list[str] = []

    duplicate_item_ids = collect_duplicate_ids(item_ids)
    for item_id, item_names in sorted(duplicate_item_ids.items()):
        errors.append(f"Duplicate AP item ID {item_id}: {item_names}")

    duplicate_location_ids = collect_duplicate_ids(location_ids)
    for location_id, location_names in sorted(duplicate_location_ids.items()):
        errors.append(f"Duplicate AP location ID {location_id}: {location_names}")

    return errors


def entity_scalar(block: str, property_name: str) -> str | None:
    match = re.search(
        rf'\b{re.escape(property_name)}\s*=\s*"([^"]+)";', block
    )
    return match.group(1) if match else None


def runtime_automap_families(families: dict) -> dict[int, str]:
    """Classify runtime locations from their generated contract categories."""
    challenge_registry = load_challenge_registry()
    classified: dict[int, str] = {}
    for family_name, family in families.items():
        for category in family.get("match", {}).get("runtime_categories", []):
            if category not in challenge_registry:
                raise ValueError(
                    f"Automap family {family_name} references unknown runtime category "
                    f"{category}"
                )
            for entry in challenge_registry[category]:
                location_id = entry["location_id"]
                previous = classified.get(location_id)
                if previous is not None:
                    raise ValueError(
                        f"Runtime location {location_id} overlaps Automap families "
                        f"{previous} and {family_name}"
                    )
                classified[location_id] = family_name
    return classified


def validate_automap_family_registry(
    location_ids: dict[str, int], runtime_locations: set[int]
) -> list[str]:
    errors: list[str] = []
    registry = read_json(AUTOMAP_FAMILY_REGISTRY_PATH)
    families = registry.get("families", {})
    required_fields = {
        "match", "vanilla_class", "automap_marker_source", "automap_properties",
        "dossier_total_owner", "collected_state_writer", "reward_edge", "safe_cut",
        "vanilla_automap", "vanilla_exploration",
    }
    for family_name, family in families.items():
        missing = sorted(required_fields - set(family))
        if missing:
            errors.append(f"Automap family {family_name} is missing fields: {missing}")
        if "poster" in family.get("automap_properties", []):
            errors.append(f"Automap family {family_name} reuses Hub-only poster")

    classified: dict[int, str] = {}
    exact_families = {
        location_id: family_name
        for family_name, family in families.items()
        for location_id in family.get("match", {}).get("location_ids", [])
    }

    map_sources = load_map_registry(MAP_SOURCES_PATH)["maps"]
    for map_key, source in map_sources.items():
        if not source.get("enabled", True):
            continue
        config = read_json(ROOT / source["level_config"])
        source_text = (ROOT / "vanillamaps" / source["source_file"]).read_text(
            encoding="utf-8"
        )
        for ap_check, location_id in config.get("entities", {}).items():
            entity_name = ap_check.removeprefix("AP_CHECK_").lower()
            bounds = find_entity_block_bounds(source_text, entity_name)
            if bounds is None:
                errors.append(f"Automap source entity missing: {map_key}/{entity_name}")
                continue
            block = source_text[bounds[0]:bounds[1]]
            inherit = entity_scalar(block, "inherit")
            family_name = exact_families.get(location_id)
            if family_name is None:
                matches = [
                    name for name, family in families.items()
                    if any(
                        inherit and inherit.startswith(prefix)
                        for prefix in family.get("match", {}).get("inherit_prefixes", [])
                    )
                ]
                if len(matches) != 1:
                    errors.append(
                        f"Automap family coverage for {location_id}/{entity_name}: {matches}"
                    )
                    continue
                family_name = matches[0]
            classified[location_id] = family_name
            family = families[family_name]
            if family_name not in {"independent_ice_trigger", "independent_rocket_trigger"}:
                actual_class = entity_scalar(block, "class")
                if actual_class != family["vanilla_class"]:
                    errors.append(
                        f"Automap family class drift for {location_id}: {actual_class}"
                    )
            actual_automap = entity_scalar(block, "automapPropertiesDecl")
            allowed_automap = family.get("automap_properties", [])
            if actual_automap not in allowed_automap and not (
                actual_automap is None and not allowed_automap
            ):
                errors.append(
                    f"Automap field drift for {location_id}: {actual_automap} not in {allowed_automap}"
                )

        for encounter in config.get("secret_encounters", []):
            location_id = encounter["location_id"]
            if exact_families.get(location_id):
                errors.append(f"Secret encounter {location_id} overlaps exact Automap family")
            classified[location_id] = "secret_encounters"

    try:
        runtime_families = runtime_automap_families(families)
    except ValueError as exc:
        errors.append(str(exc))
        runtime_families = {}
    if set(runtime_families) != runtime_locations:
        errors.append(
            "Runtime Automap family coverage drift: missing="
            f"{sorted(runtime_locations - set(runtime_families))}, extra="
            f"{sorted(set(runtime_families) - runtime_locations)}"
        )
    classified.update(runtime_families)

    all_location_values = set(location_ids.values())
    if set(classified) != all_location_values:
        errors.append(
            "Automap family registry is incomplete: missing="
            f"{sorted(all_location_values - set(classified))}, extra="
            f"{sorted(set(classified) - all_location_values)}"
        )

    pilot = registry.get("pilot", {})
    pilot_source = map_sources.get(pilot.get("map_key"), {})
    if pilot_source:
        source_text = (ROOT / "vanillamaps" / pilot_source["source_file"]).read_text(
            encoding="utf-8"
        )
        config = read_json(ROOT / pilot_source["level_config"])
        policies = config.get("target_policies", {})
        for entity_name, expected_decl in pilot.get("marker_entities", {}).items():
            bounds = find_entity_block_bounds(source_text, entity_name)
            block = source_text[bounds[0]:bounds[1]] if bounds else ""
            policy = policies.get(entity_name, {})
            marker = policy.get("native_automap_carrier", {})
            if entity_scalar(block, "automapPropertiesDecl") != expected_decl:
                errors.append(f"Pilot source Automap field drift: {entity_name}")
            if marker.get("automap_properties_decl") != expected_decl:
                errors.append(f"Pilot marker does not copy exact family field: {entity_name}")
            for property_name, marker_key in (
                ("inherit", "source_inherit"),
                ("class", "source_class"),
                ("progressionCategory", "source_progression_category"),
            ):
                if entity_scalar(block, property_name) != marker.get(marker_key):
                    errors.append(f"Pilot source evidence drift: {entity_name}/{property_name}")
            marker_text = json.dumps(marker, sort_keys=True).lower()
            if any(term in marker_text for term in ("poster", "currency", "perk", "give", "grant")):
                errors.append(f"Pilot marker contains forbidden reward/blanket field: {entity_name}")
            if not policy.get("independent_ap_trigger"):
                errors.append(f"Pilot carrier lacks independent AP trigger: {entity_name}")
            if any("ap_remove_native_automap_" in target for target in policy.get("independent_targets", [])):
                errors.append(f"Pilot carrier incorrectly removes persistent Automap marker: {entity_name}")
        negative = pilot.get("negative_control")
        bounds = find_entity_block_bounds(source_text, negative) if negative else None
        if bounds and entity_scalar(source_text[bounds[0]:bounds[1]], "automapPropertiesDecl"):
            errors.append("Pilot negative-control family unexpectedly has a vanilla marker")
    return errors


def validate_generated_automap_carriers() -> list[str]:
    """Audit native carriers and reject the unresolved persistent-visual cut.

    This is deliberately generated-map validation: source metadata alone cannot
    prove that the independent AP trigger did not drift from its exact vanilla
    map edge or that the carrier lost its vanilla grant fields.
    """
    errors: list[str] = []
    registry = read_json(AUTOMAP_FAMILY_REGISTRY_PATH)["families"]
    exact_families = {
        location_id: family_name
        for family_name, family in registry.items()
        for location_id in family.get("match", {}).get("location_ids", [])
    }
    sources = read_json(MAP_SOURCES_PATH)["maps"]
    items = read_json(ROOT / "data" / "items.json")
    reward_terms = (
        "useableComponentDecl", "triggerDef", "canBePossessed", "equipOnPickup",
        "forceEquip", "currencyList", "itemList", "inventory", "useStat",
        "onUseCodexEntry", "progressionCategory", "clipModelInfo",
        "pickup_statIncreases", "use_statIncreases", "spawn_statIncreases",
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        generated_dir = Path(tmpdir)
        for map_key, source in sources.items():
            if not source.get("enabled", True):
                continue
            config = read_json(ROOT / source["level_config"])
            vanilla = (ROOT / "vanillamaps" / source["source_file"]).read_text(
                encoding="utf-8"
            )
            output = generated_dir / f"{map_key}.entities"
            manifest = generated_dir / f"{map_key}.json"
            try:
                generate_map(
                    ROOT / "vanillamaps" / source["source_file"], output,
                    ROOT / source["level_config"], manifest, items,
                )
            except Exception as exc:
                errors.append(f"Automap carrier generation failed for {map_key}: {exc}")
                continue
            generated = output.read_text(encoding="utf-8")
            assets = {
                asset["key"]: asset for asset in config.get("assets", [])
            }
            default_asset = assets.get(config.get("default_visual_asset"), {})
            default_visual_model = default_asset.get(
                "model", "art/pickups/question_mark_a.lwo"
            )
            if "ap_remove_native_automap_" in generated:
                errors.append(f"Automap carrier marker removal reappeared in {map_key}")
            secret_count = len(config.get("secret_encounters", []))
            if generated.count('automapPropertiesDecl = "automap_encounter_secret";') < secret_count:
                errors.append(
                    f"Automap secret marker coverage drift in {map_key}: "
                    f"expected at least {secret_count} native markers"
                )
            for ap_check, location_id in config.get("entities", {}).items():
                entity_name = ap_check.removeprefix("AP_CHECK_").lower()
                policy = config.get("target_policies", {}).get(entity_name, {})
                visual_policy = policy.get("independent_visual", {})
                visual_asset = assets.get(visual_policy.get("asset"), {})
                expected_visual_model = visual_asset.get(
                    "model",
                    visual_policy.get("model", default_visual_model),
                )
                if policy.get("native_entity_contract") is not None:
                    continue
                source_bounds = find_entity_block_bounds(vanilla, entity_name)
                if source_bounds is None:
                    continue
                source_block = vanilla[source_bounds[0]:source_bounds[1]]
                inherit = entity_scalar(source_block, "inherit") or ""
                family_name = exact_families.get(location_id) or next((
                    name for name, value in registry.items()
                    if any(
                        inherit.startswith(prefix)
                        for prefix in value["match"].get("inherit_prefixes", [])
                    )
                ), None)
                family = registry.get(family_name, {})
                if family.get("carrier_mode") == "persistent_native_idprop2":
                    if policy.get("native_automap_contract"):
                        carrier_bounds = find_entity_block_bounds(generated, entity_name)
                        if carrier_bounds is None:
                            errors.append(
                                f"Native Automap prototype missing for {location_id}"
                            )
                            continue
                        carrier = generated[carrier_bounds[0]:carrier_bounds[1]]
                        if "useableComponentDecl" not in carrier:
                            errors.append(
                                f"Native Automap lifecycle stripped for {location_id}"
                            )
                        if "fxDecl" in carrier or "updateFX" in carrier:
                            errors.append(f"Native Automap fire FX retained for {location_id}")
                        expected = [*extract_target_names(source_block), ap_check]
                        if extract_target_names(carrier) != expected:
                            errors.append(f"Native Automap AP target drift for {location_id}")
                        errors.append(
                            f"Native Automap prototype runtime pending for {location_id}: "
                            "zero-XP reward cut, removal, marker transition, and reload "
                            "have no runtime PASS"
                        )
                        continue
                    carrier_bounds = find_entity_block_bounds(generated, entity_name)
                    trigger_bounds = find_entity_block_bounds(
                        generated, f"ap_independent_{entity_name}"
                    )
                    if carrier_bounds is None or trigger_bounds is None:
                        errors.append(f"Automap carrier missing for {location_id}/{entity_name}")
                        continue
                    carrier = generated[carrier_bounds[0]:carrier_bounds[1]]
                    trigger = generated[trigger_bounds[0]:trigger_bounds[1]]
                    for field in ("inherit", "class", "automapPropertiesDecl"):
                        if entity_scalar(carrier, field) != entity_scalar(source_block, field):
                            errors.append(f"Automap source metadata drift for {location_id}/{field}")
                    if f'model = "{expected_visual_model}";' not in carrier:
                        errors.append(f"Automap carrier lacks AP visual for {location_id}")
                    if extract_target_names(carrier):
                        errors.append(f"Automap carrier retained vanilla targets for {location_id}")
                    if any(term in carrier for term in reward_terms):
                        errors.append(f"Automap carrier retains reward edge for {location_id}")
                    expected = [*extract_target_names(source_block), ap_check]
                    if extract_target_names(trigger) != expected:
                        errors.append(f"Automap functional target drift for {location_id}")
                    if extract_target_names(trigger).count(ap_check) != 1:
                        errors.append(f"Automap AP check multiplicity drift for {location_id}")
                    errors.append(
                        "Automap lifecycle unresolved for "
                        f"{location_id}/{entity_name}: reward-free carrier has no "
                        "proven physical-removal/FX-shutdown/collected-marker writer"
                    )
                elif family_name in {"sentinel_crystals", "modbots", "runes"}:
                    visual = policy.get("independent_visual", {})
                    completion_targets = policy.get("completion_targets", [])
                    if visual and completion_targets:
                        visual_name = visual.get("entity_name")
                        cleanup_name = visual.get("cleanup_entity")
                        visual_bounds = find_entity_block_bounds(generated, visual_name)
                        cleanup_bounds = find_entity_block_bounds(generated, cleanup_name)
                        check_bounds = find_entity_block_bounds(generated, ap_check)
                        if not all((visual_bounds, cleanup_bounds, check_bounds)):
                            errors.append(
                                f"Generic Automap prototype graph missing for {location_id}"
                            )
                            continue
                        cleanup = generated[cleanup_bounds[0]:cleanup_bounds[1]]
                        check = generated[check_bounds[0]:check_bounds[1]]
                        if extract_target_names(cleanup) != [visual_name]:
                            errors.append(
                                f"Generic Automap cleanup escaped visual for {location_id}"
                            )
                        if cleanup_name not in extract_target_names(check):
                            errors.append(
                                f"Generic Automap live cleanup is disconnected for {location_id}"
                            )
                        errors.append(
                            f"Generic Automap prototype runtime pending for {location_id}: "
                            "visual/marker removal and checked-state reload bootstrap "
                            "have no runtime PASS"
                        )
                        continue
                    errors.append(
                        f"Automap marker unresolved for {location_id}/{entity_name}: "
                        f"{family_name} collected state is coupled to an unsafe native interaction"
                    )
                elif family_name in {
                    "ability_progression", "weapons_equipment",
                    "independent_ice_trigger", "independent_rocket_trigger",
                }:
                    errors.append(
                        f"Automap marker missing for {location_id}/{entity_name}: "
                        f"{family_name} has no proven generic marker lifecycle"
                    )
                elif inherit.startswith("progress/praetor_token"):
                    trigger_name = f"ap_independent_{entity_name}"
                    visual_name = f"ap_location_visual_{location_id}"
                    cleanup_name = f"ap_remove_location_visual_{location_id}"
                    trigger_bounds = find_entity_block_bounds(generated, trigger_name)
                    visual_bounds = find_entity_block_bounds(generated, visual_name)
                    cleanup_bounds = find_entity_block_bounds(generated, cleanup_name)
                    if not all((trigger_bounds, visual_bounds, cleanup_bounds)):
                        errors.append(f"Generic AP pickup graph missing for {location_id}")
                        continue
                    if find_entity_block_bounds(generated, entity_name) is not None:
                        errors.append(f"Native Praetor owner retained for {location_id}")
                    trigger = generated[trigger_bounds[0]:trigger_bounds[1]]
                    visual = generated[visual_bounds[0]:visual_bounds[1]]
                    cleanup = generated[cleanup_bounds[0]:cleanup_bounds[1]]
                    if extract_target_names(trigger) != [ap_check, cleanup_name]:
                        errors.append(f"Generic AP target drift for {location_id}")
                    if extract_target_names(cleanup) != [visual_name]:
                        errors.append(f"Generic AP visual cleanup drift for {location_id}")
                    if f'model = "{expected_visual_model}";' not in visual:
                        errors.append(f"Generic AP visual missing for {location_id}")
                    for forbidden in (
                        "progress/praetor_token", "idInteractable_GiveItems",
                        "useableComponentDecl", "currencyList",
                        "CURRENCY_PRAETOR_UPGRADE", "praetor_suit_token.md6",
                    ):
                        if forbidden in trigger or forbidden in visual:
                            errors.append(
                                f"Native Praetor behavior retained for {location_id}: "
                                f"{forbidden}"
                            )
    return errors


def validate_automap_prototypes_only() -> list[str]:
    """Audit generated Automap prototype graphs without rejecting normal visuals."""
    errors: list[str] = []
    sources = read_json(MAP_SOURCES_PATH)["maps"]
    items = read_json(ROOT / "data" / "items.json")
    for source in sources.values():
        if not source.get("enabled", True):
            continue
        config = read_json(ROOT / source["level_config"])
        for entity_name, policy in config.get("target_policies", {}).items():
            if "native_automap_carrier" in policy:
                errors.append(f"Retired Automap carrier policy remains: {entity_name}")
            if policy.get("native_automap_contract"):
                errors.append(f"Unexpected native Automap prototype: {entity_name}")
            visual = policy.get("independent_visual", {})
            if visual.get("automap_properties_decl") and not policy.get("allow_automap_properties"):
                errors.append(f"Unexpected generic Automap prototype: {entity_name}")

    with tempfile.TemporaryDirectory() as tmpdir:
        generated_dir = Path(tmpdir)
        for map_key, source in sources.items():
            if not source.get("enabled", True):
                continue
            config = read_json(ROOT / source["level_config"])
            output = generated_dir / f"{map_key}.entities"
            manifest = generated_dir / f"{map_key}.json"
            try:
                generate_map(
                    ROOT / "vanillamaps" / source["source_file"], output,
                    ROOT / source["level_config"], manifest, items,
                )
            except Exception as exc:
                errors.append(f"Automap prototype generation failed for {map_key}: {exc}")
                continue
            generated = output.read_text(encoding="utf-8")
            visual_names = set(re.findall(r"entityDef (ap_location_visual_\d+)", generated))
            for visual_name in visual_names:
                visual_bounds = find_entity_block_bounds(generated, visual_name)
                if visual_bounds is None:
                    errors.append(f"Generated Automap visual is unreadable: {visual_name}")
                    continue
                visual = generated[visual_bounds[0]:visual_bounds[1]]
                if extract_target_names(visual):
                    errors.append(f"Generated Automap visual has functional targets: {visual_name}")

            for ap_check, location_id in config.get("entities", {}).items():
                entity_name = ap_check.removeprefix("AP_CHECK_").lower()
                if entity_name in {
                    "mech_street_pickup_collectible_toys_doomguy_1",
                    "mech_street_progress_mod_bot_1_e1m1",
                } or config.get(
                    "target_policies", {}
                ).get(entity_name, {}).get("independent_visual") or config.get(
                    "target_policies", {}
                ).get(entity_name, {}).get("native_entity_contract") is not None or config.get(
                    "target_policies", {}
                ).get(entity_name, {}).get("preserve_original_visual"):
                    continue
                bounds = find_entity_block_bounds(generated, entity_name)
                if bounds is None:
                    continue
                block = generated[bounds[0]:bounds[1]]
                for forbidden in (
                    "automapPropertiesDecl", "fxDecl", "thinkComponentDecl",
                    "question_mark_a.lwo",
                ):
                    if forbidden in block:
                        errors.append(
                            f"Retired Automap carrier field remains for {location_id}: "
                            f"{forbidden}"
                        )

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--map", dest="map_key")
    args = parser.parse_args(argv)
    if args.map_key:
        from tools.validation.pipeline import Pipeline

        pipeline = Pipeline()
        pipeline.validate_map(args.map_key)
        pipeline.report()
        print(f"map data validation passed: {args.map_key}")
        return 0
    errors: list[str] = []
    warnings: list[str] = []

    try:
        assert_frozen_map_baselines()
    except (OSError, ValueError) as exc:
        errors.append(f"Map baseline failed: {exc}")
    try:
        assert_hub_diff_classified()
    except (OSError, ValueError) as exc:
        errors.append(f"Hub entity diff classification failed: {exc}")

    item_ids = extract_namedtuple_table(APWORLD / "items.py", "item_data_table")
    location_ids = extract_namedtuple_table(APWORLD / "locations.py", "location_data_table")
    try:
        classification_path = ROOT / "data" / "item_classifications.json"
        classification_document = read_json(classification_path)
        classification_identity = load_item_classification_identity(classification_path)
        if classification_document.get("item_mapping_revision") != 6:
            errors.append("Packaged item classification revision drifted")
        if classification_document.get("source") != ITEM_CLASSIFICATION_SOURCE:
            errors.append(
                "Packaged item classifications have an unauthenticated source path"
            )
        expected_source_sha256 = hashlib.sha256((APWORLD / "items.py").read_bytes()).hexdigest()
        if classification_document.get("source_sha256") != expected_source_sha256:
            errors.append("Packaged item classifications have a stale source SHA-256")
        packaged_item_names = {
            entry["name"]: item_id
            for item_id, entry in classification_identity.items()
        }
        if packaged_item_names != item_ids:
            errors.append(
                "Packaged item classifications diverge from APWorld item IDs/names"
            )
        expected_classifications = extract_item_classifications(APWORLD / "items.py")
        packaged_classifications = {
            item_id: entry["classification"]
            for item_id, entry in classification_identity.items()
        }
        if packaged_classifications != expected_classifications:
            errors.append("Packaged item classifications diverge from APWorld classifications")
    except (OSError, SyntaxError, ValueError, RuntimeError) as exc:
        errors.append(f"Packaged item classifications invalid: {exc}")
    try:
        location_identity = read_json(ROOT / "data" / "location_names.json")
        packaged_location_names = {
            name: int(location_id)
            for location_id, name in location_identity.get(
                "locations", {}
            ).items()
        }
        if (
            location_identity.get("schema_version") != 1
            or packaged_location_names != location_ids
        ):
            errors.append(
                "Packaged location names diverge from APWorld locations"
            )
    except (OSError, ValueError) as exc:
        errors.append(f"Packaged location names invalid: {exc}")
    reserved_item_ids = extract_frozenset_constant(APWORLD / "items.py", "RESERVED_ITEM_IDS")
    reserved_location_ids = {7770055, 7770068}
    reused_location_ids = sorted(reserved_location_ids & set(location_ids.values()))
    if reused_location_ids:
        errors.append(f"Reserved location IDs must not be reused: {reused_location_ids}")
    commands = {int(key): value for key, value in read_json(ROOT / "data" / "items.json").items()}
    errors.extend(validate_devinv_mapping(item_ids, commands))
    errors.extend(validate_start_inventory_catalog())
    errors.extend(validate_support_rune_foundation(item_ids, commands))
    errors.extend(validate_replay_registry(item_ids, commands))
    if {name: location_ids.get(name) for name in BATTERY_LOCATIONS} != BATTERY_LOCATIONS:
        errors.append("Six physical Sentinel Battery AP locations must remain active")
    if item_ids.get("Sentinel Battery") != 7770016:
        errors.append("Sentinel Battery single item ID drifted")
    if item_ids.get("Sentinel Battery Bundle") != 7770142:
        errors.append("Sentinel Battery Bundle item ID drifted")
    for item_id, count in BATTERY_ITEM_COMMANDS.items():
        if commands.get(item_id) != {
            "type": "currency",
            "currency": "CURRENCY_SENTINEL_BATTERY",
            "count": count,
        }:
            errors.append(f"Sentinel Battery AP command {item_id} must grant exactly {count}")
    if sum(BATTERY_ITEM_COMMANDS.values()) != 3:
        errors.append("Sentinel Battery item-type currency contract drifted")
    for deprecated_id in (7770019, 7770057):
        if deprecated_id not in reserved_item_ids:
            errors.append(f"Deprecated item ID {deprecated_id} is not reserved")
        if deprecated_id in item_ids:
            errors.append(f"Deprecated item ID {deprecated_id} reappeared as an active AP item")
        if deprecated_id in commands:
            errors.append(f"Deprecated item ID {deprecated_id} reappeared as an AP command")
    if "Weapon Mastery Token" in item_ids:
        errors.append("Weapon Mastery Token reappeared as an active AP item")
    if "CURRENCY_WEAPON_MASTERY" in json.dumps(commands, sort_keys=True):
        errors.append("An AP command grants forbidden CURRENCY_WEAPON_MASTERY")
    runtime_location_mapping = read_json(ROOT / "data" / "runtime_locations.json")
    runtime_locations = set(runtime_location_mapping.values())
    errors.extend(validate_automap_family_registry(location_ids, runtime_locations))
    try:
        assert_separate_automap_helper_guard()
    except ValueError as exc:
        errors.append(f"Generated Automap helper validation failed: {exc}")
    errors.extend(validate_automap_prototypes_only())
    try:
        packaged_visuals = load_automap_visual_registry(
            ROOT / "data" / "checked_location_visuals.json"
        )
        if packaged_visuals != build_authorial_registry(ROOT):
            errors.append(
                "checked_location_visuals.json diverges from authoritative catalog/policy"
            )
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        errors.append(f"Checked-location visual registry invalid: {exc}")
    challenge_registry = load_challenge_registry()
    mastery_entries = challenge_registry["weapon_masteries"]
    for entry in mastery_entries:
        expected_command = {
            "type": "perk",
            "perk": entry["gameplay_perk"],
        }
        if commands.get(entry["item_id"]) != expected_command:
            errors.append(
                f"{entry['name']} AP item must use typed give-then-activate perk delivery"
            )
    registry_locations = {
        entry["name"]: entry["location_id"]
        for entry in all_location_entries(challenge_registry)
    }
    if runtime_location_mapping != registry_locations:
        errors.append("runtime_locations.json diverges from challenge registry")
    for name, location_id in registry_locations.items():
        if location_ids.get(name) != location_id:
            errors.append(f"Mission registry/APWorld mapping drift: {name}={location_id}")
    runtime_item_collisions = sorted(
        set(registry_locations.values()) & set(item_ids.values())
    )
    if runtime_item_collisions:
        errors.append(
            "Runtime location IDs must not reuse item IDs: "
            f"{runtime_item_collisions}"
        )
    all_map_check_ids = set()
    for _map_key, source in load_map_registry(MAP_SOURCES_PATH)["maps"].items():
        if not source.get("enabled", True):
            continue
        cfg = read_json(ROOT / source["level_config"])
        all_map_check_ids.update(cfg.get("entities", {}).values())
    if not all_map_check_ids.isdisjoint(runtime_locations):
        overlap = sorted(all_map_check_ids & runtime_locations)
        errors.append(f"Physical map check IDs must be disjoint from runtime location IDs: {overlap}")
    mastery_location_names = [
        name for name in location_ids
        if "Weapon Mastery Challenge" in name
    ]
    expected_mastery_location_names = [entry["name"] for entry in mastery_entries]
    if mastery_location_names != expected_mastery_location_names:
        errors.append(f"Base Mastery registry/APWorld drift: {mastery_location_names}")
    expected_mission_challenges = {
        entry["name"]: entry["location_id"]
        for entry in (
            *challenge_registry.get("mission_challenges", []),
            *challenge_registry.get("all_mission_challenges", []),
        )
    }
    actual_mission_challenges = {
        name: location_id
        for name, location_id in location_ids.items()
        if name in expected_mission_challenges
    }
    if actual_mission_challenges != expected_mission_challenges:
        errors.append(
            "Mission Challenge registry/APWorld drift: "
            f"actual={actual_mission_challenges}, "
            f"expected={expected_mission_challenges}"
        )
    for aggregate_entry in challenge_registry.get("all_mission_challenges", []):
        if location_ids.get(aggregate_entry["name"]) != aggregate_entry["location_id"]:
            errors.append(
                "Aggregate Mission Challenge ID mismatch: "
                f"{aggregate_entry['name']}"
            )
    # Parse ast of a few key python files
    source_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            ROOT / "doom_eap" / "runtime" / "bridge_client.py",
            ROOT / "tools" / "maps" / "ap_map_generator.py",
            ROOT / "doom_eap" / "contracts" / "challenge_registry.py",
        )
    )
    for forbidden in (
        "append_graph_entries", "watchers_for_map", "AP_RUNTIME_CHECK_",
        "3_900_000_000", "3_800_000_000", "give armor -200",
    ):
        if forbidden in source_text:
            errors.append(f"Rejected watcher/Armor Drain source returned: {forbidden}")
    map_registry = load_map_registry(MAP_SOURCES_PATH)
    map_sources = map_registry["maps"]

    forbidden_decl_path = "propitem/ap/"
    for path in (
        *(ROOT / source["level_config"] for source in map_sources.values()),
        ROOT / "tools" / "maps" / "ap_map_generator.py",
        ROOT / "scripts" / "build" / "playable_test.sh",
    ):
        if forbidden_decl_path in path.read_text(encoding="utf-8"):
            errors.append(f"Scripted pickup uses forbidden custom DECL path: {path}")
    for path in (ROOT / "packaging" / "mod_assets").rglob("*"):
        if path.is_file() and forbidden_decl_path in path.as_posix():
            errors.append(f"Forbidden custom scripted-pickup DECL is packaged: {path}")

    for override_path in (ROOT / "packaging" / "mod_assets").rglob("*.decl"):
        if "propitem/ap/" in override_path.as_posix():
            errors.append(f"Forbidden scripted-pickup DECL override remains packaged: {override_path}")

    manifests: dict[str, dict[str, int]] = {}
    for path in sorted((ROOT / "manifests").glob("*.json")):
        manifest = read_json(path)
        manifests[path.stem] = manifest
        for location_id, declarations in sorted(
            collect_duplicate_ids(manifest).items()
        ):
            errors.append(
                f"Duplicate manifest location ID in {path.name}: "
                f"{location_id}: {declarations}"
            )
    manifest_location_ids = [
        location_id
        for manifest in manifests.values()
        for location_id in manifest.values()
    ]
    manifest_location_owners: dict[int, list[str]] = {}
    for map_key, manifest in manifests.items():
        for declaration, location_id in manifest.items():
            manifest_location_owners.setdefault(location_id, []).append(
                f"{map_key}/{declaration}"
            )
    for location_id, owners in sorted(manifest_location_owners.items()):
        if len(owners) > 1:
            errors.append(f"Duplicate manifest location ID {location_id}: {owners}")
    for location_id in BATTERY_LOCATIONS.values():
        if manifest_location_ids.count(location_id) != 1:
            errors.append(f"Physical Sentinel Battery {location_id} must have one active manifest check")

    physical_location_count = 0
    praetor_policy_count = 0
    prohibited_praetor_policy = "praetor" + "_token_ap"
    config_pairs = sorted(
        (map_key, ROOT / source["level_config"])
        for map_key, source in map_sources.items()
        if source.get("enabled", True)
    )
    config_paths = [path for _, path in config_pairs]
    for map_key, path in config_pairs:
        config_data = read_json(path)
        if prohibited_praetor_policy in config_data:
            errors.append(f"Retired Praetor policy remains in {path.name}")
        if config_data.get("map_key") != map_key:
            errors.append(f"Missing or divergent map_key in {path.name}")
        config = dict(config_data.get("entities", {}))
        for ap_check in config:
            if "PRAETOR" not in ap_check:
                continue
            praetor_policy_count += 1
            entity_name = ap_check.removeprefix("AP_CHECK_").lower()
            policy = config_data.get("target_policies", {}).get(entity_name, {})
            if "native_entity_contract" in policy:
                errors.append(
                    f"Praetor token retains native entity contract: {path.name}/{ap_check}"
                )
            if policy and (
                not policy.get("independent_ap_trigger")
                or not policy.get("remove_original")
                or set(policy) - {
                    "independent_ap_trigger", "remove_original", "drop_targets",
                    "preserve_targets",
                }
            ):
                errors.append(
                    f"Praetor token lacks generic AP pickup policy: {path.name}/{ap_check}"
                )
        physical_location_count += len(config)
        encounter_checks = {
            encounter["ap_check"]
            for encounter in config_data.get("secret_encounters", [])
        }
        declared_checks = set(config) | encounter_checks
        try:
            feedback = ap_map_generator.load_explicit_location_feedback(
                map_key,
                config_data.get("location_feedback", {}),
                declared_checks,
            )
        except ValueError as exc:
            errors.append(f"Invalid location feedback policies in {path.name}: {exc}")
            feedback = {}
        unknown_feedback = sorted(
            set(feedback) - declared_checks
        )
        if unknown_feedback:
            errors.append(
                f"Unknown location feedback keys in {path.name}: "
                f"{unknown_feedback}"
            )
        default_feedback = sorted(
            declared_checks - set(feedback)
        )
        if default_feedback:
            errors.append(
                f"{path.name} has public-package default feedback policies: "
                f"{default_feedback}"
            )
        for ap_check, record in feedback.items():
            if not isinstance(record, dict):
                errors.append(
                    f"Invalid location feedback record: {path.name}/{ap_check}"
                )
                continue
            policy = record.get("policy")
            if policy not in {"vanilla_only", "ap_only", "vanilla_and_ap"}:
                errors.append(
                    f"Invalid location feedback policy: {path.name}/{ap_check}"
                )
            if policy == "vanilla_only":
                audited_vanilla_feedback_categories = ("CODEX", "CHEATS", "ALBUMS", "SECRET_ENCOUNTER", "EXTRA_LIFE", "TOYS", "MOD_BOT", "ARGENT_CELL", "PRAETOR", "RUNE")
                if not any(cat in ap_check for cat in audited_vanilla_feedback_categories) and not record.get("vanilla_feedback_proof"):
                    errors.append(
                        f"vanilla_only policy for non-audited UI category lacks required vanilla_feedback_proof: {path.name}/{ap_check}"
                    )
        reused_config_ids = sorted(reserved_location_ids & set(config.values()))
        if reused_config_ids:
            errors.append(f"Reserved location IDs remain in {path.name}: {reused_config_ids}")
        for encounter in config_data.get("secret_encounters", []):
            config[encounter["ap_check"]] = encounter["location_id"]
        manifest_path = ROOT / map_sources[map_key]["manifest"]
        if not manifest_path.exists():
            errors.append(f"Missing manifest for {path.name}")
            continue
        manifest = read_json(manifest_path)
        if config != manifest:
            errors.append(f"Config/manifest mismatch: {path.name}")
    if physical_location_count != 290:
        errors.append(
            f"Expected 290 physical entity locations, found {physical_location_count}"
        )
    expected_praetor_policy_count = sum(
        "Praetor Suit Token" in name for name in location_ids
    )
    if praetor_policy_count != expected_praetor_policy_count:
        errors.append(
            "Shared-policy Praetor Token coverage drift: "
            f"expected={expected_praetor_policy_count}, "
            f"found={praetor_policy_count}"
        )

    enabled_map_sources = {
        plan.map_key: map_sources[plan.map_key]
        for plan in validation_plan(map_registry)
        if plan.release_asset
    }
    expected_level_configs = {
        ROOT / source["level_config"] for source in enabled_map_sources.values()
    }
    if expected_level_configs != set(config_paths):
        errors.append("Enabled map sources are not aligned with canonical map configs")

    for map_key, source in enabled_map_sources.items():
        config_path = ROOT / source["level_config"]
        source_path = ROOT / "vanillamaps" / source["source_file"]
        try:
            config_data = read_json(config_path)
            validate_target_policies(
                config_data.get("entities", {}),
                config_data.get("target_policies", {}),
                source_path.read_text(encoding="utf-8"),
            )
            source_content = source_path.read_text(encoding="utf-8")
            target_policies = config_data.get("target_policies", {})
            secret_feedback = {
                encounter["ap_check"]: encounter
                for encounter in config_data.get("secret_encounters", [])
            }
            for ap_check, feedback in config_data.get(
                "location_feedback", {}
            ).items():
                if not isinstance(feedback, dict) or feedback.get("policy") != "vanilla_only":
                    continue
                if ap_check in secret_feedback:
                    manager = secret_feedback[ap_check].get(
                        "manager_entity",
                        secret_feedback[ap_check].get("manager"),
                    )
                    if (
                        not manager
                        or find_entity_block_bounds(source_content, manager)
                        is None
                    ):
                        errors.append(
                            f"vanilla_only secret owner missing for "
                            f"{map_key}/{ap_check}"
                        )
                    continue
                entity_name = ap_check.removeprefix("AP_CHECK_").lower()
                bounds = find_entity_block_bounds(source_content, entity_name)
                if bounds is None:
                    errors.append(
                        f"vanilla_only source missing for {map_key}/{ap_check}"
                    )
                    continue
                source_block = source_content[bounds[0]:bounds[1]]
                policy = target_policies.get(entity_name, {})
                vanilla_owner = feedback.get("vanilla_owner")
                owner_is_secret = False
                if vanilla_owner:
                    owner_bounds = find_entity_block_bounds(
                        source_content, vanilla_owner
                    )
                    if owner_bounds is not None:
                        owner_block = source_content[
                            owner_bounds[0]:owner_bounds[1]
                        ]
                        owner_is_secret = (
                            'inherit = "target/secret";' in owner_block
                        )
                if (
                    "pickups/secret_item" not in source_block
                    and "native_entity_contract" not in policy
                    and not owner_is_secret
                ):
                    errors.append(
                        f"vanilla_only lacks reviewed vanilla feedback owner: "
                        f"{map_key}/{ap_check}"
                    )
        except ValueError as exc:
            errors.append(f"Target-policy validation failed for {map_key}: {exc}")
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
    manifest_location_id_set = set(manifest_location_ids)
    missing_locations = sorted(manifest_location_id_set - set(location_ids.values()))
    unmanifested_locations = sorted(
        set(location_ids.values()) - manifest_location_id_set - runtime_locations
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
    overlap = runtime_locations & manifest_location_id_set
    if overlap:
        errors.append(f"Runtime location IDs also present in map manifests: {sorted(overlap)}")
    reused_manifest_ids = sorted(reserved_location_ids & manifest_location_id_set)
    if reused_manifest_ids:
        errors.append(f"Reserved location IDs remain in manifests: {reused_manifest_ids}")

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
    registry = load_primitive_registry()
    contracts = load_foundation_contracts()
    try:
        validate_primitive_registry(registry)
    except ValueError as exc:
        errors.append(f"Foundation primitive registry is invalid: {exc}")
    if contracts.get("counts") != {
        "items": len(item_ids),
        "locations": 369,
        "map_checks": 307,
        "runtime_locations": 62,
        "runtime_goals": 1,
        "route_sentinel_batteries": 18,
    }:
        errors.append("Foundation frozen counts changed")
    try:
        plans = compile_all_item_plans(commands)
    except ValueError as exc:
        errors.append(f"Item delivery plan compilation failed: {exc}")
        plans = []
    if len(plans) != len(commands):
        errors.append(f"Expected {len(commands)} compiled item plans, found {len(plans)}")

    generated_bootstrap = generate_bootstrap_entities()
    if generated_bootstrap or any(prefix in generated_bootstrap for prefix in BOOTSTRAP_ENTITY_PREFIXES):
        errors.append("Rejected stat-write bootstrap entities reappeared")
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

    # Load item names for notification entity validation
    try:
        item_names_path = ROOT / "data" / "item_replay_policies.json"
        with open(item_names_path, encoding="utf-8") as f:
            policies_data = json.load(f)
        item_names = {int(k): v.get("name", "") for k, v in policies_data.get("items", {}).items()}
    except (FileNotFoundError, json.JSONDecodeError):
        item_names = None

    generated_real_commands = generate_rpc_command_entities(commands, item_names=item_names)
    if "give armor -200" in json.dumps(commands, sort_keys=True) or "give armor -200" in generated_real_commands:
        errors.append("Armor Drain Trap command reappeared")
    if "CURRENCY_WEAPON_MASTERY" in generated_real_commands:
        errors.append("Generated item entities grant forbidden CURRENCY_WEAPON_MASTERY")
    battery_chain = (
        f'entityDef {RPC_ENTITY_PREFIX}_7770016 {{',
        'class = "idTarget_GiveItems";',
        'currencyType = "CURRENCY_SENTINEL_BATTERY";',
        "count = 1;",
    )
    if not all(fragment in generated_real_commands for fragment in battery_chain):
        errors.append("Sentinel Battery lacks the restored direct currency primitive")
    battery_bundle_chain = (
        f'entityDef {RPC_ENTITY_PREFIX}_7770142 {{',
        'class = "idTarget_GiveItems";',
        'currencyType = "CURRENCY_SENTINEL_BATTERY";',
        "count = 2;",
    )
    if not all(fragment in generated_real_commands for fragment in battery_bundle_chain):
        errors.append("Sentinel Battery Bundle must use direct map-side currency count 2")
    if "CURRENCY_WEAPON_UPGRADE" in generated_real_commands:
        errors.append("Weapon Point currency command entered the deferred 0.3.0 economy")
    native_hook_terms = (
        "WriteProcessMemory", "VirtualProtectEx", "VirtualAllocEx",
        "CreateRemoteThread", "MH_CreateHook", "DetourAttach",
    )
    native_runtime_source = "\n".join(
        (ROOT / "native" / "client" / name).read_text(encoding="utf-8", errors="ignore")
        for name in (
            "ap_client_exe.cpp",
            "game_state_probe.cpp",
            "game_state_probe.h",
            "ap_runtime_rpc_client.cpp",
            "ap_runtime_rpc_client.h",
            "ap_runtime_rpc_seh.c",
            "ap_runtime_rpc_seh.h",
        )
    )
    rpc_idl_path = ROOT / "native" / "client" / "ap_runtime_rpc.idl"
    rpc_idl = rpc_idl_path.read_text(encoding="utf-8", errors="ignore")
    normalized_idl = re.sub(r"\s+", " ", rpc_idl).strip()
    if any(fragment not in normalized_idl for fragment in
           ("1c9ca7c8-d421-482d-b85d-79fac33b2658", "version(1.0)",
            "implicit_handle(handle_t ap_runtime_rpc__MIDL_AutoBindHandle)")):
        errors.append("AP runtime RPC IDL is missing required interface metadata")
    if "explicit_handle" in normalized_idl:
        errors.append("AP runtime RPC IDL must use implicit binding")
    exact_rpc_declarations = (
        "void ap_execute( [in, string] unsigned char* command);",
        "void ap_request_entities( [in, string] unsigned char* path, [in] boolean begin, [in] int size);",
        "void ap_upload_chunk( [in] int size, [in] int offset, [in, size_is(size)] unsigned char* data);",
        "void ap_retrieve_entities( [in, out] int* size, [out, size_is(*size)] unsigned char* data);",
        "void ap_retrieve_encounter( [in, out] int* size, [out, size_is(*size)] unsigned char* data);",
        "void ap_retrieve_checkpoint( [in, out] int* size, [out, size_is(*size)] unsigned char* data);",
        "void ap_retrieve_spawn( [in, out] int* size, [out, size_is(*size)] unsigned char* data);",
        "void ap_health( [in, out] int* state);",
    )
    expected_interface_body = "interface ap_runtime_rpc { " + " ".join(exact_rpc_declarations) + " }"
    interface_match = re.search(r"interface ap_runtime_rpc \{ (.*) \}", normalized_idl)
    if interface_match is None or "interface ap_runtime_rpc { " + interface_match.group(1) + " }" != expected_interface_body:
        errors.append("AP runtime RPC IDL interface does not match the exact eight-operation contract")
    wrapper_source = (ROOT / "native" / "client" / "ap_runtime_rpc_client.cpp").read_text(encoding="utf-8")
    if "ncacn_np" not in wrapper_source or r"\\pipe\\meathook_interface_rpc" not in wrapper_source:
        errors.append("AP runtime RPC wrapper is missing private transport constants")
    seh_source = (ROOT / "native" / "client" / "ap_runtime_rpc_seh.c").read_text(encoding="utf-8")
    if (
        "ApRpcSetImplicitBinding(binding)" not in seh_source
        or "ApRpcClearImplicitBinding()" not in seh_source
        or "RpcTryExcept" not in seh_source
        or "ap_execute(command)" not in seh_source
    ):
        errors.append("AP runtime RPC SEH wrapper does not enforce implicit binding ABI")
    forbidden_paths = [ROOT / name for name in
                       ("meathook_interface.h", "meathook_interface_c.c", "mhclient.h", "mhclient.cpp",
                        "native/client/meathook_interface.h", "native/client/meathook_interface_c.c",
                        "native/client/mhclient.h", "native/client/mhclient.cpp")]
    if any(path.exists() for path in forbidden_paths):
        errors.append("Forbidden native RPC path still exists")
    tracked_native = list((ROOT / "native").rglob("*"))
    generated_markers = ("ALWAYS GENERATED", "MIDL compiler version", "@@MIDL_FILE_HEADING")
    for path in tracked_native:
        if path.is_file() and path.suffix in (".c", ".h", ".cpp"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if any(marker in text for marker in generated_markers):
                errors.append(f"Generated RPC marker found in tracked native source: {path}")
            if (path.name.endswith("_c.c") or path.name == "ap_runtime_rpc.h") and path.name != "ap_runtime_rpc_seh.h":
                errors.append(f"Generated RPC artifact found under native: {path}")
    if (ROOT / ".git").exists():
        tracked = subprocess.run(["git", "ls-files", "native"], cwd=ROOT, capture_output=True, text=True, check=False)
        if tracked.returncode == 0 and any(
            (ROOT / line).exists() and ("generated-rpc" in line or line.endswith("_c.c"))
            for line in tracked.stdout.splitlines()
        ):
            errors.append("Generated RPC build artifact is tracked")
    for term in native_hook_terms:
        if term in native_runtime_source:
            errors.append(f"Forbidden in-process/remote hook primitive entered runtime: {term}")
    if (
        'inherit = "target/give_item";' in generated_real_commands
        or 'inherit = "target/player_stat_modifier";' in generated_real_commands
    ):
        errors.append("Generated item entities contain a rejected primitive")
    for item_id, command_value in commands.items():
        if isinstance(command_value, str):
            if re.search(r"sharedammopool/(?:fuel|bfg)\s+0(?:\s|$)", command_value):
                errors.append(f"Drain trap {item_id} must not use a zero amount")
            if (
                command_requires_map_side_rpc(command_value)
                and f"entityDef {RPC_ENTITY_PREFIX}_{item_id} {{" not in generated_real_commands
            ):
                errors.append(
                    f"Item command {item_id} lacks map-side RPC entity"
                )
        elif isinstance(command_value, list):
            for command_index, command in enumerate(command_value):
                if re.search(r"sharedammopool/(?:fuel|bfg)\s+0(?:\s|$)", command):
                    errors.append(
                        f"Drain trap {item_id}[{command_index}] must not use a zero amount"
                    )
                if (
                    command_requires_map_side_rpc(command)
                    and f"entityDef {RPC_ENTITY_PREFIX}_{item_id}_{command_index} {{"
                    not in generated_real_commands
                ):
                    errors.append(
                        f"Item command {item_id}[{command_index}] lacks "
                        "map-side RPC entity"
                    )

    relay = generate_target_relay("AP_CHECK_VALIDATION", 7770999, "")
    secret_relay = generate_event_relay(
        "AP_CHECK_SECRET_VALIDATION", 7770998, "", include_notification=False
    )
    event = generate_check_event(7770999)
    notification = generate_pickup_notification(7770999)
    if (
        'item[0] = "ap_notify_location_7770999";' not in relay
        or f'item[1] = "{EVENT_ENTITY_PREFIX}7770999";' not in relay
        or 'class = "idTarget_Notification";' not in notification
        or 'notificationType = "HUD_NOTIFY_CODEX_RECIEVED";' not in notification
        or 'header = "#str_ap_location_sent";' not in notification
        or 'subtext = "#str_ap_location_7770999";' not in notification
    ):
        errors.append("AP checks are not connected to Codex location notifications")
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
        or "ap_notify_location_7770998" in secret_relay
    ):
        errors.append("Secret encounter checks do not use the validated event-only relay")

    for item_id, command_value in commands.items():
        if isinstance(command_value, dict):
            command_type = command_value.get("type")
            if command_type == "no_op":
                continue
            if command_type in {"progressive_perk", "progressive_item"}:
                perks = command_value.get("perks")
                stage_effects = []
                if isinstance(perks, list):
                    for stage in perks:
                        stage_effects.extend([stage] if isinstance(stage, str) else stage if isinstance(stage, list) else [])
                if (
                    not isinstance(perks, list)
                    or not perks
                    or not all(
                        isinstance(perk, str)
                        and (perk.startswith("perk/player/") or perk.startswith("weapon/player/"))
                        for perk in stage_effects
                    )
                    or not stage_effects
                ):
                    errors.append(
                        f"Progressive command {item_id} must define "
                        "player perk or weapon stages"
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
        f"{len(location_ids)} locations, {len(manifest_location_ids)} map checks, "
        f"{len(enabled_map_sources)} enabled map sources, "
        f"and {len(runtime_locations)} runtime checks."
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
