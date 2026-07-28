#!/usr/bin/env python3
"""Fail-closed terminal map hooks for Hell on Earth and Exultia checks."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from campaign_goal_contract import load_campaign_goal_contract
from publisher_contracts import (
    PublisherContract,
    load_publisher_contracts,
    map_publishers_for_owner,
)
from tools.maps.ap_map_generator import (
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


def _render_relay(name: str, targets: list[str], delay: float) -> str:
    items = "\n".join(
        f'\t\t\t\titem[{index}] = "{target}";'
        for index, target in enumerate(targets)
    )
    return f'''entity {{
\tentityDef {name} {{
\t\tinherit = "target/relay";
\t\tclass = "idTarget_Count";
\t\texpandInheritance = false;
\t\tpoolCount = 0;
\t\tpoolGranularity = 2;
\t\tnetworkReplicated = false;
\t\tdisableAIPooling = false;
\t\tedit = {{
\t\t\tcount = 1;
\t\t\ttargets = {{
\t\t\t\tnum = {len(targets)};
{items}
\t\t\t}}
\t\t\tdelay = {delay:.2f};
\t\t}}
\t}}
}}
'''


def _render_command(name: str, command: str) -> str:
    return f'''entity {{
\tentityDef {name} {{
\t\tclass = "idTarget_Command";
\t\texpandInheritance = false;
\t\tpoolCount = 0;
\t\tpoolGranularity = 2;
\t\tnetworkReplicated = false;
\t\tdisableAIPooling = false;
\t\tedit = {{
\t\t\tcommandText = "{command}";
\t\t}}
\t}}
}}
'''


def compile_publishers(publishers: tuple[PublisherContract, ...]) -> dict:
    """Compile deterministic, isolated marker/dump pipelines for one owner."""
    if not publishers:
        raise ValueError("terminal owner requires at least one publisher")
    ordered = tuple(sorted(publishers, key=lambda publisher: publisher.key))
    owner_targets: list[str] = []
    entities: list[str] = []
    audit: dict[str, dict] = {}
    native_targets: list[str] = []
    seen_entity_names: set[str] = set()
    for index, publisher in enumerate(ordered):
        triggers = publisher.triggers_for("map_event_file")
        if len(triggers) != 1:
            raise ValueError(f"{publisher.key}: compiler requires one map_event_file trigger")
        trigger = triggers[0]
        prefix = f"ap_publisher_{publisher.key}"
        relay = f"{prefix}_relay"
        marker = f"{prefix}_marker"
        dump_delay = f"{prefix}_dump_delay"
        dump = f"{prefix}_dump"
        names = {relay, marker, dump_delay, dump}
        if seen_entity_names & names:
            raise ValueError(f"{publisher.key}: generated publisher entity collision")
        seen_entity_names.update(names)
        owner_targets.append(relay)
        offset = index * 0.35
        entities.extend([
            _render_relay(relay, [marker, dump_delay], offset),
            _render_command(marker, f"echo {trigger['marker']}"),
            _render_relay(dump_delay, [dump], 0.10),
            _render_command(dump, f"condump {trigger['filename']}"),
        ])
        audit[publisher.key] = {
            "relay": relay,
            "marker_entity": marker,
            "dump_relay": dump_delay,
            "dump_entity": dump,
            "filename": trigger["filename"],
            "marker": trigger["marker"],
            "offset": offset,
        }
        for effect in publisher.effects:
            if effect["strategy"] == "preserved_native_target":
                native_targets.append(effect["target"])
    for native_target in dict.fromkeys(native_targets):
        native_relay = f"ap_publisher_preserved_{native_target}_relay"
        owner_targets.append(native_relay)
        entities.append(_render_relay(native_relay, [native_target], len(ordered) * 0.35 + 0.25))
    return {
        "owner_targets": owner_targets,
        "entities": "".join(entities),
        "publishers": audit,
        "preserved_native_targets": list(dict.fromkeys(native_targets)),
    }


def _patch_sentinel_prime_end(
    mission: dict,
    goal: dict,
    publishers: tuple[PublisherContract, ...],
    root: Path,
    generated_map: Path,
) -> dict:
    source = (root / mission["source_path"]).read_text(encoding="utf-8")
    source_bounds = find_entity_block_bounds(source, mission["owner"])
    if source_bounds is None or source.count(f"entityDef {mission['owner']}") != 1:
        raise ValueError("Sentinel Prime terminal owner is missing or duplicated")
    source_block = source[source_bounds[0]:source_bounds[1]]
    source_sha = _sha256(source_block.encode("utf-8"))
    if source_sha != mission["source_sha256"]:
        raise ValueError("Sentinel Prime terminal owner hash mismatch")
    for snippet in mission["required_snippets"]:
        if snippet not in source_block:
            raise ValueError(f"Sentinel Prime terminal owner drift: missing {snippet}")
    if extract_target_names(source_block):
        raise ValueError("Sentinel Prime terminal unexpectedly has targets")

    text = generated_map.read_text(encoding="utf-8")
    bounds = find_entity_block_bounds(text, mission["owner"])
    if bounds is None or _sha256(text[bounds[0]:bounds[1]].encode("utf-8")) != source_sha:
        raise ValueError("generated Sentinel Prime terminal owner drift")
    native = source_block.replace(
        f"entityDef {mission['owner']}",
        f"entityDef {mission['native_owner']}",
        1,
    )
    relay = generate_event_relay(
        mission["owner"], mission["location_id"], "",
        include_notification=False,
    )
    relay_bounds = find_entity_block_bounds(relay, mission["owner"])
    if relay_bounds is None:
        raise ValueError("could not build Sentinel Prime terminal relay")
    compiled = compile_publishers(
        map_publishers_for_owner(publishers, mission["map_key"], mission["owner"])
    )
    if compiled["preserved_native_targets"] != [mission["native_owner"]]:
        raise ValueError("Sentinel Prime preserved native target contract drift")
    relay_block = replace_targets_block(
        relay[relay_bounds[0]:relay_bounds[1]],
        compiled["owner_targets"],
    )
    result = text[:bounds[0]] + relay_block + native + text[bounds[1]:]
    generated_map.write_text(
        result.rstrip() + "\n" + compiled["entities"],
        encoding="utf-8",
        newline="",
    )
    return {
        "source_path": mission["source_path"],
        "source_sha256": source_sha,
        "owner": mission["owner"],
        "native_owner": mission["native_owner"],
        "runtime_map": goal["runtime_map"],
        "destination_map": goal["destination_map"],
        "before_targets": [],
        "after_targets": compiled["owner_targets"],
        "location_id": mission["location_id"],
        "location_event_target": compiled["publishers"]["sentinel_prime_mission_complete"]["relay"],
        "event_file": goal["event_filename"],
        "marker": goal["marker"],
        "publishers": compiled["publishers"],
        "preserved_native_targets": compiled["preserved_native_targets"],
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
    campaign_goal_contract = load_campaign_goal_contract(root / "data" / "campaign_goal_contract.json")
    publisher_contracts = load_publisher_contracts(root / "data" / "publisher_contracts.json")
    contract_items = {
        name: value for name, value in contracts.items()
        if isinstance(value, dict) and "map_key" in value
    }
    required_keys = {contract["map_key"] for contract in contract_items.values()}
    if set(generated_maps) < required_keys:
        raise ValueError("Mission Complete generated map input is incomplete")
    before_maps = {key: path.read_text(encoding="utf-8") for key, path in generated_maps.items()}
    audits: dict[str, dict] = {}
    terminal_audit: dict | None = None
    for name, contract in contract_items.items():
        strategy = contract.get("patch_strategy")
        if strategy == "logic_node_targets":
            audit = _patch_hell(contract, root, mod_root)
        elif strategy == "entity_targets":
            audit = _patch_exultia(contract, root, generated_maps[contract["map_key"]])
        elif strategy == "terminal_publishers":
            audit = _patch_sentinel_prime_end(
                contract,
                campaign_goal_contract,
                publisher_contracts,
                root,
                generated_maps[contract["map_key"]],
            )
            terminal_audit = audit
        else:
            raise ValueError(f"{name}: unknown Mission Complete patch strategy {strategy!r}")
        audits[name] = audit
    for name, contract in contract_items.items():
        if contract["patch_strategy"] == "terminal_publishers":
            continue
        _append_standard_event_target(
            generated_maps[contract["map_key"]], contract["ap_check"], contract["location_id"]
        )
        audit = audits[name]
        text = generated_maps[contract["map_key"]].read_text(encoding="utf-8")
        expected_ap_target_references = 2 if "owner" in contract else 1
        if text.count(contract["ap_check"]) != expected_ap_target_references:
            raise ValueError(f"{contract['map_key']}: AP target definition/reference count drift")
        if text.count(f"AP_CHECK_EVENT_{contract['location_id']}") != 1:
            raise ValueError(f"{contract['map_key']}: standard AP event count drift")
        audit["event_target"] = f"ap_event_{contract['location_id']}"
        audit["owner_target_references"] = 1
    unrelated_owners = {contract["owner"] for contract in contract_items.values() if "owner" in contract}
    unrelated_owners.add(campaign_goal_contract["owner"])
    unrelated = sum(
        _unrelated_entity_diff_count(
            before_maps[key], path.read_text(encoding="utf-8"),
            unrelated_owners,
        )
        for key, path in generated_maps.items()
    )
    res = dict(audits)
    if terminal_audit is not None:
        res["campaign_goal"] = terminal_audit
    res["unrelated_generated_entity_diff_count"] = unrelated
    return res


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
