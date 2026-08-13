#!/usr/bin/env python3
"""Compile launcher Starting Inventory choices from APWorld DevInv legality."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
AP_ITEMS = ROOT.parent / "Archipelago" / "worlds" / "doometernal" / "items.py"
MAPPING = ROOT / "data" / "devinv_start_mapping.json"
OUTPUT = ROOT / "data" / "start_inventory_catalog.json"


def _legal_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "DEVINV_START_INVENTORY_ITEM_NAMES" for target in node.targets)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "frozenset"
        ):
            values = ast.literal_eval(node.value.args[0])
            if not isinstance(values, set) or not all(isinstance(value, str) for value in values):
                raise ValueError("DevInv start inventory legality must be a string set")
            return values
    raise ValueError("APWorld has no DEVINV_START_INVENTORY_ITEM_NAMES")


def compile_catalog() -> dict[str, object]:
    legal_names = _legal_names(AP_ITEMS)
    mapping = json.loads(MAPPING.read_text(encoding="utf-8"))
    raw_items = mapping.get("items")
    if mapping.get("schema_version") != 1 or not isinstance(raw_items, dict):
        raise ValueError("DevInv start mapping has an unsupported schema")
    mapped_names = {
        item["name"]
        for item in raw_items.values()
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    if mapped_names != legal_names:
        raise ValueError(
            "DevInv start mapping coverage drift: "
            f"missing={sorted(legal_names - mapped_names)}, extra={sorted(mapped_names - legal_names)}"
        )
    return {
        "schema_version": 1,
        "source": "Archipelago/worlds/doometernal/items.py + data/devinv_start_mapping.json",
        "items": [{"name": name, "label": name} for name in sorted(legal_names, key=str.casefold)],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    rendered = json.dumps(compile_catalog(), indent=2) + "\n"
    if args.check:
        if not args.output.is_file() or args.output.read_text(encoding="utf-8") != rendered:
            raise SystemExit("data/start_inventory_catalog.json is stale; regenerate projection")
        print("starting inventory catalog: up-to-date")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8", newline="\n")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
