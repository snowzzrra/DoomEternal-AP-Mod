import re

from doom_eap.content.master_levels import load_master_levels
from tools.maps.ap_map_generator import (
    append_target_to_named_entity,
    build_universal_physical_policy,
    entity_definition_index,
    extract_target_names,
    find_entity_block_bounds,
    generate_automap_location_helper,
    generate_check_event,
    generate_independent_pickup_trigger,
    generate_inert_location_visual,
    generate_pickup_notification,
    generate_target_relay,
    replace_targets_block,
)
from tools.maps.mission_complete_map_patcher import _render_relay


def in_layer(block, layer):
    block = re.sub(r'\blayers\s*\{[^}]*\}\s*', '', block, count=1)
    return block.replace('entity {', f'entity {{\n\tlayers {{ "{layer}" }}', 1)


def gate_normal_checks(text, entities, layer):
    index = entity_definition_index(text)
    changes = []
    for check, code in entities.items():
        source = check.removeprefix("AP_CHECK_").lower()
        for name in ("ap_independent_" + source, check, f"ap_location_visual_{code}", f"ap_automap_location_{code}"):
            bounds = find_entity_block_bounds(text, name, index=index)
            if bounds is not None:
                start, end = bounds
                if name != check and re.search(r'\blayers\s*\{', text[start:end]):
                    continue
                changes.append((start, end, in_layer(text[start:end], layer)))
    for start, end, replacement in sorted(changes, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def pickup_entities(source, pickup, stage, model, placements, notifications):
    code, check = pickup["location_id"], pickup["ap_check"]
    source = in_layer(source, stage["layers"][0])
    policy = build_universal_physical_policy(check, code, source, model,
        policy={key: pickup[key] for key in ("reveal_entity", "gate_relay") if key in pickup})
    policy.update({key: pickup[key] for key in ("reveal_entity", "gate_relay") if key in pickup})
    policy["independent_entity_name"] = f"ap_independent_ml_{code}"
    result = generate_independent_pickup_trigger(pickup["source_entity"], check, source, policy)
    result += generate_inert_location_visual(source, policy)
    result += in_layer(generate_automap_location_helper(source, code, policy), stage["layers"][0])
    result += generate_target_relay(check, code, "", completion_targets=policy["completion_targets"],
                                    include_notification=notifications)
    result += generate_check_event(code)
    if notifications:
        result += generate_pickup_notification(code, placements.get(code))
    return result


def reveal_pickup(text, pickup, shared):
    if "reveal_entity" not in pickup:
        return text
    code, source = pickup["location_id"], pickup["source_entity"]
    bounds = find_entity_block_bounds(text, pickup["reveal_entity"])
    if bounds is None:
        raise ValueError(f"Master Level pickup reveal missing: {source}")
    block = text[slice(*bounds)]
    targets = [target for target in extract_target_names(block) if shared or target != source]
    targets.append(f"ap_location_visual_{code}")
    block = replace_targets_block(block, targets)
    if pickup["gate_relay"] == pickup["reveal_entity"]:
        name = pickup["reveal_entity"]
        native = name + "_ap_native_reveal"
        block = block.replace("entityDef " + name + " {", "entityDef " + native + " {", 1)
        # checkpoint starts and encounter scripts must enable the same hidden trigger.
        block += _render_relay(name, [native, f"ap_independent_ml_{code}"], 0)
        return text[:bounds[0]] + block + text[bounds[1]:]
    text = text[:bounds[0]] + block + text[bounds[1]:]
    return append_target_to_named_entity(text, pickup["gate_relay"], f"ap_independent_ml_{code}")


def compile_master_pickups(text, source, config, model, placements, root, notifications):
    manifest = {}
    for stage in load_master_levels(root).values():
        if stage["map_key"] != config["map_key"]:
            continue
        text = gate_normal_checks(text, config.get("entities", {}), stage["normal_layer"])
        source_index = entity_definition_index(source)
        for pickup in stage["pickups"]:
            name = pickup["source_entity"]
            bounds = find_entity_block_bounds(source, name, index=source_index)
            if bounds is None:
                raise ValueError(f"Master Level pickup source missing: {name}")
            original = source[bounds[0]:bounds[1]]
            shared = stage["layers"][0] not in original
            text = reveal_pickup(text, pickup, shared)
            present = find_entity_block_bounds(text, name)
            if present is not None:
                replacement = in_layer(text[slice(*present)], stage["normal_layer"]) if shared else ""
                text = text[:present[0]] + replacement + text[present[1]:]
            text += pickup_entities(original, pickup, stage, model, placements, notifications)
            manifest[pickup["ap_check"]] = pickup["location_id"]
    return text, manifest
