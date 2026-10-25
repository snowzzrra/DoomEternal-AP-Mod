"""Extract native spawn counts and wait-constrained population envelopes."""

import argparse
from collections import defaultdict
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re

from doom_eap.content.enemy_species import enemy_spawn_mask
from doom_eap.content.vanilla_enemy_profiles import (
    CANONICAL_ORDER, ROLES, SPECIAL_SPECIES, attach_encounters, catalog_profile, reference_source, source_species,
)
from .encounter_catalog import SPAWN_CLASSES, entity_records, list_items


def population_bound(capacities, constraints, selected, *, node_budget=20000):
    active = {index for index in selected if capacities[index]}
    clauses = [(frozenset(indices & active), maximum) for indices, maximum in constraints]
    clauses = [(indices, maximum) for indices, maximum in clauses if indices]
    grouped = defaultdict(int)
    for index in active:
        membership = tuple(i for i, (indices, _) in enumerate(clauses) if index in indices)
        grouped[membership] += capacities[index]
    free = grouped.pop((), 0)
    items = sorted(grouped.items(), key=lambda row: (-len(row[0]), -row[1]))
    initial = tuple(maximum for _, maximum in clauses)
    visits = 0

    def upper(position, remaining):
        caps = [min([cap, *(remaining[index] for index in membership)])
                for membership, cap in items[position:]]
        value = sum(caps)
        for clause in range(len(remaining)):
            value = min(value, remaining[clause] + sum(cap for cap, (membership, _) in
                zip(caps, items[position:]) if clause not in membership))
        return value

    @lru_cache(None)
    def solve(position, remaining):
        nonlocal visits
        visits += 1
        if visits > node_budget:
            raise OverflowError
        if position == len(items):
            return 0
        membership, cap = items[position]
        cap = min([cap, *(remaining[index] for index in membership)])
        best = 0
        bound = upper(position, remaining)
        for count in range(cap, -1, -1):
            next_remaining = tuple(value - count if index in membership else value
                                   for index, value in enumerate(remaining))
            if count + upper(position + 1, next_remaining) <= best:
                continue
            best = max(best, count + solve(position + 1, next_remaining))
            if best == bound:
                break
        return best
    try:
        return free + solve(0, initial), True
    except OverflowError:
        return free + upper(0, initial), False


def _script_peak(events, sources, catalog):
    cohorts, constraints, couplings, capacities = [], [], [], []
    peak = dict(maximum=0, heavy=0, superheavy=0, stationary=0, pressure=0, mixed=0, combined_special=0,
                specials={species: 0 for species in sorted(SPECIAL_SPECIES)})
    exact, ambiguous = True, set()
    mask_for = {species: enemy_spawn_mask(row['spawn_type']) for species, row in catalog['species'].items()}

    def capture():
        nonlocal exact
        queries = {'maximum': set(range(len(cohorts))),
                   'heavy': {i for i, row in enumerate(cohorts) if 'heavy' in row['roles']},
                   'superheavy': {i for i, row in enumerate(cohorts) if 'superheavy' in row['roles']},
                   'stationary': {i for i, row in enumerate(cohorts) if 'stationary' in row['roles'] and
                                  not SPECIAL_SPECIES.intersection(row['species'])},
                   'combined_special': {i for i, row in enumerate(cohorts) if SPECIAL_SPECIES.intersection(row['species'])}}
        queries.update({species: {i for i, row in enumerate(cohorts) if species in row['species']}
                        for species in SPECIAL_SPECIES})
        for metric, selected in queries.items():
            count, solved = population_bound(capacities, [*constraints, *couplings], selected)
            exact &= solved
            if metric in SPECIAL_SPECIES:
                peak['specials'][metric] = max(peak['specials'][metric], count)
            else:
                peak[metric] = max(peak[metric], count)
        peak['pressure'] = peak['maximum']
        heavy = queries['superheavy']
        support = {i for i, row in enumerate(cohorts) if set(row['species']) & {'spirit', 'buff_pod', 'zombie_t1_screecher'}}
        for first in heavy:
            for second in support - {first}:
                if all(len({first, second} & indices) <= maximum for indices, maximum in [*constraints, *couplings]):
                    peak['mixed'] = 1

    for path, call in events:
        event = call.get('eventDef')
        args = list_items(call.get('args'))
        additions = sources.get(path, ())
        if additions:
            active = {i for i, cohort in enumerate(cohorts) if cohort['maintaining']}
            constraints = [(indices - active, maximum) for indices, maximum in constraints]
            for source in additions:
                species = source_species(source, catalog)
                count = source['logical_count']
                label = next((argument['string'] for argument in args if 'string' in argument), '')
                if len(species) > 1:
                    ambiguous.add(source['id'])
                indices = set()
                for candidate in species:
                    indices.add(len(cohorts))
                    cohorts.append(dict(species=(candidate,), roles={ROLES[candidate]}, label=label,
                        mask=mask_for[candidate], maintaining=source['family'] == 'maintenance',
                        maximum=source['maximum_emissions'], source_id=source['id']))
                    capacities.append(count)
                if len(indices) > 1:
                    couplings.append((indices, count))
            capture()
        if event == 'stopMaintainingAICount' and len(args) >= 2:
            mask, label = enemy_spawn_mask(args[0]['eEncounterSpawnType_t']), args[1]['string']
            for cohort in cohorts:
                if cohort['mask'] & mask and (not label or label == cohort['label']):
                    cohort['maintaining'] = False
        if event in {'waitAIRemaining', 'waitKillCount'} and len(args) >= 3:
            mask, count, label = enemy_spawn_mask(args[0]['eEncounterSpawnType_t']), args[1]['int'], args[2]['string']
            matched = {i for i, row in enumerate(cohorts) if row['mask'] & mask and
                       (not label or label == row['label'])}
            if event == 'waitKillCount':
                if any(not cohorts[i]['maximum'] for i in matched):
                    continue
                counts = {cohorts[i]['source_id']: cohorts[i]['maximum'] for i in matched}
                count = sum(counts.values()) - count
                if count < 0:
                    continue
            constraints.append((matched, max(0, count)))
    return peak, exact, sorted(ambiguous)


def script_replenishment_stops(events, sources, catalog):
    active, result = {}, {}
    for path, call in events:
        args = list_items(call.get('args'))
        for source in sources.get(path, ()):
            if source['family'] == 'maintenance' and source['maximum_emissions']:
                label = next((arg['string'] for arg in args if 'string' in arg), '')
                active[source['id']] = (source, label)
        if call.get('eventDef') != 'stopMaintainingAICount' or len(args) < 2:
            continue
        mask, label = enemy_spawn_mask(args[0]['eEncounterSpawnType_t']), args[1]['string']
        for key, (source, source_label) in list(active.items()):
            species = source_species(source, catalog)
            if species and (not label or label == source_label) and all(
                    enemy_spawn_mask(catalog['species'][name]['spawn_type']) & mask for name in species):
                result[key] = dict(declared_maximum=source['maximum_emissions'], pool_count=source['logical_count'],
                    stop_event_path=path, stop_spawn_type=args[0]['eEncounterSpawnType_t'], stop_label=label,
                    basis='maintenance_terminated_by_later_script_event')
                del active[key]
    return result


def build_vanilla_profiles(root, catalog=None):
    root = Path(root)
    if catalog is None:
        catalog = json.loads((root / 'doom_eap/content/native_enemy_catalog.json').read_bytes())
    maps = {}
    for key in CANONICAL_ORDER:
        source_file = root / 'vanillamaps' / f'{key}.map'
        payload = source_file.read_bytes()
        descriptor = json.loads((root / 'content/maps' / key / 'descriptor.json').read_bytes())
        if (hashlib.sha256(payload).hexdigest() != catalog['maps'][key]['source_sha256'] or
                descriptor['source_sha256'] != catalog['maps'][key]['source_sha256']):
            raise ValueError(f'Vanilla source hash mismatch: {key}')
        registry = json.loads((root / 'content/catalog/enemy_species.json').read_bytes())
        by_inherit = {inherit: name for name, row in registry['species'].items() for inherit in row.get('inherits', ())}
        all_records = list(entity_records(payload.decode('utf-8')))
        point_records = {row['name']: row for row in all_records
                         if row['class'] in SPAWN_CLASSES | {'idAI2'}}

        def point_species(name, seen=None):
            seen = set() if seen is None else seen
            if name in seen or name not in point_records:
                return set(), []
            seen.add(name)
            record = point_records[name]
            proof = [dict(entity=name, sha256=record['sha256'])]
            if record['class'] == 'idAI2':
                identity = by_inherit.get(record['inherit'])
                return ({identity} if identity else set()), proof
            edit = record['edit']
            children = (list_items(edit.get('spawners')) if record['class'] == 'idTargetSpawnGroup' else
                        [row['name'] for row in list_items(edit.get('initialEntityDefs')) or
                         list_items(edit.get('entityDefs'))])
            species = set()
            for child in children:
                if not isinstance(child, str):
                    continue
                members, evidence = point_species(child, seen)
                species.update(members)
                proof.extend(evidence)
            return species, proof

        annotated, identities = [], {}
        for source in catalog['maps'][key]['origins']:
            if not source.get('logical_count'):
                annotated.append(source)
                continue
            possible = set(source_species({**source, 'native_identity': dict(species=[name for name, row in catalog['species'].items()
                if row['spawn_type'] in source.get('logical_types', ())])}, catalog))
            evidence = []
            basis = 'explicit_native_request_type'
            if len(possible) > 1:
                physical, evidence = point_species((source.get('binding') or {}).get('reference', ''))
                matched = possible & physical
                if matched:
                    possible = matched
                basis = 'native_spawn_point_definitions' if evidence else 'native_request_type_union'
            categories = {name if name in SPECIAL_SPECIES else ROLES[name] for name in possible}
            identity = dict(species=sorted(possible),
                status='unknown_native_choice' if len(categories) != 1 else 'known_category',
                basis=basis, entities=evidence)
            identities[source['id']] = identity
            annotated.append({**source, 'native_identity': identity})
        resolved_catalog = {**catalog, 'maps': {**catalog['maps'], key: {**catalog['maps'][key], 'origins': annotated}}}
        by_entity = defaultdict(lambda: defaultdict(list))
        for source in annotated:
            identity = source.get('native_identity') or {}
            for_envelope = {**source, 'native_identity': {**identity, 'status': 'known_category'}} if identity else source
            if reference_source(for_envelope, resolved_catalog) and (source.get('binding') or {}).get('mode') == 'event':
                binding = source['binding']
                by_entity[binding['entity']][binding['path']].append(source)
        records = [record for record in all_records if record['name'] in by_entity]
        owner_peaks = {}
        source_order = {}
        source_lifetimes = {}
        for record in records:
            for script_index, script in enumerate(list_items(record['edit'].get('encounterComponent', {}).get('entityEvents'))):
                events = [(f'encounterComponent.entityEvents.item[{script_index}].events.item[{index}].eventCall', row['eventCall'])
                          for index, row in enumerate(list_items(script.get('events'))) if 'eventCall' in row]
                source_lifetimes.update(script_replenishment_stops(events, by_entity[record['name']], resolved_catalog))
                peak, exact, ambiguous = _script_peak(events, by_entity[record['name']], resolved_catalog)
                owner_peaks[f"{record['name']}/encounterComponent.entityEvents.item[{script_index}]"] = (
                    peak, exact, ambiguous)
                source_order[f"{record['name']}/encounterComponent.entityEvents.item[{script_index}]"] = record['source_line']
        resolved_catalog['maps'][key]['origins'] = [{**source, 'effective_replenishment': True}
            if source['id'] in source_lifetimes else source for source in annotated]
        profile = catalog_profile(resolved_catalog, key)
        profile.update(source_identities=identities, source_lifetimes=source_lifetimes)
        for wave in profile['waves']:
            script = wave['id'].rsplit('/', 1)[0]
            if script in owner_peaks:
                peak, exact, ambiguous = owner_peaks[script]
                wave.update(peak=peak, envelope_solved=exact,
                            source_line=source_order[script],
                            concurrency_basis='native_script_wait_constrained_upper_envelope',
                            ambiguous_population_sources=ambiguous)
            else:
                wave.update(envelope_solved=True, concurrency_basis='native_source_request_upper_envelope')
        profile['waves'].sort(key=lambda wave: (wave.get('source_line', 0),
                              re.sub(r'\d+', lambda match: match[0].zfill(10), wave['id'])))
        attach_encounters(profile)
        for row in profile['unknown_envelopes']:
            if row['id'] in owner_peaks:
                peak, exact, ambiguous = owner_peaks[row['id']]
                row.update(peak=peak, envelope_solved=exact,
                           concurrency_basis='native_script_wait_constrained_upper_envelope',
                           ambiguous_population_sources=ambiguous)
        maps[key] = profile
    return dict(schema_version=1, order=list(CANONICAL_ORDER), maps=maps,
                concurrency_basis='script-permitted upper envelope; not measured gameplay peak',
                route_basis='conditional scripts are separate templates; totals are not a played route')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = build_vanilla_profiles(args.root)
    args.output.write_text(json.dumps(result, sort_keys=True, separators=(',', ':')) + '\n', encoding='utf-8')
    print(f"Vanilla references: {len(result['maps'])} maps, {sum(len(m['waves']) for m in result['maps'].values())} templates")


if __name__ == '__main__':
    main()
