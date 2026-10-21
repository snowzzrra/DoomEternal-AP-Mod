"""Prepare frozen native body definitions without editing encounter commands."""

import hashlib
import math
import re

from doom_eap.content.enemy_species import validate_enemy_speed_percent
from doom_eap.content.native_enemy_plan import native_body_name, native_recipe_metadata
from tools.maps.ap_map_generator import find_entity_block_bounds
from tools.maps.encounter_catalog import ENTITY_HEADER, SPAWN_CLASSES, entity_records, list_items, walk_values
from tools.maps.enemy_attribute_baselines import resolve_enemy_attribute_baselines
from tools.maps.enemy_attributes import _MISSING, _attribute_source, _get, _render, materialize_enemy_attributes


def _functional_token(name):
    value = 14695981039346656037
    for byte in name.encode("utf8"):
        value = ((value ^ byte) * 1099511628211) & ((1 << 64) - 1)
    return f"{value:016x}"


def _functional_properties(edit):
    result = {"targets": edit["targets"]} if list_items(edit.get("targets")) else {}
    if edit.get("factionName"):
        result["factionName"] = edit["factionName"]
    ai = edit.get("aiEditable", {})
    if "tempMemoryData" in ai:
        result.setdefault("aiEditable", {})["tempMemoryData"] = ai["tempMemoryData"]
    spawn = {key: ai["spawnSettings"][key] for key in ("initialState", "initialTarget", "timers_setOnSpawn")
             if key in ai.get("spawnSettings", {})}
    if spawn:
        replacements = {"AIOVERRIDE_PLAY_ENTRANCE_ANIMATION": "AIOVERRIDE_TELEPORT_FORCE_SPAWN_ANGLE",
                        "AIOVERRIDE_PLAY_ENTRANCE_ANIMATION_WITH_CORRECTION": "AIOVERRIDE_TELEPORT_FORCE_SPAWN_ANGLE",
                        "AIOVERRIDE_SPAWN_ANIM_IDLE": "AIOVERRIDE_DEFAULT",
                        "AIOVERRIDE_ENTRANCE_ANIM_NO_ENEMIES": "AIOVERRIDE_TELEPORT_NO_ENEMIES",
                        "AIOVERRIDE_FORCE_MOVE_TO_TRAVERSAL": "AIOVERRIDE_FORCE_TELEPORT_TO_TRAVERSAL"}
        if "initialState" in spawn:
            spawn["initialState"] = replacements.get(spawn["initialState"], spawn["initialState"])
        result.setdefault("aiEditable", {})["spawnSettings"] = spawn
    death = {key: value for key, value in ai.get("death", {}).items() if key.startswith("trigger") and value}
    if death:
        result.setdefault("aiEditable", {})["death"] = death
    return result


def _functional_definition(body, name, properties):
    # spawned variants need their own prepared model and physics fields.
    header, _, _, prepared = _attribute_source(body)
    properties = {**{key: prepared[key] for key in ("renderModelInfo", "clipModelInfo", "afProperties", "aiHealth")
                     if key in prepared}, **properties}
    return ('entity {\n\tlayers { "spawn_target_layer" }\n\tentityDef ' + name +
        ' {\n\t\tinherit = ' + _render(header.group(1), 0) + ';\n\t\tclass = "idAI2";\n\t\tedit = ' +
        _render(properties, 2) + '\n\t}\n}')


def _independent_definitions(recipe, original, names):
    if recipe["logical_count"] != 1 or recipe["maximum_emissions"] != recipe["physical_count"]:
        raise ValueError("Independent native actor needs one finite logical occurrence")
    properties = _functional_properties(original["edit"])
    properties.update({key: original["edit"][key] for key in
                       ("spawnPosition", "spawnOrientation", "bindInfo", "flags", "dormancy") if key in original["edit"]})
    properties["flags"] = {**properties.get("flags", {}), "hide": True}
    definitions, entries = [], []
    for index, body in enumerate(recipe["bodies"]):
        name = f"ap_v070_independent_r{recipe['origin']}_{index:08x}"
        if name in names:
            raise ValueError("Duplicate independent native body definition")
        names.add(name)
        definitions.append('entity {\n\tlayers { "spawn_target_layer" }\n\tentityDef ' + name +
            ' {\n\t\tinherit = ' + _render(body["definition"], 0) + ';\n\t\tclass = "idAI2";\n\t\tedit = ' +
            _render(properties, 2) + '\n\t}\n}')
        entries.append({"name": name})
    if not entries:
        name = "ap_v070_empty_r" + recipe["origin"]
        if name in names:
            raise ValueError("Duplicate independent native empty definition")
        names.add(name)
        empty = {**original["edit"], "flags": {**original["edit"].get("flags", {}), "hide": True}}
        definitions.append('entity {\n\tlayers { "spawn_target_layer" }\n\tentityDef ' + name +
            ' {\n\t\tinherit = ' + _render(original["inherit"], 0) + ';\n\t\tclass = "idAI2";\n\t\tedit = ' +
            _render(empty, 2) + '\n\t}\n}')
        inherit = name
    else:
        inherit = entries[0]["name"]
    layers = '\tlayers { ' + ' '.join(_render(layer, 0) for layer in original["layers"]) + ' }\n' if original["layers"] else ''
    for index, entry in enumerate(entries[1:], 1):
        name = f"ap_v070_independent_body_r{recipe['origin']}_{index:08x}"
        if name in names:
            raise ValueError("Duplicate independent native body instance")
        names.add(name)
        definitions.append('entity {\n' + layers + '\tentityDef ' + name + ' {\n\t\tinherit = ' +
            _render(entry["name"], 0) + ';\n\t\tclass = "idAI2";\n\t\tedit = {}\n\t}\n}')
    initial = {"flags": {"hide": original["edit"].get("flags", {}).get("hide", False)}} if entries else {}
    replacement = ('entity {\n' + layers + '\tentityDef ' + original["name"] + ' {\n\t\tinherit = ' +
        _render(inherit, 0) + ';\n\t\tclass = "idAI2";\n\t\tedit = ' + _render(initial, 2) + '\n\t}\n}')
    library = "ap_v070_independent_r" + recipe["origin"]
    if library in names:
        raise ValueError("Duplicate independent native body library")
    names.add(library)
    values = {"num": len(entries), **{f"item[{i}]": entry for i, entry in enumerate(entries)}}
    definitions.append('entity {\n\tentityDef ' + library +
        ' {\n\t\tinherit = "target/spawn";\n\t\tclass = "idTarget_Spawn";\n\t\tedit = ' +
        _render({"entityDefs": values, "initialEntityDefs": values}, 2) + '\n\t}\n}')
    return replacement, definitions


def _functional_sources(source):
    selected = {name for name, inherit, kind in ENTITY_HEADER.findall(source)
                if kind in SPAWN_CLASSES | {"idAI2", "idInfoTraversal", "idInfoCoverExposed", "idPlayerStart", "idInfo_TeleportDestination"}
                or (kind == 'idProp2' and inherit.startswith(('pickup/ammo/', 'pickup/armor/', 'pickup/health/')))}
    records = {row["name"]: row for row in entity_records(source, include_names=selected)}
    properties = {name: _functional_properties(row["edit"]) for name, row in records.items() if row["class"] == "idAI2"}
    properties = {name: value for name, value in properties.items() if value}
    inherited = {}
    for row in records.values():
        if row['class'] != 'idTargetSpawnGroup':
            continue
        parent = row['edit'].get('targetSpawnParent')
        if parent not in records:
            continue
        spawners = row['edit'].get('spawners', {})
        for index in range(spawners.get('num', 0)):
            name = spawners.get(f'item[{index}]')
            point = records.get(name)
            if point and point['class'] in {'idTarget_Spawn', 'idTarget_Spawn_Parent'} and not point['edit'].get('initialEntityDefs', {}).get('num', 0):
                inherited.setdefault(name, set()).add(parent)
    sources = {}
    def reachable(name, visited=None):
        if visited is None:
            if name not in sources:
                sources[name] = reachable(name, set())
            return sources[name]
        if name in visited or name not in records:
            return set()
        if name in properties:
            return {name}
        row = records[name]
        if row["class"] not in SPAWN_CLASSES:
            return set()
        visited.add(name)
        result = set()
        # groups populate child definitions from their parent when initialEntityDefs is empty.
        for parent in inherited.get(name, ()):
            result.update(reachable(parent, visited))
        for _, value in walk_values(row["edit"]):
            if isinstance(value, str) and value in records:
                result.update(reachable(value, visited))
        return result
    return properties, reachable, records


def _entrance_landings(recipes, records):
    points = {}
    positions = {name: {axis: row['edit']['spawnPosition'].get(axis, 0) for axis in ('x','y','z')}
                 for name,row in records.items() if 'spawnPosition' in row['edit']}
    def resolve(name, visited=None):
        if name in points:
            return points[name]
        visited = set() if visited is None else visited
        if name in visited or name not in records:
            return set()
        visited.add(name)
        row = records[name]
        if row['class'] == 'idTarget_Spawn':
            return {name}
        return set().union(*(resolve(child, visited) for field,child in row['edit'].get('spawners', {}).items()
                             if re.fullmatch(r'item\[\d+\]', field) and isinstance(child, str)))
    def scope(recipe):
        binding = recipe['binding']
        return binding.get('owner', ''), binding.get('path', '').split('.events.item[')[0]
    def ground(name):
        edit = records[name]['edit']; spawn = edit.get('spawnEditable', {})
        templates = [records.get(value.get('name'), {}) for field,value in edit.get('entityDefs', {}).items()
                     if re.fullmatch(r'item\[\d+\]', field) and isinstance(value, dict)]
        return (not spawn.get('spawnAnim') and spawn.get('aiStateOverride', 'AIOVERRIDE_DEFAULT') in
                {'AIOVERRIDE_DEFAULT', 'AIOVERRIDE_TELEPORT', 'AIOVERRIDE_TELEPORT_FORCE_SPAWN_ANGLE'}
                and all(template.get('edit', {}).get('aiEditable', {}).get('spawnSettings', {}).get('initialState', 'AIOVERRIDE_DEFAULT') in
                        {'AIOVERRIDE_DEFAULT', 'AIOVERRIDE_TELEPORT', 'AIOVERRIDE_TELEPORT_FORCE_SPAWN_ANGLE',
                         'AIOVERRIDE_FORCE_AWARENESS_OF_PLAYER'} for template in templates)
                and (not templates or any(not any(kind in template.get('inherit', '') for kind in
                     ('/ambient/', 'cacodemon', 'painelemental', 'lostsoul', 'spirit', 'bloodangel')) for template in templates))
                and not spawn.get('spawnAt')
                and not edit.get('bindInfo') and name in positions and all(map(math.isfinite, positions[name].values())))
    airborne = {'ENCOUNTER_SPAWN_CACODEMON', 'ENCOUNTER_SPAWN_PAIN_ELEMENTAL', 'ENCOUNTER_SPAWN_LOST_SOUL',
                'ENCOUNTER_SPAWN_SPIRIT', 'ENCOUNTER_SPAWN_BLOOD_ANGEL', 'ENCOUNTER_SPAWN_TENTACLE',
                'ENCOUNTER_SPAWN_SUPER_TENTACLE', 'ENCOUNTER_SPAWN_BUFF_POD', 'ENCOUNTER_SPAWN_TURRET'}
    owners, footprints, flying_points = {}, {}, set()
    for recipe in recipes:
        reference = recipe['binding'].get('reference', '')
        points.setdefault(reference, resolve(reference))
        footprints.setdefault(scope(recipe), set()).update(points[reference])
        if recipe['selected_spawn_type'] not in airborne:
            owners.setdefault(scope(recipe), set()).update(name for name in points[reference] if ground(name))
        else:
            flying_points.update(points[reference])
    ground_points = set().union(*owners.values())
    anchors = {name for name, row in records.items()
               if (row['class'] in {'idInfoCoverExposed', 'idPlayerStart', 'idInfo_TeleportDestination'} or
                   (row['class'] == 'idTarget_Spawn' and ground(name) and
                    (name in ground_points or (name not in flying_points and row['edit'].get('entityDefs', {}).get('num', 0) > 0))) or
                   (row['class'] == 'idInfoTraversal' and row['edit'].get('snapToGround') is True))
               and not row['edit'].get('bindInfo')
               and name in positions and all(map(math.isfinite, positions[name].values()))}
    pickups = {name for name,row in records.items() if row['class'] == 'idProp2' and not row['edit'].get('bindInfo')
               and name in positions and all(map(math.isfinite, positions[name].values()))}
    fallback_owners = set()
    for owner, names in footprints.items():
        if owners.get(owner) or not names:
            continue
        footprint = [positions.get(name, {}) for name in names]
        if not all(all(math.isfinite(pos.get(axis, math.nan)) for axis in ('x','y','z')) for pos in footprint):
            continue
        # navigation and cover markers provide ground even when every entrance starts outside the arena.
        bounds = {axis: (min(pos[axis] for pos in footprint)-50, max(pos[axis] for pos in footprint)+50) for axis in ('x','y')}
        owners[owner] = {name for name in anchors if all(
            bounds[axis][0] <= positions[name][axis] <= bounds[axis][1] for axis in ('x','y'))}
        fallback_owners.add(owner)
    choices = {}
    for recipe in recipes:
        for name in points[recipe['binding'].get('reference', '')]:
            edit = records[name]['edit']
            if edit.get('spawnEditable', {}).get('aiStateOverride') not in {
                    'AIOVERRIDE_PLAY_ENTRANCE_ANIMATION', 'AIOVERRIDE_PLAY_ENTRANCE_ANIMATION_WITH_CORRECTION',
                    'AIOVERRIDE_ENTRANCE_ANIM_NO_ENEMIES'}:
                continue
            candidates = owners.get(scope(recipe), set())
            position = positions.get(name, {})
            if not all(math.isfinite(position.get(axis, math.nan)) for axis in ('x','y','z')):
                continue
            def distance(other):
                return sum((position[axis]-positions[other][axis])**2 for axis in ('x','y','z')), other
            if scope(recipe) in fallback_owners:
                candidates = {other for other in candidates if distance(other)[0] <= 50**2}
                if not candidates:
                    candidates = {other for other in pickups if distance(other)[0] <= 50**2}
                animation = edit.get('spawnEditable', {}).get('spawnAnim', '')
                direction = 1 if '_up_' in animation or '/riseup_' in animation else -1 if '_down_' in animation else 0
                directed = {other for other in candidates if direction and
                            direction*(positions[other]['z']-position['z']) >= .5 and
                            sum((position[axis]-positions[other][axis])**2 for axis in ('x','y','z')) <= 50**2}
                if directed:
                    candidates = directed
            if not candidates:
                continue
            target = min(candidates, key=distance)
            if distance(target)[0] <= 50**2:
                choices.setdefault(name, []).append((distance(target), target))
    return {name:min(options)[1] for name,options in choices.items()}


def _body_definitions(recipe, settings):
    for ordinal, body in enumerate(recipe["bodies"]):
        expected = native_body_name(recipe["origin"], ordinal, recipe["logical_count"], recipe["physical_count"],
                                    recipe["selected_spawn_type"], body, settings)
        if body["definition"] != expected:
            raise ValueError("Native body tracking differs from the recipe")
        yield body, expected
        tail = recipe["final_credit_units"]
        partial = (0 < tail < recipe["logical_count"] and recipe["maximum_emissions"] > 0
                   and ordinal == (recipe["maximum_emissions"] - 1) % recipe["physical_count"])
        if partial:
            expected = native_body_name(recipe["origin"], ordinal, recipe["logical_count"], recipe["physical_count"],
                recipe["selected_spawn_type"], body, settings, credit_units=tail)
            if body.get("tail_definition") != expected:
                raise ValueError("Native body partial credit differs from the recipe")
            yield body, expected
        elif "tail_definition" in body:
            raise ValueError("Unexpected native partial credit definition")


def materialize_native_recipe_definitions(source, map_key, plan, source_maps):
    stage = plan["maps"][map_key]
    if re.fullmatch(r"[a-z0-9_]+", map_key) is None:
        raise ValueError("Invalid native recipe map key")
    if hashlib.sha256(source.encode("utf8")).hexdigest() != stage["source_sha256"]:
        raise ValueError(f"Native recipe source hash differs: {map_key}")
    if re.fullmatch(r"[0-9a-f]{64}", plan["fingerprint"]) is None:
        raise ValueError("Invalid native recipe fingerprint")
    names = {name for name, _, _ in ENTITY_HEADER.findall(source)}
    definitions, donors, generated = [], {}, []
    functional, reachable, records = _functional_sources(source)
    for point, landing in sorted(_entrance_landings(stage['recipes'], records).items()):
        helper = 'ap_v070_landing_' + _functional_token(point)
        if helper in names:
            raise ValueError('Duplicate native entrance landing')
        names.add(helper)
        anchor = records[landing]
        edit = {key: anchor['edit'][key] for key in ('spawnPosition', 'spawnOrientation') if key in anchor['edit']}
        edit['spawnEditableShared'] = {'groupName': helper}
        generated.append('entity {\n\tentityDef ' + helper + ' {\n\t\tinherit = "target/spawn";\n'
            '\t\tclass = "idTarget_Spawn";\n\t\tedit = ' +
            _render(edit, 2) + '\n\t}\n}')
    functional_libraries = {}
    for recipe in stage["recipes"]:
        recipe_definitions = []
        reference = recipe["binding"].get("reference", recipe["binding"].get("entity", ""))
        source_names = sorted(reachable(reference)) if recipe["family"] != "summon" else []
        if recipe["physical_count"] != len(recipe["bodies"]):
            raise ValueError("Native recipe body quantity differs")
        if re.fullmatch(r"[0-9a-f]{20}", recipe["origin"]) is None:
            raise ValueError("Invalid native recipe origin")
        for body, name in _body_definitions(recipe, stage):
            reference, attributes = body["ai"], body["attributes"]
            if name in names:
                raise ValueError(f"Duplicate native body definition: {name}")
            names.add(name)
            validate_enemy_speed_percent(body["species"], attributes["speed_percent"])
            key = tuple(reference[field] for field in ("map_key", "entity", "sha256"))
            if key not in donors:
                donor_source = source_maps[reference["map_key"]]
                start, end = find_entity_block_bounds(donor_source, reference["entity"])
                block = donor_source[start:end]
                record, = entity_records(block)
                if record["class"] != "idAI2" or record["sha256"] != reference["sha256"]:
                    raise ValueError(f"Native body donor differs: {reference['entity']}")
                donor_names = {name for name, _, _ in ENTITY_HEADER.findall(donor_source)}
                if any(isinstance(value, str) and value in donor_names for _, value in walk_values(record["edit"])):
                    raise ValueError(f"Native body donor has unresolved map entity references: {reference['entity']}")
                donors[key] = block, record, None
            block, record, baseline = donors[key]
            paths = []
            if attributes["health_percent"] != 100:
                paths.extend(("aiHealth", "components", "components[0]", field) for field in ("max", "starting"))
            if attributes["damage_percent"] != 100:
                paths.append(("aiEditable", "damageOutModifiers"))
            if attributes["size_percent"] != 100:
                paths.extend(("renderModelInfo", "scale", axis) for axis in ("x", "y", "z"))
            if baseline is None and any(_get(record["edit"], path) is _MISSING for path in paths):
                baseline = resolve_enemy_attribute_baselines(block)
                donors[key] = block, record, baseline
            clone = materialize_enemy_attributes(block, name, base_properties=baseline,
                **{key: value for key, value in attributes.items() if key != "speed_percent"})
            header = ENTITY_HEADER.search(clone)
            generated.append('entity {\n\tlayers { "spawn_target_layer" }\n\t' + clone[header.start():].rstrip())
            for source_name in source_names:
                token = _functional_token(source_name)
                variant = "ap_v070_functional_" + token + "_" + _functional_token(name)
                if variant in names:
                    raise ValueError("Duplicate native functional body definition")
                names.add(variant)
                generated.append(_functional_definition(clone, variant, functional[source_name]))
                functional_libraries.setdefault(source_name, []).append({"name": variant})
            definitions.append({"name": name})
            recipe_definitions.append({"name": name})
        values = {"num": len(recipe_definitions), **{f"item[{i}]": value for i, value in enumerate(recipe_definitions)}}
        body_library = "ap_v070_bodies_r" + recipe["origin"]
        if body_library in names:
            raise ValueError("Duplicate native body library")
        names.add(body_library)
        generated.append('entity {\n\tentityDef ' + body_library +
            ' {\n\t\tinherit = "target/spawn";\n\t\tclass = "idTarget_Spawn";\n\t\tedit = ' +
            _render({"entityDefs": values, "initialEntityDefs": values}, 2) + '\n\t}\n}')
    if functional_libraries:
        functional_libraries[""] = []
    for source_name, variants in sorted(functional_libraries.items()):
        helper = "ap_v070_functional_" + _functional_token(source_name) if source_name else "ap_v070_functional_sources"
        if helper in names:
            raise ValueError("Duplicate native functional source identity")
        names.add(helper)
        values = {"num": len(variants), **{f"item[{i}]": value for i, value in enumerate(variants)}}
        generated.append('entity {\n\tentityDef ' + helper +
            ' {\n\t\tinherit = "target/spawn";\n\t\tclass = "idTarget_Spawn";\n\t\tedit = ' +
            _render({"entityDefs": values, "initialEntityDefs": values}, 2) + '\n\t}\n}')
    library = "ap_v070_recipe_plan"
    if library in names:
        raise ValueError("Duplicate native recipe library")
    metadata = native_recipe_metadata(plan, map_key)
    # native parser limits shit to 2046 bytes, so we gotta limit the metadata so it doesnt cut off
    if len(metadata.encode("ascii")) > 1800:
        chunks = [metadata[offset:offset + 1800] for offset in range(0, len(metadata), 1800)]
        for index, chunk in enumerate(chunks):
            name = f"ap_v070_recipe_plan_{index}"
            if name in names or len(_render(chunk, 0).encode("ascii")) > 2046:
                raise ValueError("Invalid native recipe metadata chunk")
            names.add(name)
            generated.append('entity {\n\tentityDef ' + name + ' {\n\t\tinherit = "target/spawn";\n'
                '\t\tclass = "idTarget_Spawn";\n\t\tedit = ' +
                _render({"spawnEditableShared": {"groupName": chunk}}, 2) + '\n\t}\n}')
        metadata = f"EAPC1|{len(chunks):08x}|{len(metadata):08x}|{_functional_token(metadata)}"
    values = {"num": len(definitions), **{f"item[{i}]": value for i, value in enumerate(definitions)}}
    generated.append('entity {\n\tentityDef ' + library + ' {\n\t\tinherit = "target/spawn";\n'
        '\t\tclass = "idTarget_Spawn";\n\t\tedit = ' + _render({"entityDefs": values, "initialEntityDefs": values,
            "spawnEditableShared": {"groupName": metadata}}, 2)
        + '\n\t}\n}')
    return source + "\n\n" + "\n\n".join(generated) + "\n"


def materialize_native_recipe_calls(source, map_key, plan, source_maps):
    from tools.maps.enemy_attributes import _edit

    prepared = materialize_native_recipe_definitions(source, map_key, plan, source_maps)
    names = {name for name, _, _ in ENTITY_HEADER.findall(prepared)}
    changes, aliases = {}, []
    for recipe in plan["maps"][map_key]["recipes"]:
        binding = recipe["binding"]
        if binding["mode"] == "direct_point":
            name, target = binding["entity"], binding["reference"]
            if name not in changes:
                start, end = find_entity_block_bounds(source, name)
                changes[name] = start, end, source[start:end]
            start, end, block = changes[name]
            owner, = entity_records(block, include_names={name})
            path = tuple(binding["path"].split("."))
            if _get(owner["edit"], path) != target:
                raise ValueError("Native direct activation binding differs from the source")
            first, last = find_entity_block_bounds(source, target)
            original, = entity_records(source[first:last])
            if original["class"] != "idTarget_Spawn":
                raise ValueError("Native direct activation target is not a spawn point")
            alias = "ap_v070_direct_r" + recipe["origin"]
            if alias in names:
                raise ValueError("Duplicate native direct point alias")
            names.add(alias)
            aliases.append(re.sub(r"(\bentityDef\s+)" + re.escape(target) + r"(?=\s*\{)",
                                  lambda match: match[1] + alias, source[first:last], count=1))
            changes[name] = start, end, _edit(block, path, alias)
            continue
        if binding["mode"] == "independent_actor":
            name = binding["entity"]
            first, last = find_entity_block_bounds(source, name)
            original, = entity_records(source[first:last])
            if original["class"] != "idAI2" or name in changes:
                raise ValueError("Independent native binding differs from the source")
            replacement, definitions = _independent_definitions(recipe, original, names)
            changes[name] = first, last, replacement
            aliases.extend(definitions)
            continue
        if binding["mode"] == "summon" and binding["entity"]:
            name = binding["entity"]
            first, last = find_entity_block_bounds(source, name)
            original, = entity_records(source[first:last])
            if original["class"] != "idArchvileTemplate":
                raise ValueError("Native summon binding is not a template")
            if name not in changes:
                changes[name] = first, last, source[first:last]
            start, end, block = changes[name]
            values = original["edit"]["summonList"]["aiSpawnTypeList"]
            positions = [index for index in range(values["num"]) if values[f"item[{index}]"] == binding["spawn_type"]]
            if len(positions) != recipe["logical_count"]:
                raise ValueError("Native summon binding quantity differs from the source")
            for index in positions:
                block = _edit(block, ("summonList", "aiSpawnTypeList", f"item[{index}]"), recipe["selected_spawn_type"])
            changes[name] = start, end, block
            continue
        if binding["mode"] != "event":
            continue
        name, path = binding["entity"], binding["path"].split(".")
        if name not in changes:
            start, end = find_entity_block_bounds(source, name)
            changes[name] = start, end, source[start:end]
        start, end, block = changes[name]
        owner, = entity_records(block, include_names={name})
        call = _get(owner["edit"], path)
        if call is _MISSING or call["eventDef"] != binding["event_def"]:
            raise ValueError("Native recipe event binding differs from the source")
        argument = binding["argument"]
        if type(argument) is not int or argument < 0:
            raise ValueError("Invalid native recipe argument binding")
        target = _get(call, ("args", f"item[{argument}]", "entity"))
        if target is _MISSING or target != binding["reference"]:
            raise ValueError("Native recipe target binding differs from the source")
        alias = "ap_v070_request_r" + recipe["origin"]
        if alias in names:
            raise ValueError("Duplicate native recipe request alias")
        names.add(alias)
        bounds = find_entity_block_bounds(source, target) if target else None
        if bounds is not None:
            first, last = bounds
            original = source[first:last]
            record, = entity_records(original)
            if record["class"] not in {"idTarget_Spawn", "idTarget_Spawn_Parent", "idTargetSpawnGroup"}:
                raise ValueError("Native recipe target is not a native spawner")
            inherit, kind = record["inherit"], record["class"]
        elif recipe["family"] in {"group", "maintenance", "staggered"}:
            inherit, kind = "encounter/spawn_group/zone", "idTargetSpawnGroup"
        else:
            inherit, kind = "target/spawn", "idTarget_Spawn"
        settings = ({"spawners": {"num": 0}, "includeTouchingSpawners": False, "includeInGlobalOverrideGroup": False,
                     "clipModelInfo": {"type": "CLIPMODEL_NONE"}}
                    if kind == "idTargetSpawnGroup" else
                    {"entityDefs": {"num": 0}, "initialEntityDefs": {"num": 0}, "targets": {"num": 0}})
        aliases.append('entity { entityDef ' + alias + ' { inherit = ' + _render(inherit, 0) +
            '; class = ' + _render(kind, 0) + '; edit = ' + _render(settings, 2) + ' } }')
        block = _edit(block, (*path, "args", f"item[{argument}]", "entity"), alias)
        changes[name] = start, end, block
    compiled = source
    for start, end, block in sorted(changes.values(), reverse=True):
        compiled = compiled[:start] + block + compiled[end:]
    return compiled + prepared[len(source):] + "\n\n".join(aliases) + ("\n" if aliases else "")
