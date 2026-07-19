#!/usr/bin/env python3
"""Fail-closed terminal map hooks for Hell on Earth and Exultia checks."""

from __future__ import annotations

import hashlib
import json
import re
import argparse
from pathlib import Path

from ap_map_generator import (
    extract_target_names,
    find_entity_block_bounds,
    generate_check_event,
    generate_event_relay,
    replace_targets_block,
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _matching_brace(text: str, opening: int) -> int:
    depth = 0
    for index in range(opening, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    raise ValueError("unbalanced DECL braces")


def _target_list(text: str, context: str) -> list[str]:
    count = re.search(r"\bnum\s*=\s*(\d+);", text)
    if not count:
        raise ValueError(f"{context}: target list has no count")
    targets = re.findall(r'item\[\d+\]\s*=\s*"([^"]+)";', text)
    if len(targets) != int(count.group(1)):
        raise ValueError(f"{context}: target count drift")
    return targets


def _hell_node_value_bounds(text: str, node_id: int) -> tuple[int, int]:
    marker = f"id = {node_id};"
    if text.count(marker) != 1:
        raise ValueError("Hell native owner node is missing or duplicated")
    node_marker = text.index(marker)
    node_start = text.rfind("item[", 0, node_marker)
    if node_start < 0:
        raise ValueError("Hell native owner node container is missing")
    node_opening = text.find("{", node_start, node_marker)
    node_end = _matching_brace(text, node_opening)
    node = text[node_start:node_end]
    if "className = \"idLogicNodeModelEntityActivate\";" not in node:
        raise ValueError("Hell native owner node class drift")
    value_match = re.search(r"\bvalue\s*=\s*\{", node)
    if not value_match or len(re.findall(r"\bvalue\s*=\s*\{", node)) != 1:
        raise ValueError("Hell native owner target list is missing or duplicated")
    start = node_start + value_match.start()
    opening = node_start + value_match.end() - 1
    return start, _matching_brace(text, opening)


def _render_hell_target_list(original: str, targets: list[str]) -> str:
    newline = "\r\n" if "\r\n" in original else "\n"
    value_match = re.match(r"(\s*)value\s*=\s*\{", original)
    if not value_match:
        raise ValueError("Hell target list formatting drift")
    indent = value_match.group(1)
    child = indent + "\t"
    item = child + "\t"
    return (
        f"{indent}value = {{{newline}"
        f"{child}num = {len(targets)};{newline}"
        + "".join(
            f'{item}item[{index}] = "{target}";{newline}'
            for index, target in enumerate(targets)
        )
        + f"{child}}}"
    )


def _patch_hell(contract: dict, root: Path, mod_root: Path) -> dict:
    source = root / contract["source_path"]
    raw = source.read_bytes()
    source_sha = _sha256(raw)
    if source_sha != contract["source_sha256"]:
        raise ValueError(f"Hell source hash mismatch: {source}")
    text = raw.decode("utf-8")
    start, end = _hell_node_value_bounds(text, contract["node_id"])
    before = _target_list(text[start:end], "Hell native owner")
    if before != contract["original_targets"]:
        raise ValueError(f"Hell native owner target drift: {before}")
    after = [contract["ap_check"], *before]
    result = text[:start] + _render_hell_target_list(text[start:end], after) + text[end:]
    if result.count(contract["ap_check"]) != 1:
        raise ValueError("Hell AP target was not inserted exactly once")
    override_raw = result.encode("utf-8")
    override_sha = _sha256(override_raw)
    expected_override = contract["override_sha256"]
    if expected_override and override_sha != expected_override:
        raise ValueError("Hell override hash mismatch")
    output = mod_root / contract["override_path"]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(override_raw)
    return {
        "source_path": contract["source_path"],
        "override_path": contract["override_path"],
        "source_sha256": source_sha,
        "expected_source_sha256": contract["source_sha256"],
        "override_sha256": override_sha,
        "expected_override_sha256": expected_override or override_sha,
        "before_targets": before,
        "after_targets": after,
        "changed_lists": 1,
    }


def _patch_exultia(contract: dict, root: Path, generated_map: Path) -> dict:
    source = (root / contract["source_path"]).read_text(encoding="utf-8")
    source_bounds = find_entity_block_bounds(source, contract["owner"])
    if source_bounds is None or source.count(f"entityDef {contract['owner']}") != 1:
        raise ValueError("Exultia native owner is missing or duplicated in source")
    source_block = source[source_bounds[0]:source_bounds[1]]
    source_sha = _sha256(source_block.encode("utf-8"))
    expected_source = contract["source_sha256"]
    if expected_source and source_sha != expected_source:
        raise ValueError("Exultia source owner hash mismatch")
    text = generated_map.read_text(encoding="utf-8")
    if text.count(f"entityDef {contract['owner']}") != 1:
        raise ValueError("Exultia native owner is missing or duplicated")
    bounds = find_entity_block_bounds(text, contract["owner"])
    if bounds is None:
        raise ValueError("Exultia native owner is missing")
    block = text[bounds[0]:bounds[1]]
    if _sha256(block.encode("utf-8")) != source_sha:
        raise ValueError("Exultia generated native owner drift")
    for snippet in contract["required_snippets"]:
        if snippet not in block:
            raise ValueError(f"Exultia native owner drift: missing {snippet}")
    before = extract_target_names(block)
    if before != contract["original_targets"]:
        raise ValueError(f"Exultia native owner target drift: {before}")
    after = [contract["ap_check"], *before]
    patched = replace_targets_block(block, after)
    result = text[:bounds[0]] + patched + text[bounds[1]:]
    generated_map.write_text(result, encoding="utf-8", newline="")
    return {
        "source_path": contract["source_path"],
        "source_sha256": source_sha,
        "expected_source_sha256": expected_source or source_sha,
        "before_targets": before,
        "after_targets": after,
        "changed_lists": 1,
    }


def _append_standard_event_target(path: Path, ap_check: str, location_id: int) -> None:
    text = path.read_text(encoding="utf-8")
    if f"entityDef {ap_check}" in text or f"AP_CHECK_EVENT_{location_id}" in text:
        raise ValueError(f"Mission Complete AP target already exists: {ap_check}")
    addition = (
        generate_event_relay(ap_check, location_id, "", include_notification=False)
        + generate_check_event(location_id)
    )
    path.write_text(text.rstrip() + "\n" + addition, encoding="utf-8", newline="")


def _patch_fortress_goal(contract: dict, root: Path, generated_map: Path) -> dict:
    source = (root / contract["source_path"]).read_text(encoding="utf-8")
    source_bounds = find_entity_block_bounds(source, contract["owner"])
    if source_bounds is None or source.count(f"entityDef {contract['owner']}") != 1:
        raise ValueError("Fortress Visit 3 native goal owner is missing or duplicated")
    source_block = source[source_bounds[0]:source_bounds[1]]
    source_sha = _sha256(source_block.encode("utf-8"))
    if source_sha != contract["source_sha256"]:
        raise ValueError("Fortress Visit 3 native goal owner hash mismatch")
    if f'"{contract["required_layer"]}"' not in source_block:
        raise ValueError("Fortress Visit 3 goal owner layer drift")
    if extract_target_names(source_block) != contract["original_targets"]:
        raise ValueError("Fortress Visit 3 goal owner target drift")

    text = generated_map.read_text(encoding="utf-8")
    bounds = find_entity_block_bounds(text, contract["owner"])
    if bounds is None:
        raise ValueError("Fortress Visit 3 generated goal owner is missing")
    block = text[bounds[0]:bounds[1]]
    if _sha256(block.encode("utf-8")) != source_sha:
        raise ValueError("Fortress Visit 3 generated goal owner drift")
    patched = replace_targets_block(block, [contract["goal_target"]])
    event = f'''entity {{
\tentityDef {contract["goal_target"]} {{
\t\tclass = "idTarget_Command";
\t\texpandInheritance = false;
\t\tpoolCount = 0;
\t\tpoolGranularity = 2;
\t\tnetworkReplicated = false;
\t\tdisableAIPooling = false;
\t\tedit = {{
\t\t\tcommandText = "echo AP_GOAL_EVENT_FORTRESS_VISIT_3; condump ap_goal_fortress_visit_3.txt";
\t\t}}
\t}}
}}
'''
    result = text[:bounds[0]] + patched + text[bounds[1]:]
    if result.count(f"entityDef {contract['goal_target']}"):
        raise ValueError("Fortress Visit 3 generated goal target already exists")
    generated_map.write_text(result.rstrip() + "\n" + event, encoding="utf-8", newline="")
    return {
        "source_path": contract["source_path"],
        "source_sha256": source_sha,
        "owner": contract["owner"],
        "layer": contract["required_layer"],
        "before_targets": [],
        "after_targets": [contract["goal_target"]],
        "event_file": contract["goal_event_file"],
        "terminal": contract["terminal"],
        "changed_lists": 1,
    }


def _unrelated_entity_diff_count(before: str, after: str, owners: set[str]) -> int:
    def blocks(text: str) -> dict[str, str]:
        result = {}
        pattern = re.compile(r"\bentity\s*\{\s*entityDef\s+([^\s{]+)")
        for match in pattern.finditer(text):
            opening = text.find("{", match.start())
            end = _matching_brace(text, opening)
            result[match.group(1)] = text[match.start():end]
        return result

    original = blocks(before)
    patched = blocks(after)
    return sum(
        original[name] != patched.get(name)
        for name in original
        if name not in owners
    )


def patch_mission_complete_maps(contract_path: Path, generated_maps: dict[str, Path], mod_root: Path) -> dict:
    contracts = json.loads(contract_path.read_text(encoding="utf-8"))
    if contracts.get("schema_version") != 1:
        raise ValueError("unsupported Mission Complete map contract schema")
    root = contract_path.parent.parent
    hell_contract = contracts["hell_on_earth"]
    exultia_contract = contracts["exultia"]
    doom_hunter_contract = contracts["doom_hunter_base"]
    fortress_goal_contract = contracts["fortress_visit_3_goal"]
    if set(generated_maps) < {
        hell_contract["map_key"], exultia_contract["map_key"],
        doom_hunter_contract["map_key"],
    }:
        raise ValueError("Mission Complete generated map input is incomplete")
    before_maps = {key: path.read_text(encoding="utf-8") for key, path in generated_maps.items()}
    hell = _patch_hell(hell_contract, root, mod_root)
    exultia = _patch_exultia(exultia_contract, root, generated_maps[exultia_contract["map_key"]])
    doom_hunter = _patch_exultia(
        doom_hunter_contract, root,
        generated_maps[doom_hunter_contract["map_key"]],
    )
    fortress_goal = _patch_fortress_goal(
        fortress_goal_contract, root,
        generated_maps[fortress_goal_contract["map_key"]],
    )
    _append_standard_event_target(
        generated_maps[hell_contract["map_key"]], hell_contract["ap_check"], hell_contract["location_id"]
    )
    _append_standard_event_target(
        generated_maps[exultia_contract["map_key"]], exultia_contract["ap_check"], exultia_contract["location_id"]
    )
    _append_standard_event_target(
        generated_maps[doom_hunter_contract["map_key"]],
        doom_hunter_contract["ap_check"], doom_hunter_contract["location_id"],
    )
    for contract, audit in (
        (hell_contract, hell), (exultia_contract, exultia),
        (doom_hunter_contract, doom_hunter),
    ):
        text = generated_maps[contract["map_key"]].read_text(encoding="utf-8")
        expected_ap_target_references = 2 if "owner" in contract else 1
        if text.count(contract["ap_check"]) != expected_ap_target_references:
            raise ValueError(f"{contract['map_key']}: AP target definition/reference count drift")
        if text.count(f"AP_CHECK_EVENT_{contract['location_id']}") != 1:
            raise ValueError(f"{contract['map_key']}: standard AP event count drift")
        audit["event_target"] = f"ap_event_{contract['location_id']}"
        audit["owner_target_references"] = 1
    unrelated = sum(
        _unrelated_entity_diff_count(
            before_maps[key], path.read_text(encoding="utf-8"),
            ({exultia_contract["owner"]} if key == exultia_contract["map_key"] else
             {doom_hunter_contract["owner"]} if key == doom_hunter_contract["map_key"] else set()),
        )
        for key, path in generated_maps.items()
    )
    return {
        "hell_on_earth": hell,
        "exultia": exultia,
        "doom_hunter_base": doom_hunter,
        "fortress_visit_3_goal": fortress_goal,
        "unrelated_generated_entity_diff_count": unrelated,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contracts", type=Path, required=True)
    parser.add_argument("--generated-map", action="append", default=[], metavar="KEY=PATH")
    parser.add_argument("--mod-root", type=Path, required=True)
    parser.add_argument("--audit-output", type=Path)
    args = parser.parse_args()
    generated_maps = {}
    for value in args.generated_map:
        key, separator, path = value.partition("=")
        if not key or not separator or not path:
            raise ValueError(f"invalid --generated-map: {value}")
        generated_maps[key] = Path(path)
    audit = patch_mission_complete_maps(args.contracts, generated_maps, args.mod_root)
    if args.audit_output:
        args.audit_output.parent.mkdir(parents=True, exist_ok=True)
        args.audit_output.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(audit, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
