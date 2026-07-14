#!/usr/bin/env python3
"""Fail-closed, contract-driven patches for versioned logicentity DECLs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def count_link(text: str, from_node: int, from_pin: int, to_node: int) -> int:
    pattern = re.compile(
        rf'fromNodeId\s*=\s*{from_node};\s*'
        rf'fromPinId\s*=\s*{from_pin};\s*'
        rf'toNodeId\s*=\s*{to_node};\s*toPinId\s*=\s*0;',
        re.MULTILINE,
    )
    return len(pattern.findall(text))


def patch_contract(contract_path: Path, location_id: str, output: Path) -> dict:
    contracts = json.loads(contract_path.read_text(encoding="utf-8"))
    contract = contracts["locations"][location_id]
    if contract["strategy"] != "logic_decl_patch":
        raise ValueError(f"{location_id}: not a logic_decl_patch contract")
    root = contract_path.parent.parent
    source = root / contract["logic_decl_patch"]["source_path"]
    if not source.is_file():
        raise ValueError(f"logic DECL source missing: {source}")
    raw = source.read_bytes()
    expected_hash = contract["logic_decl_patch"]["source_sha256"]
    if sha256(raw) != expected_hash:
        raise ValueError(f"logic DECL source hash mismatch: {source}")
    text = raw.decode("utf-8")
    patch = contract["logic_decl_patch"]
    inventory_signature = (
        f'id = {patch["inventory_check_node"]};'
    )
    if text.count(inventory_signature) != 1:
        raise ValueError("expected Ice inventory-check node is not unique")
    if text.count(patch["inventory_decl"]) != 1:
        raise ValueError("expected Ice inventory declaration is not unique")
    architecture = contract.get("post_ice_architecture")
    if architecture:
        entry_node = architecture["entry_node"]
        transaction_node = architecture["transaction_node"]
        if text.count(f'id = {entry_node};') != 1:
            raise ValueError("expected one Restore Ship Power post-Ice entry node")
        if text.count(f'id = {transaction_node};') != 1:
            raise ValueError("expected one post-Ice transaction node")
        if count_link(text, entry_node, 2, transaction_node) != architecture["entry_count"]:
            raise ValueError("Restore Ship Power does not have the contracted post-Ice entry")

    edge_pattern = re.compile(
        rf'(id\s*=\s*{patch["edge_id"]};\s*'
        rf'fromNodeId\s*=\s*{patch["inventory_check_node"]};\s*'
        rf'fromPinId\s*=\s*{patch["bypassed_pin"]};\s*'
        rf'toNodeId\s*=\s*){patch["old_destination"]}(;\s*toPinId\s*=\s*0;)',
        re.MULTILINE,
    )
    matches = list(edge_pattern.finditer(text))
    if len(matches) != 1:
        raise ValueError(f"expected one Ice branch edge, found {len(matches)}")
    result = edge_pattern.sub(
        rf'\g<1>{patch["preserved_destination"]}\g<2>', text, count=1
    )
    if result.count(inventory_signature) != 1 or result.count(patch["inventory_decl"]) != 1:
        raise ValueError("patch changed the Ice inventory-check identity")
    if architecture and count_link(
        result, architecture["entry_node"], 2, architecture["transaction_node"]
    ) != architecture["entry_count"]:
        raise ValueError("patch changed the Restore Ship Power post-Ice entry")
    for reference in patch.get("preserved_references", []):
        if text.count(reference) != result.count(reference) or reference not in result:
            raise ValueError(f"patch did not preserve reference: {reference}")
    for reference in patch.get("forbidden_patch_references", []):
        if reference in result:
            raise ValueError(f"forbidden reference in logic override: {reference}")

    before_lines = text.splitlines()
    after_lines = result.splitlines()
    changed = [
        {"line": index + 1, "before": before, "after": after}
        for index, (before, after) in enumerate(zip(before_lines, after_lines))
        if before != after
    ]
    if len(before_lines) != len(after_lines) or len(changed) != 1:
        raise ValueError(f"logic patch is not a one-line edge rewrite: {changed}")
    if str(patch["old_destination"]) not in changed[0]["before"] or str(patch["preserved_destination"]) not in changed[0]["after"]:
        raise ValueError("unexpected structural logic patch diff")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(result, encoding="utf-8", newline="")
    snapshot = {
        "location_id": int(location_id),
        "event": contract["logic_decl_patch"]["observed_event"],
        "inventory_check": {
            "node": patch["inventory_check_node"],
            "decl": patch["inventory_decl"],
        },
        "changed_edge": {
            "edge": patch["edge_id"],
            "from_pin": patch["bypassed_pin"],
            "before_destination": patch["old_destination"],
            "after_destination": patch["preserved_destination"],
        },
        "post_ice_architecture": architecture,
        "source_sha256": sha256(raw),
        "override_sha256": sha256(result.encode("utf-8")),
        "changed_lines": changed,
    }
    expected_override = patch.get("override_sha256")
    if expected_override and snapshot["override_sha256"] != expected_override:
        raise ValueError("logic DECL override hash mismatch")
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contracts", type=Path, required=True)
    parser.add_argument("--location", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path)
    args = parser.parse_args()
    snapshot = patch_contract(args.contracts, args.location, args.output)
    if args.snapshot:
        args.snapshot.parent.mkdir(parents=True, exist_ok=True)
        args.snapshot.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(snapshot, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
