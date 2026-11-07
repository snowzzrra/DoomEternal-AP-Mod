"""Canonical sequence stages derived from the authored map/location catalog."""

import json

from .master_levels import load_master_levels


def _validate_stage_id(key, row):
    if not isinstance(row, dict) or row.get("status") not in ("active", "tombstone"):
        raise ValueError(f"Stage {key} has an invalid identity status")
    for field, minimum in (("access_id", 1), ("native_index", 0)):
        number = row.get(field)
        if type(number) is not int or number < minimum:
            raise ValueError(f"Stage {key} has an invalid {field}")


def _load_stage_ids(catalog):
    document = json.loads((catalog.root / "content/catalog/campaign_stage_ids.json").read_bytes())
    if not isinstance(document, dict) or (type(document.get("format")), document.get("format")) != (int, 1):
        raise ValueError("Unsupported campaign stage identity format")
    rows = document.get("stages")
    if not isinstance(rows, dict):
        raise ValueError("Campaign stage identities require a stages object")
    active = set()
    for key, row in rows.items():
        _validate_stage_id(key, row)
        if row["status"] == "active":
            active.add(key)
    for field in ("access_id", "native_index"):
        numbers = [row[field] for row in rows.values()]
        if len(set(numbers)) != len(numbers):
            raise ValueError(f"Campaign stages reuse {field}, including reserved tombstones")
    if active != (set(catalog.maps) - {"hub"}) | set(load_master_levels(catalog.root)):
        raise ValueError("Active campaign stage identities must match mission descriptors")
    return rows


def _campaign_stages_regions(catalog, completion, matches, spec, special, stage_ids, stages):
    if len(matches) != 1:
        raise ValueError(f"Stage {spec.key} requires its existing completion location: {completion}")
    regions = sorted(
        ((name, meta) for name, meta in catalog.region_metadata.items()
         if meta.get("mission_key") == spec.key),
        key=lambda pair: pair[1]["order"],
    )
    if not regions:
        raise ValueError(f"Stage {spec.key} has no authored entry region")
    stages.append({
        "id": spec.key, "name": spec.display_name,
        "source": "tag2" if spec.runtime_map.startswith("game/dlc2/") else
                  "tag1" if spec.runtime_map.startswith("game/dlc/") else "base",
        "map": spec.runtime_map, "kind": "boss" if special else "mission",
        "completion": completion, "completion_id": matches[0].location_id,
        "entry_region": regions[0][0], "regions": [name for name, _ in regions],
        "access_id": stage_ids[spec.key]["access_id"],
        "native_index": stage_ids[spec.key]["native_index"],
        "variant": "mission", "map_key": spec.key,
    })


def campaign_stages(catalog, *, include_master_levels=True):
    stage_ids = _load_stage_ids(catalog)
    stages = []
    for spec in sorted(catalog.maps.values(), key=lambda spec: spec.data["order"]):
        if spec.key == "hub":
            continue
        special = spec.key == "e5m4_boss"
        completion = spec.display_name + (" - Defeated" if special else " - Mission Complete")
        matches = [location for location in catalog.runtime_locations if location.name == completion]
        _campaign_stages_regions(catalog, completion, matches, spec, special, stage_ids, stages)
    stages.extend(_master_stages(catalog, stage_ids, include_master_levels))
    return stages


def _master_stages(catalog, stage_ids, enabled):
    stages = []
    if not enabled:
        return stages
    for key, master in load_master_levels(catalog.root).items():
        spec = catalog.maps[master["map_key"]]
        stages.append({"name": master["name"], "map_key": master["map_key"], "variant": master["variant"],
                       "entry": master["entry"], "layers": master["layers"], "checkpoint": master["checkpoint"],
                       "entry_region": master["entry_region"], "completion_id": master["completion_id"], "id": key, "kind": "master", "map": spec.runtime_map,
                       "source": "tag2" if spec.requires_dlc_content else "base",
                       "completion": master["name"] + " - Mission Complete", "regions": [master["entry_region"]],
                       "access_id": stage_ids[key]["access_id"], "native_index": stage_ids[key]["native_index"]})
    return stages
