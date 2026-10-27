"""Vanilla spawn templates and script population envelopes."""

from collections import Counter
from functools import cache
from importlib.resources import files
import json
import re

from .enemy_options import SPECIES_GROUPS


CANONICAL_ORDER = (
    'e1m1_intro', 'e1m2_war', 'e1m3_cult', 'e1m4_boss', 'e2m1_nest',
    'e2m2_base', 'e2m3_core', 'e2m4_boss', 'e3m1_slayer', 'e3m2_hell',
    'e3m2_hell_b', 'e3m3_maykr', 'e3m4_boss', 'e4m1_rig', 'e4m2_swamp',
    'e4m3_mcity', 'e5m1_spear', 'e5m2_earth', 'e5m3_hell', 'e5m4_boss',
)
BOSS_MAPS = frozenset({'e1m4_boss', 'e2m4_boss', 'e3m4_boss', 'e5m4_boss'})
SPECIAL_SPECIES = frozenset({
    'archvile', 'marauder', 'spirit', 'buff_pod', 'zombie_t1_screecher',
    'blood_angel', 'cursed_prowler', 'stone_imp', 'armored_baron',
})
ROLES = {species: role for role, members in SPECIES_GROUPS.items() for species in members}


def _catalog_species(source, catalog):
    types = set(source.get('logical_types', ()))
    return tuple(sorted(species for species, row in catalog['species'].items()
                        if row['spawn_type'] in types))


def native_source_identity(source, catalog):
    if 'native_identity' in source:
        return source['native_identity']
    map_key = source['id'].split('/', 1)[0]
    recorded = load_vanilla_profiles()['maps'].get(map_key)
    stage = catalog['maps'].get(map_key)
    if recorded and stage and stage.get('source_sha256') == recorded['source_sha256']:
        identity = recorded.get('source_identities', {}).get(source['id'])
        if identity:
            return identity
    species = _catalog_species(source, catalog)
    categories = {name if name in SPECIAL_SPECIES else ROLES[name] for name in species}
    return dict(species=list(species), status='unknown_native_choice' if len(categories) != 1 else 'known_category',
                basis='native_request_types')


def source_species(source, catalog):
    return tuple(native_source_identity(source, catalog)['species'])


def source_replenishment(source, catalog):
    if not source['maximum_emissions'] or source.get('effective_replenishment'):
        return True
    key = source['id'].split('/', 1)[0]
    recorded = load_vanilla_profiles()['maps'].get(key)
    stage = catalog['maps'].get(key)
    return bool(recorded and stage and stage.get('source_sha256') == recorded['source_sha256'] and
                source['id'] in recorded.get('source_lifetimes', {}))


def reference_source(source, catalog=None):
    from .native_enemy_safety import CINEMATIC_BOSS_ACTORS

    if (source.get('protected_reason') or source.get('inactive_reason') or source.get('runtime_only')
            or source.get('family') == 'summon'):
        return False
    if ((source.get('native_identity') or {}).get('status') == 'unknown_native_choice' or
            catalog is not None and native_source_identity(source, catalog)['status'] == 'unknown_native_choice'):
        return False
    binding = source.get('binding') or {}
    owner = binding.get('owner', binding.get('entity', ''))
    map_key = source['id'].split('/', 1)[0]
    if binding.get('entity') in CINEMATIC_BOSS_ACTORS.get(map_key, ()):
        return False
    layers = source.get('layers', ())
    return not ('telemetry' in owner.lower() or 'masterlevel' in owner.lower()
                or 'master_level' in owner.lower() or
                layers and all('master' in layer.lower() for layer in layers))


def request_peak(slots):
    total = sum(slot['count'] for slot in slots)
    return dict(maximum=total, pressure=total,
        heavy=sum(slot['count'] for slot in slots if any(ROLES[s] == 'heavy' for s in slot['species'])),
        superheavy=sum(slot['count'] for slot in slots if any(ROLES[s] == 'superheavy' for s in slot['species'])),
        stationary=sum(slot['count'] for slot in slots if any(ROLES[s] == 'stationary' and s not in SPECIAL_SPECIES
                                                             for s in slot['species'])),
        mixed=0, combined_special=sum(slot['count'] for slot in slots
                                     if SPECIAL_SPECIES.intersection(slot['species'])),
        specials={name: sum(slot['count'] for slot in slots if name in slot['species'])
                  for name in sorted(SPECIAL_SPECIES)})


def catalog_profile(catalog, map_key):
    stage = catalog['maps'][map_key]
    waves, excluded, unknown = {}, [], []
    for source in stage['origins']:
        if not reference_source(source, catalog):
            excluded.append(source['id'])
            identity = native_source_identity(source, catalog) if source.get('logical_count') else {}
            if (identity.get('status') == 'unknown_native_choice' and reference_source(
                    {**source, 'native_identity': {**identity, 'status': 'known_category'}}, catalog)):
                unknown.append(dict(source_id=source['id'], family=source['family'],
                    logical_count=source['logical_count'], maximum_emissions=source['maximum_emissions'],
                    renewable=not source['maximum_emissions'] or bool(source.get('effective_replenishment')),
                    binding=source.get('binding'), wave=source.get('wave', source.get('phase', source['id'])),
                    identity=identity))
            continue
        species = source_species(source, catalog)
        if not species:
            raise ValueError(f"Vanilla source identity is unresolved: {source['id']}")
        key = source.get('wave', source.get('phase', source['id']))
        wave = waves.setdefault(key, dict(id=key, owner=(source.get('binding') or {}).get('owner',
            (source.get('binding') or {}).get('entity', source['id'])), slots=[]))
        count = source['logical_count']
        maximum = source['maximum_emissions']
        wave['slots'].append(dict(source_id=source['id'], count=count, species=list(species),
                                 family=source['family'], maximum_emissions=maximum,
                                 renewable=maximum == 0 or bool(source.get('effective_replenishment'))))
    for wave in waves.values():
        counts, finite, identities = Counter(), Counter(), Counter()
        ambiguous, renewable = [], 0
        for slot in wave['slots']:
            roles = {ROLES[species] for species in slot['species']}
            if len(roles) == 1:
                role = next(iter(roles))
                counts[role] += slot['count']
                finite[role] += 0 if slot['renewable'] else slot['maximum_emissions']
            else:
                ambiguous.append(slot['source_id'])
            if len(slot['species']) == 1:
                identities[slot['species'][0]] += slot['count']
            if slot['renewable']:
                renewable += slot['count']
        wave.update(counts=dict(counts), finite_counts=dict(finite), species_counts=dict(identities),
                    total=sum(slot['count'] for slot in wave['slots']),
                    finite_total=sum(slot['maximum_emissions'] for slot in wave['slots'] if not slot['renewable']),
                    renewable_targets=renewable, ambiguous_sources=ambiguous,
                    peak=request_peak(wave['slots']), envelope_solved=True,
                    concurrency_basis='native_source_request_upper_envelope')
    ordered = sorted(waves.values(), key=lambda wave: (wave['owner'],
                     re.sub(r'\d+', lambda match: match[0].zfill(10), wave['id'])))
    totals = Counter()
    for wave in ordered:
        totals.update(wave['finite_counts'])
    result = dict(map_key=map_key, source_sha256=stage.get('source_sha256', ''),
                boss=map_key in BOSS_MAPS, waves=ordered, finite_counts=dict(totals),
                finite_total=sum(wave['finite_total'] for wave in ordered),
                renewable_targets=sum(wave['renewable_targets'] for wave in ordered),
                summon_templates=[dict(source_id=source['id'], count=source['logical_count'],
                    species=list(source_species(source, catalog)), family='summon',
                    maximum_emissions=source['maximum_emissions'])
                    for source in stage['origins'] if source.get('family') == 'summon'
                    and not source.get('protected_reason') and not source.get('inactive_reason')],
                excluded_sources=excluded,
                unknown_sources=unknown,
                quantity_basis='authored_spawn_templates_including_conditional_scripts')
    attach_encounters(result)
    attach_unknown_envelopes(result)
    return result


def attach_encounters(profile):
    grouped = {}
    for wave in profile['waves']:
        match = re.search(r'/encounterComponent\.entityEvents\.item\[(\d+)\]/\d+$', wave['id'])
        key = wave['id'].rsplit('/', 1)[0] if match else wave['id']
        encounter = grouped.setdefault(key, dict(id=key, owner=wave['owner'],
            script_index=int(match[1]) if match else None,
            kind='scripted_encounter' if match else wave['slots'][0]['family'], waves=[]))
        encounter['waves'].append(wave)
    for encounter in grouped.values():
        encounter['slots'] = [slot for wave in encounter['waves'] for slot in wave['slots']]
        for field in ('counts', 'finite_counts', 'species_counts'):
            totals = Counter()
            for wave in encounter['waves']:
                totals.update(wave[field])
            encounter[field] = dict(totals)
        for field in ('total', 'finite_total', 'renewable_targets'):
            encounter[field] = sum(wave[field] for wave in encounter['waves'])
        peaks = [wave['peak'] for wave in encounter['waves'] if 'peak' in wave]
        if peaks:
            encounter['peak'] = {field: max(peak.get(field, 0) for peak in peaks)
                                 for field in ('maximum', 'heavy', 'superheavy', 'stationary', 'pressure', 'mixed', 'combined_special')}
            encounter['peak']['specials'] = {species: max(peak['specials'].get(species, 0) for peak in peaks)
                                              for species in sorted(SPECIAL_SPECIES)}
    profile['encounters'] = list(grouped.values())
    first = {}
    for encounter in profile['encounters']:
        owner = encounter['owner']
        if owner not in first or (encounter['script_index'] is not None and
                                  encounter['script_index'] < (first[owner]['script_index'] or 0)):
            first[owner] = encounter
    profile['primary_encounters'] = [encounter['id'] for encounter in first.values()]
    profile['primary_basis'] = 'lowest nonempty script index; alternatives retained separately'


def attach_unknown_envelopes(profile):
    grouped = {}
    known = {row['id']: row for row in profile['encounters']}
    for source in profile['unknown_sources']:
        wave = source['wave']
        match = re.search(r'/encounterComponent\.entityEvents\.item\[(\d+)\]/\d+$', wave)
        key = wave.rsplit('/', 1)[0] if match else wave
        binding = source.get('binding') or {}
        row = grouped.setdefault(key, dict(id=key, owner=binding.get('owner', binding.get('entity', source['source_id'])),
            script_index=int(match[1]) if match else None, source_ids=[], slots=list(known.get(key, {}).get('slots', ()))))
        row['source_ids'].append(source['source_id'])
        row['slots'].append(dict(count=source['logical_count'], species=source['identity']['species']))
    for row in grouped.values():
        row['peak'] = request_peak(row.pop('slots'))
        row.update(envelope_solved=True, concurrency_basis='native_source_request_upper_envelope')
        if row['id'] in known:
            known[row['id']]['peak'] = row['peak']
    profile['unknown_envelopes'] = list(grouped.values())


@cache
def load_vanilla_profiles():
    value = json.loads(files(__package__).joinpath('vanilla_enemy_profiles.json').read_bytes())
    if value.get('schema_version') != 1 or tuple(value.get('order', ())) != CANONICAL_ORDER:
        raise ValueError('Unsupported vanilla enemy reference')
    return value


def vanilla_profile(catalog, map_key):
    recorded = load_vanilla_profiles()['maps'].get(map_key)
    source_hash = catalog['maps'][map_key].get('source_sha256')
    if recorded and source_hash and recorded['source_sha256'] == source_hash:
        return recorded
    return catalog_profile(catalog, map_key)
