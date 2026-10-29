"""Spatial and scripted actor restrictions for native encounter replacements."""
from copy import deepcopy
from importlib.resources import files
import json

SCRIPTED_BOSS_ACTORS = {'e1m4_boss': frozenset({'boss_intro_ai_superheavy_doomhunter_stage1_1'})}
CINEMATIC_BOSS_ACTORS = {'e2m2_base': frozenset({'confront_samuel_ai'})}
FLYING_SPAWN_POINTS = {'e1m4_boss': frozenset({'wing_a_target_spawn_41', 'wing_a_target_spawn_42'})}
FLYING_REPLACEMENTS = frozenset({'cacodemon', 'pain_elemental', 'lost_soul'})
FLYING_SUMMONS = {'e4m3_mcity': frozenset({'ENCOUNTER_SPAWN_CACODEMON', 'ENCOUNTER_SPAWN_PAIN_ELEMENTAL'})}
SMALL_GROUND_ENEMIES = frozenset({'imp', 'hell_soldier', 'shotgun_soldier', 'chaingun_soldier', 'demonic_trooper'})
GROUND_ENCOUNTERS = {
    ('e1m4_boss', 'train_encounter_manager_intro_train_1'): SMALL_GROUND_ENEMIES,
    ('e1m4_boss', 'intro_encounter_manager_intro_arena_1'): SMALL_GROUND_ENEMIES | {
        'carcass', 'dread_knight', 'hell_knight', 'revenant', 'prowler', 'whiplash'},
}


def preserve_challenge_sources(origins, map_key, catalog):
    from .vanilla_enemy_profiles import load_vanilla_profiles

    targets = json.loads(files(__package__).joinpath('challenge_enemy_targets.json').read_bytes())
    targets = [target for target in targets if target['map_key'] == map_key]
    if not targets:
        return origins
    profile = load_vanilla_profiles()['maps'].get(map_key)
    # challenge requirements describe the qualified retail map.
    if not profile or profile['source_sha256'] != catalog['maps'][map_key]['source_sha256']:
        return origins
    slots = {slot['source_id']: slot for encounter in profile.get('encounters', [])
             if encounter.get('combat_kind') not in {'secret_encounter', 'slayer_gate_encounter'}
             and encounter['id'] in profile['primary_encounters']
             for slot in encounter['slots'] if not slot['renewable']}
    preserved = set()
    for target in targets:
        minimum = target['minimum']
        candidates = [source for source in origins if source['id'] in slots
                      and not source.get('protected_reason') and not source.get('inactive_reason')
                      and set(slots[source['id']]['species']) <= set(target['species'])]
        for source in sorted(candidates, key=lambda row: (row['id'] not in preserved, row['id'])):
            if minimum <= 0:
                break
            preserved.add(source['id'])
            minimum -= min(source['logical_count'], slots[source['id']]['count'])
        if minimum > 0:
            raise ValueError(f"Challenge has no reachable native enemy supply: {target['name']}")
    return [{**source, 'protected_reason': 'challenge enemy supply'} if source['id'] in preserved else source
            for source in origins]


def _replace_species(body, species, recipe, ordinal, enemies, catalog, speed_capabilities):
    from .native_enemy_plan import _body_hitpoints, native_body_name

    before = body['species']
    limits = body.get('population_limits', {})
    if before == 'spirit' and species == 'cacodemon' and limits.get('strict'):
        body['population_limits'] = {**limits, 'counter': 'spirit', 'heavy': min(limits['maximum'],
            limits['heavy'] + limits.get('species', {}).get('spirit', 0))}
    donor = catalog['species'][species]
    if speed_capabilities[species] != 'qualified':
        body['attributes']['speed_percent'] = 100
    body.update(species=species, spawn_type=donor['spawn_type'], ai=dict(donor['ai']),
                hitpoints=_body_hitpoints(donor, body['attributes']))
    body['definition'] = native_body_name(recipe['origin'], ordinal, recipe['logical_count'],
        recipe['physical_count'], recipe['selected_spawn_type'], body, enemies)
    if 'tail_definition' in body:
        body['tail_definition'] = native_body_name(recipe['origin'], ordinal, recipe['logical_count'],
            recipe['physical_count'], recipe['selected_spawn_type'], body, enemies,
            credit_units=recipe['final_credit_units'])


def _requires_flying(recipe, map_key):
    return (recipe['binding'].get('reference') in FLYING_SPAWN_POINTS.get(map_key, ()) or
            recipe['binding']['mode'] == 'summon' and
            recipe['selected_spawn_type'] in FLYING_SUMMONS.get(map_key, ()))


def _flying_recipe(recipe, map_key, enemies, seed, settings, catalog, capabilities, changes):
    from .enemy_plan import sample_species

    flying = _requires_flying(recipe, map_key)
    if not flying or all(body['species'] in FLYING_REPLACEMENTS for body in recipe['bodies']):
        return None
    alternate = deepcopy(recipe)
    enabled = [species for species in FLYING_REPLACEMENTS if enemies['weights'][species] > 0]
    for ordinal, body in enumerate(recipe['bodies']):
        if body['species'] in FLYING_REPLACEMENTS:
            continue
        species = (sample_species(enemies, enabled, seed=seed,
                   instance_key=[recipe['id'], ordinal, 'flying_spawn']) if enabled else 'cacodemon')
        before = body['species']
        _replace_species(body, species, recipe, ordinal, settings, catalog, capabilities)
        changes.append({'origin': recipe['id'], 'action': 'flying_spawn', 'before': before, 'after': species})
    return alternate


COUNTER_REPLACEMENTS = (('cursed_prowler', 'prowler', 'blood_punch_fallback'),
                        ('spirit', 'cacodemon', 'microwave_fallback'),
                        ('stone_imp', 'imp', 'stone_counter_fallback'),
                        ('armored_baron', 'baron', 'armored_counter_fallback'))


def _counter_recipe(recipe, settings, catalog, capabilities, changes):
    replacements = {species: replacement for species, replacement, action in COUNTER_REPLACEMENTS}
    if not any(body['species'] in replacements for body in recipe['bodies']):
        return None
    alternate = deepcopy(recipe)
    for ordinal, body in enumerate(alternate['bodies']):
        if body['species'] in replacements:
            _replace_species(body, replacements[body['species']], alternate, ordinal, settings, catalog, capabilities)
    present = {body['species'] for body in recipe['bodies']}
    changes.extend({'origin': recipe['id'], 'action': action, 'after': replacement}
                   for species, replacement, action in COUNTER_REPLACEMENTS if species in present)
    return alternate


def _project_stage_safety(stage, map_key, enemies, seed, catalog, capabilities):
    protected = SCRIPTED_BOSS_ACTORS.get(map_key, frozenset()) | CINEMATIC_BOSS_ACTORS.get(map_key, frozenset())
    settings = {**enemies, 'special_limits': stage['special_limits']}
    changes = []
    for recipe in stage['recipes']:
        binding = recipe['binding']
        if binding['mode'] == 'independent_actor' and binding['entity'] in protected:
            stage.setdefault('passthrough_actors', []).append(binding['entity'])
            changes.append({'origin': recipe['id'], 'action': 'preserve_scripted_boss'})
        alternate = _flying_recipe(recipe, map_key, enemies, seed, settings, catalog, capabilities, changes)
        if alternate is not None:
            stage.setdefault('compatibility_recipes', []).append(alternate)
    for recipe in stage['recipes']:
        alternate = _counter_recipe(recipe, settings, catalog, capabilities, changes)
        if alternate is not None:
            stage.setdefault('counter_recipes', []).append(alternate)
    return changes


def resolve_native_stage(stage, map_key, enemies, seed, catalog):
    from .enemy_species import enemy_speed_capabilities

    _project_stage_safety(stage, map_key, enemies, seed, catalog, enemy_speed_capabilities())
    actors = set(stage.pop('passthrough_actors', ()))
    preserved = [recipe for recipe in stage['recipes'] if recipe['binding'].get('entity') in actors]
    stage['protected'].extend({'id': recipe['id'], 'reason': 'scripted boss acquisition actor'} for recipe in preserved)
    preserved_ids = {recipe['id'] for recipe in preserved}
    for key in ('recipes', 'compatibility_recipes', 'counter_recipes'):
        if key in stage:
            stage[key] = [recipe for recipe in stage[key] if recipe['id'] not in preserved_ids]
    return stage


AMBIENT_ENCOUNTERS = {
    'e2m3_core': frozenset({
        'slayer_gate_encounter_manager_ammo_room',
        'slayer_gate_encounter_manager_slayer_side_closet',
        'slayer_gate_encounter_manager_slayer_lobby',
    }),
}

OPENING_REGIONS = {
    'e1m1_intro': ('Hell on Earth - Barge - Hell Priest Barge', 'Hell on Earth - City - Midtown'),
    'e1m2_war': ('Exultia - Exultia - Mourning Spire', 'Exultia - Exultia - Central Rotunda',
                 "Exultia - Exultia - Belphegor's Gate", 'Exultia - Exultia - Dour Slough'),
    'e1m3_cult': ('Cultist Base - Grotto of Penance', 'Cultist Base - Station of Inquisition',
                  'Cultist Base - Basilica of Adjuration'),
    'e1m4_boss': ('Doom Hunter Base - Station of Redemption', 'Doom Hunter Base - Lazarus Generators',
                  'Doom Hunter Base - Tower of Baphomet'),
    'e2m1_nest': ('Super Gore Nest - Upper Area - Mixom Square', 'Super Gore Nest - Upper Area - Star Mall',
                  'Super Gore Nest - Upper Area - Vermilion Canal'),
    'e2m2_base': ('ARC Complex - Emerson Hotel', "ARC Complex - Mario's Restaurant", 'ARC Complex - ARC Turret'),
    'e2m3_core': ('Mars Core - Phobos - Phobos Teleport Station', 'Mars Core - Phobos - BFG-10000 Gun Deck',
                  'Mars Core - UAC - BFG-10000 Ventilation'),
    'e3m1_slayer': ('Taras Nabad - City Outskirts', 'Taras Nabad - Atrium Plaza',
                    'Taras Nabad - Slayer Creation Chamber'),
    'e3m2_hell': ("Nekravol - Pain's Basin", 'Nekravol - The Mill', 'Nekravol - Passage of the Damned'),
    'e3m2_hell_b': ('Nekravol Part II - Corpse Conveyor', 'Nekravol Part II - Soul Extraction',
                    'Nekravol Part II - Demon Foundry'),
    'e3m3_maykr': ('Urdak - Infernal Demodulator', 'Urdak - The Sanctuary', 'Urdak - Guardians of Sanctuary'),
    'e4m1_rig': ('UAC Atlantica Facility - UAC Facility (Intact) - Landing Pad',
                 'UAC Atlantica Facility - UAC Facility (Intact) - Service Elevator',
                 'UAC Atlantica Facility - UAC Facility (Intact) - Generator Tower'),
    'e4m2_swamp': ('The Blood Swamps - Swamp Entrance', 'The Blood Swamps - Underworld Crossroad',
                   'The Blood Swamps - Sunken Courtyard'),
    'e4m3_mcity': ('The Holt - Tranquility', 'The Holt - Crimson Forest', 'The Holt - Maykr Pillars'),
    'e5m1_spear': ('The World Spear - Sentinel Village - Village Outskirts',
                   'The World Spear - Sentinel Village - Village Entrance',
                   'The World Spear - Sentinel Village - Sentinel Village'),
    'e5m2_earth': ('Reclaimed Earth - City Entrance', 'Reclaimed Earth - Reclaimed Train Stop',
                   'Reclaimed Earth - Overgrown Offices'),
    'e5m3_hell': ('Immora - The Dark Lord', 'Immora - Immora Assault - Battlefield',
                  'Immora - Immora Assault - Plateau Battle'),
}
OPENING_PREFIXES = {
    'e1m1_intro': ('barge_', 'mech_', 'corner_street_', 'crack_building_'),
    'e1m2_war': ('capitol_', 'tutorial_', 'extraction_'),
}
OPENING_OWNERS = {
    'e5m2_earth': frozenset({'ai_encounter_manager_dlc1_opening'}),
}


def opening_source(stage, source, *, verified=False):
    binding = source.get('binding') or {}
    if verified and stage == 'e1m1_intro' and binding.get('owner') == 'mech_street_encounter_manager_amb_caco':
        return False
    if verified and stage in OPENING_OWNERS:
        return any(binding.get(field) in OPENING_OWNERS[stage] for field in ('owner', 'entity'))
    if stage in OPENING_REGIONS and stage not in OPENING_PREFIXES:
        return True
    return any(binding.get(field, '').startswith(OPENING_PREFIXES.get(stage, ()))
               for field in ('owner', 'entity'))
