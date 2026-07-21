#!/usr/bin/env python3
"""Small textual reference auditor for scripted map locations."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

ENTITY_NAME = re.compile(r"entityDef\s+([A-Za-z0-9_]+)\s*\{")
QUOTED = re.compile(r'"([^"\r\n]+)"')


def entity_blocks(text: str):
    for match in ENTITY_NAME.finditer(text):
        start = match.start()
        depth = 0
        opened = False
        for index in range(match.end() - 1, len(text)):
            if text[index] == "{":
                depth += 1
                opened = True
            elif text[index] == "}":
                depth -= 1
                if opened and depth == 0:
                    yield match.group(1), text[start:index + 1]
                    break


def classify(block: str) -> list[str]:
    classes = []
    checks = {
        "timeline": r"timeline|show|hide|remove|enable",
        "layer_checkpoint_start": r"layer|checkpoint|player_start|spawnSpot",
        "stat_ownership_reader": r"InventoryCheck|inventory|gameStat|canBePossessed|ownership|throwable/",
        "grant": r"GiveItems|give_item|grant|reward|useableComponentDecl|equipOnPickup",
        "edge_sensitive": r"repeat\s*=\s*true|toggle|triggerOnce|OnActivated|TriggerReceive",
        "delay": r"\bdelay\s*=|LogicNodeModelDelay",
        "poi_objective": r"objective|\bpoi\b|POI",
    }
    for name, pattern in checks.items():
        if re.search(pattern, block, re.IGNORECASE):
            classes.append(name)
    return classes or ["other"]


def audit(map_path: Path, selected: str, decl_roots: list[Path]) -> dict:
    text = map_path.read_text(encoding="utf-8", errors="replace")
    blocks = dict(entity_blocks(text))
    direct = sorted(set(QUOTED.findall(blocks.get(selected, ""))))
    direct = [name for name in direct if name in blocks and name != selected]
    reverse = []
    for name, block in blocks.items():
        if name != selected and f'"{selected}"' in block:
            reverse.append({"entity": name, "classes": classify(block)})
    decl_refs = []
    for root in decl_roots:
        paths = [root] if root.is_file() else sorted(root.rglob("*.decl"))
        for path in paths:
            body = path.read_text(encoding="utf-8", errors="replace")
            if selected in body:
                decl_refs.append({
                    "path": path.name,
                    "classes": classify(body),
                    "mentions": body.count(selected),
                })
    return {
        "entity": selected,
        "direct_targets": direct,
        "reverse_entities": sorted(reverse, key=lambda item: item["entity"]),
        "logic_decl_refs": sorted(decl_refs, key=lambda item: item["path"]),
        "selected_classes": classify(blocks.get(selected, "")),
    }


def validate_contracts(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    root = path.parent.parent
    errors = []
    for location_id, contract in sorted(data["locations"].items()):
        if contract["kind"] not in {"simple", "independent", "scripted"}:
            errors.append(f"{location_id}: unknown kind")
        if contract.get("strategy") not in {
            "direct_targets", "independent_trigger", "logic_decl_patch"
        }:
            errors.append(f"{location_id}: unknown scripted-location strategy")
        classified = contract.get("classified_external_references", {})
        for reference in contract.get("external_references", []):
            if reference not in classified:
                errors.append(f"{location_id}: unclassified external reference {reference}")
        if contract["kind"] in {"simple", "independent"} and any(
            value == "unclassified" for value in classified.values()
        ):
            errors.append(f"{location_id}: unclassified external reference")
        config_ref = contract.get("config")
        policy_name = contract.get("policy")
        if config_ref and policy_name:
            config = json.loads((root / config_ref).read_text(encoding="utf-8"))
            trigger = contract.get("trigger")
            if trigger and (
                config["target_policies"][policy_name].get("independent_position") != trigger["position"]
                or config["target_policies"][policy_name].get("independent_size") != trigger["size"]
            ):
                errors.append(f"{location_id}: config trigger geometry does not match contract")
            visual = contract.get("visual")
            configured_visual = config["target_policies"][policy_name].get("independent_visual")
            if visual and (
                not configured_visual
                or configured_visual.get("entity_name") != visual["entity"]
                or configured_visual.get("model") != visual["model"]
                or configured_visual.get("position") != visual["position"]
                or configured_visual.get("scale") != visual["scale"]
                or configured_visual.get("cleanup_entity")
                != contract.get("cleanup", {}).get("entity")
            ):
                errors.append(f"{location_id}: config visual does not match contract")
        architecture = contract.get("post_ice_architecture")
        if architecture is not None and (
            architecture.get("owner") != "Restore Ship Power / info_logic_hub_from_e1m2"
            or architecture.get("entry_count") != 1
            or architecture.get("location_role") != "check_only"
        ):
            errors.append(f"{location_id}: invalid post-Ice ownership contract")
    return errors


def verify_generated_location(contract_path: Path, map_path: Path, location_id: str) -> list[str]:
    data = json.loads(contract_path.read_text(encoding="utf-8"))
    contract = data["locations"][location_id]
    blocks = dict(entity_blocks(map_path.read_text(encoding="utf-8", errors="replace")))
    entrypoint = contract["entrypoint"]
    block = blocks.get(entrypoint)
    if block is None:
        return [f"{location_id}: generated entrypoint is missing: {entrypoint}"]
    errors = []
    targets = extract_targets(block)
    expected = contract.get("direct_targets")
    if expected is not None and targets != expected:
        errors.append(f"{location_id}: generated targets {targets} do not match {expected}")
    ap_checks = [target for target in targets if target.startswith("AP_CHECK_")]
    if len(ap_checks) != 1:
        errors.append(f"{location_id}: expected exactly one AP check, found {ap_checks}")
    forbidden = sorted(
        set(targets) & set(contract.get("forbidden_generated_references", []))
    )
    if forbidden:
        errors.append(f"{location_id}: generated entrypoint reaches forbidden target(s) {forbidden}")
    terms = [term for term in contract.get("forbidden_terms", []) if term in block]
    if terms:
        errors.append(f"{location_id}: generated entrypoint contains forbidden term(s) {terms}")
    expected_hash = contract.get("generated_entrypoint_sha256")
    actual_hash = hashlib.sha256(block.encode()).hexdigest()
    if expected_hash and actual_hash != expected_hash:
        errors.append(
            f"{location_id}: generated entrypoint hash drift: {actual_hash} != {expected_hash}"
        )
    trigger = contract.get("trigger")
    if trigger:
        if extract_vector(block, "spawnPosition") != trigger["position"]:
            errors.append(f"{location_id}: trigger position drift")
        if extract_vector(block, "size") != trigger["size"]:
            errors.append(f"{location_id}: trigger volume drift")
        if trigger.get("one_shot") and "triggerOnce = true;" not in block:
            errors.append(f"{location_id}: trigger is not one-shot")
    visual = contract.get("visual")
    if visual:
        visual_block = blocks.get(visual["entity"])
        if visual_block is None:
            errors.append(f"{location_id}: visual is missing: {visual['entity']}")
        else:
            expected_visual_hash = visual.get("generated_sha256")
            actual_visual_hash = hashlib.sha256(visual_block.encode()).hexdigest()
            if expected_visual_hash and actual_visual_hash != expected_visual_hash:
                errors.append(
                    f"{location_id}: visual hash drift: "
                    f"{actual_visual_hash} != {expected_visual_hash}"
                )
            expected_fragments = (
                f'inherit = "{visual["inherit"]}";',
                f'class = "{visual["class"]}";',
                f'model = "{visual["model"]}";',
                f'type = "{visual["clip_type"]}";',
                "networkReplicated = false;",
                "contributesToLightProbeGen = false;",
            )
            for fragment in expected_fragments:
                if fragment not in visual_block:
                    errors.append(f"{location_id}: visual missing {fragment}")
            if extract_vector(visual_block, "spawnPosition") != visual["position"]:
                errors.append(f"{location_id}: visual position drift")
            if extract_vector(visual_block, "scale") != visual["scale"]:
                errors.append(f"{location_id}: visual scale drift")
            if extract_targets(visual_block) != visual.get("targets", []):
                errors.append(f"{location_id}: visual has functional targets")
            forbidden_visual = (
                "bindInfo", "automapPropertiesDecl", "useableComponentDecl",
                "equipOnPickup", "canBePossessed", "reward", "currency",
                "inventory", "throwable/", "target_relay", "target_give_item",
                "objective", "checkpoint", "timeline", "itemList",
            )
            present = [term for term in forbidden_visual if term.lower() in visual_block.lower()]
            if present:
                errors.append(f"{location_id}: visual contains forbidden term(s) {present}")
    cleanup = contract.get("cleanup")
    if cleanup:
        cleanup_block = blocks.get(cleanup["entity"])
        if cleanup_block is None:
            errors.append(f"{location_id}: cleanup is missing: {cleanup['entity']}")
        else:
            if targets[-1:] != [cleanup["entity"]]:
                errors.append(f"{location_id}: cleanup is not the last trigger target")
            if extract_targets(cleanup_block) != cleanup["targets"]:
                errors.append(f"{location_id}: cleanup targets functional or unexpected entity")
            for fragment in (
                f'inherit = "{cleanup["inherit"]}";',
                f'class = "{cleanup["class"]}";',
            ):
                if fragment not in cleanup_block:
                    errors.append(f"{location_id}: cleanup missing {fragment}")
            reached = set(extract_targets(cleanup_block))
            for target in tuple(reached):
                reached.update(extract_targets(blocks.get(target, "")))
            forbidden_reached = sorted(
                reached & set(contract.get("forbidden_cleanup_references", []))
            )
            if forbidden_reached:
                errors.append(
                    f"{location_id}: cleanup reaches functional entities {forbidden_reached}"
                )
    return errors


def extract_vector(block: str, property_name: str) -> list[float] | None:
    match = re.search(
        rf"{re.escape(property_name)}\s*=\s*\{{\s*"
        r"x\s*=\s*([-+0-9.eE]+);\s*"
        r"y\s*=\s*([-+0-9.eE]+);\s*"
        r"z\s*=\s*([-+0-9.eE]+);\s*\}",
        block,
    )
    return [float(value) for value in match.groups()] if match else None


def extract_targets(block: str) -> list[str]:
    target_match = re.search(r"targets\s*=\s*\{.*?\}", block, re.DOTALL)
    return QUOTED.findall(target_match.group(0)) if target_match else []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--map", type=Path)
    parser.add_argument("--entity")
    parser.add_argument("--decl-root", action="append", type=Path, default=[])
    parser.add_argument("--contracts", type=Path)
    parser.add_argument("--verify-generated-map", type=Path)
    parser.add_argument("--location")
    args = parser.parse_args()
    if args.contracts:
        errors = validate_contracts(args.contracts)
        if args.verify_generated_map:
            if not args.location:
                parser.error("--verify-generated-map requires --location")
            errors.extend(
                verify_generated_location(
                    args.contracts, args.verify_generated_map, args.location
                )
            )
        if errors:
            print("\n".join(errors))
            return 1
        print("scripted location contracts: OK")
        return 0
    print(json.dumps(audit(args.map, args.entity, args.decl_root), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
