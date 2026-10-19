"""Extract native enemy origins without approving encounters or species pairs."""

import argparse
from collections import Counter
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path

from doom_eap.content.enemy_plan import BASE_COMBAT_COST
from doom_eap.content.enemy_species import enemy_spawn_mask, project_species_profiles

from .encounter_catalog import ENTITY_HEADER, ENTITY_START, SPAWN_CLASSES, entity_records, list_items, walk_values


EMITTERS = {
    "spawnSingleAI": "immediate",
    "spawnAI": "group",
    "staggeredAISpawn": "staggered",
    "maintainAICount": "maintenance",
    "spawnArchvile": "archvile",
    "spawnSpirit": "spirit",
    "spawnPossessedAI": "possessed",
    "spawnBuffPod": "buff_pod",
}
RUNTIME_FAMILIES = ("point_factory", "independent_actor", "summon", "restore")
DIRECT_TARGET_EMITTERS = {"idTarget_Count", "idTrigger_Facing"}


def _at(value, path):
    for name in path.split("."):
        value = value[name]
    return value


def _protection(record, profile, script, event):
    rule = profile.get("protection", {})
    if rule.get("entire_encounter") or {"script": script, "event": event} in rule.get("spawn_events", []):
        return rule["reason"]
    return None


def extract_native_inventory(source, map_key, *, profiles=None):
    profiles = profiles or {}
    selected, headers = set(), {}
    starts = [match.start() for match in ENTITY_START.finditer(source)]
    for start, end in zip(starts, starts[1:] + [len(source)]):
        block = source[start:end]
        header = ENTITY_HEADER.search(block)
        if header is None:
            raise ValueError(f"Native entity header is invalid at {map_key}/{start}")
        name, _, kind = header.groups()
        if name in headers:
            raise ValueError(f"Duplicate native entity: {map_key}/{name}")
        headers[name] = kind
        if kind in SPAWN_CLASSES | {"idAI2", "idEncounterManager", "idArchvileTemplate"} or "eventDef" in block or "targets" in block or "systemSpawnTargets" in block:
            selected.add(name)
    records = {row["name"]: row for row in entity_records(source, include_names=selected)}
    protected_entities = {name: profile["protection"]["reason"] for profile in profiles.values()
                          for name in profile.get("protection", {}).get("entities", [])}
    templates, adopted = set(), set()
    emitters, actors, summon_templates, points, direct, dependencies = [], [], [], [], [], []
    for record in records.values():
        edit, kind = record["edit"], record["class"]
        if kind != "idTarget_Remove":
            for field in ("targets", "systemSpawnTargets"):
                targets = edit.get(field) or {}
                count = targets.get("num", 0)
                if type(count) is not int or not 0 <= count <= 100000:
                    raise ValueError(f"Invalid native activation list: {map_key}/{record['name']}/{field}")
                for index in range(count):
                    target = targets.get(f"item[{index}]")
                    if isinstance(target, str) and headers.get(target) == "idTarget_Spawn":
                        path = f"{field}.item[{index}]"
                        protection = profiles.get(record["name"], {}).get("protection", {})
                        reason = protected_entities.get(record["name"]) or protected_entities.get(target)
                        if protection.get("entire_encounter"):
                            reason = protection["reason"]
                        row = {"id": f"{map_key}/{record['name']}/{path}", "family": "direct_point",
                            "entity": record["name"], "owner": record["name"], "path": path, "reference": target,
                            "source_class": kind, "layers": record["layers"], "protected_reason": reason}
                        (direct if field == "targets" and kind in DIRECT_TARGET_EMITTERS else dependencies).append(row)
        phases = {}
        if kind in SPAWN_CLASSES:
            points.append({"entity": record["name"], "class": kind, "sha256": record["sha256"],
                           "layers": record["layers"], "references": sorted({
                               value for _, value in walk_values(edit) if isinstance(value, str) and value in headers}),
                           "definitions": [item["name"] for field in ("entityDefs", "initialEntityDefs")
                                           for item in list_items(edit.get(field))]})
            for field in ("entityDefs", "initialEntityDefs"):
                templates.update(item["name"] for item in list_items(edit.get(field)))
        if kind == "idEncounterManager":
            adopted.update(list_items(edit.get("adoptedStandAloneAI")))
        if kind == "idArchvileTemplate":
            summon_templates.append({"entity": record["name"], "sha256": record["sha256"],
                                     "layers": record["layers"],
                                     "spawn_types": list_items(edit.get("summonList", {}).get("aiSpawnTypeList")),
                                     "references": sorted({value for _, value in walk_values(edit)
                                                           if isinstance(value, str) and value in headers})})
        for path, event in walk_values(edit):
            if not path.endswith(".eventDef") or not isinstance(event, str):
                continue
            call_path = path.rsplit(".", 1)[0]
            call = _at(edit, call_path)
            script_path = call_path.split(".events.", 1)[0]
            arguments = list_items(call.get("args"))
            if event == "waitAIRemaining" and len(arguments) >= 3 and arguments[:3] == [
                    {"eEncounterSpawnType_t": "ENCOUNTER_SPAWN_ANY"}, {"int": 0}, {"string": ""}]:
                phases[script_path] = phases.get(script_path, 0) + 1
            if not event.startswith(("spawn", "staggeredAISpawn", "maintainAI")):
                continue
            parts = call_path.split(".")
            native = parts[:2] == ["encounterComponent", "entityEvents"] and len(parts) == 6
            script = int(parts[2][5:-1]) if native else None
            index = int(parts[4][5:-1]) if native else None
            owner = record["name"]
            if "entityEvents" in parts:
                owner_path = ".".join(parts[:parts.index("entityEvents") + 2])
                owner = _at(edit, owner_path).get("entity") or owner
            reason = _protection(record, profiles.get(record["name"], {}), script, index)
            owner_protection = profiles.get(owner, {}).get("protection", {})
            if owner_protection.get("entire_encounter"):
                reason = owner_protection["reason"]
            origin = {"id": f"{map_key}/{record['name']}/{call_path}", "entity": record["name"],
                      "owner": owner, "layers": record["layers"], "path": call_path, "script_index": script, "event_index": index,
                      "event_def": event, "family": EMITTERS.get(event, "unknown"),
                      "phase": f"{record['name']}/{script_path}/{phases.get(script_path, 0)}",
                      "tutorial_hint": any("tutorial" in value.lower() for value in [record["name"], *record["layers"]]),
                      "protected_reason": reason, "arguments": arguments}
            emitters.append(origin)
    for record in records.values():
        if record["class"] != "idAI2":
            continue
        name = record["name"]
        actors.append({"entity": name, "inherit": record["inherit"], "sha256": record["sha256"],
                       "identity_inherit": record["edit"].get("goreComponent", {}).get("goreContainer"),
                       "layers": record["layers"], "point_template": name in templates,
                       "adopted": name in adopted, "protected_reason": protected_entities.get(name) or
                       _protection(record, {}, None, None)})
    return {"map_key": map_key, "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            "emitters": sorted(emitters, key=lambda row: row["id"]),
            "actors": sorted(actors, key=lambda row: row["entity"]),
            "points": sorted(points, key=lambda row: row["entity"]),
            "summon_templates": sorted(summon_templates, key=lambda row: row["entity"]),
            "direct_activations": sorted(direct, key=lambda row: row["id"]),
            "point_dependencies": sorted(dependencies, key=lambda row: row["id"])}


def build_native_inventory(root, map_keys=None):
    root = Path(root)
    profiles = json.loads((root / "content/catalog/encounter_profiles.json").read_bytes())["maps"]
    maps = {}
    for descriptor_path in sorted((root / "content/maps").glob("*/descriptor.json")):
        key = descriptor_path.parent.name
        if map_keys is not None and key not in map_keys:
            continue
        descriptor = json.loads(descriptor_path.read_bytes())
        payload = (root / "vanillamaps" / descriptor["source_file"]).read_bytes()
        if hashlib.sha256(payload).hexdigest() != descriptor["source_sha256"]:
            raise ValueError(f"Native source differs from descriptor: {key}")
        maps[key] = extract_native_inventory(payload.decode("utf-8"), key, profiles=profiles.get(key))
        maps[key]["runtime_map"] = descriptor["runtime_map"]
    if map_keys is not None and set(map_keys) != maps.keys():
        raise ValueError("Requested native source map is absent")
    return {"schema_version": 1, "runtime_families": list(RUNTIME_FAMILIES), "maps": maps}


def inventory_summary(inventory):
    emitters = [row for value in inventory["maps"].values() for row in value["emitters"]]
    actors = [row for value in inventory["maps"].values() for row in value["actors"]]
    return {"maps": len(inventory["maps"]), "emitters": dict(sorted(Counter(row["family"] for row in emitters).items())),
            "protected_emitters": sum(row["protected_reason"] is not None for row in emitters),
            "actor_definitions": len(actors), "point_templates": sum(row["point_template"] for row in actors),
            "adopted_actor_definitions": sum(row["adopted"] for row in actors),
            "runtime_coverage_verified": False}


def native_recipe_catalog(inventory, registry):
    identities = registry["species"]
    by_inherit = {inherit: species for species, row in identities.items() for inherit in row["inherits"]}
    population_types = {row["encounter_spawn_type"] for row in registry["population_encounter_conversion"]["values"].values()}
    species = {}
    for name in project_species_profiles(registry):
        row = identities[name]
        physical = sorted(set(row["spawn_type"].replace("|", " ").split()) & population_types)
        if len(physical) != 1:
            raise ValueError(f"Ambiguous physical population type: {name}")
        template = next(template for template in row["templates"] if template["role"] == "preferred")
        species[name] = {"spawn_type": physical[0], "ai": {key: template[key] for key in ("map_key", "entity", "sha256")}}
    masks = {name: enemy_spawn_mask(row["spawn_type"]) for name, row in species.items()}
    boss_mask = enemy_spawn_mask("ENCOUNTER_SPAWN_ICON_OF_SIN ENCOUNTER_SPAWN_GLADIATOR ENCOUNTER_SPAWN_SAMUEL_BOSS")
    type_names = {row["encounter_value"]: row["encounter_spawn_type"]
                  for row in registry["population_encounter_conversion"]["values"].values()}
    maps = {}
    for map_key, value in inventory["maps"].items():
        actors = {row["entity"]: row for row in value["actors"]}
        points = {row["entity"]: row for row in value["points"]}
        origins, unresolved = [], []

        def actor_species(actor):
            return by_inherit.get(actor["inherit"], by_inherit.get(actor.get("identity_inherit")))

        def point_actors(name, visited=None):
            visited = set() if visited is None else visited
            if name in visited or name not in points:
                return set()
            visited.add(name)
            point = points[name]
            result = {entity for entity in point["definitions"] if entity in actors}
            for reference in point["references"]:
                result.update(point_actors(reference, visited))
            return result

        def add(origin, source_mask, count, maximum, reference):
            identity = {"id": origin["id"], "family": origin["family"], "layers": list(origin.get("layers", []))}
            if origin.get("protected_reason"):
                origins.append({**identity, "protected_reason": origin["protected_reason"]})
                return
            templates = point_actors(reference) if reference else set()
            if templates and all(actors[name]["inherit"].startswith("ai/boss/") for name in templates):
                origins.append({**identity, "protected_reason": "native boss actor"})
                return
            if not source_mask & ~boss_mask:
                origins.append({**identity, "protected_reason": "native boss type"})
                return
            if count == 0:
                origins.append({**identity, "inactive_reason": "native zero-count request"})
                return
            candidates = sorted({actor_species(actors[name]) for name in templates if actor_species(actors[name])})
            matching = [name for name in candidates if masks[name] & source_mask]
            if matching:
                candidates = matching
            if not candidates:
                candidates = sorted(name for name in masks if masks[name] & source_mask)
            if not candidates or count <= 0:
                unresolved.append({"id": origin["id"], "reason": "logical population is unresolved"})
                return
            logical_types = sorted({species[name]["spawn_type"] for name in candidates if masks[name] & source_mask})
            if not logical_types:
                logical_types = sorted(name for mask, name in type_names.items() if mask & source_mask)
            if not logical_types:
                unresolved.append({"id": origin["id"], "reason": "native logical mask is unresolved"})
                return
            original_type = origin.get("spawn_type", " ".join(logical_types))
            if origin["family"] == "direct_point":
                binding = {"mode": "direct_point", **{key: origin[key] for key in ("entity", "owner", "path", "reference")}}
            elif "path" in origin:
                binding = {"mode": "event", **{key: origin[key] for key in ("entity", "owner", "path", "event_def")},
                           "argument": origin["reference_argument"], "reference": reference,
                           "arguments": origin["arguments"]}
            else:
                binding = {"mode": origin["family"], "entity": origin["entity"]}
                if origin["family"] == "summon":
                    binding["spawn_type"] = original_type
            origins.append({**identity,
                "phase": origin.get("phase", origin["id"]),
                "spawn_type": original_type, "logical_types": logical_types,
                "logical_count": count, "maximum_emissions": maximum,
                "binding": binding,
                "combat_cost": str(Fraction(sum(BASE_COMBAT_COST[name] for name in candidates) * count, len(candidates)))})

        for row in value["emitters"]:
            args, family = row["arguments"], row["family"]
            identity = {"id": row["id"], "family": family, "layers": list(row.get("layers", []))}
            if row["protected_reason"]:
                origins.append({**identity, "protected_reason": row["protected_reason"]})
                continue
            if family in {"immediate", "possessed"} and not args[1]["entity"]:
                origins.append({**identity, "inactive_reason": "native null spawn point"})
                if family == "possessed":
                    origins.append({**identity, "id": row["id"] + "/spirit", "family": "spirit", "inactive_reason": "native null host spawn point"})
                continue
            if family == "archvile" and not args[1]["entity"]:
                origins.append({**identity, "inactive_reason": "native null archvile template"})
                continue
            if family in {"immediate", "group", "staggered", "maintenance", "possessed"}:
                mask = enemy_spawn_mask(args[0]["eEncounterSpawnType_t"])
                count = args[1]["int"] if family in {"group", "staggered", "maintenance"} else 1
                maximum = max(0, args[2]["int"]) if family == "maintenance" else count
                argument = 5 if family == "maintenance" else 2 if family in {"group", "staggered"} else 1
                target = args[argument]["entity"]
                add({**row, "spawn_type": args[0]["eEncounterSpawnType_t"], "reference_argument": argument}, mask, count, maximum, target)
                if family == "possessed":
                    add({**row, "id": row["id"] + "/spirit", "family": "spirit", "reference_argument": 3},
                        masks["spirit"], 1, 1, args[3]["entity"])
            elif family in {"archvile", "spirit", "buff_pod"}:
                add({**row, "reference_argument": 0}, masks[family], 1, 1, args[0]["entity"])
            else:
                unresolved.append({"id": row["id"], "reason": f"unknown emitter {row['event_def']}"})
        for row in value.get("direct_activations", []):
            templates = point_actors(row["reference"])
            source_mask = sum({masks[actor_species(actors[name])] for name in templates if actor_species(actors[name])})
            if source_mask:
                add(row, source_mask, 1, 1, row["reference"])
        for row in value["actors"]:
            if row["point_template"]:
                continue
            origin = {**row, "id": f"{map_key}/{row['entity']}", "family": "independent_actor"}
            identity = {key: origin[key] for key in ("id", "family", "layers")}
            if row["protected_reason"]:
                origins.append({**identity, "protected_reason": row["protected_reason"]})
            elif row["inherit"].startswith("ai/boss/"):
                origins.append({**identity, "protected_reason": "native boss actor"})
            elif actor_species(row):
                add(origin, masks[actor_species(row)], 1, 1, "")
            else:
                unresolved.append({"id": origin["id"], "reason": f"unknown actor identity {row['inherit']}"})
        for template in value["summon_templates"]:
            for spawn_type, count in sorted(Counter(template.get("spawn_types", [])).items()):
                add({"id": f"{map_key}/{template['entity']}/{spawn_type}", "phase": f"{template['entity']}/summons",
                     "family": "summon", "entity": template["entity"], "layers": template.get("layers", []), "spawn_type": spawn_type},
                    enemy_spawn_mask(spawn_type), count, 0, "")
        runtime = []
        for spawn_type in sorted({row["spawn_type"] for row in species.values()}):
            costs = [BASE_COMBAT_COST[name] for name, row in species.items() if row["spawn_type"] == spawn_type]
            runtime.append({"id": f"{map_key}/native_summons/{spawn_type}", "family": "summon", "runtime_only": True,
                "spawn_type": spawn_type, "logical_types": [spawn_type], "logical_count": 100, "maximum_emissions": 0,
                "binding": {"mode": "summon", "entity": ""}, "combat_cost": str(Fraction(sum(costs) * 100, len(costs)))})
        maps[map_key] = {"source_sha256": value["source_sha256"], "runtime_map": value.get("runtime_map", map_key),
                         "origins": origins, "runtime_origins": runtime, "unresolved": unresolved,
                         "point_dependencies": value.get("point_dependencies", [])}
    return {"schema_version": 1, "species": species, "maps": maps}


def build_native_recipe_catalog(root):
    root = Path(root)
    from .ap_map_generator import find_entity_block_bounds
    from .enemy_attribute_baselines import resolve_enemy_attribute_baselines

    catalog = native_recipe_catalog(build_native_inventory(root),
                                   json.loads((root / "content/catalog/enemy_species.json").read_bytes()))
    sources = {}
    for row in catalog["species"].values():
        reference = row["ai"]
        key = reference["map_key"]
        if key not in sources:
            sources[key] = (root / "vanillamaps" / (key + ".map")).read_text(encoding="utf-8")
        start, end = find_entity_block_bounds(sources[key], reference["entity"])
        values = resolve_enemy_attribute_baselines(sources[key][start:end])["aiHealth"]["components"]["components[0]"]
        hitpoints = {}
        for field in ("max", "starting"):
            value = values[field]
            if isinstance(value, dict) and set(value) == {"defaultValue"}:
                value = value["defaultValue"]
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or field == "max" and value == 0:
                raise ValueError("Invalid native body health input")
            hitpoints[field] = value
        if hitpoints["starting"] > hitpoints["max"]:
            raise ValueError("Native body starts above its maximum health")
        row["hitpoints"] = hitpoints
    return catalog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--map", action="append", dest="maps")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    inventory = build_native_inventory(args.root, args.maps)
    if args.output:
        args.output.write_text(json.dumps(inventory, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    summary = inventory_summary(inventory)
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
