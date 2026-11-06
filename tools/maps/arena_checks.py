"""Insert checks at authored arena completion points."""

from doom_eap.content.arena_checks import arena_check_name, load_arena_checks
from tools.maps.ap_map_generator import (
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


def _replace_entity(text, name, transform):
    bounds = find_entity_block_bounds(text, name)
    if bounds is None:
        raise ValueError(f"Arena completion entity missing: {name}")
    start, end = bounds
    return text[:start] + transform(text[start:end]) + text[end:]


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
    for arena in arenas:
        check = arena_check_name(arena)
        code = arena["location_id"]
        manifest[check] = code
        if find_entity_block_bounds(text, check) is not None:
            continue
        for source in arena["completion"]:
            if "target" in source:
                text = _replace_entity(text, source["target"], lambda block, check=check: _target_hook(block, check))
            else:
                text = _replace_entity(text, source["manager"],
                                       lambda block, source=source, check=check: _script_hook(block, source, check))
        relay = generate_event_relay(check, code, "", include_notification=notifications).replace(
            "count = 1;", "count = 1;\n\t\t\treuseable = false;", 1)
        text += in_layer(relay, arena["layer"]) if arena.get("layer") else relay
        if notifications:
            text += generate_pickup_notification(code, placements.get(code))
        text += generate_check_event(code) + suppress_arena_check(arena)
    return text, manifest
