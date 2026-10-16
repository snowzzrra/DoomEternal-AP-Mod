"""Materialize frozen encounter operations without choosing enemies or waves.

Schema 1 contains map_key, source_sha256 and operations. Each operation selects
manager {entity, sha256}, script_index, event_index, event_def, event_sha256 and
from {species, spawn_type, spawner}. Its sole wave contains one instance with
species, spawn_type, ai, ai_name, spawner_name and four integer attributes.
Entity references contain map_key, entity and sha256. Hashes use UTF-8 text;
entity and eventCall hashes strip surrounding whitespace. species_profiles
supplies spawn_type, optional proved spawn_types and permitted inherits.
Schema 2 also accepts finite spawnAI groups. Each instance pins origin_spawner;
multiple waves pin the immediately following ANY/zero wait as barrier, give each
wave label and equal min_spawn_stagger/max_spawn_stagger, and each instance group_name.
Schema 3 staged operations can pin a private native reserve and its commit/finish triggers.
"""

import hashlib
import math
import re

from doom_eap.content.enemy_options import SPECIES_IDS
from doom_eap.content.enemy_species import validate_enemy_speed_percent
from tools.maps.ap_map_generator import find_entity_block_bounds, find_matching_brace
from tools.maps.encounter_catalog import ENTITY_HEADER, entity_records, list_items, walk_values
from tools.maps.encounter_source_definitions import SourceDefinitions
from tools.maps.enemy_attribute_baselines import resolve_enemy_attribute_baselines
from tools.maps.enemy_attributes import _CLONE_NAME, _MISSING, _get, _member, _render, _set, materialize_enemy_attributes


def text_sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _keys(value, keys, context):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError(f"Unsupported frozen {context} fields")


def _edit_span(block):
    match = re.search(r"\bedit\s*=\s*\{", block)
    if match is None:
        raise ValueError("Entity has no edit object")
    return match.end() - 1, find_matching_brace(block, match.end() - 1)


def _value_span(block, path):
    opening, end = _edit_span(block)
    start = opening + 1
    end -= 1
    for name in path:
        member = _member(block[start:end], name)
        if member is None:
            raise ValueError("Frozen event path is absent: " + ".".join(path))
        relative_start, relative_end, is_object = member
        if not is_object:
            raise ValueError("Frozen event path is not an object: " + ".".join(path))
        opening = start + relative_start
        end = find_matching_brace(block, opening)
        start = opening + 1
    return opening, end


def _edit(block, path, value):
    opening, end = _edit_span(block)
    body = _set(block[opening + 1:end - 1], path, value)
    return block[:opening + 1] + body + block[end - 1:]


def _rename(block, name):
    header = ENTITY_HEADER.search(block)
    return block[:header.start(1)] + name + block[header.end(1):]


def _call(name, args):
    return _render({"eventDef": name, "args": {"num": len(args), **{
        f"item[{index}]": value for index, value in enumerate(args)}}}, 4)


def _layers(block, layers):
    value = "layers { " + " ".join(_render(layer, 0) for layer in layers) + " }"
    header = ENTITY_HEADER.search(block)
    match = re.search(r"\blayers\s*\{", block[:header.start()])
    if match is not None:
        end = find_matching_brace(block, match.end() - 1)
        return block[:match.start()] + value + block[end:]
    opening = block.index("{") + 1
    return block[:opening] + "\n" + value + block[opening:]


def _replace_events(body, events, changes):
    replacements = []
    next_index = 0
    for index in range(len(events)):
        start, end, _ = _member(body, f"item[{index}]")
        field = re.search(rf"\bitem\[{index}\]\s*=\s*$", body[:start])
        indent = re.search(r"[\t ]*$", body[:field.start()]).group()
        values = changes.get(index)
        if values is None:
            values = [body[start:end]]
        else:
            values = ["{ eventCall = " + value + " }" for value in values]
        replacement = ("\n" + indent).join(
            f"item[{next_index + offset}] = " + value for offset, value in enumerate(values))
        replacements.append((field.start(), end, replacement))
        next_index += len(values)
    for start, end, replacement in reversed(replacements):
        body = body[:start] + replacement + body[end:]
    return _set(body, ("num",), next_index)


def materialize_frozen_encounters(source_text, plan, source_maps, *, species_profiles, source_registry=None, source_root=None):
    """Return a complete map only after every frozen operation passes validation.

    Schema 1 supports one instance per spawnSingleAI. Schema 2 expands finite
    spawnAI groups and stages waves only before an immediately following barrier.
    Schema 3 optionally reserves native ZombieT1 in a private manager with queued cleanup.
    Inherited values require a source-pinned baseline; foreign linked entities
    need separate qualification. Inline base_properties is unsupported.
    """
    _keys(plan, ("schema", "map_key", "source_sha256", "operations"), "plan")
    if type(plan["schema"]) is not int or plan["schema"] not in (1, 2, 3):
        raise ValueError("Unsupported frozen plan schema")
    map_key = plan["map_key"]
    if not isinstance(map_key, str) or text_sha256(source_text) != plan["source_sha256"]:
        raise ValueError("Frozen source hash mismatch")
    if not isinstance(plan["operations"], list):
        raise ValueError("Frozen operations must be a list")
    if map_key in source_maps and source_maps[map_key] != source_text:
        raise ValueError("Target map differs from frozen source map")
    if not plan["operations"]:
        return source_text
    source_definitions = SourceDefinitions(source_registry, source_root)
    maps = {**source_maps, map_key: source_text}
    names = {}
    cache = {}
    attribute_bases = {}

    def entity_names(key):
        if key not in names:
            headers = list(ENTITY_HEADER.finditer(maps[key]))
            names[key] = {header.group(1) for header in headers}
            if len(headers) != len(names[key]):
                raise ValueError(f"Duplicate entity name in source map: {key}")
        return names[key]

    def source_entity(key, name):
        if key not in maps or name not in entity_names(key):
            raise ValueError(f"Frozen source entity is absent: {key}/{name}")
        if (key, name) not in cache:
            bounds = find_entity_block_bounds(maps[key], name)
            block = maps[key][bounds[0]:bounds[1]]
            record = next(entity_records(block, include_names={name}))
            cache[key, name] = block, record, bounds
        return cache[key, name]

    def entity(reference, class_name):
        _keys(reference, ("map_key", "entity", "sha256"), "entity reference")
        key, name = reference["map_key"], reference["entity"]
        block, record, bounds = source_entity(key, name)
        if text_sha256(block.strip()) != reference["sha256"]:
            raise ValueError(f"Frozen entity hash mismatch: {key}/{name}")
        if record["class"] != class_name:
            raise ValueError(f"Frozen entity class mismatch: {key}/{name}")
        return block, record, bounds

    def species(value, actor):
        canonical = value["species"]
        profile = species_profiles.get(canonical)
        if canonical not in SPECIES_IDS or not isinstance(profile, dict):
            raise ValueError(f"Unknown canonical species: {canonical}")
        if value["spawn_type"] not in profile.get("spawn_types", (profile.get("spawn_type"),)) or actor["inherit"] not in profile.get("inherits", ()):
            raise ValueError(f"Frozen species/profile/template mismatch: {canonical}")

    original_names = entity_names(map_key)
    source_group_names = set()
    for group in entity_records(source_text):
        if group["class"] != "idTargetSpawnGroup":
            continue
        source_group_names.add(group["name"])
        if group["inherit"] not in ("encounter/spawn_group/zone", "encounter/spawn_group/cylinder"):
            raise ValueError("Unqualified source spawn group defaults")
        if group["edit"].get("exclusive", False):
            raise ValueError("Source exclusive spawn group needs overlap qualification: " + group["name"])
    generated_names = set()
    selected_events = set()
    replacements = []
    clones = []
    scripts = {}
    wave_labels = set()
    reserve_tokens = set()

    def reserve_recipe(reserve, manager, manager_record, origin):
        _keys(reserve, ("commit_template", "finish_template", "manager_name", "commit_name", "finish_name",
            "label", "completion_flag", "desired_count", "min_spawn_delay", "max_spawn_delay"), "reserve")
        if origin["species"] != "zombie_tier_1" or origin["spawn_type"] != "ENCOUNTER_SPAWN_ZOMBIE_TIER_1":
            raise ValueError("Native reserve requires authored ZombieT1 supply")
        tokens = [reserve[field] for field in ("manager_name", "commit_name", "finish_name", "label", "completion_flag")]
        if any(not isinstance(token, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token) is None for token in tokens):
            raise ValueError("Frozen reserve needs private identifiers")
        if len(set(tokens)) != len(tokens) or any(token in original_names or token in generated_names or token in wave_labels
                or token in reserve_tokens or f'"{token}"' in source_text for token in tokens):
            raise ValueError("Frozen reserve identifier collision")
        count = reserve["desired_count"]
        if type(count) is not int or not 1 <= count <= 0x7fffffff:
            raise ValueError("Frozen reserve desired_count must be a positive int32")
        minimum, maximum = reserve["min_spawn_delay"], reserve["max_spawn_delay"]
        if any(type(value) not in (int, float) or not 0 <= value <= 3.4028234663852886e38 or not math.isfinite(value)
               for value in (minimum, maximum)) or maximum < minimum:
            raise ValueError("Frozen reserve needs finite ordered spawn delays")
        trigger_blocks = []
        for field, name, class_name, inherit in (
                ("commit_template", reserve["commit_name"], "idEncounterTrigger_Commit", "encounter/trigger/commit"),
                ("finish_template", reserve["finish_name"], "idEncounterTrigger_RaiseUserFlag", "encounter/trigger/user_flag")):
            trigger, record, _ = entity(reserve[field], class_name)
            if record["inherit"] != inherit:
                raise ValueError("Unqualified reserve trigger inherit")
            trigger = _layers(_rename(trigger, name), manager_record["layers"])
            position = manager_record["edit"].get("spawnPosition")
            if position is not None:
                trigger = _edit(trigger, ("spawnPosition",), position)
            trigger = _edit(trigger, ("clipModelInfo", "clipModelName"), None)
            trigger = _edit(trigger, ("triggerFirst",), False)
            if field == "finish_template":
                trigger = _edit(trigger, ("userFlag",), reserve["completion_flag"])
            trigger_blocks.append(trigger)
        label, flag = reserve["label"], reserve["completion_flag"]
        reserve_calls = [
            ("maintainAICount", [{"eEncounterSpawnType_t": origin["spawn_type"]}, {"int": count}, {"int": 0},
                {"float": minimum}, {"int": 0}, {"entity": origin["spawner"]["entity"]}, {"string": label}, {"float": maximum}]),
            ("waitForEventFlag", [{"eEncounterEventFlags_t": "ENCOUNTER_EVENT_FLAG_USER_EVENT"}, {"string": flag},
                {"bool": True}, {"bool": False}]),
            ("stopMaintainingAICount", [{"eEncounterSpawnType_t": "ENCOUNTER_SPAWN_ANY"}, {"string": label}]),
            # native remove detaches manager records before queued entity destruction
            ("removeAI", [{"eEncounterSpawnType_t": "ENCOUNTER_SPAWN_ANY"}, {"string": label}]),
        ]
        private_manager = _rename(manager, reserve["manager_name"])
        events = {"num": len(reserve_calls), **{f"item[{index}]": {"eventCall": {"eventDef": name,
            "args": {"num": len(args), **{f"item[{arg_index}]": arg for arg_index, arg in enumerate(args)}}}}
            for index, (name, args) in enumerate(reserve_calls)}}
        component = {"entityEvents": {"num": 1, "item[0]": {"entity": reserve["manager_name"], "events": events}}}
        private_manager = _edit(private_manager, ("encounterComponent",), component)
        for field, targets in (("preCombatTriggers", []), ("commitTriggers", [reserve["commit_name"]]),
                ("exitTriggers", []), ("userFlagTriggers", [reserve["finish_name"]])):
            private_manager = _edit(private_manager, (field,), {"num": len(targets), **{
                f"item[{index}]": target for index, target in enumerate(targets)}})
        private_manager = _edit(private_manager, ("ignoreTelemetry",), True)
        private_manager = _edit(private_manager, ("enableAIHighlightOnFinish",), False)
        generated_names.update(tokens[:3])
        reserve_tokens.update(tokens)
        clones.extend((private_manager, *trigger_blocks))
        return (_call("activateTarget", [{"entity": reserve["commit_name"]}, {"string": label}]),
            _call("activateTarget", [{"entity": reserve["finish_name"]}, {"string": flag}]))

    for operation in plan["operations"]:
        waves = operation.get("waves")
        if not isinstance(waves, list) or not waves:
            raise ValueError("Frozen operation requires at least one wave")
        staged = len(waves) > 1
        operation_keys = ("manager", "script_index", "event_index", "event_def", "event_sha256", "from", "waves")
        extra = ("barrier",) if staged and plan["schema"] >= 2 else ()
        if staged and plan["schema"] == 3 and "reserve" in operation:
            extra += ("reserve",)
        _keys(operation, (*operation_keys, *extra), "operation")
        _keys(operation["manager"], ("entity", "sha256"), "manager")
        manager_name = operation["manager"]["entity"]
        manager_ref = {"map_key": map_key, **operation["manager"]}
        manager, manager_record, manager_bounds = entity(manager_ref, "idEncounterManager")
        script_index, event_index = operation["script_index"], operation["event_index"]
        if any(type(index) is not int or index < 0 for index in (script_index, event_index)):
            raise ValueError("Frozen script/event indices must be nonnegative integers")
        event_key = manager_name, script_index, event_index
        if event_key in selected_events:
            raise ValueError("Duplicate frozen operation for one event")
        selected_events.add(event_key)
        event_path = ("encounterComponent", "entityEvents", f"item[{script_index}]", "events", f"item[{event_index}]", "eventCall")
        event_start, event_end = _value_span(manager, event_path)
        event_text = manager[event_start:event_end]
        if text_sha256(event_text.strip()) != operation["event_sha256"]:
            raise ValueError("Frozen event hash mismatch")
        event = _get(manager_record["edit"], event_path)
        event_def = event.get("eventDef")
        if operation["event_def"] != event_def or event_def not in (("spawnSingleAI",) if plan["schema"] == 1 else ("spawnSingleAI", "spawnAI")):
            raise ValueError("Unsupported frozen spawn event")
        args = list_items(event.get("args"))
        grouped = event_def == "spawnAI"
        layout = ("eEncounterSpawnType_t", "int", "entity", "string") if grouped else ("eEncounterSpawnType_t", "entity", "string")
        if len(args) != len(layout) or any(set(arg) != {kind} for arg, kind in zip(args, layout)):
            raise ValueError("Unqualified spawn argument layout")
        if grouped and (type(args[1]["int"]) is not int or args[1]["int"] <= 0):
            raise ValueError("Frozen spawnAI requires a positive finite total")
        if staged and not grouped:
            raise ValueError("Staged waves require an authored spawnAI group")
        origin = operation["from"]
        _keys(origin, ("species", "spawn_type", "spawner"), "origin")
        if origin["spawner"]["map_key"] != map_key:
            raise ValueError("Frozen origin spawner must belong to the target map")
        origin_block, origin_record, _ = entity(origin["spawner"], "idTargetSpawnGroup" if grouped else "idTarget_Spawn")
        if args[0]["eEncounterSpawnType_t"] != origin["spawn_type"] or args[2 if grouped else 1]["entity"] != origin_record["name"]:
            raise ValueError("Frozen origin does not match the selected event")
        source_events = _get(manager_record["edit"], event_path[:-2])
        source_event_items = list_items(source_events)
        if plan["schema"] >= 2:
            _keys(source_event_items[event_index], ("eventCall",), "event wrapper")
        generated_calls = []
        if staged:
            barrier = operation["barrier"]
            _keys(barrier, ("event_index", "event_sha256"), "barrier")
            if type(barrier["event_index"]) is not int or barrier["event_index"] != event_index + 1 or barrier["event_index"] >= len(source_event_items):
                raise ValueError("Frozen barrier must immediately follow the spawn event")
            barrier_index = barrier["event_index"]
            _keys(source_event_items[barrier_index], ("eventCall",), "barrier wrapper")
            barrier_path = (*event_path[:-2], f"item[{barrier_index}]", "eventCall")
            barrier_start, barrier_end = _value_span(manager, barrier_path)
            barrier_call = source_event_items[barrier_index]["eventCall"]
            if text_sha256(manager[barrier_start:barrier_end].strip()) != barrier["event_sha256"]:
                raise ValueError("Frozen barrier hash mismatch")
            if barrier_call != {"eventDef": "waitAIRemaining", "args": {"num": 4,
                    "item[0]": {"eEncounterSpawnType_t": "ENCOUNTER_SPAWN_ANY"}, "item[1]": {"int": 0},
                    "item[2]": {"string": ""}, "item[3]": {"bool": False}}}:
                raise ValueError("Frozen barrier must wait for ANY remaining zero")
            barrier_key = manager_name, script_index, barrier_index
            if barrier_key in selected_events:
                raise ValueError("Duplicate frozen barrier")
            selected_events.add(barrier_key)
        reserve = operation.get("reserve")
        if "reserve" in operation:
            source_scripts = list_items(manager_record["edit"]["encounterComponent"]["entityEvents"])
            if len(source_scripts) != 1 or script_index != 0 or source_scripts[0].get("entity") != manager_name:
                raise ValueError("Native reserve requires one self-owned manager script")
            if barrier_index != len(source_event_items) - 2:
                raise ValueError("Native reserve barrier must be the penultimate source event")
            relay_call = source_event_items[-1].get("eventCall", {})
            relay_args = list_items(relay_call.get("args"))
            if relay_call.get("eventDef") != "activateTarget" or len(relay_args) != 2 or set(relay_args[0]) != {"entity"} or set(relay_args[1]) != {"string"}:
                raise ValueError("Native reserve requires an authored final relay")
            _, relay, _ = source_entity(map_key, relay_args[0]["entity"])
            if relay["class"] != "idTarget_Count" or relay["inherit"] != "target/relay":
                raise ValueError("Unqualified native reserve final relay")
            reserve_start, reserve_finish = reserve_recipe(reserve, manager, manager_record, origin)
            generated_calls.append(reserve_start)
        for wave in waves:
            _keys(wave, ("instances", "label", "min_spawn_stagger", "max_spawn_stagger") if staged else ("instances",), "wave")
            instances = wave["instances"]
            if not isinstance(instances, list) or not instances or (not grouped and len(instances) != 1):
                raise ValueError("Frozen wave has an unsupported instance count")
            if staged:
                label = wave["label"]
                if not isinstance(label, str) or not label or label in wave_labels or label in reserve_tokens or any(value == label for _, value in walk_values(manager_record["edit"])):
                    raise ValueError("Frozen wave needs a private unique label")
                wave_labels.add(label)
                minimum, maximum = wave["min_spawn_stagger"], wave["max_spawn_stagger"]
                if any(type(value) not in (int, float) or value < 0 or value > 3.4028234663852886e38 or not math.isfinite(value)
                       for value in (minimum, maximum)) or maximum != minimum:
                    raise ValueError("Single-member stagger commands require one finite frozen spawn time")
            for instance in instances:
                instance_keys = ("species", "spawn_type", "ai", "ai_name", "spawner_name", "attributes")
                _keys(instance, (*instance_keys, "origin_spawner", "group_name") if staged else (*instance_keys, "origin_spawner") if grouped else instance_keys, "instance")
                if grouped:
                    if instance["origin_spawner"]["map_key"] != map_key:
                        raise ValueError("Frozen member spawner must belong to the target map")
                    spawner, spawner_record, _ = entity(instance["origin_spawner"], "idTarget_Spawn")
                    if spawner_record["name"] not in list_items(origin_record["edit"].get("spawners")):
                        raise ValueError("Frozen member spawner is outside the authored group")
                else:
                    spawner, spawner_record = origin_block, origin_record
                definitions = list_items(spawner_record["edit"].get("entityDefs"))
                definition_row = spawner_record
                if not definitions and grouped:
                    parent_name = origin_record["edit"].get("targetSpawnParent")
                    if parent_name:
                        _, parent, _ = source_entity(map_key, parent_name)
                        if parent["class"] != "idTarget_Spawn_Parent":
                            raise ValueError("Origin group parent is not idTarget_Spawn_Parent")
                        parent_definitions = list_items(parent["edit"].get("entityDefs"))
                        if len(parent_definitions) == 1:
                            definitions = parent_definitions
                            definition_row = parent
                native_selection = len(definitions) > 1
                if len(definitions) == 1 and set(definitions[0]) != {"name"}:
                    raise ValueError("Origin spawner must select exactly one authored AI")
                def source_record(name):
                    return source_entity(map_key, name)[1]
                admission_parents = None
                if not definitions:
                    original_ai_name, admission_parents = source_definitions.select_admitted_point(spawner_record, source_group_names,
                        origin["spawn_type"], manager_record["edit"].get("aiTypeDefAssignments"), source_record,
                        context_layers=manager_record["layers"])
                else:
                    original_ai_name = source_definitions.select(definitions, origin["spawn_type"],
                        manager_record["edit"].get("aiTypeDefAssignments"), source_record,
                        initial_definitions=list_items(definition_row["edit"].get("initialEntityDefs")))
                parent_names = [origin_record["edit"].get("targetSpawnParent")] if grouped else []
                parent_names.append(spawner_record["edit"].get("targetSpawnParent"))
                for parent_name in filter(None, parent_names if native_selection and admission_parents is None else []):
                    parent = source_record(parent_name)
                    if parent["class"] != "idTarget_Spawn_Parent":
                        raise ValueError("Origin parent is not idTarget_Spawn_Parent")
                    selected_parent = source_definitions.select(list_items(parent["edit"].get("entityDefs")),
                        origin["spawn_type"], manager_record["edit"].get("aiTypeDefAssignments"), source_record,
                        initial_definitions=list_items(parent["edit"].get("initialEntityDefs")))
                    if selected_parent != original_ai_name:
                        raise ValueError("Origin parent selects another authored AI")
                _, original_ai, _ = source_entity(map_key, original_ai_name)
                if original_ai["class"] != "idAI2":
                    raise ValueError("Origin spawner does not reference idAI2")
                species(origin, original_ai)
                ai, ai_record, _ = entity(instance["ai"], "idAI2")
                species(instance, ai_record)
                attributes = instance["attributes"]
                _keys(attributes, ("health_percent", "damage_percent", "speed_percent", "size_percent"), "attributes")
                speed = attributes["speed_percent"]
                validate_enemy_speed_percent(instance["species"], speed)
                group_name = instance["group_name"] if staged else "owner_" + instance["spawner_name"]
                for name in (instance["ai_name"], instance["spawner_name"], group_name):
                    match = _CLONE_NAME.fullmatch(name)
                    if match is None or int(match.group(1)) != speed:
                        raise ValueError("Frozen clone name does not encode its speed_percent")
                    if name in original_names or name in generated_names or name in reserve_tokens:
                        raise ValueError(f"Duplicate frozen clone name: {name}")
                    generated_names.add(name)
                paths = []
                if attributes["health_percent"] != 100:
                    paths.extend(("aiHealth", "components", "components[0]", field) for field in ("max", "starting"))
                if attributes["damage_percent"] != 100:
                    paths.append(("aiEditable", "damageOutModifiers"))
                if attributes["size_percent"] != 100:
                    paths.extend(("renderModelInfo", "scale", axis) for axis in ("x", "y", "z"))
                inherited = any(_get(ai_record["edit"], path) is _MISSING for path in paths)
                if inherited and ai_record["sha256"] not in attribute_bases:
                    attribute_bases[ai_record["sha256"]] = resolve_enemy_attribute_baselines(ai)
                ai_clone = materialize_enemy_attributes(ai, instance["ai_name"],
                    base_properties=attribute_bases[ai_record["sha256"]] if inherited else None,
                    **{key: value for key, value in attributes.items() if key != "speed_percent"})
                for path, value in walk_values(ai_record["edit"]):
                    if value == ai_record["name"]:
                        ai_clone = _edit(ai_clone, tuple(path.split(".")), instance["ai_name"])
                    elif isinstance(value, str) and value in entity_names(instance["ai"]["map_key"]) and instance["ai"]["map_key"] != map_key:
                        raise ValueError(f"Foreign AI has an unresolved entity reference: {path}")
                point = spawner_record["edit"].get("spawnPosition")
                if not isinstance(point, dict) or set(point) != {"x", "y", "z"}:
                    raise ValueError("Origin spawner needs an authored spawnPosition")
                ai_clone = _edit(ai_clone, ("spawnPosition",), point)
                orientation = spawner_record["edit"].get("spawnOrientation")
                if orientation is not None:
                    ai_clone = _edit(ai_clone, ("spawnOrientation",), orientation)
                elif "spawnOrientation" in ai_record["edit"]:
                    raise ValueError("Origin spawner orientation is not authored for this AI template")
                spawner_clone = _rename(spawner, instance["spawner_name"])
                for path, value in walk_values(spawner_record["edit"]):
                    if value == original_ai_name:
                        spawner_clone = _edit(spawner_clone, tuple(path.split(".")), instance["ai_name"])
                definitions = {"num": 1, "item[0]": {"name": instance["ai_name"]}}
                spawner_clone = _edit(spawner_clone, ("entityDefs",), definitions)
                spawner_clone = _edit(spawner_clone, ("initialEntityDefs",), definitions)
                group_settings = {"spawners": {"num": 1, "item[0]": instance["spawner_name"]},
                    "exclusive": True, "includeTouchingSpawners": False, "targetSpawnSubGroups": {"num": 0},
                    "remainActiveAfterTouch": False, "includeInGlobalOverrideGroup": False}
                if staged:
                    group_clone = _rename(origin_block, group_name)
                    for field, value in group_settings.items():
                        group_clone = _edit(group_clone, (field,), value)
                    generated_calls.append(_call("staggeredAISpawn", [{"eEncounterSpawnType_t": instance["spawn_type"]},
                        {"int": 1}, {"entity": group_name}, {"string": label}, {"float": minimum}, {"float": maximum}]))
                else:
                    group_clone = ('entity {\nentityDef ' + group_name + ' {\n'
                        'inherit = "encounter/spawn_group/zone";\nclass = "idTargetSpawnGroup";\nedit = '
                        + _render(group_settings, 2) + '\n}\n}')
                    generated_calls.append(_call("spawnSingleAI", [{"eEncounterSpawnType_t": instance["spawn_type"]},
                        {"entity": instance["spawner_name"]}, {"string": args[-1]["string"]}]))
                clones.extend((group_clone, spawner_clone, ai_clone))
            if staged:
                generated_calls.append(_call("waitStaggeredSpawnComplete", [{"eEncounterSpawnType_t": "ENCOUNTER_SPAWN_ANY"},
                    {"int": 0}, {"string": label}, {"bool": False}]))
                generated_calls.append(manager[barrier_start:barrier_end])
        if "reserve" in operation:
            generated_calls.append(reserve_finish)
        if plan["schema"] == 1:
            instance = waves[0]["instances"][0]
            event_body = _set(event_text[1:-1], ("args", "item[0]", "eEncounterSpawnType_t"), instance["spawn_type"])
            event_body = _set(event_body, ("args", "item[1]", "entity"), instance["spawner_name"])
            replacements.append((manager_bounds[0] + event_start, manager_bounds[0] + event_end, "{" + event_body + "}"))
        else:
            key = manager_name, script_index
            scripts.setdefault(key, (manager, manager_bounds, source_event_items, {}))[3][event_index] = generated_calls
            if staged:
                scripts[key][3][barrier_index] = []

    for (manager_name, script_index), (manager, manager_bounds, events, changes) in scripts.items():
        path = ("encounterComponent", "entityEvents", f"item[{script_index}]", "events")
        start, end = _value_span(manager, path)
        replacements.append((manager_bounds[0] + start, manager_bounds[0] + end,
            "{" + _replace_events(manager[start + 1:end - 1], events, changes) + "}"))
    result = source_text
    for start, end, replacement in sorted(replacements, reverse=True):
        result = result[:start] + replacement + result[end:]
    if clones:
        result += "\n" + "\n".join(clones) + "\n"
    for block in clones:
        next(entity_records(block, include_names=generated_names))
    for manager_name, _, _ in selected_events:
        bounds = find_entity_block_bounds(result, manager_name)
        next(entity_records(result[bounds[0]:bounds[1]], include_names={manager_name}))
    return result
