"""Prepare frozen native body definitions and encounter bindings."""

import hashlib
import math
import re

from doom_eap.content.enemy_species import validate_enemy_speed_percent
from doom_eap.content.native_enemy_plan import native_body_name, native_recipe_metadata
from tools.maps.ap_map_generator import entity_definition_index, find_entity_block_bounds
from tools.maps.encounter_catalog import ENTITY_HEADER, SPAWN_CLASSES, entity_records, list_items, walk_values
from tools.maps.enemy_attribute_baselines import resolve_enemy_attribute_baselines
from tools.maps.enemy_attributes import _MISSING, _attribute_source, _edit, _get, _render, materialize_enemy_attributes


def _functional_token(name):
    value = 14695981039346656037
    for byte in name.encode("utf8"):
        value = ((value ^ byte) * 1099511628211) & ((1 << 64) - 1)
    return f"{value:016x}"


def _functional_properties(edit):
    result = {"targets": edit["targets"]} if list_items(edit.get("targets")) else {}
    ai = edit.get("aiEditable", {})
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
        for field in ('spawners', 'spawnersTouchingPrimitives'):
            spawners = row['edit'].get(field, {})
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


def _entrance_endpoint(edit):
    animation = edit.get('spawnEditable', {}).get('spawnAnim', '').rsplit('/', 1)[-1]
    position = edit.get('spawnPosition', {})
    if 'spawnPosition' not in edit or not all(math.isfinite(position.get(axis, 0)) for axis in ('x','y','z')):
        return None
    if animation == 'monster_closet_rand' or re.fullmatch(r'sneaky_spawn_\d+', animation):
        return dict(position)
    jump = re.fullmatch(r'jump_forward_(\d+)(?:_(up|down)_(\d+))?', animation)
    if not jump:
        return None
    forward = edit.get('spawnOrientation', {}).get('mat', {}).get('mat[0]', {'x': 1})
    # animation distances are centimeters; map positions are meters.
    result = {axis: position.get(axis, 0) + forward.get(axis, int(axis == 'x')) * int(jump[1]) / 100
              for axis in ('x', 'y', 'z')}
    if jump[2]:
        result['z'] += int(jump[3]) / (100 if jump[2] == 'up' else -100)
    return result if all(map(math.isfinite, result.values())) else None


def _entrance_landings(recipes, records, *, map_key=None):
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
    def ground(name, verified=False):
        edit = records[name]['edit']; spawn = edit.get('spawnEditable', {})
        entrance = (verified and spawn.get('aiStateOverride') in {
            'AIOVERRIDE_PLAY_ENTRANCE_ANIMATION', 'AIOVERRIDE_PLAY_ENTRANCE_ANIMATION_WITH_CORRECTION'}
            and re.fullmatch(r'jump_forward_\d+(?:_(?:up|down)_\d+)?', spawn.get('spawnAnim', '').rsplit('/', 1)[-1]))
        templates = [records.get(value.get('name'), {}) for field,value in edit.get('entityDefs', {}).items()
                     if re.fullmatch(r'item\[\d+\]', field) and isinstance(value, dict)]
        return ((entrance or not spawn.get('spawnAnim') and spawn.get('aiStateOverride', 'AIOVERRIDE_DEFAULT') in
                {'AIOVERRIDE_DEFAULT', 'AIOVERRIDE_TELEPORT', 'AIOVERRIDE_TELEPORT_FORCE_SPAWN_ANGLE'})
                and all(template.get('edit', {}).get('aiEditable', {}).get('spawnSettings', {}).get('initialState', 'AIOVERRIDE_DEFAULT') in
                        {'AIOVERRIDE_DEFAULT', 'AIOVERRIDE_TELEPORT', 'AIOVERRIDE_TELEPORT_FORCE_SPAWN_ANGLE',
                         'AIOVERRIDE_FORCE_AWARENESS_OF_PLAYER'} for template in templates)
                and (verified or templates)
                and (not templates or (any if verified else all)(template.get('class') == 'idAI2' and
                     not any(kind in template.get('inherit', '') for kind in
                     ('/ambient/', '/friendly/', 'cacodemon', 'painelemental', 'lostsoul', 'spirit', 'bloodangel')) for template in templates))
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
            owners.setdefault(scope(recipe), set()).update(name for name in points[reference] if ground(name, True))
        else:
            flying_points.update(points[reference])
    ground_points = set().union(*owners.values())
    anchors = {name for name, row in records.items()
               if (row['class'] in {'idInfoCoverExposed', 'idPlayerStart', 'idInfo_TeleportDestination'} or
                   (row['class'] == 'idTarget_Spawn' and ground(name, name in ground_points) and
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
    portal_floors = {'underwater_facility_arena_target_spawn_154': 'underwater_facility_arena_target_spawn_156',
                     'underwater_facility_arena_target_spawn_155': 'underwater_facility_arena_target_spawn_160'}
    floor_overrides = {'game_target_spawn_950': 'game_target_spawn_735',
                       'game_target_spawn_340': 'game_target_spawn_735'} if map_key == 'e1m3_cult' else {}
    for recipe in recipes:
        needs_floor = recipe['selected_spawn_type'] in {
            'ENCOUNTER_SPAWN_CACODEMON', 'ENCOUNTER_SPAWN_PAIN_ELEMENTAL', 'ENCOUNTER_SPAWN_LOST_SOUL',
            'ENCOUNTER_SPAWN_SPIRIT', 'ENCOUNTER_SPAWN_BLOOD_ANGEL'} and any(
                body['species'] not in {'cacodemon', 'pain_elemental', 'lost_soul', 'spirit', 'blood_angel'}
                for body in recipe['bodies'])
        for name in points[recipe['binding'].get('reference', '')]:
            edit = records[name]['edit']
            spawn = edit.get('spawnEditable', {})
            if name in floor_overrides:
                target = floor_overrides[name]
                if target not in records or not ground(target, True):
                    raise ValueError('Cultist combat floor differs from the source')
                choices.setdefault(name, []).append(((0, target), target))
                continue
            if spawn.get('aiStateOverride') in {'AIOVERRIDE_FORCE_MOVE_TO_TRAVERSAL', 'AIOVERRIDE_FORCE_TELEPORT_TO_TRAVERSAL'}:
                target = spawn.get('initialTargetOverride')
                if not target and name in positions:
                    colocated = [key for key, row in records.items() if row['class'] == 'idInfoTraversal'
                        and key in positions and 'traversalEndpoint' in row['edit']
                        and not row['edit'].get('bindInfo')
                        and sum((positions[name][axis] - positions[key][axis]) ** 2 for axis in ('x', 'y', 'z')) <= .01]
                    if len(colocated) == 1:
                        target = colocated[0]
                traversal = records.get(target, {})
                if (traversal.get('class') == 'idInfoTraversal' and target in positions and
                        'traversalEndpoint' in traversal['edit']):
                    choices.setdefault(name, []).append(((0, target), target))
                    continue
            if not needs_floor and edit.get('spawnEditable', {}).get('aiStateOverride') not in {
                    'AIOVERRIDE_PLAY_ENTRANCE_ANIMATION', 'AIOVERRIDE_PLAY_ENTRANCE_ANIMATION_WITH_CORRECTION',
                    'AIOVERRIDE_ENTRANCE_ANIM_NO_ENEMIES', 'AIOVERRIDE_SPAWN_ANIM_IDLE',
                    'AIOVERRIDE_FORCE_MOVE_TO_TRAVERSAL', 'AIOVERRIDE_FORCE_TELEPORT_TO_TRAVERSAL'}:
                continue
            if name in portal_floors:
                target = portal_floors[name]
                # the portal animation's name does not describe its final floor position.
                if target not in records or not ground(target, True):
                    raise ValueError('Atlantica portal floor differs from the source')
                choices.setdefault(name, []).append(((0, target), target))
                continue
            if not edit.get('bindInfo') and _entrance_endpoint(edit) is not None:
                choices.setdefault(name, []).append(((0, name), name))
                continue
            candidates = owners.get(scope(recipe), set())
            position = positions.get(name, {})
            if not all(math.isfinite(position.get(axis, math.nan)) for axis in ('x','y','z')):
                continue
            def distance(other):
                return sum((position[axis]-positions[other][axis])**2 for axis in ('x','y','z')), other
            candidates = {other for other in candidates if distance(other)[0] <= 50**2}
            if scope(recipe) in fallback_owners or not candidates:
                if not candidates:
                    candidates = {other for other in anchors if distance(other)[0] <= 50**2}
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


def materialize_native_recipe_definitions(source, map_key, plan, source_maps, *, enemy_revision=2):
    stage = plan["maps"][map_key]
    settings = {**stage, "revision": enemy_revision}
    if re.fullmatch(r"[a-z0-9_]+", map_key) is None:
        raise ValueError("Invalid native recipe map key")
    if hashlib.sha256(source.encode("utf8")).hexdigest() != stage["source_sha256"]:
        raise ValueError(f"Native recipe source hash differs: {map_key}")
    if re.fullmatch(r"[0-9a-f]{64}", plan["fingerprint"]) is None:
        raise ValueError("Invalid native recipe fingerprint")
    names = {name for name, _, _ in ENTITY_HEADER.findall(source)}
    definitions, donors, generated = [], {}, []
    donor_indexes = {}
    if plan.get('revision', 0) >= 17:
        groups = {}
        for recipe in stage['recipes']:
            binding = recipe['binding']
            if not recipe.get('runtime_only') and '.events.item[' in binding.get('path', ''):
                key = binding.get('owner', ''), binding['path'].split('.events.item[', 1)[0]
                groups.setdefault(key, []).append(recipe)
        for recipes in groups.values():
            species = {body['species'] for recipe in recipes for body in recipe['bodies']}
            if species.intersection({'zombie_tier_1', 'zombie_tier_3'}) and species - {'zombie_tier_1', 'zombie_tier_3'}:
                for recipe in recipes:
                    marker = 'ap_v070_optional_zombies_r' + recipe['origin']
                    generated.append('entity { entityDef ' + marker + ' { inherit = "info/null"; class = "idInfo"; edit = {} } }')
    functional, reachable, records = _functional_sources(source)
    for point, landing in sorted(_entrance_landings(stage['recipes'], records, map_key=map_key).items()):
        helper = 'ap_v070_landing_' + _functional_token(point)
        if helper in names:
            raise ValueError('Duplicate native entrance landing')
        names.add(helper)
        anchor = records[landing]
        edit = {key: anchor['edit'][key] for key in ('spawnPosition', 'spawnOrientation') if key in anchor['edit']}
        endpoint = _entrance_endpoint(anchor['edit'])
        if endpoint is not None:
            edit['spawnPosition'] = endpoint
        if ('traversalEndpoint' in anchor['edit'] and
                records[point]['edit'].get('spawnEditable', {}).get('aiStateOverride') in
                {'AIOVERRIDE_FORCE_MOVE_TO_TRAVERSAL', 'AIOVERRIDE_FORCE_TELEPORT_TO_TRAVERSAL'}):
            endpoint = anchor['edit']['traversalEndpoint']
            matrix = anchor['edit'].get('spawnOrientation', {}).get('mat', {})
            edit['spawnPosition'] = {axis: anchor['edit']['spawnPosition'].get(axis, 0) + sum(
                endpoint.get(local, 0) * matrix.get(f'mat[{index}]', {}).get(axis, int(axis == local))
                for index, local in enumerate(('x', 'y', 'z'))) for axis in ('x', 'y', 'z')}
            if not all(math.isfinite(value) for value in edit['spawnPosition'].values()):
                raise ValueError('Native traversal landing is not finite')
        edit['spawnEditableShared'] = {'groupName': helper}
        generated.append('entity {\n\tentityDef ' + helper + ' {\n\t\tinherit = "target/spawn";\n'
            '\t\tclass = "idTarget_Spawn";\n\t\tedit = ' +
            _render(edit, 2) + '\n\t}\n}')
    functional_libraries = {}
    emitted_bodies = {}
    for recipe, compatible, prefix in ([(recipe, False, 'ap_v070_bodies_r') for recipe in stage['recipes']] +
            [(recipe, True, 'ap_v070_compat_r') for recipe in stage.get('compatibility_recipes', [])] +
            [(recipe, True, 'ap_v070_counter_r') for recipe in stage.get('counter_recipes', [])]):
        recipe_definitions = []
        reference = recipe["binding"].get("reference", recipe["binding"].get("entity", ""))
        source_names = sorted(reachable(reference)) if recipe["family"] != "summon" else []
        if recipe["physical_count"] != len(recipe["bodies"]):
            raise ValueError("Native recipe body quantity differs")
        if re.fullmatch(r"[0-9a-f]{20}", recipe["origin"]) is None:
            raise ValueError("Invalid native recipe origin")
        for body, name in _body_definitions(recipe, settings):
            reference, attributes = body["ai"], body["attributes"]
            if compatible and emitted_bodies.get(name) == body:
                recipe_definitions.append({"name": name})
                continue
            if name in names:
                raise ValueError(f"Duplicate native body definition: {name}")
            names.add(name)
            emitted_bodies[name] = body
            validate_enemy_speed_percent(body["species"], attributes["speed_percent"])
            key = tuple(reference[field] for field in ("map_key", "entity", "sha256"))
            if key not in donors:
                donor_source = source_maps[reference["map_key"]]
                if reference["map_key"] not in donor_indexes:
                    donor_indexes[reference["map_key"]] = (
                        entity_definition_index(donor_source),
                        {name for name, _, _ in ENTITY_HEADER.findall(donor_source)},
                    )
                donor_index, donor_names = donor_indexes[reference["map_key"]]
                start, end = find_entity_block_bounds(donor_source, reference["entity"], index=donor_index)
                block = donor_source[start:end]
                record, = entity_records(block)
                if record["class"] != "idAI2" or record["sha256"] != reference["sha256"]:
                    raise ValueError(f"Native body donor differs: {reference['entity']}")
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
            clone = _edit(clone, ('flags', 'hide'), False)
            if recipe["binding"].get("entity") == "ap_darklord_phantoms":
                # the boss spawns outside an encounter, so the body must declare its own awake state like vanilla summons.
                clone = _edit(clone, ('aiEditable', 'spawnSettings', 'initialState'),
                              'AIOVERRIDE_FORCE_AWARENESS_OF_PLAYER')
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
        body_library = prefix + recipe["origin"]
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
    # native entity string fields accept at most 2046 encoded bytes.
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


def _refill_limits(source, recipes):
    limits = {}
    owners = {r['binding'].get('owner') for r in recipes
              if r['family'] == 'maintenance' and not r['maximum_emissions']}
    records = {r['name']: r for r in entity_records(source, include_names=owners - {None, ''})}
    for owner, record in records.items():
        kills, calls, stops = [], [], set()
        for path, value in walk_values(record['edit']):
            if path.endswith('.eventDef'):
                call = _get(record['edit'], tuple(path.rsplit('.', 1)[0].split('.')))
                if value == 'waitKillCount':
                    kills.append(set(call['args']['item[0]']['eEncounterSpawnType_t'].split()))
                if value == 'stopMaintainingAICount' and calls and calls[-1][0] == path.rsplit('.item[', 1)[0]:
                    previous = calls[-1][1]
                    if previous['eventDef'] == 'waitAIRemaining':
                        mask = previous['args']['item[0]']['eEncounterSpawnType_t']
                        label = next((arg['string'] for arg in list_items(call['args']) if 'string' in arg), '')
                        stops.add((label, mask))
                calls.append((path.rsplit('.item[', 1)[0], call))
        finite = sum(r['maximum_emissions'] for r in recipes if r['binding'].get('owner') == owner
                     and r['family'] in {'immediate', 'group', 'staggered'})
        for recipe in recipes:
            if recipe['binding'].get('owner') != owner or recipe['family'] != 'maintenance' or recipe['maximum_emissions']:
                continue
            types = set(recipe['selected_spawn_type'].split())
            binding = recipe['binding']
            call = _get(record['edit'], tuple(binding.get('path', '').split('.')))
            label = _get(call, ('args', 'item[6]', 'string')) if isinstance(call, dict) else _MISSING
            finite_stop = any(label == stop_label and 'ENCOUNTER_SPAWN_ANY' not in mask.split()
                              and not types.intersection(mask.split()) for stop_label, mask in stops)
            barrier = False
            active = False
            script = binding.get('path', '').rsplit('.item[', 1)[0]
            for sequence, event in calls:
                if sequence != script:
                    continue
                if event is call:
                    active = True
                elif active and event['eventDef'] == 'stopMaintainingAICount':
                    mask = event['args']['item[0]']['eEncounterSpawnType_t'].split()
                    stop_label = next((arg['string'] for arg in list_items(event['args']) if 'string' in arg), '')
                    if (not stop_label or stop_label == label) and ('ENCOUNTER_SPAWN_ANY' in mask or types.intersection(mask)):
                        break
                elif active and event['eventDef'] == 'waitAIRemaining':
                    mask = event['args']['item[0]']['eEncounterSpawnType_t'].split()
                    barrier |= 'ENCOUNTER_SPAWN_ANY' in mask or bool(types.intersection(mask))
            if (barrier or finite and finite_stop) and not any('ENCOUNTER_SPAWN_ANY' in mask or mask & types for mask in kills):
                limits[recipe['origin']] = max(recipe['physical_count'] * 2, finite + recipe['physical_count'])
    return limits


def _neutralize_cleanup_damage(block, scripts):
    owner, = entity_records(block)
    previous = {}
    for path, value in walk_values(owner['edit']):
        if not path.endswith('.eventDef'):
            continue
        script = path.rsplit('.item[', 1)[0]
        if script not in scripts:
            continue
        call_path = tuple(path.rsplit('.', 1)[0].split('.'))
        call = _get(owner['edit'], call_path)
        if (value == 'damageAI' and previous.get(script) == 'waitKillCount'
                and _get(call, ('args', 'item[0]', 'decl', 'damage')) == 'damage/ai/kill_full_gib'
                and _get(call, ('args', 'item[1]', 'eEncounterSpawnType_t')) == 'ENCOUNTER_SPAWN_ANY'
                and _get(call, ('args', 'item[2]', 'string')) == ''):
            block = _edit(block, call_path, {'eventDef': 'wait', 'args': {
                'num': 2, 'item[0]': {'float': 0}, 'item[1]': {'bool': False}}})
        if value != 'stopMaintainingAICount':
            previous[script] = value
    return block


def _post_bfg_awareness(block):
    owner, = entity_records(block)
    path = ('encounterComponent', 'entityEvents', 'item[0]', 'events')
    events = _get(owner['edit'], path)
    calls = [row['eventCall'] for row in list_items(events)]
    if (len(calls) != 6 or calls[-2]['eventDef'] != 'waitAIRemaining' or
            calls[-1]['eventDef'] != 'activateTarget' or
            calls[-1]['args']['item[0]'].get('entity') != 'phobos_target_relay_119'):
        raise ValueError('Mars Core post-BFG combat gate differs from the source')
    # the combat exit also raises awareness if the landing misses its traversal volume
    call = {'eventDef': 'raiseEventFlagOnExternalScript', 'args': {'num': 3,
        'item[0]': {'entity': 'phobos_encounter_manager_15'},
        'item[1]': {'eEncounterEventFlags_t': 'ENCOUNTER_EVENT_FLAG_USER_EVENT'},
        'item[2]': {'string': 'exit_precom_aware'}}}
    return _edit(block, path, {**events, 'num': 7, 'item[6]': {'eventCall': call}})


def _source_fallback_definition(record):
    if record['class'] != 'idTarget_Spawn':
        raise ValueError('Native fallback source is not a spawn point')
    name = 'ap_v070_source_' + _functional_token(record['name'])
    settings = {**record['edit'], 'flags': {**record['edit'].get('flags', {}), 'noFlood': False}}
    return name, ('entity { entityDef ' + name + ' { inherit = ' + _render(record['inherit'], 0) +
                  '; class = "idTarget_Spawn"; edit = ' + _render(settings, 2) + ' } }')


def materialize_native_recipe_calls(source, map_key, plan, source_maps, *, enemy_revision=2):
    prepared = materialize_native_recipe_definitions(source, map_key, plan, source_maps, enemy_revision=enemy_revision)
    names = {name for name, _, _ in ENTITY_HEADER.findall(prepared)}
    changes, aliases = {}, []
    source_index = entity_definition_index(source)
    owners = {}
    for origin, limit in _refill_limits(source, plan['maps'][map_key]['recipes']).items():
        alias = 'ap_v070_refill_r' + origin
        if alias in names or limit > 0x7fffffff:
            raise ValueError('Invalid native refill limit')
        names.add(alias)
        aliases.append('entity { entityDef ' + alias + ' { inherit = "target/spawn"; class = "idTarget_Spawn"; '
                       'edit = { spawnEditableShared = { groupName = "' + str(limit) + '"; } } } }')
    for recipe in plan["maps"][map_key]["recipes"]:
        binding = recipe["binding"]
        if binding["mode"] == "direct_point":
            name, target = binding["entity"], binding["reference"]
            if name not in changes:
                start, end = find_entity_block_bounds(source, name, index=source_index)
                changes[name] = start, end, source[start:end]
            start, end, block = changes[name]
            if name not in owners:
                owners[name], = entity_records(block, include_names={name})
            owner = owners[name]
            path = tuple(binding["path"].split("."))
            if _get(owner["edit"], path) != target:
                raise ValueError("Native direct activation binding differs from the source")
            first, last = find_entity_block_bounds(source, target, index=source_index)
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
            _get(owner["edit"], path[:-1])[path[-1]] = alias
            continue
        if binding["mode"] == "independent_actor":
            name = binding["entity"]
            first, last = find_entity_block_bounds(source, name, index=source_index)
            original, = entity_records(source[first:last])
            if original["class"] != "idAI2" or name in changes:
                raise ValueError("Independent native binding differs from the source")
            replacement, definitions = _independent_definitions(recipe, original, names)
            if name not in plan['maps'][map_key].get('passthrough_actors', []):
                changes[name] = first, last, replacement
            else:
                marker = 'ap_v070_passthrough_r' + recipe['origin']
                aliases.append('entity { entityDef ' + marker + ' { inherit = "info/null"; class = "idInfo"; edit = {} } }')
            aliases.extend(definitions)
            continue
        if binding["mode"] == "summon" and binding["entity"] not in {"", "ap_darklord_phantoms"}:
            name = binding["entity"]
            first, last = find_entity_block_bounds(source, name, index=source_index)
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
            start, end = find_entity_block_bounds(source, name, index=source_index)
            changes[name] = start, end, source[start:end]
        start, end, block = changes[name]
        if name not in owners:
            owners[name], = entity_records(block, include_names={name})
        owner = owners[name]
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
        bounds = find_entity_block_bounds(source, target, index=source_index) if target else None
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
        fallback_record = None
        if (recipe['family'] == 'group' and binding.get('owner') == '_example_encounter_manager_0' and
                target == 'breach_wasteland_arena_encounter_spawn_group_zone_2'):
            if bounds is None or kind != 'idTargetSpawnGroup':
                raise ValueError('Immora breach source is not a spawn group')
            fallback_target = 'breach_wasteland_arena_target_spawn_60'
            fallback_bounds = find_entity_block_bounds(source, fallback_target, index=source_index)
            if fallback_bounds is None:
                raise ValueError('Immora breach fallback source is absent')
            first, last = fallback_bounds
            fallback_record, = entity_records(source[first:last])
        elif bounds is not None and kind == 'idTarget_Spawn' and recipe['family'] in {'immediate', 'possessed'}:
            fallback_record = record
        if fallback_record:
            fallback, definition = _source_fallback_definition(fallback_record)
            if fallback not in names:
                names.add(fallback)
                aliases.append(definition)
        aliases.append('entity { entityDef ' + alias + ' { inherit = ' + _render(inherit, 0) +
            '; class = ' + _render(kind, 0) + '; edit = ' + _render(settings, 2) + ' } }')
        block = _edit(block, (*path, "args", f"item[{argument}]", "entity"), alias)
        call["args"][f"item[{argument}]"]["entity"] = alias
        changes[name] = start, end, block
    for name, (start, end, block) in changes.items():
        scripts = {recipe['binding']['path'].rsplit('.item[', 1)[0]
                   for recipe in plan['maps'][map_key]['recipes']
                   if recipe['binding'].get('entity') == name and recipe['binding']['mode'] == 'event'}
        if scripts:
            changes[name] = start, end, _neutralize_cleanup_damage(block, scripts)
    if map_key == 'e2m3_core' and 'phobos_encounter_manager_6' in changes:
        start, end, block = changes['phobos_encounter_manager_6']
        changes['phobos_encounter_manager_6'] = start, end, _post_bfg_awareness(block)
    fragments = []
    offset = 0
    for start, end, block in sorted(changes.values()):
        fragments.extend((source[offset:start], block))
        offset = end
    fragments.append(source[offset:])
    compiled = "".join(fragments)
    return compiled + prepared[len(source):] + "\n\n".join(aliases) + ("\n" if aliases else "")
