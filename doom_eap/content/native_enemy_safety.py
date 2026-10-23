"""Spatial and scripted actor restrictions for native encounter replacements."""
from copy import deepcopy
from importlib.resources import files
import json

SCRIPTED_BOSS_ACTORS = {'e1m4_boss': frozenset({'boss_intro_ai_superheavy_doomhunter_stage1_1'})}
FLYING_SPAWN_POINTS = {'e1m4_boss': frozenset({'wing_a_target_spawn_41', 'wing_a_target_spawn_42'})}
FLYING_REPLACEMENTS = frozenset({'cacodemon', 'pain_elemental', 'lost_soul'})


def project_native_safety(plan, enemies):
    from .enemy_plan import sample_species
    from .enemy_species import enemy_speed_capabilities
    from .native_enemy_plan import _body_hitpoints, native_body_name, native_recipe_fingerprint

    if plan is None:
        return None
    affected = (set(SCRIPTED_BOSS_ACTORS) | set(FLYING_SPAWN_POINTS)) & set(plan['maps'])
    if not affected:
        return plan
    result = deepcopy(plan)
    catalog = json.loads(files(__package__).joinpath('native_enemy_catalog.json').read_bytes())
    speed_capabilities = enemy_speed_capabilities()
    changes = []
    for map_key in sorted(affected):
        stage = result['maps'][map_key]
        protected = SCRIPTED_BOSS_ACTORS.get(map_key, ())
        points = FLYING_SPAWN_POINTS.get(map_key, ())
        for recipe in stage['recipes']:
            binding = recipe['binding']
            if binding['mode'] == 'independent_actor' and binding['entity'] in protected:
                stage.setdefault('passthrough_actors', []).append(binding['entity'])
                changes.append({'origin': recipe['id'], 'action': 'preserve_scripted_boss'})
            if binding.get('reference') not in points or all(body['species'] in FLYING_REPLACEMENTS for body in recipe['bodies']):
                continue
            stage.setdefault('compatibility_recipes', []).append(deepcopy(recipe))
            for ordinal, body in enumerate(recipe['bodies']):
                if body['species'] in FLYING_REPLACEMENTS:
                    continue
                species = sample_species(enemies, FLYING_REPLACEMENTS, seed=plan['seed'],
                                         instance_key=[recipe['id'], ordinal, 'flying_spawn'])
                donor = catalog['species'][species]
                before = body['species']
                if speed_capabilities[species] != 'qualified':
                    body['attributes']['speed_percent'] = 100
                body.update(species=species, spawn_type=donor['spawn_type'], ai=dict(donor['ai']),
                            hitpoints=_body_hitpoints(donor, body['attributes']))
                body['definition'] = native_body_name(recipe['origin'], ordinal, recipe['logical_count'],
                    recipe['physical_count'], recipe['selected_spawn_type'], body, {**enemies, 'special_limits': stage['special_limits']})
                if 'tail_definition' in body:
                    body['tail_definition'] = native_body_name(recipe['origin'], ordinal, recipe['logical_count'],
                        recipe['physical_count'], recipe['selected_spawn_type'], body,
                        {**enemies, 'special_limits': stage['special_limits']}, credit_units=recipe['final_credit_units'])
                changes.append({'origin': recipe['id'], 'action': 'flying_spawn', 'before': before, 'after': species})
    if not changes:
        return plan
    result['source_fingerprint'] = plan['fingerprint']
    result['compiler_adjustments'] = changes
    result['fingerprint'] = native_recipe_fingerprint(result)
    return result
OPENING_REGIONS = {
    'e1m1_intro': ('Hell on Earth - Barge - Hell Priest Barge', 'Hell on Earth - City - Midtown'),
    'e1m2_war': ('Exultia - Exultia - Mourning Spire', 'Exultia - Exultia - Central Rotunda',
                 "Exultia - Exultia - Belphegor's Gate", 'Exultia - Exultia - Dour Slough'),
}
OPENING_PREFIXES = {
    'e1m1_intro': ('barge_', 'mech_', 'corner_street_', 'crack_building_'),
    'e1m2_war': ('capitol_', 'tutorial_', 'extraction_'),
}


def opening_source(stage, source):
    binding = source.get('binding') or {}
    return any(binding.get(field, '').startswith(OPENING_PREFIXES.get(stage, ()))
               for field in ('owner', 'entity'))
