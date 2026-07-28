"""Describe one normalized map and every downstream generated surface."""

from __future__ import annotations

import argparse
import json

from content_catalog import load_content_catalog, thaw_content
from tools.maps.map_semantic_baseline import baseline_path


def describe(map_key: str) -> dict:
    catalog = load_content_catalog()
    spec = catalog.map(map_key)
    physical = [
        {"id": item.location_id, "name": item.name, "strategy": item.strategy}
        for item in catalog.physical_locations if item.map_key == map_key
    ]
    runtime = [
        {"id": item.location_id, "name": item.name, "strategy": item.strategy}
        for item in catalog.runtime_locations if item.mission_key == map_key
    ]
    publishers = [
        {
            "key": item.key,
            "triggers": [dict(trigger) for trigger in item.triggers],
            "effects": [dict(effect) for effect in item.effects],
        }
        for item in catalog.publishers if item.map_key == map_key
    ]
    assets = [
        {
            "key": item.key,
            "strategy": item.strategy,
            "model": item.model,
            "donor": dict(item.donor),
            "replacement_slot_policy": item.replacement_slot_policy,
            "replacement_slot": thaw_content(item.replacement_slot),
            "usage_policy": item.usage_policy,
            "preserve": item.preserve,
        }
        for item in catalog.assets if item.map_key == map_key
    ]
    route = json.loads(
        (catalog.root / "data" / "campaign_route.json").read_text(encoding="utf-8")
    )
    return {
        "map_key": map_key,
        "source": spec.source_file,
        "owner": spec.resource_owner,
        "resource_base": spec.resource_base,
        "physical_locations": physical,
        "runtime_locations": runtime,
        "publishers": publishers,
        "assets": assets,
        "route": [
            row for row in route.get("connections", [])
            if spec.display_name in row[:2]
        ],
        "generated_apworld_rows": len(physical) + len(runtime),
        "baseline": str(baseline_path(map_key)),
        "package_plan": {
            "generated_output": spec.data["generated_output"],
            "resource_owner": spec.resource_owner,
            "relative_entities_path": spec.relative_entities_path,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("map_key")
    args = parser.parse_args()
    print(json.dumps(describe(args.map_key), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
