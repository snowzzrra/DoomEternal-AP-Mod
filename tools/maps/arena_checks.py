"""Insert checks at authored arena completion points."""

from doom_eap.content.arena_checks import arena_check_name, load_arena_checks
from tools.maps.ap_map_generator import (
    apply_entity_edits,
    entity_definition_index,
    extract_target_names,
    find_entity_block_bounds,
    generate_check_event,
    generate_event_relay,
    generate_pickup_notification,
    replace_targets_block,
)
from tools.maps.encounter_catalog import entity_records, list_items
from tools.maps.enemy_attributes import _edit
from tools.maps.master_levels import in_layer


def room_compile_maps(maps, active_locations, root):
    active = set(active_locations)
    arena_maps = {entry.get("map_key", key) for key, entry in load_arena_checks(root).items()
                  if any(arena["location_id"] in active for arena in entry["arenas"])}
    return {key: row for key, row in maps.items() if row["recipes"] or key in arena_maps}


def _replace_entity(text, name, transform, index, changes):
    bounds = find_entity_block_bounds(text, name, index=index)
    if bounds is None:
        raise ValueError(f"Arena completion entity missing: {name}")
    start, end = bounds
    block = changes.get(name, (start, end, text[start:end]))[2]
    changes[name] = start, end, transform(block)


def _script_hook(block, source, check):
    name = source["manager"]
    record, = entity_records(block, include_names={name})
    path = ("encounterComponent", "entityEvents", f"item[{source['script']}]", "events")
    events = list_items(record["edit"][path[0]][path[1]][path[2]][path[3]])
    call = {"eventDef": "activateTarget", "args": {"num": 2, "item[0]": {"entity": check},
                                               "item[1]": {"string": ""}}}
    if any(event.get("eventCall") == call for event in events):
        return block
    if source["before"] is None:
        index = len(events)
    else:
        matches = [i for i, event in enumerate(events) if event.get("eventCall") == source["before"]]
        if len(matches) != source.get("expected_occurrences", 1):
            raise ValueError(f"{name}: arena completion point missing or ambiguous")
        index = matches[source.get("occurrence", 0)]
    events.insert(index, {"eventCall": call})
    value = {"num": len(events), **{f"item[{i}]": event for i, event in enumerate(events)}}
    return _edit(block, path, value)


def _target_hook(block, check):
    record, = entity_records(block, include_names={block.split("entityDef ", 1)[1].split()[0]})
    if record["class"] != "idTarget_Count":
        raise ValueError("Arena completion join must be a native count relay")
    targets = extract_target_names(block)
    return replace_targets_block(block, [check, *[target for target in targets if target != check]])


def suppress_arena_check(arena):
    return ('entity {\n\tentityDef ap_suppress_arena_' + str(arena["location_id"]) + ' {\n'
            '\tinherit = "target/remove";\n\tclass = "idTarget_Remove";\n'
            '\texpandInheritance = false;\n\tedit = {\n\t\ttargets = {\n'
            '\t\t\tnum = 1;\n\t\t\titem[0] = "' + arena_check_name(arena) + '";\n\t\t}\n\t}\n}\n}\n')


def compile_arena_checks(text, arenas, placements, *, notifications=True):
    manifest = {}
    index = entity_definition_index(text)
    existing = set(index)
    changes, additions = {}, []
    for arena in arenas:
        check = arena_check_name(arena)
        code = arena["location_id"]
        manifest[check] = code
        if check in existing:
            continue
        existing.add(check)
        for source in arena["completion"]:
            if "target" in source:
                _replace_entity(text, source["target"], lambda block, check=check: _target_hook(block, check), index, changes)
            else:
                _replace_entity(text, source["manager"],
                                lambda block, source=source, check=check: _script_hook(block, source, check), index, changes)
        relay = generate_event_relay(check, code, "", include_notification=notifications).replace(
            "count = 1;", "count = 1;\n\t\t\treuseable = false;", 1)
        additions.append(in_layer(relay, arena["layer"]) if arena.get("layer") else relay)
        if notifications:
            additions.append(generate_pickup_notification(code, placements.get(code)))
        additions.append(generate_check_event(code) + suppress_arena_check(arena))
    return apply_entity_edits(text, changes.values()) + "".join(additions), manifest
