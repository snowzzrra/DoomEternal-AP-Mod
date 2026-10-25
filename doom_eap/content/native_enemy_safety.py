"""Spatial and scripted actor restrictions for native encounter replacements."""
from copy import deepcopy
from importlib.resources import files
import json

SCRIPTED_BOSS_ACTORS = {'e1m4_boss': frozenset({'boss_intro_ai_superheavy_doomhunter_stage1_1'})}
CINEMATIC_BOSS_ACTORS = {'e2m2_base': frozenset({'confront_samuel_ai'})}
FLYING_SPAWN_POINTS = {'e1m4_boss': frozenset({'wing_a_target_spawn_41', 'wing_a_target_spawn_42'})}
FLYING_REPLACEMENTS = frozenset({'cacodemon', 'pain_elemental', 'lost_soul'})
SMALL_GROUND_ENEMIES = frozenset({'imp', 'hell_soldier', 'shotgun_soldier', 'chaingun_soldier', 'demonic_trooper'})
GROUND_ENCOUNTERS = {
    ('e1m4_boss', 'train_encounter_manager_intro_train_1'): SMALL_GROUND_ENEMIES,
    ('e1m4_boss', 'intro_encounter_manager_intro_arena_1'): SMALL_GROUND_ENEMIES | {
        'carcass', 'dread_knight', 'hell_knight', 'revenant', 'prowler', 'whiplash'},
}


def _replace_species(body, species, recipe, ordinal, enemies, catalog, speed_capabilities):
    from .native_enemy_plan import _body_hitpoints, native_body_name

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


def project_native_safety(plan, enemies):
    from .enemy_plan import sample_species
    from .enemy_species import enemy_speed_capabilities
    from .native_enemy_plan import native_recipe_fingerprint

    if plan is None:
        return None
    affected = (set(SCRIPTED_BOSS_ACTORS) | set(CINEMATIC_BOSS_ACTORS) | set(FLYING_SPAWN_POINTS)) & set(plan['maps'])
    affected.update(key for key, stage in plan['maps'].items()
                    if any(body['species'] == 'cursed_prowler' for recipe in stage['recipes'] for body in recipe['bodies']))
    if not affected:
        return plan
    result = deepcopy(plan)
    catalog = json.loads(files(__package__).joinpath('native_enemy_catalog.json').read_bytes())
    speed_capabilities = enemy_speed_capabilities()
    changes = []
    for map_key in sorted(affected):
        stage = result['maps'][map_key]
        protected = SCRIPTED_BOSS_ACTORS.get(map_key, frozenset()) | CINEMATIC_BOSS_ACTORS.get(map_key, frozenset())
        points = FLYING_SPAWN_POINTS.get(map_key, ())
        settings = {**enemies, 'special_limits': stage['special_limits']}
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
                before = body['species']
                _replace_species(body, species, recipe, ordinal, settings, catalog, speed_capabilities)
                changes.append({'origin': recipe['id'], 'action': 'flying_spawn', 'before': before, 'after': species})
        for recipe in stage['recipes']:
            if any(body['species'] == 'cursed_prowler' for body in recipe['bodies']):
                alternate = deepcopy(recipe)
                for ordinal, body in enumerate(alternate['bodies']):
                    if body['species'] == 'cursed_prowler':
                        _replace_species(body, 'prowler', alternate, ordinal, settings, catalog, speed_capabilities)
                stage.setdefault('counter_recipes', []).append(alternate)
                changes.append({'origin': recipe['id'], 'action': 'blood_punch_fallback', 'after': 'prowler'})
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


def opening_source(stage, source):
    if stage in OPENING_REGIONS and stage not in OPENING_PREFIXES:
        return True
    binding = source.get('binding') or {}
    return any(binding.get(field, '').startswith(OPENING_PREFIXES.get(stage, ()))
               for field in ('owner', 'entity'))
