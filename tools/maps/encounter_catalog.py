"""Extract encounter evidence from the same authored inputs as the map compiler."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

from tools.maps.ap_map_generator import find_matching_brace


ENTITY_START = re.compile(r"(?m)^entity\s*\{")
ENTITY_HEADER = re.compile(r'entityDef\s+(\S+)\s*\{\s*inherit\s*=\s*"([^"]*)";\s*class\s*=\s*"([^"]+)";')
MEMBER = re.compile(r'\s*([\w.\[\]-]+)\s*=\s*(\{|"(?:\\.|[^"\\])*"|[^;{}]+;)')
SPAWN_CLASSES = {"idTarget_Spawn", "idTarget_Spawn_Parent", "idTargetSpawnGroup"}


def parse_object(text):
    """Read ordered entity properties without flattening event or argument lists."""
    result = {}
    offset = 0
    while offset < len(text):
        match = MEMBER.match(text, offset)
        if not match:
            if text[offset:].strip():
                raise ValueError(f"Unsupported entity property near {text[offset:offset + 80]!r}")
            break
        name, value = match.groups()
        if name in result:
            raise ValueError(f"Duplicate entity property: {name}")
        if value == "{":
            end = find_matching_brace(text, match.end() - 1)
            result[name] = parse_object(text[match.end():end - 1])
            offset = end
        elif value.startswith('"'):
            result[name] = json.loads(value)
            offset = match.end()
        else:
            value = value.rstrip(";").strip()
            if value in {"true", "false", "NULL"}:
                result[name] = {"true": True, "false": False, "NULL": None}[value]
            else:
                try:
                    result[name] = int(value)
                except ValueError:
                    result[name] = float(value)
            offset = match.end()
        while offset < len(text) and (text[offset].isspace() or text[offset] == ";"):
            offset += 1
    return result


def list_items(value):
    if not value:
        return []
    items = [value[f"item[{index}]"] for index in range(value["num"])]
    if len(value) != len(items) + 1:
        raise ValueError("Entity list has properties outside its declared item count")
    return items


def entity_records(text, *, include_names=None):
    starts = [match.start() for match in ENTITY_START.finditer(text)]
    source_line = 1 + text[:starts[0]].count("\n") if starts else 1
    for start, end in zip(starts, starts[1:] + [len(text)]):
        block = text[start:end]
        block_line = source_line
        source_line += block.count("\n")
        header = ENTITY_HEADER.search(block)
        if not header:
            raise ValueError(f"Entity header not understood at offset {start}")
        name, inherit, class_name = header.groups()
        selected = name in include_names if include_names is not None else (
            class_name in SPAWN_CLASSES | {"idAI2", "idEncounterManager", "idArchvileTemplate"}
        )
        if not selected:
            continue
        edit = re.search(r"\bedit\s*=\s*\{", block)
        if not edit:
            raise ValueError(f"Entity edit block missing: {name}")
        edit_end = find_matching_brace(block, edit.end() - 1)
        layers = re.search(r"\blayers\s*\{([^}]+)\}", block[:header.start()])
        yield {
            "name": name,
            "inherit": inherit,
            "class": class_name,
            "layers": re.findall(r'"([^"]+)"', layers.group(1)) if layers else [],
            "source_line": block_line,
            "sha256": hashlib.sha256(block.strip().encode("utf-8")).hexdigest(),
            "edit": parse_object(block[edit.end():edit_end - 1]),
        }


def walk_values(value, path=""):
    if isinstance(value, dict):
        for name, child in value.items():
            yield from walk_values(child, f"{path}.{name}" if path else name)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk_values(child, f"{path}[{index}]")
    else:
        yield path, value


def event_population(event):
    name = event["eventDef"]
    args = list_items(event.get("args"))
    if name == "spawnSingleAI":
        return {"kind": "finite_request", "requested_count": 1}
    if name == "spawnAI":
        return {"kind": "finite_request", "requested_count": args[1]["int"]}
    if name == "maintainAICount":
        maximum = args[2]["int"]
        return {
            "kind": "renewable" if maximum <= 0 else "finite_maintenance",
            "desired_count": args[1]["int"],
            "maximum_spawn_count": maximum,
            "respawn_delay": args[3]["float"],
            "respawn_threshold": args[4]["int"],
            "initial_cohort": None,
        }
    if name.startswith("spawn") or name == "staggeredAISpawn":
        return {"kind": "unqualified", "requested_count": None}
    return None


def extract_map(text, map_key, *, categories=None, profiles=None):
    categories = categories or {}
    profiles = profiles or {}
    records = list(entity_records(text))
    if set(profiles) - {record["name"] for record in records if record["class"] == "idEncounterManager"}:
        raise ValueError(f"Reviewed encounter is absent from source: {map_key}")
    headers = list(ENTITY_HEADER.finditer(text))
    entity_names = {header.group(1) for header in headers}
    if len(headers) != len(entity_names):
        raise ValueError(f"Duplicate entity definition in map: {map_key}")
    incoming = {}
    starts = [match.start() for match in ENTITY_START.finditer(text)]
    for start, end in zip(starts, starts[1:] + [len(text)]):
        block = text[start:end]
        header = ENTITY_HEADER.search(block)
        edit = re.search(r"\bedit\s*=\s*\{", block)
        if not edit:
            continue
        edit_end = find_matching_brace(block, edit.end() - 1)
        for member in MEMBER.finditer(block[edit.end():edit_end - 1]):
            token = member.group(2)
            if token.startswith('"'):
                target = json.loads(token)
                if target in entity_names:
                    incoming.setdefault(target, set()).add(header.group(1))
    by_name = {record["name"]: record for record in records}
    pending = records
    while pending:
        references = {
            value for record in pending for _, value in walk_values(record["edit"])
            if isinstance(value, str) and value in entity_names
        }
        referrers = {name for record in pending for name in incoming.get(record["name"], ())}
        missing = (references | referrers) - by_name.keys()
        pending = list(entity_records(text, include_names=missing)) if missing else []
        by_name.update((record["name"], record) for record in pending)
    result = {"map_key": map_key, "encounters": [], "spawns": [], "ai_templates": [],
              "summon_templates": [], "linked_entities": []}
    for record in by_name.values():
        edit = record.pop("edit")
        record["entity_references"] = [
            {"path": path, "entity": value} for path, value in walk_values(edit)
            if isinstance(value, str) and value in entity_names
        ]
        if record["class"] in SPAWN_CLASSES:
            record["properties"] = edit
            result["spawns"].append(record)
        elif record["class"] == "idAI2":
            record["properties"] = edit
            record["reference_candidates"] = sorted({value for _, value in walk_values(edit) if isinstance(value, str) and "/" in value and not value.startswith("#")})
            record["resource_dependencies"] = None
            record["attribute_operations"] = None
            result["ai_templates"].append(record)
        elif record["class"] == "idArchvileTemplate":
            record["properties"] = edit
            result["summon_templates"].append(record)
        elif record["class"] != "idEncounterManager":
            record["properties"] = edit
            result["linked_entities"].append(record)
        else:
            name = record["name"]
            profile = profiles.get(name, {})
            component = edit.pop("encounterComponent", {})
            scripts = list_items(component.get("entityEvents"))
            protection = profile.get("protection", {})
            if isinstance(protection, dict):
                if set(protection.get("entities", [])) - entity_names:
                    raise ValueError(f"Protected entity is absent from source: {map_key}/{name}")
                for selector in protection.get("spawn_events", []):
                    event = list_items(scripts[selector["script"]]["events"])[selector["event"]]["eventCall"]
                    if event_population(event) is None:
                        raise ValueError(f"Protected spawn event is absent: {map_key}/{name}")
            populations = []
            for script_index, script in enumerate(scripts):
                for path, value in walk_values(script.get("events", {})):
                    if path.endswith("eventDef"):
                        node = script["events"]
                        for part in path.split(".")[:-1]:
                            node = node[part]
                        population = event_population(node)
                        if population:
                            populations.append({"script": script_index, "path": path.rsplit(".", 1)[0], "event": value, **population})
            record.update({
                "id": f"{map_key}/{name}",
                "category": profile.get("category", categories.get(name, "unqualified")),
                "protection": profile.get("protection"),
                "review": profile.get("evidence"),
                "scripts": scripts,
                "spawn_types": sorted({species for path, value in walk_values(scripts) if path.endswith("eEncounterSpawnType_t") for species in value.split()}),
                "component_properties": component,
                "manager_properties": edit,
                "population_requests": populations,
                "finite_kill_budget": None,
                "completion_qualified": False,
                "geometry": None,
                "simultaneous_ai_limit": None,
            })
            record["component_properties"].pop("entityEvents", None)
            result["encounters"].append(record)
    return result


def build_catalog(root, map_keys=None):
    root = Path(root)
    review_path = root / "content/catalog/encounter_profiles.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    maps = []
    for directory in sorted((root / "content/maps").iterdir()):
        if map_keys and directory.name not in map_keys:
            continue
        descriptor = json.loads((directory / "descriptor.json").read_text(encoding="utf-8"))
        locations = json.loads((directory / "locations.json").read_text(encoding="utf-8"))
        categories = {spec.get("manager", spec.get("manager_entity")): "secret_encounter" for spec in locations.get("secret_encounters", [])}
        onboarding = json.loads((directory / "onboarding.json").read_text(encoding="utf-8"))
        for completion in onboarding.get("source_evidence", {}).get("completion_relays", {}).values():
            categories[completion["manager"]] = "escalation"
        source = root / "vanillamaps" / descriptor["source_file"]
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != descriptor["source_sha256"]:
            raise ValueError(f"Map source differs from its descriptor: {directory.name}")
        catalog = extract_map(data.decode("utf-8"), directory.name, categories=categories, profiles=review["maps"].get(directory.name))
        catalog["source"] = {"path": source.relative_to(root).as_posix(), "sha256": hashlib.sha256(data).hexdigest()}
        catalog["runtime_map"] = descriptor["runtime_map"]
        catalog["ap_resource"] = descriptor["resource_path"]
        maps.append(catalog)
    if map_keys and set(map_keys) != {entry["map_key"] for entry in maps}:
        raise ValueError("Requested map is absent from the authored map descriptors")
    return {
        "schema_version": 3,
        "status": "source_evidence",
        "review_sha256": hashlib.sha256(review_path.read_bytes()).hexdigest(),
        "qualification_gaps": review["qualification_gaps"],
        "maps": maps,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--map", action="append", dest="map_keys")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if any(args.output.resolve().is_relative_to((args.root / name).resolve()) for name in ("vanillamaps", "content")):
        parser.error("Output must be outside authored map and content directories")
    catalog = build_catalog(args.root, args.map_keys)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"maps": len(catalog["maps"]), "encounters": sum(len(entry["encounters"]) for entry in catalog["maps"]), "categories": dict(Counter(encounter["category"] for entry in catalog["maps"] for encounter in entry["encounters"])), "finite_kill_budget": None}))


if __name__ == "__main__":
    main()
