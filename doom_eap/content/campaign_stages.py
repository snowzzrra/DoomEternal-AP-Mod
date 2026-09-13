"""Canonical sequence stages derived from the authored map/location catalog."""


def campaign_stages(catalog):
    stages = []
    for spec in sorted(catalog.maps.values(), key=lambda spec: spec.data["order"]):
        if spec.key == "hub":
            continue
        special = spec.key == "e5m4_boss"
        completion = spec.display_name + (" - Defeated" if special else " - Mission Complete")
        matches = [location for location in catalog.runtime_locations if location.name == completion]
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
            # Permanent range: chronological catalog ordinal, never filtered seed order.
            "access_id": 7771000 + len(stages),
            "native_index": len(stages),
        })
    if len(stages) != 20 or sum(stage["kind"] == "mission" for stage in stages) != 19:
        raise ValueError("Unified campaign requires 19 Mission Nodes and the Dark Lord encounter")
    return stages
