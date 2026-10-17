"""Verify curated first-pass opportunities against their pinned authoring sources."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from doom_eap.content.enemy_species import enemy_spawn_mask

from .ap_map_generator import find_entity_block_bounds
from .encounter_catalog import ENTITY_HEADER, entity_records, list_items
from .encounter_materializer import _value_span, text_sha256
from .encounter_source_definitions import SourceDefinitions
from .enemy_attribute_baselines import resolve_enemy_attribute_baselines, positive_enemy_hitpoints


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _get(value, path):
    for key in path.split("."):
        value = value[key]
    return value


def typed_consumers(calls, spawn_type, label):
    source_mask = enemy_spawn_mask(spawn_type)
    if source_mask == (1 << 44) - 1:
        return []
    result = []
    for index, call in enumerate(calls):
        if call["eventDef"] in {"spawnSingleAI", "spawnAI", "staggeredAISpawn", "maintainAICount"}:
            continue
        args = list_items(call["args"])
        masks = [enemy_spawn_mask(arg["eEncounterSpawnType_t"]) for arg in args if "eEncounterSpawnType_t" in arg]
        labels = [arg["string"] for arg in args if "string" in arg]
        if any(mask != (1 << 44) - 1 and mask & source_mask for mask in masks) and (not labels or label in labels or "" in labels):
            result.append(dict(event_index=index, event_def=call["eventDef"], spawn_type=spawn_type,
                replacement_qualification="preserve_source_spawn_type"))
    return result


def _verify_staggered_completion(calls, first, last, spawn_type, label):
    source_mask = enemy_spawn_mask(spawn_type)
    for call in calls[first + 1:last + 1]:
        kind = call["eventDef"]
        if kind not in {"waitStaggeredSpawnComplete", "waitAIRemaining"}:
            continue
        args = list_items(call["args"])
        if len(args) != 4:
            continue
        mask = enemy_spawn_mask(args[0].get("eEncounterSpawnType_t"))
        if mask & source_mask != source_mask:
            continue
        if args[2].get("string") not in {"", label} or type(args[1].get("int")) is not int or args[1]["int"] != 0:
            continue
        if kind == "waitStaggeredSpawnComplete":
            return
        break
    raise ValueError("Staggered source needs queue completion before its population-zero gate")


def verify_catalog(catalog, source_bytes, registry_bytes, *, logic_sources=None, root=None):
    """Return scoped counts after checking source pins, cohorts and species bindings."""
    _require(catalog["schema_version"] == 1, "Unsupported opportunity catalog schema")
    _require(hashlib.sha256(source_bytes).hexdigest() == catalog["source"]["sha256"], "Canonical map hash differs")
    _require(hashlib.sha256(registry_bytes).hexdigest() == catalog["species_registry_sha256"], "Species registry hash differs")
    source = source_bytes.decode("utf-8")
    source_names = {match.group(1) for match in ENTITY_HEADER.finditer(source)}
    group_names = {match.group(1) for match in ENTITY_HEADER.finditer(source) if match.group(3) == 'idTargetSpawnGroup'}
    registry = json.loads(registry_bytes)
    profiles = registry["species"]
    source_definitions = SourceDefinitions(registry, root)
    for path, pin in catalog.get("logic_sources", {}).items():
        _require(logic_sources is not None and path in logic_sources, f"Missing pinned logic declaration: {path}")
        _require(hashlib.sha256(logic_sources[path]).hexdigest() == pin["sha256"], f"Logic declaration pin differs: {path}")
    attribute_baselines = None
    if 'attribute_baselines_sha256' in catalog:
        path = Path(root or Path(__file__).resolve().parents[2]) / 'content/catalog/enemy_attribute_baselines.json'
        raw = path.read_bytes()
        _require(hashlib.sha256(raw).hexdigest() == catalog['attribute_baselines_sha256'], 'Attribute baseline pin differs')
        attribute_baselines = json.loads(raw)
    pins = catalog["entities"]
    records = {row["name"]: row for row in entity_records(source, include_names=set(pins))}
    _require(records.keys() == pins.keys(), "A pinned entity is absent from the canonical map")

    def record(name):
        _require(name in records, f"Source definition is not pinned: {name}")
        return records[name]
    for name, pin in pins.items():
        row = records[name]
        _require(row["sha256"] == pin["sha256"] and row["class"] == pin["class_name"], f"Entity pin differs: {name}")
        if "layers" in pin:
            _require(row["layers"] == pin["layers"], f"Entity layers differ: {name}")
        if "inherit" in pin:
            _require(row["inherit"] == pin["inherit"], f"AI inherit differs: {name}")
        if "spawn_position" in pin:
            _require(row["edit"]["spawnPosition"] == pin["spawn_position"], f"Spawner position differs: {name}")
        if "spawn_override" in pin:
            _require(row["edit"].get("spawnEditable", {}).get("aiStateOverride") == pin["spawn_override"], f"Spawner override differs: {name}")
            if pin["spawn_override"] == "AIOVERRIDE_PLAY_ENTRANCE_ANIMATION":
                _require(pin.get("replacement_qualification") == "preserve_source_species_until_entrance_animation_is_qualified"
                    and pin.get("protected_reason") == "authored_entrance_animation", f"Entrance animation protection differs: {name}")
        if "starting_health" in pin:
            health = row["edit"].get("aiHealth", {}).get("components", {}).get("components[0]", {})
            if 'health_source' in pin:
                _require(pin['health_source'] == 'qualified_attribute_baseline' and attribute_baselines is not None,
                    f'Missing qualified health baseline: {name}')
                start, end = find_entity_block_bounds(source, name)
                health = resolve_enemy_attribute_baselines(source[start:end], catalog=attribute_baselines).get('aiHealth', {}).get('components', {}).get('components[0]', {})
            _require(health.get("starting") == pin["starting_health"] and positive_enemy_hitpoints(health),
                f"AI health differs: {name}")

    identities = set()
    segment_ids = set()
    for segment in catalog["segments"]:
        _require(segment["id"] not in segment_ids, f"Duplicate segment: {segment['id']}")
        segment_ids.add(segment["id"])
        manager_name = segment["manager"]
        manager = records[manager_name]
        _require(manager["class"] == "idEncounterManager", f"Invalid manager: {manager_name}")
        edit = manager["edit"]
        scripts = list_items(edit["encounterComponent"]["entityEvents"])
        script_index = segment["script_index"]
        _require(type(script_index) is int and 0 <= script_index < len(scripts), f"Invalid script: {manager_name}")
        events = list_items(scripts[script_index]["events"])
        calls = [item["eventCall"] for item in events]
        scope = segment.get("source_slice", dict(first_event_index=0, last_event_index=len(calls) - 1))
        first, last = scope["first_event_index"], scope["last_event_index"]
        _require(type(first) is int and type(last) is int and 0 <= first <= last < len(calls), "Invalid source slice")
        _require(not any(call["eventDef"] in {"migrateAIFromExternalScript", "removeAI", "spawnArchvile", "spawnSpirit", "spawnBuffPod", "spawnPossessedAI"}
            for call in calls[first:last + 1]), f"Manager requires another cohort qualification: {manager_name}")
        _require(not list_items(edit.get("preCombatTriggers")), f"PreCombat selection requires review: {manager_name}")
        activation = segment["activation"]
        for field in ("commitTriggers", "exitTriggers", "userFlagTriggers"):
            actual = list_items(edit.get(field))
            bindings = activation["bindings"].get(field, [])
            _require([binding["entity"] for binding in bindings] == actual, f"Trigger bindings differ: {manager_name}.{field}")
            for binding in bindings:
                if binding.get("source_resolved") is False:
                    _require(binding["entity"] not in source_names, f"Unresolved binding is declared: {binding['entity']}")
                    continue
                trigger = records[binding["entity"]]
                expected_class = {"commitTriggers": "idEncounterTrigger_Commit", "exitTriggers": "idEncounterTrigger_Exit", "userFlagTriggers": "idEncounterTrigger_RaiseUserFlag"}[field]
                _require(trigger["class"] == expected_class, f"Trigger binding class differs: {binding['entity']}")
                if field == "exitTriggers":
                    _require(binding["force_ai_to_flee"] == trigger["edit"].get("forceAIToFlee", False), "Exit fleeing differs")
                if field == "userFlagTriggers":
                    _require(binding["label"] == trigger["edit"]["userFlag"], "User flag differs")
        for incoming in activation["incoming_targets"]:
            _require(_get(records[incoming["entity"]]["edit"], incoming["path"]) == manager_name, "Incoming target differs")
        for route in activation.get("routes", []):
            for edge in route["edges"]:
                _require(_get(records[edge["source"]]["edit"], edge["path"]) == edge["target"], "Activation route differs")
        gates = []
        for index, call in enumerate(calls):
            args = list_items(call["args"])
            if not first <= index <= last:
                continue
            if call["eventDef"] in {"waitForEventFlag", "waitMulitpleConditions", "waitAIHealthLevel"} or (call["eventDef"] == "waitAIRemaining" and args[1]["int"] > 0):
                gates.append(dict(event_index=index, event_def=call["eventDef"], args=args))
        _require(activation["event_gates"] == gates, f"Event gates differ: {manager_name}")
        source_creations = {index for index, call in enumerate(calls) if first <= index <= last and call["eventDef"] in {"spawnSingleAI", "spawnAI", "staggeredAISpawn", "maintainAICount"}}
        _require({row["event_index"] for row in segment["creations"]} == source_creations, f"Creation coverage differs: {manager_name}")
        start, end = find_entity_block_bounds(source, manager_name)
        block = source[start:end]
        for field in ("release_anchor", "phase_anchor"):
            if field not in activation:
                continue
            anchor = activation[field]
            index = anchor["event_index"]
            _require(type(index) is int and first <= index <= last, "Phase anchor is outside the source slice")
            left, right = _value_span(block, ("encounterComponent", "entityEvents", f"item[{script_index}]", "events", f"item[{index}]", "eventCall"))
            _require(calls[index]["eventDef"] == anchor["event_def"] and text_sha256(block[left:right].strip()) == anchor["event_sha256"], "Phase anchor differs")
        for control in activation.get("control_events", []):
            call = calls[control["event_index"]]
            _require(call["eventDef"] == control["event_def"] == "activateTarget" and
                list_items(call["args"])[0]["entity"] == control["target"] and control["target"] in records, "Source control target differs")
        if "release_anchor" in activation:
            for grouping in activation["combat_groupings"]:
                group = records[grouping["grouping"]]
                call = calls[grouping["event_index"]]
                _require(call["eventDef"] == "activateCombatGrouping" and list_items(call["args"])[0]["entity"] == grouping["grouping"], "Combat grouping event differs")
                _require(group["class"] == "idCombatGrouping" and list_items(group["edit"]["rolePriorities"]) == [grouping["role"]], "Combat role differs")
        finite_completion = activation.get("finite_maintenance_completion")
        if finite_completion:
            group_index = finite_completion["condition_group_event_index"]
            maintain_index = finite_completion["maintain_wait_event_index"]
            population_index = finite_completion["population_wait_event_index"]
            _require(first <= group_index and (maintain_index, population_index) == (group_index + 1, group_index + 2) and population_index <= last, "Maintenance completion slice differs")
            _require(calls[group_index]["eventDef"] == "waitMulitpleConditions" and list_items(calls[group_index]["args"]) ==
                [dict(int=2), dict(encounterLogicOperator_t="ENCOUNTER_LOGICAL_OP_AND"), dict(bool=False)], "Maintenance completion requires AND")
            _require(calls[maintain_index]["eventDef"] == "waitMaintainComplete" and calls[population_index]["eventDef"] == "waitAIRemaining", "Maintenance completion commands differ")
            _require(list_items(calls[population_index]["args"]) == [dict(eEncounterSpawnType_t="ENCOUNTER_SPAWN_ANY"), dict(int=0), dict(string=""), dict(bool=False)], "Maintenance population barrier differs")
        total = 0
        for creation in segment["creations"]:
            event_index = creation["event_index"]
            key = (manager_name, script_index, event_index)
            _require(key not in identities, f"Duplicate first cohort: {key}")
            identities.add(key)
            call = calls[event_index]
            event_path = ("encounterComponent", "entityEvents", f"item[{script_index}]", "events", f"item[{event_index}]", "eventCall")
            left, right = _value_span(block, event_path)
            _require(text_sha256(block[left:right].strip()) == creation["event_sha256"], f"Event pin differs: {key}")
            _require(call["eventDef"] == creation["event_def"], f"Creation command differs: {key}")
            _require(creation["replacement_constraints"] == typed_consumers(calls, creation["spawn_type"], creation["label"]), f"Typed consumers differ: {key}")
            args = list_items(call["args"])
            maintenance = call["eventDef"] == "maintainAICount"
            grouped = call["eventDef"] != "spawnSingleAI"
            count = args[2]["int"] if maintenance else args[1]["int"] if grouped else 1
            _require(type(count) is int and count > 0 and type(creation["count"]) is int and creation["count"] == count,
                f"Finite count differs: {key}")
            total += count
            label = args[6 if maintenance else 3 if grouped else 2]["string"]
            if call["eventDef"] == "staggeredAISpawn":
                _verify_staggered_completion(calls, event_index, last, creation["spawn_type"], label)
            _require(args[0]["eEncounterSpawnType_t"] == creation["spawn_type"] and label == creation["label"], f"Spawn cohort differs: {key}")
            _require(args[5 if maintenance else 2 if grouped else 1]["entity"] == creation["spawner"], f"Origin spawner differs: {key}")
            if maintenance:
                _require(finite_completion and event_index in finite_completion["maintenance_event_indices"], "Finite maintenance has no exhaustion proof")
                _require(list_items(calls[finite_completion["maintain_wait_event_index"]]["args"]) ==
                    [dict(eEncounterSpawnType_t=creation["spawn_type"]), dict(int=0), dict(string=label), dict(bool=False)], "Maintenance exhaustion cohort differs")
                _require(creation["desired_count"] == args[1]["int"] and creation["count_kind"] == "successful_new_maintenance_emissions", "Maintenance accounting differs")
                _require(creation["replacement_qualification"] == "preserve_source_producer_until_maintenance_materialization_is_qualified", "Maintenance replacement is unqualified")
                for prior_index in creation["existing_cohort_creation_indices"]:
                    prior = next(row for row in segment["creations"] if row["event_index"] == prior_index)
                    _require(prior_index < event_index and prior["label"] == label and prior["spawn_type"] == creation["spawn_type"], "Existing maintenance cohort differs")
            origin = records[creation["spawner"]]
            _require(origin["class"] == ("idTargetSpawnGroup" if grouped else "idTarget_Spawn"), f"Spawner class differs: {key}")
            members = [name for name in list_items(origin["edit"]["spawners"]) if records[name]["class"] != "idTarget_Spawn_Parent"] if grouped else [creation["spawner"]]
            _require([row["spawner"] for row in creation["members"]] == members, f"Group member coverage differs: {key}")
            profile = profiles[creation["species"]]
            concrete_any = creation["spawn_type"] == "ENCOUNTER_SPAWN_ANY" and creation.get("source_identity") == "concrete_spawner_definitions"
            _require(profile["status"] == "qualified" and (creation["spawn_type"] in profile["source_spawn_types"] or concrete_any
                or (creation["spawn_type"] != "ENCOUNTER_SPAWN_ANY" and any(enemy_spawn_mask(creation["spawn_type"]) & enemy_spawn_mask(mask)
                    for mask in profile["source_spawn_types"] if mask != "ENCOUNTER_SPAWN_ANY"))),
                f"Unqualified species mask: {key}")
            if concrete_any:
                _require(creation.get("replacement_qualification") == "preserve_source_event_until_any_materialization_is_qualified",
                    f"ANY origin replacement is unqualified: {key}")
            for member in creation["members"]:
                point = records[member["spawner"]]
                definitions = list_items(point["edit"].get("entityDefs"))
                definition_source = member["spawner"]
                if not definitions and grouped:
                    parent_name = origin["edit"].get("targetSpawnParent")
                    if parent_name:
                        _require(parent_name in records and records[parent_name]["class"] == "idTarget_Spawn_Parent", f"Group parent differs: {key}")
                        parent_definitions = list_items(records[parent_name]["edit"].get("entityDefs"))
                        if len(parent_definitions) == 1:
                            definitions = parent_definitions
                            definition_source = parent_name
                native_selection = len(definitions) > 1
                _require(point["class"] == "idTarget_Spawn", f"Source point class differs: {key}")
                admission_parents = None
                if not definitions:
                    selected_ai, admission_parents = source_definitions.select_admitted_point(point, group_names,
                        creation["spawn_type"], edit.get("aiTypeDefAssignments"), record, context_layers=manager["layers"])
                    _require(member.get("definition_sources") == admission_parents and "definition_source" not in member,
                        f"Definition sources differ: {key}")
                else:
                    _require(member.get("definition_source", member["spawner"]) == definition_source and "definition_sources" not in member,
                        f"Definition source differs: {key}")
                    selected_ai = source_definitions.select(definitions, creation["spawn_type"], edit.get("aiTypeDefAssignments"), record,
                        initial_definitions=list_items(records[definition_source]["edit"].get("initialEntityDefs")))
                _require(selected_ai == member["ai"], f"Authored AI differs: {key}")
                if concrete_any:
                    related = [point, records[definition_source]]
                    _require(definitions == [dict(name=selected_ai)] and all(list_items(value["edit"].get("initialEntityDefs"))
                        in ([], [dict(name=selected_ai)]) for value in related), f"ANY source definition reset differs: {key}")
                parents = [origin["edit"].get("targetSpawnParent")] if grouped else []
                parents.append(point["edit"].get("targetSpawnParent"))
                for parent_name in filter(None, parents if native_selection and admission_parents is None else []):
                    parent = record(parent_name)
                    _require(parent["class"] == "idTarget_Spawn_Parent", f"Parent class differs: {key}")
                    parent_ai = source_definitions.select(list_items(parent["edit"].get("entityDefs")),
                        creation["spawn_type"], edit.get("aiTypeDefAssignments"), record,
                        initial_definitions=list_items(parent["edit"].get("initialEntityDefs")))
                    _require(parent_ai == selected_ai, f"Parent definition differs: {key}")
                ai = records[member["ai"]]
                _require(ai["class"] == "idAI2" and ai["inherit"] in profile["inherits"], f"Species/template binding differs: {key}")
        _require(type(segment["segment_N"]) is int and total == segment["segment_N"], f"Segment N differs: {segment['id']}")
    if source_definitions.ancestry_sha256 is not None:
        _require(catalog.get("definition_ancestry_sha256") == source_definitions.ancestry_sha256, "Definition ancestry pin differs")
    _require(catalog["counting"]["map_N"] is None and catalog["counting"]["campaign_N"] is None, "Segment qualification does not establish map/campaign N")
    return dict(segments=len(segment_ids), scoped_opportunities=sum(row["segment_N"] for row in catalog["segments"]), entities=len(records))


def main():
    root = Path(__file__).resolve().parents[2]
    registry = (root / "content/catalog/enemy_species.json").read_bytes()
    for path in sorted((root / "content/catalog/encounter_opportunities").glob("*.json")):
        catalog = json.loads(path.read_bytes())
        logic = {name: (root / name).read_bytes() for name in catalog.get("logic_sources", {})}
        result = verify_catalog(catalog, (root / catalog["source"]["path"]).read_bytes(), registry, logic_sources=logic, root=root)
        print(json.dumps(dict(map_key=catalog["map_key"], **result)))


if __name__ == "__main__":
    main()
