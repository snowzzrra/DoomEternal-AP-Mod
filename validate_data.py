#!/usr/bin/env python3
"""Validate APWorld IDs, bridge commands, level configs, and manifests."""

from __future__ import annotations

import ast
import json
import re
import sys
import tempfile
from pathlib import Path

from ap_map_generator import (
    EVENT_ENTITY_PREFIX,
    RPC_ENTITY_PREFIX,
    command_requires_map_side_rpc,
    generate_bootstrap_entities,
    generate_check_event,
    generate_event_relay,
    generate_pickup_notification,
    generate_rpc_command_entities,
    generate_target_relay,
    find_entity_block_bounds,
    extract_target_names,
    generate_map,
    validate_target_policies,
)
from automap_baseline_guard import assert_separate_automap_helper_guard
from bootstrap_actions import BOOTSTRAP_ENTITY_PREFIXES
from foundation import (
    compile_all_item_plans,
    load_foundation_contracts,
    load_primitive_registry,
    validate_entity_shape,
    validate_primitive_registry,
)
from challenge_registry import all_location_entries, load_challenge_registry


ROOT = Path(__file__).resolve().parent
APWORLD = ROOT.parent / "Archipelago" / "worlds" / "doometernal"
MAP_SOURCES_PATH = ROOT / "data" / "map_sources.json"
AUTOMAP_FAMILY_REGISTRY_PATH = ROOT / "data" / "automap_family_registry.json"
BATTERY_LOCATIONS = {
    "Exultia - Sentinel Battery": 7770084,
    "Cultist Base - Sentinel Battery 1": 7770057,
    "Cultist Base - Sentinel Battery 2": 7770069,
    "Cultist Base - Sentinel Battery 3": 7770070,
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
                for key, value in zip(node.value.keys, node.value.values)
                if ast.literal_eval(value.args[0]) is not None
            }
    raise RuntimeError(f"Could not find {variable} in {path}")


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

    map_sources = read_json(MAP_SOURCES_PATH).get("maps", {})
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

    for location_id in runtime_locations:
        family_name = exact_families.get(location_id)
        if family_name not in {"runtime_mission", "runtime_mastery", "runtime_challenge"}:
            errors.append(f"Runtime location {location_id} lacks exact Automap family")
        else:
            classified[location_id] = family_name

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
                    if 'model = "art/pickups/question_mark_a.lwo";' not in carrier:
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
                    generated_bounds = find_entity_block_bounds(generated, entity_name)
                    if generated_bounds is None:
                        errors.append(f"Praetor token missing for {location_id}")
                        continue
                    token = generated[generated_bounds[0]:generated_bounds[1]]
                    if "currencyList" in token or "CURRENCY_PRAETOR_UPGRADE" in token:
                        errors.append(f"Praetor reward retained for {location_id}")
                    if 'model = "art/pickups/question_mark_a.lwo";' not in token:
                        errors.append(f"Praetor token lacks AP visual for {location_id}")
                    if extract_target_names(token) != [*extract_target_names(source_block), ap_check]:
                        errors.append(f"Praetor functional target drift for {location_id}")
                    for field in ("inherit", "class", "automapPropertiesDecl", "progressionCategory"):
                        if entity_scalar(token, field) != entity_scalar(source_block, field):
                            errors.append(f"Praetor marker/category drift for {location_id}/{field}")
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
            if visual.get("automap_properties_decl") and entity_name != (
                "mech_street_progress_mod_bot_1_e1m1"
            ):
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
                } or "praetor_token" in entity_name or config.get(
                    "target_policies", {}
                ).get(entity_name, {}).get("independent_visual") or config.get(
                    "target_policies", {}
                ).get(entity_name, {}).get("native_entity_contract"):
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

            if map_key != "e1m1_intro":
                continue

            visual_name = "ap_location_visual_7770015"
            cleanup_name = "ap_remove_location_visual_7770015"
            visual_bounds = find_entity_block_bounds(generated, visual_name)
            cleanup_bounds = find_entity_block_bounds(generated, cleanup_name)
            check_bounds = find_entity_block_bounds(
                generated, "AP_CHECK_MECH_STREET_PROGRESS_MOD_BOT_1_E1M1"
            )
            if not all((visual_bounds, cleanup_bounds, check_bounds)):
                errors.append("Modbot generic Automap prototype graph is incomplete")
            else:
                visual = generated[visual_bounds[0]:visual_bounds[1]]
                cleanup = generated[cleanup_bounds[0]:cleanup_bounds[1]]
                check = generated[check_bounds[0]:check_bounds[1]]
                if any(term in visual for term in (
                    "fxDecl", "useableComponentDecl", "currency",
                    "inventory", "perk", "targets",
                )):
                    errors.append("Modbot visual has a forbidden gameplay or FX edge")
                if extract_target_names(cleanup) != [visual_name]:
                    errors.append("Modbot cleanup target reaches outside the prototype visual")
                if extract_target_names(check) != [
                    cleanup_name,
                    "ap_notify_AP_CHECK_MECH_STREET_PROGRESS_MOD_BOT_1_E1M1",
                    "ap_event_7770015",
                ]:
                    errors.append("Modbot AP check target graph drifted")
    return errors


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []

    item_ids = extract_namedtuple_table(APWORLD / "items.py", "item_data_table")
    location_ids = extract_namedtuple_table(APWORLD / "locations.py", "location_data_table")
    reserved_item_ids = extract_frozenset_constant(APWORLD / "items.py", "RESERVED_ITEM_IDS")
    reserved_location_ids = {7770055, 7770068}
    reused_location_ids = sorted(reserved_location_ids & set(location_ids.values()))
    if reused_location_ids:
        errors.append(f"Reserved location IDs must not be reused: {reused_location_ids}")
    commands = {int(key): value for key, value in read_json(ROOT / "data" / "items.json").items()}
    if {name: location_ids.get(name) for name in BATTERY_LOCATIONS} != BATTERY_LOCATIONS:
        errors.append("Four physical Sentinel Battery AP locations must remain active")
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
    mastery_location_names = [
        name for name in location_ids
        if "Weapon Mastery Challenge" in name
    ]
    expected_mastery_location_names = [entry["name"] for entry in mastery_entries]
    if mastery_location_names != expected_mastery_location_names:
        errors.append(f"Base Mastery registry/APWorld drift: {mastery_location_names}")
    mission_challenge_location_names = [
        name for name in location_ids
        if "Mission Challenge -" in name
        or name == challenge_registry["all_mission_challenges"]["name"]
    ]
    expected_mission_challenge_names = [
        *[entry["name"] for entry in challenge_registry["mission_challenges"]],
        challenge_registry["all_mission_challenges"]["name"],
    ]
    if mission_challenge_location_names != expected_mission_challenge_names:
        errors.append(
            "Cultist Mission Challenge registry/APWorld drift: "
            f"{mission_challenge_location_names}"
        )
    aggregate_entry = challenge_registry["all_mission_challenges"]
    if location_ids.get(aggregate_entry["name"]) != aggregate_entry["location_id"]:
        errors.append("All Mission Challenges aggregate/APWorld mapping drift")
    source_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "bridge_client.py", ROOT / "ap_map_generator.py", ROOT / "challenge_registry.py")
    )
    for forbidden in (
        "append_graph_entries", "watchers_for_map", "AP_RUNTIME_CHECK_",
        "3_900_000_000", "3_800_000_000", "give armor -200",
    ):
        if forbidden in source_text:
            errors.append(f"Rejected watcher/Armor Drain source returned: {forbidden}")
    map_sources = read_json(MAP_SOURCES_PATH).get("maps", {})

    forbidden_decl_path = "propitem/ap/"
    for path in (
        ROOT / "level_configs" / "hub.json",
        ROOT / "level_configs" / "e1m3_cult.json",
        ROOT / "ap_map_generator.py",
    ):
        if forbidden_decl_path in path.read_text(encoding="utf-8"):
            errors.append(f"Scripted pickup uses forbidden custom DECL path: {path}")
    for path in (ROOT / "packaging" / "mod_assets").rglob("*"):
        if path.is_file() and forbidden_decl_path in path.as_posix():
            errors.append(f"Forbidden custom scripted-pickup DECL is packaged: {path}")

    forbidden_decl_overrides = (
        ROOT / "packaging" / "mod_assets" / "hub_patch2" / "generated" /
        "decls" / "propitem" / "propitem" / "equipment" / "ice_bomb.decl",
        ROOT / "packaging" / "mod_assets" / "e1m3_cult_patch3" / "generated" /
        "decls" / "propitem" / "propitem" / "weapon" /
        "rocket_launcher" / "base.decl",
    )
    for override_path in forbidden_decl_overrides:
        if override_path.exists():
            errors.append(f"Forbidden scripted-pickup DECL override remains packaged: {override_path}")

    manifests: dict[str, int] = {}
    for path in sorted((ROOT / "manifests").glob("*.json")):
        for declaration, location_id in read_json(path).items():
            if declaration in manifests:
                errors.append(f"Duplicate manifest declaration: {declaration}")
            if location_id in manifests.values():
                errors.append(f"Duplicate manifest location ID: {location_id}")
            manifests[declaration] = location_id
    for location_id in BATTERY_LOCATIONS.values():
        if list(manifests.values()).count(location_id) != 1:
            errors.append(f"Physical Sentinel Battery {location_id} must have one active manifest check")

    physical_location_count = 0
    for path in sorted((ROOT / "level_configs").glob("*.json")):
        config_data = read_json(path)
        if config_data.get("map_key") != path.stem:
            errors.append(f"Missing or divergent map_key in {path.name}")
        config = dict(config_data.get("entities", {}))
        physical_location_count += len(config)
        reused_config_ids = sorted(reserved_location_ids & set(config.values()))
        if reused_config_ids:
            errors.append(f"Reserved location IDs remain in {path.name}: {reused_config_ids}")
        for encounter in config_data.get("secret_encounters", []):
            config[encounter["ap_check"]] = encounter["location_id"]
        manifest_path = ROOT / "manifests" / path.name
        if not manifest_path.exists():
            errors.append(f"Missing manifest for {path.name}")
            continue
        manifest = read_json(manifest_path)
        if config != manifest:
            errors.append(f"Config/manifest mismatch: {path.name}")
    if physical_location_count != 76:
        errors.append(
            f"Expected 76 physical locations after the reused Suit check, found {physical_location_count}"
        )

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
        config_path = ROOT / source["level_config"]
        source_path = ROOT / "vanillamaps" / source["source_file"]
        try:
            config_data = read_json(config_path)
            validate_target_policies(
                config_data.get("entities", {}),
                config_data.get("target_policies", {}),
                source_path.read_text(encoding="utf-8"),
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
    reused_manifest_ids = sorted(reserved_location_ids & set(manifests.values()))
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
        "items": 116,
        "locations": 100,
        "map_checks": 80,
        "runtime_locations": 20,
        "runtime_goals": 1,
        "route_sentinel_batteries": 5,
    }:
        errors.append("Foundation frozen counts changed")
    try:
        plans = compile_all_item_plans(commands)
    except ValueError as exc:
        errors.append(f"Item delivery plan compilation failed: {exc}")
        plans = []
    if len(plans) != 116:
        errors.append(f"Expected 116 compiled item plans, found {len(plans)}")

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

    generated_real_commands = generate_rpc_command_entities(commands)
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
        (ROOT / name).read_text(encoding="utf-8", errors="ignore")
        for name in ("ap_client_exe.cpp", "game_state_probe.cpp", "game_state_probe.h", "mhclient.cpp", "mhclient.h")
    )
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
