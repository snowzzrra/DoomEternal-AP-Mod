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
Schema 4 also stages single and staggered sources within finite, control-free phases.
Schema 5 combines reinforcements by phase and accepts removed spawn calls.
"""

import hashlib
import math
import re

from doom_eap.content.enemy_options import SPECIES_IDS
from doom_eap.content.enemy_species import enemy_spawn_mask, enemy_tracking_suffix, validate_enemy_speed_percent
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


def finite_population_cleared(events, barrier_index):
    live = []
    pending = []
    for index, item in enumerate(events[:barrier_index + 1]):
        call = item["eventCall"]
        kind = call["eventDef"]
        args = list_items(call.get("args"))
        if kind in ("spawnSingleAI", "spawnAI", "staggeredAISpawn"):
            if kind != "spawnSingleAI" and (type(args[1].get("int")) is not int or args[1]["int"] <= 0):
                return False
            cohort = (enemy_spawn_mask(args[0]["eEncounterSpawnType_t"]), args[2 if kind == "spawnSingleAI" else 3]["string"])
            live.append(cohort)
            if kind == "staggeredAISpawn":
                pending.append(cohort)
        elif kind == "waitStaggeredSpawnComplete":
            if len(args) == 4 and type(args[1].get("int")) is int and args[1]["int"] == 0:
                mask = enemy_spawn_mask(args[0]["eEncounterSpawnType_t"])
                label = args[2]["string"]
                pending = [(source, cohort) for source, cohort in pending if source & ~mask or (label and cohort != label)]
        elif kind == "waitAIRemaining":
            if len(args) != 4 or type(args[1].get("int")) is not int or args[1]["int"] < 0:
                return False
            if args[1]["int"]:
                continue
            mask = enemy_spawn_mask(args[0]["eEncounterSpawnType_t"])
            label = args[2]["string"]
            # A temporarily empty cohort can still have pending native emissions.
            if any(source & mask and (not label or cohort == label) for source, cohort in pending):
                return False
            remaining = [(source, cohort) for source, cohort in live if source & ~mask or (label and cohort != label)]
            if index == barrier_index:
                return bool(live) and not remaining
            live = remaining
        elif kind not in ("wait", "waitStaggeredSpawnComplete"):
            return False
    return False


def materialize_frozen_encounters(source_text, plan, source_maps, *, species_profiles, source_registry=None, source_root=None):
    """Return a complete map only after every frozen operation passes validation.

    Schema 1 supports one instance per spawnSingleAI. Schema 2 expands finite
    spawnAI groups and stages waves only before an immediately following barrier.
    Schema 3 optionally reserves native ZombieT1 in a private manager with queued cleanup.
    Inherited values require a source-pinned baseline; foreign linked entities
    need separate qualification. Inline base_properties is unsupported.
    """
    _keys(plan, ("schema", "map_key", "source_sha256", "operations"), "plan")
    if type(plan["schema"]) is not int or plan["schema"] not in (1, 2, 3, 4, 5, 6, 7):
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
    phase_waves = {}
    phase_queues = {}
    phase_supplies = {}
    source_ready_flags = {}
    wave_labels = set()
    reserve_tokens = set()

    def reserve_recipe(reserve, manager, manager_record, origin):
        _keys(reserve, ("commit_template", "finish_template", "manager_name", "commit_name", "finish_name",
            "label", "completion_flag", "desired_count", "min_spawn_delay", "max_spawn_delay",
            *(("supply_source",) if isinstance(reserve, dict) and "supply_source" in reserve else ())), "reserve")
        supply = reserve.get("supply_source")
        if supply:
            _keys(supply, ("species", "spawn_type", "ai", "spawners"), "supply source")
            if supply["species"] != "imp" or supply["spawn_type"] != "ENCOUNTER_SPAWN_IMP":
                raise ValueError("Native reserve needs normal Imp supply")
            if not isinstance(supply["spawners"], list) or not supply["spawners"]:
                raise ValueError("Native reserve source points are missing")
            ai, ai_record, _ = entity(supply["ai"], "idAI2")
            species(supply, ai_record)
            ai_name = reserve["label"] + "_ai__ap_v070_s00100"
            group_name = reserve["label"] + "_spawns"
            names = [ai_name, group_name]
            ai_clone = _rename(ai, ai_name).replace('"' + ai_record["name"] + '"', '"' + ai_name + '"')
            foreign = entity_names(supply["ai"]["map_key"]) - original_names - {ai_record["name"]}
            if any('"' + name + '"' in ai_clone for name in foreign):
                raise ValueError("Native reserve AI has unresolved foreign entity references")
            points = []
            definitions = {"num": 1, "item[0]": {"name": ai_name}}
            for index, reference in enumerate(supply["spawners"]):
                if reference["map_key"] != map_key:
                    raise ValueError("Native reserve source points must belong to the map")
                point, _, _ = entity(reference, "idTarget_Spawn")
                point_name = reserve["label"] + "_point_" + str(index)
                point = _rename(point, point_name)
                point = _edit(point, ("entityDefs",), definitions)
                point = _edit(point, ("initialEntityDefs",), definitions)
                point = _edit(point, ("targetSpawnParent",), "")
                points.append(point_name)
                names.append(point_name)
                clones.append(point)
            if any(name in original_names or name in generated_names for name in names):
                raise ValueError("Native reserve identifier collision")
            settings = {"spawners": {"num": len(points), **{f"item[{i}]": name for i, name in enumerate(points)}},
                        "exclusive": True, "includeTouchingSpawners": False, "targetSpawnSubGroups": {"num": 0},
                        "remainActiveAfterTouch": False, "includeInGlobalOverrideGroup": False}
            group = ('entity { entityDef ' + group_name + ' { inherit = "encounter/spawn_group/zone"; '
                     'class = "idTargetSpawnGroup"; edit = ' + _render(settings, 2) + ' } }')
            clones.extend((_layers(group, manager_record["layers"]), ai_clone))
            generated_names.update(names)
            origin = {**supply, "spawner": {"entity": group_name}}
        elif origin["species"] != "zombie_tier_1" or origin["spawn_type"] != "ENCOUNTER_SPAWN_ZOMBIE_TIER_1":
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
        ready_calls = []
        start_calls = [_call("activateTarget", [{"entity": reserve["commit_name"]}, {"string": label}])]
        if supply and plan["schema"] >= 5:
            ready_name, ready_flag = reserve["manager_name"] + "_ready", flag + "_ready"
            if ready_name in original_names or ready_name in generated_names:
                raise ValueError("Native reserve readiness identifier collision")
            trigger_blocks.append(_edit(_rename(trigger_blocks[-1], ready_name), ("userFlag",), ready_flag))
            generated_names.add(ready_name)
            source_ready_flags.setdefault(manager_record["name"], []).append(ready_name)
            ready_calls = [
                ("staggeredAISpawn", [{"eEncounterSpawnType_t": origin["spawn_type"]}, {"int": 1},
                    {"entity": origin["spawner"]["entity"]}, {"string": label}, {"float": 0}, {"float": 0}]),
                ("waitStaggeredSpawnComplete", [{"eEncounterSpawnType_t": "ENCOUNTER_SPAWN_ANY"},
                    {"int": 0}, {"string": label}, {"bool": False}]),
                ("activateTarget", [{"entity": ready_name}, {"string": ready_flag}]),
            ]
            start_calls.append(_call("waitForEventFlag", [{"eEncounterEventFlags_t": "ENCOUNTER_EVENT_FLAG_USER_EVENT"},
                {"string": ready_flag}, {"bool": True}, {"bool": False}]))
        reserve_calls = [*ready_calls,
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
        return (start_calls, _call("activateTarget", [{"entity": reserve["finish_name"]}, {"string": flag}]))

    for operation in plan["operations"]:
        waves = operation.get("waves")
        if not isinstance(waves, list) or (not waves and plan["schema"] < 5):
            raise ValueError("Frozen operation requires at least one wave")
        staged = len(waves) > 1
        ambient = plan["schema"] >= 6 and operation.get("barrier") is None
        if ambient and staged:
            raise ValueError("Ambient emissions require a single frozen queue")
        operation_keys = ("manager", "script_index", "event_index", "event_def", "event_sha256", "from", "waves")
        extra = ("barrier",) if (staged and plan["schema"] >= 2) or plan["schema"] >= 4 else ()
        if "reserve" in operation and (plan["schema"] >= 5 or staged and plan["schema"] == 3):
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
        supported = ("spawnSingleAI",) if plan["schema"] == 1 else ("spawnSingleAI", "spawnAI")
        if plan["schema"] >= 4:
            supported += ("staggeredAISpawn",)
        if operation["event_def"] != event_def or event_def not in supported:
            raise ValueError("Unsupported frozen spawn event")
        args = list_items(event.get("args"))
        grouped = event_def != "spawnSingleAI"
        layout = ("eEncounterSpawnType_t", "int", "entity", "string") if grouped else ("eEncounterSpawnType_t", "entity", "string")
        if event_def == "staggeredAISpawn":
            layout += ("float", "float")
        if len(args) != len(layout) or any(set(arg) != {kind} for arg, kind in zip(args, layout)):
            raise ValueError("Unqualified spawn argument layout")
        if grouped and (type(args[1]["int"]) is not int or args[1]["int"] <= 0):
            raise ValueError("Frozen spawnAI requires a positive finite total")
        if staged and not grouped and plan["schema"] < 4:
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
        if ambient:
            source_scripts = list_items(manager_record["edit"]["encounterComponent"]["entityEvents"])
            allowed = {"spawnSingleAI", "spawnAI", "wait", "waitStaggeredSpawnComplete"}
            if (len(source_scripts) != 1 or source_scripts[0].get("entity") != manager_name
                    or any(row.get("eventCall", {}).get("eventDef") not in allowed for row in source_event_items)):
                raise ValueError("Ambient emissions cannot alter encounter control or death gates")
            for row in source_event_items:
                call = row["eventCall"]
                if call["eventDef"] in ("spawnAI", "staggeredAISpawn"):
                    native_args = list_items(call["args"])
                    if type(native_args[1].get("int")) is not int or native_args[1]["int"] <= 0:
                        raise ValueError("Ambient emissions require positive finite source requests")
        if not ambient and (staged or plan["schema"] >= 4):
            barrier = operation["barrier"]
            _keys(barrier, ("event_index", "event_sha256"), "barrier")
            if type(barrier["event_index"]) is not int or not event_index < barrier["event_index"] < len(source_event_items):
                raise ValueError("Frozen barrier must follow the spawn event")
            if plan["schema"] < 4 and barrier["event_index"] != event_index + 1:
                raise ValueError("Frozen barrier must immediately follow the spawn event")
            barrier_index = barrier["event_index"]
            if plan["schema"] >= 4:
                scripts_in_source = list_items(manager_record["edit"]["encounterComponent"]["entityEvents"])
                if len(scripts_in_source) != 1 or scripts_in_source[0].get("entity") != manager_name:
                    raise ValueError("Finite phase waves require one manager script")
                finite_calls = {"spawnSingleAI", "spawnAI", "staggeredAISpawn", "wait", "waitStaggeredSpawnComplete"}
                if any(item.get("eventCall", {}).get("eventDef") not in finite_calls
                       for item in source_event_items[event_index + 1:barrier_index]):
                    raise ValueError("Finite phase waves cannot cross control or death-count gates")
                for item in source_event_items:
                    source_call = item.get("eventCall", {})
                    source_def = source_call.get("eventDef")
                    if source_def in ("maintainAICount", "waitKillCount"):
                        raise ValueError("Finite phase waves cannot alter renewable or kill-count scripts")
                    if source_def in ("spawnAI", "staggeredAISpawn"):
                        source_args = list_items(source_call.get("args"))
                        if len(source_args) < 2 or type(source_args[1].get("int")) is not int or source_args[1]["int"] <= 0:
                            raise ValueError("Finite phase waves require finite source requests")
                pending_labels = dict.fromkeys(list_items(item["eventCall"]["args"])[3]["string"]
                    for item in source_event_items[:event_index] if item.get("eventCall", {}).get("eventDef") == "staggeredAISpawn")
                for pending_label in pending_labels if staged and plan["schema"] == 4 else ():
                    generated_calls.append(_call("waitStaggeredSpawnComplete", [
                        {"eEncounterSpawnType_t": "ENCOUNTER_SPAWN_ANY"}, {"int": 0}, {"string": pending_label}, {"bool": False}]))
            _keys(source_event_items[barrier_index], ("eventCall",), "barrier wrapper")
            barrier_path = (*event_path[:-2], f"item[{barrier_index}]", "eventCall")
            barrier_start, barrier_end = _value_span(manager, barrier_path)
            barrier_call = source_event_items[barrier_index]["eventCall"]
            if text_sha256(manager[barrier_start:barrier_end].strip()) != barrier["event_sha256"]:
                raise ValueError("Frozen barrier hash mismatch")
            barrier_args = list_items(barrier_call.get("args"))
            source_label = args[3 if grouped else 2]["string"]
            clears_population = bool(barrier_args) and barrier_args[0] == {"eEncounterSpawnType_t": "ENCOUNTER_SPAWN_ANY"}
            if not clears_population and plan["schema"] >= 7:
                clears_population = (not list_items(manager_record["edit"].get("adoptedStandAloneAI"))
                                     and not list_items(manager_record["edit"].get("preCombatTriggers"))
                                     and finite_population_cleared(source_event_items, barrier_index))
            if (barrier_call.get("eventDef") != "waitAIRemaining" or len(barrier_args) != 4
                    or not clears_population or type(barrier_args[1].get("int")) is not int or barrier_args[1]["int"] != 0
                    or barrier_args[2] not in ({"string": ""}, {"string": source_label})
                    or set(barrier_args[3]) != {"bool"} or type(barrier_args[3]["bool"]) is not bool
                    or (plan["schema"] < 4 and barrier_args[2:] != [{"string": ""}, {"bool": False}])):
                raise ValueError("Frozen barrier must wait for ANY remaining zero")
            barrier_key = manager_name, script_index, barrier_index
            if staged and plan["schema"] < 4 and barrier_index == event_index + 1:
                if barrier_key in selected_events:
                    raise ValueError("Duplicate frozen barrier")
                selected_events.add(barrier_key)
        reserve = operation.get("reserve")
        if "reserve" in operation:
            source_scripts = list_items(manager_record["edit"]["encounterComponent"]["entityEvents"])
            if len(source_scripts) != 1 or script_index != 0 or source_scripts[0].get("entity") != manager_name:
                raise ValueError("Native reserve requires one self-owned manager script")
            if plan["schema"] < 5:
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
            generated_calls.extend(reserve_start)
            if plan["schema"] >= 5:
                if ambient:
                    raise ValueError("Renewable combat supply needs a closing native gate")
                phase_supplies.setdefault(barrier_key, []).append(reserve_finish)
        if plan["schema"] >= 5 and not ambient and waves:
            queue_type = origin["spawn_type"] if plan["schema"] >= 7 else "ENCOUNTER_SPAWN_ANY"
            phase_queues.setdefault(barrier_key, set()).add((queue_type, args[3 if grouped else 2]["string"]))
        for wave_index, wave in enumerate(waves):
            queued = staged and (plan["schema"] < 5 or wave_index > 0)
            wave_calls = []
            _keys(wave, ("instances", "label", "min_spawn_stagger", "max_spawn_stagger") if queued else ("instances",), "wave")
            instances = wave["instances"]
            if not isinstance(instances, list) or not instances or (not ambient and not grouped and len(instances) != 1):
                raise ValueError("Frozen wave has an unsupported instance count")
            if not ambient and plan["schema"] >= 4 and len(instances) > (args[1]["int"] if grouped else 1):
                raise ValueError("Frozen wave exceeds its finite source request")
            if queued:
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
                instance_fields = (*instance_keys, "origin_spawner") if grouped else instance_keys
                if staged or plan["schema"] >= 7:
                    instance_fields += ("group_name",)
                _keys(instance, instance_fields, "instance")
                if plan["schema"] >= 7:
                    tracking = enemy_tracking_suffix(origin["spawn_type"], instance["spawn_type"])
                    if any(not instance[field].rsplit("_l", 1)[0].endswith(tracking)
                           for field in ("ai_name", "spawner_name", "group_name")):
                        raise ValueError("Frozen tracking differs from the native source slot")
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
                group_name = instance["group_name"] if staged or plan["schema"] >= 7 else "owner_" + instance["spawner_name"]
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
                # native spawning applies the point world axis even when its decl omits it.
                spawner_clone = _rename(spawner, instance["spawner_name"])
                for path, value in walk_values(spawner_record["edit"]):
                    if value == original_ai_name:
                        spawner_clone = _edit(spawner_clone, tuple(path.split(".")), instance["ai_name"])
                definitions = {"num": 1, "item[0]": {"name": instance["ai_name"]}}
                spawner_clone = _edit(spawner_clone, ("entityDefs",), definitions)
                spawner_clone = _edit(spawner_clone, ("initialEntityDefs",), definitions)
                spawner_clone = _edit(spawner_clone, ("targetSpawnParent",), "")
                group_settings = {"spawners": {"num": 1, "item[0]": instance["spawner_name"]},
                    "exclusive": True, "includeTouchingSpawners": False, "targetSpawnSubGroups": {"num": 0},
                    "remainActiveAfterTouch": False, "includeInGlobalOverrideGroup": False}
                if staged and grouped:
                    group_clone = _rename(origin_block, group_name)
                    for field, value in group_settings.items():
                        group_clone = _edit(group_clone, (field,), value)
                else:
                    group_clone = ('entity {\nentityDef ' + group_name + ' {\n'
                        'inherit = "encounter/spawn_group/zone";\nclass = "idTargetSpawnGroup";\nedit = '
                        + _render(group_settings, 2) + '\n}\n}')
                spawn_type = origin["spawn_type"] if plan["schema"] >= 7 else instance["spawn_type"]
                if ambient:
                    native_label = args[3 if grouped else 2]["string"]
                    delays = [{"float": 0}, {"float": 0}]
                    wave_calls.append(_call("staggeredAISpawn", [{"eEncounterSpawnType_t": spawn_type},
                        {"int": 1}, {"entity": group_name}, {"string": native_label}, *delays]))
                elif plan["schema"] >= 5 or queued or event_def == "staggeredAISpawn":
                    delays = ([{"float": minimum}, {"float": maximum}] if queued else args[4:6]
                              if event_def == "staggeredAISpawn" else [{"float": 0}, {"float": 0}])
                    wave_calls.append(_call("staggeredAISpawn", [{"eEncounterSpawnType_t": spawn_type},
                        {"int": 1}, {"entity": group_name},
                        {"string": label if queued else args[3 if grouped else 2]["string"]}, *delays]))
                else:
                    wave_calls.append(_call("spawnSingleAI", [{"eEncounterSpawnType_t": spawn_type},
                        {"entity": instance["spawner_name"]}, {"string": args[3 if grouped else 2]["string"]}]))
                clones.extend((group_clone, spawner_clone, ai_clone))
            if ambient:
                wave_calls.append(_call("waitStaggeredSpawnComplete", [{"eEncounterSpawnType_t": "ENCOUNTER_SPAWN_ANY"},
                    {"int": 0}, {"string": native_label}, {"bool": False}]))
            if plan["schema"] >= 5 and wave_index:
                wait = _call("waitStaggeredSpawnComplete", [{"eEncounterSpawnType_t": "ENCOUNTER_SPAWN_ANY"},
                    {"int": 0}, {"string": label}, {"bool": False}])
                clear = _call("waitAIRemaining", [*barrier_args[:2], {"string": label}, barrier_args[3]])
                phase_waves.setdefault(barrier_key, {}).setdefault(wave_index, []).append((wave_calls, wait, clear))
            elif staged and plan["schema"] < 5:
                wave_calls.append(_call("waitStaggeredSpawnComplete", [{"eEncounterSpawnType_t": "ENCOUNTER_SPAWN_ANY"},
                    {"int": 0}, {"string": label}, {"bool": False}]))
                if plan["schema"] == 4:
                    # isolate each wave while preserving the final native gate
                    wave_calls.append(_call("waitAIRemaining", [*barrier_args[:2], {"string": label}, barrier_args[3]]))
                else:
                    wave_calls.append(manager[barrier_start:barrier_end])
            if plan["schema"] < 5 or wave_index == 0:
                generated_calls.extend(wave_calls)
        if "reserve" in operation and plan["schema"] < 5:
            generated_calls.append(reserve_finish)
        if plan["schema"] == 1:
            instance = waves[0]["instances"][0]
            event_body = _set(event_text[1:-1], ("args", "item[0]", "eEncounterSpawnType_t"), instance["spawn_type"])
            event_body = _set(event_body, ("args", "item[1]", "entity"), instance["spawner_name"])
            replacements.append((manager_bounds[0] + event_start, manager_bounds[0] + event_end, "{" + event_body + "}"))
        else:
            key = manager_name, script_index
            scripts.setdefault(key, (manager, manager_bounds, source_event_items, {}))[3][event_index] = generated_calls
            if staged and plan["schema"] < 4 and barrier_index == event_index + 1:
                scripts[key][3][barrier_index] = []

    for (manager_name, script_index, barrier_index), rounds in phase_waves.items():
        manager, _, events, changes = scripts[manager_name, script_index]
        path = ("encounterComponent", "entityEvents", f"item[{script_index}]", "events", f"item[{barrier_index}]", "eventCall")
        start, end = _value_span(manager, path)
        barrier = manager[start:end]
        calls = []
        labels = dict.fromkeys(list_items(item["eventCall"]["args"])[3]["string"] for item in events[:barrier_index]
            if item.get("eventCall", {}).get("eventDef") == "staggeredAISpawn")
        for label in labels:
            calls.append(_call("waitStaggeredSpawnComplete", [{"eEncounterSpawnType_t": "ENCOUNTER_SPAWN_ANY"},
                {"int": 0}, {"string": label}, {"bool": False}]))
        calls.append(barrier)
        for index in sorted(rounds):
            for spawns, _, _ in rounds[index]:
                calls.extend(spawns)
            calls.extend(wait for _, wait, _ in rounds[index])
            calls.extend(clear for _, _, clear in rounds[index])
        calls.append(barrier)
        changes[barrier_index] = calls

    for (manager_name, script_index, barrier_index), labels in phase_queues.items():
        manager, _, _, changes = scripts[manager_name, script_index]
        path = ("encounterComponent", "entityEvents", f"item[{script_index}]", "events", f"item[{barrier_index}]", "eventCall")
        start, end = _value_span(manager, path)
        gates = [_call("waitStaggeredSpawnComplete", [{"eEncounterSpawnType_t": source_type},
            {"int": 0}, {"string": label}, {"bool": False}]) for source_type, label in sorted(labels)]
        changes[barrier_index] = [*gates, *changes.get(barrier_index, [manager[start:end]])]

    for (manager_name, script_index, barrier_index), finishes in phase_supplies.items():
        manager, _, _, changes = scripts[manager_name, script_index]
        path = ("encounterComponent", "entityEvents", f"item[{script_index}]", "events", f"item[{barrier_index}]", "eventCall")
        start, end = _value_span(manager, path)
        changes[barrier_index] = [*changes.get(barrier_index, [manager[start:end]]), *finishes]

    for (manager_name, script_index), (manager, manager_bounds, events, changes) in scripts.items():
        path = ("encounterComponent", "entityEvents", f"item[{script_index}]", "events")
        start, end = _value_span(manager, path)
        replacements.append((manager_bounds[0] + start, manager_bounds[0] + end,
            "{" + _replace_events(manager[start + 1:end - 1], events, changes) + "}"))
    result = source_text
    for start, end, replacement in sorted(replacements, reverse=True):
        result = result[:start] + replacement + result[end:]
    for manager_name, flags in source_ready_flags.items():
        start, end = find_entity_block_bounds(result, manager_name)
        block = result[start:end]
        record = next(entity_records(block, include_names={manager_name}))
        targets = [*list_items(record["edit"].get("userFlagTriggers")), *flags]
        block = _edit(block, ("userFlagTriggers",), {"num": len(targets), **{
            f"item[{index}]": name for index, name in enumerate(targets)}})
        result = result[:start] + block + result[end:]
    if clones:
        result += "\n" + "\n".join(clones) + "\n"
    for block in clones:
        next(entity_records(block, include_names=generated_names))
    for manager_name, _, _ in selected_events:
        bounds = find_entity_block_bounds(result, manager_name)
        next(entity_records(result[bounds[0]:bounds[1]], include_names={manager_name}))
    return result
