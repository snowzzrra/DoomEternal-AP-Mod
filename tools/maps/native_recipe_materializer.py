"""Prepare frozen native body definitions and encounter bindings."""

import copy
import hashlib
import math
import re

from doom_eap.content.enemy_species import validate_enemy_speed_percent
from doom_eap.content.native_enemy_plan import native_body_name, native_recipe_metadata
from tools.maps.ap_map_generator import entity_definition_index, find_entity_block_bounds
from tools.maps.encounter_catalog import (
    ENTITY_HEADER,
    SPAWN_CLASSES,
    entity_records,
    list_items,
    load_encounter_profiles,
    walk_values,
)
from tools.maps.enemy_attribute_baselines import resolve_enemy_attribute_baselines
from tools.maps.enemy_attributes import _MISSING, _attribute_source, _edit, _get, _render, materialize_enemy_attributes
from tools.maps.native_enemy_inventory import _protection, _spawn_constraints

MAX_POSITION_DIAGNOSTICS_PER_MAP = 512
MAX_POSITION_DIAGNOSTICS = MAX_POSITION_DIAGNOSTICS_PER_MAP * 64


class RecipeMaterializationError(ValueError):
    failure_domain = "room_package"

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _resolved_population_limits(population):
    if population is None:
        return {}
    if population.heavy > 0xffffff:
        raise RecipeMaterializationError('recipe_unsafe', 'Population heavy limit exceeds its native field.')
    return {"population_limits": dict(scope=int(population.scope, 16), maximum=population.maximum,
        heavy=population.heavy, superheavy=population.superheavy, pressure=population.pressure,
        mixed=population.mixed, strict=population.strict, counter=population.counter,
        **({'species': dict(population.species_caps)} if population.species_caps else {}))}


def _resolved_native_body(body, templates):
    from doom_eap.content.native_enemy_plan import _body_hitpoints

    donor = templates[body.template_ref]
    attributes = dict(zip(("health_percent", "damage_percent", "speed_percent", "size_percent"),
        (body.health_percent, body.damage_percent, body.speed_percent, body.size_percent), strict=False))
    if donor['qualification']['status'] != 'qualified' or any(value > 10000 for value in attributes.values()):
        raise RecipeMaterializationError('recipe_unsafe', 'Enemy body attributes are not qualified.')
    validate_enemy_speed_percent(donor['species'], body.speed_percent)
    return dict(species=donor['species'], spawn_type=donor['spawn_type'], ai=dict(donor['ai']),
        attributes=attributes, hitpoints=_body_hitpoints(donor, attributes),
        special_limits=dict(zip(('marauder', 'archvile', 'spirit', 'combined_special'), body.limits.special, strict=False)),
        **_resolved_population_limits(body.limits.population))


def _check_resolved_source(row, source, map_key, restrictions):
    binding = source['binding']
    protected = set(restrictions['scripted_boss_actors'].get(map_key, ()))
    protected.update(restrictions['cinematic_boss_actors'].get(map_key, ()))
    if source.get('protected_reason') or source.get('inactive_reason') or binding.get('entity') in protected:
        raise RecipeMaterializationError('recipe_unsafe', 'Recipe replaces a protected or inactive source.')
    if row.logical_count != source['logical_count'] or row.spawn_type_ref not in source['logical_types']:
        raise RecipeMaterializationError('recipe_source', 'Recipe differs from its native source request.')
    maximum = source.get('maximum_emissions', source['logical_count'])
    units = (row.maximum_emissions - 1) * row.logical_count + row.final_credit_units
    if row.bodies and maximum and (not row.maximum_emissions or units > maximum * len(row.bodies)):
        raise RecipeMaterializationError('recipe_quota', 'Recipe quota differs from its native source.')


def _resolved_requires_flying(recipe, map_key, restrictions):
    binding = recipe['binding']
    return (binding.get('reference') in restrictions['flying_spawn_points'].get(map_key, ()) or
            (binding['mode'] == 'summon' and
            recipe['selected_spawn_type'] in restrictions['flying_summons'].get(map_key, ())))


def _check_resolved_species(recipe, source, map_key, role, restrictions):
    from doom_eap.content.native_enemy_safety import SMALL_GROUND_ENEMIES

    species = {body['species'] for body in recipe['bodies']}
    allowed = source.get('allowed_species')
    if source['logical_types'] == ['ENCOUNTER_SPAWN_TENTACLE']:
        allowed = SMALL_GROUND_ENEMIES
    allowed = next((rule['species'] for rule in restrictions['ground_encounters']
                    if (rule['map_key'], rule['owner']) == (map_key, source['binding'].get('owner'))), allowed)
    if allowed and not species <= set(allowed):
        raise RecipeMaterializationError('recipe_unsafe', 'Enemy body does not fit its source.')
    _check_resolved_safety(recipe, map_key, role, restrictions)
    if role != 'compatibility' and _resolved_requires_flying(recipe, map_key, restrictions) and not species <= set(restrictions['flying_replacements']):
        raise RecipeMaterializationError('recipe_unsafe', 'Enemy source requires flying bodies.')


def _safety_species(recipe, map_key, restrictions):
    replacements = dict(restrictions['safety_replacements'])
    if recipe['binding'].get('reference') in restrictions['spirit_denied_spawn_points'].get(map_key, ()):
        replacements['spirit'] = 'cacodemon'
    return [replacements.get(body['species'], body['species']) for body in recipe['bodies']]


def _check_resolved_safety(recipe, map_key, role, restrictions):
    if role != 'compatibility' and _safety_species(recipe, map_key, restrictions) != [
            body['species'] for body in recipe['bodies']]:
        raise RecipeMaterializationError('recipe_unsafe', 'Enemy bodies violate spawn safety restrictions.')


def _check_safety_compatibility(original, alternate, map_key, restrictions):
    if _resolved_requires_flying(original, map_key, restrictions):
        return
    expected = _safety_species(alternate, map_key, restrictions)
    before = [body['species'] for body in alternate['bodies']]
    after = [body['species'] for body in original['bodies']]
    if expected == before or expected != after:
        raise RecipeMaterializationError('recipe_role', 'Recipe source does not permit these compatibility bodies.')


def _resolved_native_row(row, source, map_key, catalog, identity):
    from doom_eap.content.enemy_species import enemy_spawn_mask
    from doom_eap.content.native_enemy_plan import _name_recipe_bodies, recipe_token

    restrictions = catalog['runtime_restrictions']
    _check_resolved_source(row, source, map_key, restrictions)
    recipe = dict(id=source['id'], origin=recipe_token([identity, map_key, source['id']]),
        family=source['family'], binding=dict(source['binding']), runtime_only=source.get('runtime_only', False),
        selected_spawn_type=row.spawn_type_ref, logical_mask=enemy_spawn_mask(row.spawn_type_ref),
        logical_count=row.logical_count, physical_count=len(row.bodies), maximum_emissions=row.maximum_emissions,
        final_credit_units=row.final_credit_units, renewable=row.renewable,
        quantity_ratio=[row.ratio_numerator, row.ratio_denominator],
        bodies=[_resolved_native_body(body, catalog['templates']) for body in row.bodies])
    _check_resolved_species(recipe, source, map_key, row.role_ref, restrictions)
    _name_recipe_bodies(recipe, {'revision': 9})
    return recipe


def _check_resolved_alternate(original, alternate, field, map_key, catalog):
    if original is None:
        raise RecipeMaterializationError('recipe_role', 'Recipe alternative has no primary row.')
    fields = ('logical_count', 'physical_count', 'maximum_emissions', 'final_credit_units',
              'quantity_ratio', 'selected_spawn_type', 'renewable')
    if any(original[key] != alternate[key] for key in fields):
        raise RecipeMaterializationError('recipe_role', 'Recipe alternative changes its request quota.')
    expected = _resolved_counter_species(original, catalog)
    if field == 'counter_recipes' and [body['species'] for body in alternate['bodies']] != expected:
        raise RecipeMaterializationError('recipe_role', 'Recipe counter bodies do not match the primary row.')
    if field == 'compatibility_recipes':
        _check_safety_compatibility(original, alternate, map_key, catalog['runtime_restrictions'])


def _resolved_counter_species(recipe, catalog):
    replacements = {rule['species']: rule['replacement'] for rule in catalog['counter_rules']}
    return [replacements.get(body['species'], body['species']) for body in recipe['bodies']]


def _check_resolved_alternatives(stage, map_key, catalog):
    primary = {row['id']: row for row in stage['recipes']}
    replacements = {rule['species']: rule['replacement'] for rule in catalog['counter_rules']}
    for field in ('compatibility_recipes', 'counter_recipes'):
        for alternate in stage[field]:
            _check_resolved_alternate(primary.get(alternate['id']), alternate, field, map_key, catalog)
    counters = {row['id'] for row in stage['counter_recipes']}
    if any(row['id'] not in counters and any(body['species'] in replacements for body in row['bodies'])
           for row in primary.values()):
        raise RecipeMaterializationError('recipe_role', 'Enemy recipe is missing its counter row.')


def _resolved_native_stage(record, map_key, catalog, identity):
    native = catalog['maps'][map_key]
    sources = {source['id']: source for source in native['origins'] + native.get('runtime_origins', [])}
    stage = dict(source_sha256=native['source_sha256'], runtime_map=native['runtime_map'],
                 recipes=[], compatibility_recipes=[], counter_recipes=[])
    fields = dict(primary='recipes', compatibility='compatibility_recipes', counter='counter_recipes')
    for row in record.rows:
        source = sources[catalog['sources'][row.source_ref]['source_id']]
        stage[fields[row.role_ref]].append(_resolved_native_row(row, source, map_key, catalog, identity))
    _check_resolved_alternatives(stage, map_key, catalog)
    return stage


def expand_resolved_native_recipes(slot, catalog):
    """Bind admitted room decisions to installed donors without selecting replacements."""
    from doom_eap.content.native_enemy_plan import native_recipe_fingerprint
    from doom_eap.content.recipe_codec import decode_recipes
    from doom_eap.content.slot_data_contract import catalog_tables

    catalog = catalog_tables(catalog)
    try:
        recipes = decode_recipes(slot.recipes)
        if not recipes.enabled and any(record.rows for record in recipes.maps.values()):
            raise RecipeMaterializationError('recipe_unsafe', 'Disabled recipes contain replacement rows.')
        plan = dict(revision=20, source_fingerprint=slot.identity.recipe_digest,
            maps={key: _resolved_native_stage(record, key, catalog, slot.identity.recipe_digest)
                  for key, record in recipes.maps.items()})
        plan['fingerprint'] = native_recipe_fingerprint(plan)
        return plan
    except (KeyError, TypeError, IndexError) as error:
        raise RecipeMaterializationError('recipe_reference', f'Recipe cannot resolve an installed reference: {error}') from error
    except ValueError as error:
        if isinstance(error, RecipeMaterializationError):
            raise
        raise RecipeMaterializationError('recipe_unsafe', str(error)) from error


def materialize_resolved_timers(text, member, slot, catalog):
    from doom_eap.content.slot_data_contract import catalog_tables

    catalog = catalog_tables(catalog)
    for ref, duration in slot.recipes.timers:
        timer = catalog['timers'][ref]
        runtime = catalog['maps'][timer['stage_id']]['runtime_map']
        if not member.endswith('/maps/' + runtime + '.entities'):
            continue
        bounds = find_entity_block_bounds(text, timer['entity'])
        if bounds is None:
            raise RecipeMaterializationError('recipe_timer', 'Resolved timer target is missing.')
        try:
            block = _edit(text[slice(*bounds)], ('challengeDuration',), duration)
        except ValueError as error:
            raise RecipeMaterializationError('recipe_timer', str(error)) from error
        text = text[:bounds[0]] + block + text[bounds[1]:]
    return text


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


def _independent_definitions_layers(definitions, entries, names, original, properties, recipe):
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
    return inherit, initial, layers


def _independent_definitions(recipe, original, names):
    if recipe["logical_count"] != 1 or recipe["maximum_emissions"] != recipe["physical_count"]:
        raise ValueError("Independent native actor needs one finite logical occurrence")
    properties = _functional_properties(original["edit"])
    properties.update({key: original["edit"][key] for key in
                       ("spawnPosition", "spawnOrientation", "bindInfo", "flags", "dormancy") if key in original["edit"]})
    properties["flags"] = {**properties.get("flags", {}), "hide": True}
    definitions, entries = [], []
    inherit, initial, layers = _independent_definitions_layers(definitions, entries, names, original, properties, recipe)
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


def _functional_sources_inherited(records):
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
    return inherited


def _source_record(source, name, index, records):
    if name not in records:
        start, end = find_entity_block_bounds(source, name, index=index)
        records[name], = entity_records(source[start:end], include_names={name})
    return records[name]


def _functional_source_names(source):
    return {name for name, inherit, kind in ENTITY_HEADER.findall(source)
                if kind in SPAWN_CLASSES | {"idAI2", "idInfoTraversal", "idInfoCoverExposed", "idPlayerStart", "idInfo_TeleportDestination"}
                or (kind == 'idProp2' and inherit.startswith(('pickup/ammo/', 'pickup/armor/', 'pickup/health/')))}


def _functional_sources_properties(source, cache=None):
    selected = _functional_source_names(source)
    records = {row["name"]: row for row in entity_records(source, include_names=selected)}
    if cache is not None:
        cache.update(records)
    properties = {name: _functional_properties(row["edit"]) for name, row in records.items() if row["class"] == "idAI2"}
    properties = {name: value for name, value in properties.items() if value}
    return properties, records


def _reachable_class(inherited, name, properties, reachable, records, visited):
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


def _functional_sources(source, cache=None):
    properties, records = _functional_sources_properties(source, cache)
    inherited = _functional_sources_inherited(records)
    sources = {}


    def reachable(name, visited=None):
        if visited is None:
            if name not in sources:
                sources[name] = reachable(name, set())
            return sources[name]
        return _reachable_class(inherited, name, properties, reachable, records, visited)
    return properties, reachable, records


def _entrance_endpoint_fullmatch(animation, edit, position):
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


def _entrance_endpoint(edit):
    animation = edit.get('spawnEditable', {}).get('spawnAnim', '').rsplit('/', 1)[-1]
    position = edit.get('spawnPosition', {})
    if 'spawnPosition' not in edit or not all(math.isfinite(position.get(axis, 0)) for axis in ('x','y','z')):
        return None
    return _entrance_endpoint_fullmatch(animation, edit, position)


def _ground_edit(name, records, verified):
    edit = records[name]['edit']
    spawn = edit.get('spawnEditable', {})
    entrance = (verified and spawn.get('aiStateOverride') in {
        'AIOVERRIDE_PLAY_ENTRANCE_ANIMATION', 'AIOVERRIDE_PLAY_ENTRANCE_ANIMATION_WITH_CORRECTION'}
        and re.fullmatch(r'jump_forward_\d+(?:_(?:up|down)_\d+)?', spawn.get('spawnAnim', '').rsplit('/', 1)[-1]))
    templates = [records.get(value.get('name'), {}) for field,value in edit.get('entityDefs', {}).items()
                 if re.fullmatch(r'item\[\d+\]', field) and isinstance(value, dict)]
    return edit, entrance, spawn, templates


def _ground_spawn_animation(entrance, spawn, templates):
    return (entrance or (not spawn.get('spawnAnim') and spawn.get('aiStateOverride', 'AIOVERRIDE_DEFAULT') in {'AIOVERRIDE_DEFAULT', 'AIOVERRIDE_TELEPORT', 'AIOVERRIDE_TELEPORT_FORCE_SPAWN_ANGLE'})) and all(template.get('edit', {}).get('aiEditable', {}).get('spawnSettings', {}).get('initialState', 'AIOVERRIDE_DEFAULT') in {'AIOVERRIDE_DEFAULT', 'AIOVERRIDE_TELEPORT', 'AIOVERRIDE_TELEPORT_FORCE_SPAWN_ANGLE', 'AIOVERRIDE_FORCE_AWARENESS_OF_PLAYER'} for template in templates)


def _ground_spawn_templates(entrance, spawn, templates, verified):
    return _ground_spawn_animation(entrance, spawn, templates) and (verified or templates) and (not templates or (any if verified else all)(template.get('class') == 'idAI2' and (not any(kind in template.get('inherit', '') for kind in ('/ambient/', '/friendly/', 'cacodemon', 'painelemental', 'lostsoul', 'spirit', 'bloodangel'))) for template in templates))


def _entrance_landings_airborne(points, positions, recipes, records, resolve, scope):


    def ground(name, verified=False):
        edit, entrance, spawn, templates = _ground_edit(name, records, verified)
        return (_ground_spawn_templates(entrance, spawn, templates, verified) and (not spawn.get('spawnAt')) and (not edit.get('bindInfo')) and (name in positions) and all(map(math.isfinite, positions[name].values())))
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
    return flying_points, footprints, ground, owners


def _entrance_landings_animation(anchors, candidates, distance, edit, pickups):
    if not candidates:
        candidates = {other for other in anchors if distance(other)[0] <= 50**2}
    if not candidates:
        candidates = {other for other in pickups if distance(other)[0] <= 50**2}
    animation = edit.get('spawnEditable', {}).get('spawnAnim', '')
    direction = 1 if '_up_' in animation or '/riseup_' in animation else -1 if '_down_' in animation else 0
    return candidates, direction


def _entrance_landings_points(records):
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
    return points, positions, resolve


def _landing_anchors_pickups(positions, records):
    pickups = {name for name,row in records.items() if row['class'] == 'idProp2' and not row['edit'].get('bindInfo')
               and name in positions and all(map(math.isfinite, positions[name].values()))}
    return pickups


def _landing_anchor_class(flying_points, ground, ground_points, name, row):
    return (row['class'] in {'idInfoCoverExposed', 'idPlayerStart', 'idInfo_TeleportDestination'} or (row['class'] == 'idTarget_Spawn' and ground(name, name in ground_points) and (name in ground_points or (name not in flying_points and row['edit'].get('entityDefs', {}).get('num', 0) > 0))) or (row['class'] == 'idInfoTraversal' and row['edit'].get('snapToGround') is True))


def _landing_anchors(flying_points, ground, owners, positions, records):
    ground_points = set().union(*owners.values())
    anchors = {name for name, row in records.items()
               if _landing_anchor_class(flying_points, ground, ground_points, name, row) and (not row['edit'].get('bindInfo')) and (name in positions) and all(map(math.isfinite, positions[name].values()))}
    pickups = _landing_anchors_pickups(positions, records)
    return anchors, pickups


def _fallback_landing_owners_bounds(anchors, footprint, owner, owners, positions):
    bounds = {axis: (min(pos[axis] for pos in footprint)-50, max(pos[axis] for pos in footprint)+50) for axis in ('x','y')}
    owners[owner] = {name for name in anchors if all(
        bounds[axis][0] <= positions[name][axis] <= bounds[axis][1] for axis in ('x','y'))}


def _fallback_landing_owners(anchors, footprints, owners, positions):
    fallback_owners = set()
    for owner, names in footprints.items():
        if owners.get(owner) or not names:
            continue
        footprint = [positions.get(name, {}) for name in names]
        if not all(all(math.isfinite(pos.get(axis, math.nan)) for axis in ('x','y','z')) for pos in footprint):
            continue
        # navigation and cover markers provide ground even when every entrance starts outside the arena.
        _fallback_landing_owners_bounds(anchors, footprint, owner, owners, positions)
        fallback_owners.add(owner)
    return fallback_owners


def _traversal_landing_target_colocated(name, positions, records, target):
    colocated = [key for key, row in records.items() if row['class'] == 'idInfoTraversal'
        and key in positions and 'traversalEndpoint' in row['edit']
        and not row['edit'].get('bindInfo')
        and sum((positions[name][axis] - positions[key][axis]) ** 2 for axis in ('x', 'y', 'z')) <= .01]
    if len(colocated) == 1:
        target = colocated[0]
    return target


def _traversal_landing_target(name, positions, records, spawn):
    target = spawn.get('initialTargetOverride')
    if not target and name in positions:
        target = _traversal_landing_target_colocated(name, positions, records, target)
    return target


def _directed_landing_candidates(candidates, direction, position, positions):
    directed = {other for other in candidates if direction and
                direction*(positions[other]['z']-position['z']) >= .5 and
                sum((position[axis]-positions[other][axis])**2 for axis in ('x','y','z')) <= 50**2}
    if directed:
        candidates = directed
    return candidates


def _nearest_entrance_landing(anchors, choices, edit, fallback_owners, name, owners, pickups, positions, recipe, scope):
    candidates = owners.get(scope(recipe), set())
    position = positions.get(name, {})
    if not all(math.isfinite(position.get(axis, math.nan)) for axis in ('x','y','z')):
        return
    def distance(other):
        target = positions[other]
        return ((position['x'] - target['x'])**2 + (position['y'] - target['y'])**2
                + (position['z'] - target['z'])**2), other
    candidates = {other for other in candidates if distance(other)[0] <= 50**2}
    if scope(recipe) in fallback_owners or not candidates:
        candidates, direction = _entrance_landings_animation(anchors, candidates, distance, edit, pickups)
        candidates = _directed_landing_candidates(candidates, direction, position, positions)
    if not candidates:
        return
    target = min(candidates, key=distance)
    if distance(target)[0] <= 50**2:
        choices.setdefault(name, []).append((distance(target), target))


def _fixed_entrance_landing(name, overrides, records, ground, choices, message):
    if name not in overrides:
        return False
    target = overrides[name]
    if target not in records or not ground(target, True):
        raise ValueError(message)
    choices.setdefault(name, []).append(((0, target), target))
    return True


def _is_traversal_landing(positions, target, traversal):
    return traversal.get('class') == 'idInfoTraversal' and target in positions and ('traversalEndpoint' in traversal['edit'])


def _choose_entrance_landing(anchors, choices, fallback_owners, floor_overrides, ground, name, needs_floor, owners, pickups, portal_floors, positions, recipe, records, scope):
    edit = records[name]['edit']
    spawn = edit.get('spawnEditable', {})
    if _fixed_entrance_landing(name, floor_overrides, records, ground, choices, 'Cultist combat floor differs from the source'):
        return
    if spawn.get('aiStateOverride') in {'AIOVERRIDE_FORCE_MOVE_TO_TRAVERSAL', 'AIOVERRIDE_FORCE_TELEPORT_TO_TRAVERSAL'}:
        target = _traversal_landing_target(name, positions, records, spawn)
        traversal = records.get(target, {})
        if (_is_traversal_landing(positions, target, traversal)):
            choices.setdefault(name, []).append(((0, target), target))
            return
    if not needs_floor and edit.get('spawnEditable', {}).get('aiStateOverride') not in {
            'AIOVERRIDE_PLAY_ENTRANCE_ANIMATION', 'AIOVERRIDE_PLAY_ENTRANCE_ANIMATION_WITH_CORRECTION',
            'AIOVERRIDE_ENTRANCE_ANIM_NO_ENEMIES', 'AIOVERRIDE_SPAWN_ANIM_IDLE',
            'AIOVERRIDE_FORCE_MOVE_TO_TRAVERSAL', 'AIOVERRIDE_FORCE_TELEPORT_TO_TRAVERSAL'}:
        return
    # the portal animation's name does not describe its final floor position.
    if _fixed_entrance_landing(name, portal_floors, records, ground, choices, 'Atlantica portal floor differs from the source'):
        return
    if not edit.get('bindInfo') and _entrance_endpoint(edit) is not None:
        choices.setdefault(name, []).append(((0, name), name))
        return
    return _nearest_entrance_landing(anchors, choices, edit, fallback_owners, name, owners, pickups, positions, recipe, scope)


def _entrance_landings(recipes, records, *, map_key=None):
    points, positions, resolve = _entrance_landings_points(records)
    def scope(recipe):
        binding = recipe['binding']
        return binding.get('owner', ''), binding.get('path', '').split('.events.item[')[0]
    flying_points, footprints, ground, owners = _entrance_landings_airborne(points, positions, recipes, records, resolve, scope)
    anchors, pickups = _landing_anchors(flying_points, ground, owners, positions, records)
    fallback_owners = _fallback_landing_owners(anchors, footprints, owners, positions)
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
            _choose_entrance_landing(anchors, choices, fallback_owners, floor_overrides, ground, name, needs_floor, owners, pickups, portal_floors, positions, recipe, records, scope)
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


def _spawn_position(name, row, landing_positions, map_key, recipe):
    original = row['edit'].get('spawnPosition')
    if original is None:
        return None
    final = {axis: landing_positions.get(name, original).get(axis, 0) for axis in ('x', 'y', 'z')}
    return {'map': map_key, 'recipe': recipe['origin'], 'source': name[:160],
            'position': final, 'relocated': final != {axis: original.get(axis, 0) for axis in ('x', 'y', 'z')}}


def _spawn_references(row, records):
    if row['class'] not in SPAWN_CLASSES:
        return []
    return [value for _, value in walk_values(row['edit']) if isinstance(value, str) and value in records]


def _position_diagnostic_limit(map_key, diagnostics, compatible):
    if diagnostics is None or compatible:
        return 0
    remaining = max(0, MAX_POSITION_DIAGNOSTICS_PER_MAP - sum(row['map'] == map_key for row in diagnostics))
    return min(len(diagnostics) + remaining, MAX_POSITION_DIAGNOSTICS)


def _record_spawn_positions(reference, records, landing_positions, map_key, recipe, diagnostics, compatible):
    limit = _position_diagnostic_limit(map_key, diagnostics, compatible)
    if not limit:
        return
    visited, pending, initial_count = set(), [reference], len(diagnostics)
    while pending and len(visited) < 256:
        name = pending.pop()
        if name in visited or name not in records:
            continue
        visited.add(name)
        row = records[name]
        position = _spawn_position(name, row, landing_positions, map_key, recipe)
        if position is not None and len(diagnostics) < limit:
            diagnostics.append(position)
        pending.extend(reversed(_spawn_references(row, records)))
    if len(diagnostics) == initial_count and len(diagnostics) < limit:
        diagnostics.append({'map': map_key, 'recipe': recipe['origin'], 'source': str(reference)[:160],
                            'position': None, 'relocated': None, 'reason': 'runtime_selected_position'})


def _materialize_native_recipe_definitions_groups(generated, stage):
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


def _materialize_native_recipe_definitions_baseline(attributes, body, donor_indexes, donors, emitted_bodies, name, names, reference, source_maps):
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
    return baseline, block, key, record


def _materialize_native_recipe_definitions_library(definitions, generated, map_key, names, plan):
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
    return library, metadata, values


def _materialize_native_recipe_definitions_anchor(helper, landing, names, point, records):
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
    return edit


def _materialize_native_recipe_definitions_damage_percent(attributes, baseline, block, donors, key, name, paths, recipe, record):
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
    return clone


def _build_landing_definitions(generated, map_key, names, records, stage):
    landing_positions = {}
    for point, landing in sorted(_entrance_landings(stage['recipes'], records, map_key=map_key).items()):
        helper = 'ap_v070_landing_' + _functional_token(point)
        if helper in names:
            raise ValueError('Duplicate native entrance landing')
        edit = _materialize_native_recipe_definitions_anchor(helper, landing, names, point, records)
        edit['spawnEditableShared'] = {'groupName': helper}
        landing_positions[point] = edit.get('spawnPosition')
        generated.append('entity {\n\tentityDef ' + helper + ' {\n\t\tinherit = "target/spawn";\n'
            '\t\tclass = "idTarget_Spawn";\n\t\tedit = ' +
            _render(edit, 2) + '\n\t}\n}')
    return landing_positions


def _append_functional_variants(clone, functional, functional_libraries, generated, name, names, source_names):
    for source_name in source_names:
        token = _functional_token(source_name)
        variant = "ap_v070_functional_" + token + "_" + _functional_token(name)
        if variant in names:
            raise ValueError("Duplicate native functional body definition")
        names.add(variant)
        generated.append(_functional_definition(clone, variant, functional[source_name]))
        functional_libraries.setdefault(source_name, []).append({"name": variant})


def _append_native_body(body, compatible, definitions, donor_indexes, donors, emitted_bodies, functional, functional_libraries, generated, name, names, recipe, recipe_definitions, source_maps, source_names, clones):
    reference, attributes = body["ai"], body["attributes"]
    if compatible and emitted_bodies.get(name) == body:
        recipe_definitions.append({"name": name})
        return
    if name in names:
        raise ValueError(f"Duplicate native body definition: {name}")
    baseline, block, key, record = _materialize_native_recipe_definitions_baseline(attributes, body, donor_indexes, donors, emitted_bodies, name, names, reference, source_maps)
    paths = []
    if attributes["health_percent"] != 100:
        paths.extend(("aiHealth", "components", "components[0]", field) for field in ("max", "starting"))
    clone_key = key, tuple((field, type(value), value) for field, value in sorted(attributes.items())), recipe['binding'].get('entity') == 'ap_darklord_phantoms'
    if clone_key not in clones:
        clones[clone_key] = _materialize_native_recipe_definitions_damage_percent(attributes, baseline, block, donors, key, name, paths, recipe, record)
    clone = clones[clone_key]
    header = ENTITY_HEADER.search(clone)
    clone = clone[:header.start(1)] + name + clone[header.end(1):]
    generated.append('entity {\n\tlayers { "spawn_target_layer" }\n\t' + clone[header.start():].rstrip())
    _append_functional_variants(clone, functional, functional_libraries, generated, name, names, source_names)
    definitions.append({"name": name})
    recipe_definitions.append({"name": name})


def _append_body_library(generated, names, prefix, recipe, recipe_definitions):
    values = {"num": len(recipe_definitions), **{f"item[{i}]": value for i, value in enumerate(recipe_definitions)}}
    body_library = prefix + recipe["origin"]
    if body_library in names:
        raise ValueError("Duplicate native body library")
    names.add(body_library)
    generated.append('entity {\n\tentityDef ' + body_library +
        ' {\n\t\tinherit = "target/spawn";\n\t\tclass = "idTarget_Spawn";\n\t\tedit = ' +
        _render({"entityDefs": values, "initialEntityDefs": values}, 2) + '\n\t}\n}')


def _build_body_libraries(definitions, diagnostics, donor_indexes, donors, emitted_bodies, functional, functional_libraries, generated, landing_positions, map_key, names, reachable, records, settings, source_maps, stage):
    clones = {}
    for recipe, compatible, prefix in ([(recipe, False, 'ap_v070_bodies_r') for recipe in stage['recipes']] +
            [(recipe, True, 'ap_v070_compat_r') for recipe in stage.get('compatibility_recipes', [])] +
            [(recipe, True, 'ap_v070_counter_r') for recipe in stage.get('counter_recipes', [])]):
        recipe_definitions = []
        reference = recipe["binding"].get("reference", recipe["binding"].get("entity", ""))
        _record_spawn_positions(reference, records, landing_positions, map_key, recipe, diagnostics, compatible)
        source_names = sorted(reachable(reference)) if recipe["family"] != "summon" else []
        if recipe["physical_count"] != len(recipe["bodies"]):
            raise ValueError("Native recipe body quantity differs")
        if re.fullmatch(r"[0-9a-f]{20}", recipe["origin"]) is None:
            raise ValueError("Invalid native recipe origin")
        for body, name in _body_definitions(recipe, settings):
            _append_native_body(body, compatible, definitions, donor_indexes, donors, emitted_bodies, functional, functional_libraries, generated, name, names, recipe, recipe_definitions, source_maps, source_names, clones)
        _append_body_library(generated, names, prefix, recipe, recipe_definitions)


def _validate_recipe_source(map_key, plan, source, stage):
    if re.fullmatch(r"[a-z0-9_]+", map_key) is None:
        raise ValueError("Invalid native recipe map key")
    if hashlib.sha256(source.encode("utf8")).hexdigest() != stage["source_sha256"]:
        raise ValueError(f"Native recipe source hash differs: {map_key}")
    if re.fullmatch(r"[0-9a-f]{64}", plan["fingerprint"]) is None:
        raise ValueError("Invalid native recipe fingerprint")


def materialize_native_recipe_definitions(source, map_key, plan, source_maps, *, enemy_revision=2, diagnostics=None, source_records=None):
    stage = plan["maps"][map_key]
    settings = {**stage, "revision": enemy_revision}
    _validate_recipe_source(map_key, plan, source, stage)
    names = {name for name, _, _ in ENTITY_HEADER.findall(source)}
    definitions, donors, generated = [], {}, []
    donor_indexes = {}
    if plan.get('revision', 0) >= 17:
        _materialize_native_recipe_definitions_groups(generated, stage)
    functional, reachable, records = _functional_sources(source, source_records)
    landing_positions = _build_landing_definitions(generated, map_key, names, records, stage)
    functional_libraries = {}
    emitted_bodies = {}
    _build_body_libraries(definitions, diagnostics, donor_indexes, donors, emitted_bodies, functional, functional_libraries, generated, landing_positions, map_key, names, reachable, records, settings, source_maps, stage)
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
    library, metadata, values = _materialize_native_recipe_definitions_library(definitions, generated, map_key, names, plan)
    generated.append('entity {\n\tentityDef ' + library + ' {\n\t\tinherit = "target/spawn";\n'
        '\t\tclass = "idTarget_Spawn";\n\t\tedit = ' + _render({"entityDefs": values, "initialEntityDefs": values,
            "spawnEditableShared": {"groupName": metadata}}, 2)
        + '\n\t}\n}')
    return source + "\n\n" + "\n\n".join(generated) + "\n"


def _refill_limits_calls(record):
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
    return calls, kills, stops


def _refill_limits_binding(recipe, record, stops, types):
    binding = recipe['binding']
    call = _get(record['edit'], tuple(binding.get('path', '').split('.')))
    label = _get(call, ('args', 'item[6]', 'string')) if isinstance(call, dict) else _MISSING
    finite_stop = any(label == stop_label and 'ENCOUNTER_SPAWN_ANY' not in mask.split()
                      and not types.intersection(mask.split()) for stop_label, mask in stops)
    return binding, call, finite_stop, label


def _refill_limits_owners(recipes, source, source_records=None):
    owners = {r['binding'].get('owner') for r in recipes
              if r['family'] == 'maintenance' and not r['maximum_emissions']}
    if source_records is not None:
        index = entity_definition_index(source)
        names = sorted((name for name in owners if name in index), key=index.get)
        return {name: _source_record(source, name, index, source_records) for name in names}
    records = {r['name']: r for r in entity_records(source, include_names=owners - {None, ''})}
    return records


def _refill_limits_finite(owner, recipes):
    finite = sum(r['maximum_emissions'] for r in recipes if r['binding'].get('owner') == owner
                 and r['family'] in {'immediate', 'group', 'staggered'})
    return finite


def _maintenance_stop_matches(label, mask, stop_label, types):
    return (not stop_label or stop_label == label) and ('ENCOUNTER_SPAWN_ANY' in mask or types.intersection(mask))


def _maintenance_stop_identity(event):
    mask = event['args']['item[0]']['eEncounterSpawnType_t'].split()
    stop_label = next((arg['string'] for arg in list_items(event['args']) if 'string' in arg), '')
    return mask, stop_label


def _refill_barrier(binding, call, calls, label, types):
    barrier = False
    active = False
    script = binding.get('path', '').rsplit('.item[', 1)[0]
    for sequence, event in calls:
        if sequence != script:
            continue
        if event is call:
            active = True
        elif active:
            if event['eventDef'] == 'stopMaintainingAICount':
                mask, stop_label = _maintenance_stop_identity(event)
                if _maintenance_stop_matches(label, mask, stop_label, types):
                    break
            elif event['eventDef'] == 'waitAIRemaining':
                mask = event['args']['item[0]']['eEncounterSpawnType_t'].split()
                barrier |= 'ENCOUNTER_SPAWN_ANY' in mask or bool(types.intersection(mask))
    return barrier


def _refill_limits_maximum_emissions(calls, finite, kills, limits, owner, recipe, record, stops):
    if recipe['binding'].get('owner') != owner or recipe['family'] != 'maintenance' or recipe['maximum_emissions']:
        return
    types = set(recipe['selected_spawn_type'].split())
    binding, call, finite_stop, label = _refill_limits_binding(recipe, record, stops, types)
    barrier = _refill_barrier(binding, call, calls, label, types)
    if (barrier or (finite and finite_stop)) and not any('ENCOUNTER_SPAWN_ANY' in mask or mask & types for mask in kills):
        limits[recipe['origin']] = max(recipe['physical_count'] * 2, finite + recipe['physical_count'])


def _refill_limits(source, recipes, source_records=None):
    limits = {}
    records = _refill_limits_owners(recipes, source, source_records)
    for owner, record in records.items():
        calls, kills, stops = _refill_limits_calls(record)
        finite = _refill_limits_finite(owner, recipes)
        for recipe in recipes:
            _refill_limits_maximum_emissions(calls, finite, kills, limits, owner, recipe, record, stops)
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


def _native_event_target(owner, path, binding):
    call = _get(owner["edit"], path)
    if call is _MISSING or call["eventDef"] != binding["event_def"]:
        raise ValueError("Native recipe event binding differs from the source")
    argument = binding["argument"]
    if type(argument) is not int or argument < 0:
        raise ValueError("Invalid native recipe argument binding")
    target = _get(call, ("args", f"item[{argument}]", "entity"))
    if target is _MISSING or target != binding["reference"]:
        raise ValueError("Native recipe target binding differs from the source")
    return target


def _protected_native_event(profile, path, bodies):
    if not profile:
        return False
    script, event = int(path[2][5:-1]), int(path[4][5:-1])
    if _protection(profile, script, event):
        return True
    allowed = _spawn_constraints(profile, script, event).get("allowed_species", ())
    return bool(allowed) and any(body["species"] not in allowed for body in bodies)


def _materialize_native_recipe_calls_fragments(aliases, changes, map_key, plan, prepared, source):
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


def _append_refill_aliases(aliases, map_key, names, plan, source, source_records=None):
    for origin, limit in _refill_limits(source, plan['maps'][map_key]['recipes'], source_records).items():
        alias = 'ap_v070_refill_r' + origin
        if alias in names or limit > 0x7fffffff:
            raise ValueError('Invalid native refill limit')
        names.add(alias)
        aliases.append('entity { entityDef ' + alias + ' { inherit = "target/spawn"; class = "idTarget_Spawn"; '
                       'edit = { spawnEditableShared = { groupName = "' + str(limit) + '"; } } } }')


def _materialize_direct_activation(aliases, binding, changes, names, owners, recipe, source, source_index, source_records):
    name, target = binding["entity"], binding["reference"]
    if name not in changes:
        start, end = find_entity_block_bounds(source, name, index=source_index)
        changes[name] = start, end, source[start:end]
    start, end, block = changes[name]
    if name not in owners:
        owners[name] = copy.deepcopy(_source_record(source, name, source_index, source_records))
    owner = owners[name]
    path = tuple(binding["path"].split("."))
    if _get(owner["edit"], path) != target:
        raise ValueError("Native direct activation binding differs from the source")
    first, last = find_entity_block_bounds(source, target, index=source_index)
    original = _source_record(source, target, source_index, source_records)
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


def _materialize_independent_actor(aliases, binding, changes, map_key, names, plan, recipe, source, source_index, source_records):
    name = binding["entity"]
    first, last = find_entity_block_bounds(source, name, index=source_index)
    original = _source_record(source, name, source_index, source_records)
    if original["class"] != "idAI2" or name in changes:
        raise ValueError("Independent native binding differs from the source")
    replacement, definitions = _independent_definitions(recipe, original, names)
    if name not in plan['maps'][map_key].get('passthrough_actors', []):
        changes[name] = first, last, replacement
    else:
        marker = 'ap_v070_passthrough_r' + recipe['origin']
        aliases.append('entity { entityDef ' + marker + ' { inherit = "info/null"; class = "idInfo"; edit = {} } }')
    aliases.extend(definitions)


def _materialize_summon(binding, changes, recipe, source, source_index, source_records):
    name = binding["entity"]
    first, last = find_entity_block_bounds(source, name, index=source_index)
    original = _source_record(source, name, source_index, source_records)
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


def _request_spawn_settings_fallback_target(bounds, kind, source, source_index):
    if bounds is None or kind != 'idTargetSpawnGroup':
        raise ValueError('Immora breach source is not a spawn group')
    fallback_target = 'breach_wasteland_arena_target_spawn_60'
    fallback_bounds = find_entity_block_bounds(source, fallback_target, index=source_index)
    if fallback_bounds is None:
        raise ValueError('Immora breach fallback source is absent')
    return fallback_bounds


def _request_source_shape(recipe, source, source_index, target, source_records):
    bounds = find_entity_block_bounds(source, target, index=source_index) if target else None
    if bounds is None:
        if recipe["family"] in {"group", "maintenance", "staggered"}:
            return bounds, None, "encounter/spawn_group/zone", "idTargetSpawnGroup"
        return bounds, None, "target/spawn", "idTarget_Spawn"
    first, last = bounds
    record = _source_record(source, target, source_index, source_records)
    if record["class"] not in {"idTarget_Spawn", "idTarget_Spawn_Parent", "idTargetSpawnGroup"}:
        raise ValueError("Native recipe target is not a native spawner")
    return bounds, record, record["inherit"], record["class"]


def _request_spawn_settings(aliases, binding, names, recipe, source, source_index, target, source_records):
    bounds, record, inherit, kind = _request_source_shape(recipe, source, source_index, target, source_records)
    settings = ({"spawners": {"num": 0}, "includeTouchingSpawners": False, "includeInGlobalOverrideGroup": False,
                 "clipModelInfo": {"type": "CLIPMODEL_NONE"}}
                if kind == "idTargetSpawnGroup" else
                {"entityDefs": {"num": 0}, "initialEntityDefs": {"num": 0}, "targets": {"num": 0}})
    fallback_record = None
    if (recipe['family'] == 'group' and binding.get('owner') == '_example_encounter_manager_0' and
            target == 'breach_wasteland_arena_encounter_spawn_group_zone_2'):
        fallback_bounds = _request_spawn_settings_fallback_target(bounds, kind, source, source_index)
        first, last = fallback_bounds
        fallback_record = _source_record(source, "breach_wasteland_arena_target_spawn_60", source_index, source_records)
    elif bounds is not None and kind == 'idTarget_Spawn' and recipe['family'] in {'immediate', 'possessed'}:
        fallback_record = record
    if fallback_record:
        fallback, definition = _source_fallback_definition(fallback_record)
        if fallback not in names:
            names.add(fallback)
            aliases.append(definition)
    return inherit, kind, settings


def _materialize_native_event(aliases, binding, changes, names, owners, profiles, recipe, source, source_index, source_records):
    name, path = binding["entity"], binding["path"].split(".")
    if name not in changes:
        start, end = find_entity_block_bounds(source, name, index=source_index)
        changes[name] = start, end, source[start:end]
    start, end, block = changes[name]
    if name not in owners:
        owners[name] = copy.deepcopy(_source_record(source, name, source_index, source_records))
    owner = owners[name]
    target = _native_event_target(owner, path, binding)
    # preserve native calls when frozen recipes conflict with encounter safety.
    if _protected_native_event(profiles.get(name, {}), path, recipe["bodies"]):
        return
    argument = binding["argument"]
    call = _get(owner["edit"], path)
    alias = "ap_v070_request_r" + recipe["origin"]
    if alias in names:
        raise ValueError("Duplicate native recipe request alias")
    names.add(alias)
    inherit, kind, settings = _request_spawn_settings(aliases, binding, names, recipe, source, source_index, target, source_records)
    aliases.append('entity { entityDef ' + alias + ' { inherit = ' + _render(inherit, 0) +
        '; class = ' + _render(kind, 0) + '; edit = ' + _render(settings, 2) + ' } }')
    block = _edit(block, (*path, "args", f"item[{argument}]", "entity"), alias)
    call["args"][f"item[{argument}]"]["entity"] = alias
    changes[name] = start, end, block


def _materialize_recipe_binding(aliases, changes, map_key, names, owners, plan, profiles, recipe, source, source_index, source_records):
    binding = recipe["binding"]
    if binding["mode"] == "direct_point":
        _materialize_direct_activation(aliases, binding, changes, names, owners, recipe, source, source_index, source_records)
        return
    if binding["mode"] == "independent_actor":
        _materialize_independent_actor(aliases, binding, changes, map_key, names, plan, recipe, source, source_index, source_records)
        return
    if binding["mode"] == "summon" and binding["entity"] not in {"", "ap_darklord_phantoms"}:
        _materialize_summon(binding, changes, recipe, source, source_index, source_records)
        return
    if binding["mode"] != "event":
        return
    return _materialize_native_event(aliases, binding, changes, names, owners, profiles, recipe, source, source_index, source_records)


def materialize_native_recipe_calls(source, map_key, plan, source_maps, *, enemy_revision=2, diagnostics=None):
    source_records = {}
    prepared = materialize_native_recipe_definitions(source, map_key, plan, source_maps, enemy_revision=enemy_revision, diagnostics=diagnostics, source_records=source_records)
    names = {name for name, _, _ in ENTITY_HEADER.findall(prepared)}
    changes, aliases = {}, []
    source_index = entity_definition_index(source)
    owners = {}
    profiles = load_encounter_profiles().get(map_key, {})
    _append_refill_aliases(aliases, map_key, names, plan, source, source_records)
    for recipe in plan["maps"][map_key]["recipes"]:
        _materialize_recipe_binding(aliases, changes, map_key, names, owners, plan, profiles, recipe, source, source_index, source_records)
    return _materialize_native_recipe_calls_fragments(aliases, changes, map_key, plan, prepared, source)
