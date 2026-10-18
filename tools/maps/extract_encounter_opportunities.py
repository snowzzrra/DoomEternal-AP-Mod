"""Extract explicitly reviewed finite source segments from pinned map snapshots."""

import argparse
import hashlib
import json
from pathlib import Path
import re
from zipfile import ZipFile

from doom_eap.content.enemy_species import enemy_spawn_mask

from .ap_map_generator import find_entity_block_bounds
from .encounter_catalog import ENTITY_HEADER, entity_records, list_items, walk_values
from .encounter_materializer import _value_span, text_sha256
from .encounter_source_definitions import SourceDefinitions
from .enemy_attribute_baselines import resolve_enemy_attribute_baselines, positive_enemy_hitpoints
from .verify_encounter_opportunities import verify_catalog, typed_consumers, initial_maintenance_count


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def extract_reviewed_catalog(source_bytes, registry_bytes, profile, *, root=None):
    _require(hashlib.sha256(source_bytes).hexdigest() == profile['source']['sha256'], 'Reviewed source hash differs')
    source = source_bytes.decode('utf-8')
    headers = list(ENTITY_HEADER.finditer(source))
    source_names = {match.group(1) for match in headers}
    _require(len(headers) == len(source_names), 'Duplicate source entity')
    managers = {match.group(1) for match in headers if match.group(3) == 'idEncounterManager'}
    group_names = {match.group(1) for match in headers if match.group(3) == 'idTargetSpawnGroup'}
    registry = json.loads(registry_bytes)
    species = registry['species']
    source_definitions = SourceDefinitions(registry, root)
    cached = {}
    blocks = {}
    pins = {}
    attribute_baselines = None
    attribute_baselines_sha256 = None

    def record(name):
        if name not in cached:
            _require(name in source_names, f'Unresolved source entity: {name}')
            start, end = find_entity_block_bounds(source, name)
            block = source[start:end]
            cached[name] = next(entity_records(block, include_names={name}))
            blocks[name] = block
        row = cached[name]
        pin = dict(sha256=row['sha256'], class_name=row['class'], layers=row['layers'])
        if row['class'] == 'idAI2':
            pin['inherit'] = row['inherit']
        if row['class'] == 'idTarget_Spawn':
            pin['spawn_position'] = row['edit']['spawnPosition']
            override = row['edit'].get('spawnEditable', {}).get('aiStateOverride')
            pin['spawn_override'] = override
            if override == 'AIOVERRIDE_PLAY_ENTRANCE_ANIMATION':
                pin.update(protected_reason='authored_entrance_animation',
                    replacement_qualification='preserve_source_species_until_entrance_animation_is_qualified')
        pins[name] = pin
        return row

    segments = []
    for selection in profile['segments']:
        name = selection['manager']
        row = record(name)
        _require(row['class'] == 'idEncounterManager' and row['sha256'] == selection['manager_sha256'], f'Reviewed manager differs: {name}')
        _require(row['layers'] == selection['layers'], f'Reviewed context differs: {name}')
        edit = row['edit']
        scripts = list_items(edit['encounterComponent']['entityEvents'])
        _require(len(scripts) == 1 and not list_items(edit.get('preCombatTriggers')), f'Alternative activation needs review: {name}')
        calls = [event['eventCall'] for event in list_items(scripts[0]['events'])]
        scope = selection.get('source_slice', dict(first_event_index=0, last_event_index=len(calls) - 1))
        first, last = scope['first_event_index'], scope['last_event_index']
        _require(type(first) is int and type(last) is int and 0 <= first <= last < len(calls), 'Invalid source slice')
        selected_calls = list(enumerate(calls))[first:last + 1]
        initial_completion = selection.get('initial_maintenance_completion')
        allowed = {'spawnSingleAI', 'spawnAI', 'staggeredAISpawn', 'waitStaggeredSpawnComplete', 'wait', 'waitAIRemaining', 'activateTarget'}
        if initial_completion:
            allowed.add('maintainAICount')
        _require(all(call['eventDef'] in allowed for _, call in selected_calls),
            f'Unsupported cohort or control operation: {name}')
        controls = []
        for index, call in selected_calls:
            if call['eventDef'] != 'activateTarget':
                continue
            args = list_items(call['args'])
            _require(len(args) == 2 and set(args[0]) == {'entity'} and set(args[1]) == {'string'},
                f'Invalid source control arguments: {name}/{index}')
            controls.append(dict(event_index=index, event_def='activateTarget', target=args[0]['entity']))
        _require(controls == selection.get('control_events', []), f'Unreviewed source control events: {name}')
        control_entities = selection.get('control_entities', {})
        _require(all(control['target'] in control_entities for control in controls), f'Unreviewed control target: {name}')
        for target, digest in control_entities.items():
            target_row = record(target)
            _require(target_row['sha256'] == digest, f'Reviewed control entity differs: {target}')
            _require(all(child in control_entities for child in list_items(target_row['edit'].get('targets'))),
                f'Unreviewed downstream control target: {target}')
        population_indices = [index for index, call in selected_calls if call['eventDef'] != 'activateTarget']
        _require(population_indices, f'No final population barrier: {name}')
        phase_index = population_indices[-1]
        if 'source_slice' not in selection:
            _require(calls[phase_index]['eventDef'] == 'waitAIRemaining' and list_items(calls[phase_index]['args'])[1]['int'] == 0,
                f'No final population barrier: {name}')
        bindings = {}
        for field, class_name in [('commitTriggers', 'idEncounterTrigger_Commit'), ('exitTriggers', 'idEncounterTrigger_Exit'),
                ('userFlagTriggers', 'idEncounterTrigger_RaiseUserFlag')]:
            bindings[field] = []
            for target in list_items(edit.get(field)):
                if target not in source_names:
                    bindings[field].append(dict(entity=target, source_resolved=False))
                    continue
                trigger = record(target)
                _require(trigger['class'] == class_name, f'Invalid trigger class: {target}')
                binding = dict(entity=target)
                if field == 'exitTriggers':
                    binding['force_ai_to_flee'] = trigger['edit'].get('forceAIToFlee', False)
                if field == 'userFlagTriggers':
                    binding['label'] = trigger['edit']['userFlag']
                bindings[field].append(binding)
        incoming = selection.get('incoming_targets', [])
        for edge in incoming:
            _require(edge['entity'] != name, f'Activation target points to its own script: {name}')
            value = record(edge['entity'])['edit']
            for field in edge['path'].split('.'):
                value = value[field]
            _require(value == name, f'Reviewed activation target differs: {name}')
        _require(any(binding.get('source_resolved') is not False for binding in bindings['commitTriggers']) or incoming,
            f'No reviewed activation: {name}')
        creations = []
        event_gates = []
        used_points = set()
        for index, call in selected_calls:
            args = list_items(call['args'])
            if call['eventDef'] == 'activateTarget':
                continue
            if call['eventDef'] == 'wait':
                _require(type(args[0]['float']) in {int, float} and args[0]['float'] >= 0, f'Invalid source delay: {name}/{index}')
                continue
            if call['eventDef'] == 'waitAIRemaining':
                remaining = args[1]['int']
                _require(type(remaining) is int and remaining >= 0, f'Invalid population gate: {name}/{index}')
                if remaining > 0:
                    event_gates.append(dict(event_index=index, event_def=call['eventDef'], args=args))
                elif enemy_spawn_mask(args[0]['eEncounterSpawnType_t']) == (1 << 44) - 1 and args[2]['string'] == '':
                    used_points.clear()
                continue
            if call['eventDef'] == 'waitStaggeredSpawnComplete':
                continue
            maintenance = call['eventDef'] == 'maintainAICount'
            grouped = call['eventDef'] in {'spawnAI', 'staggeredAISpawn', 'maintainAICount'}
            count = (initial_maintenance_count(calls, index, initial_completion['delay_event_index'])
                     if maintenance else args[1]['int'] if grouped else 1)
            spawn_type = args[0]['eEncounterSpawnType_t']
            origin_name = args[5 if maintenance else 2 if grouped else 1]['entity']
            origin = record(origin_name)
            _require(type(count) is int and count > 0, f'Unqualified finite request: {name}/{index}')
            source_mask = enemy_spawn_mask(spawn_type)
            parent_names = []
            if grouped:
                _require(origin['class'] == 'idTargetSpawnGroup', f'Unsupported group: {origin_name}')
                point_names = [point for point in list_items(origin['edit']['spawners']) if record(point)['class'] != 'idTarget_Spawn_Parent']
                parent = origin['edit'].get('targetSpawnParent')
                if parent:
                    _require(record(parent)['class'] == 'idTarget_Spawn_Parent', f'Invalid spawn parent: {parent}')
                    parent_names.append(parent)
            else:
                point_names = [origin_name]
            queued = call['eventDef'] == 'staggeredAISpawn' or (grouped and count > 1)
            unrestricted = dict(maxCount=0, reuseDelaySec=0, doBoundsTest=False, boundsTestType='BOUNDSTEST_NONE',
                fovCheck=0, minDistance=0, maxDistance=0, neighborSpawnerDistance=-1, LOS_Test='LOS_NONE',
                playerToTest='PLAYER_SP', conditionProxy='')
            reused = used_points.intersection(point_names)
            _require(point_names and len(point_names) == len(set(point_names))
                and (queued or not reused or all(record(point)['edit'].get('spawnConditions') == unrestricted for point in reused)),
                f'Source point reuse without an empty-population barrier: {name}/{index}')
            used_points.update(point_names)
            members = []
            identities = set()
            for point_name in point_names:
                point = record(point_name)
                definitions = list_items(point['edit'].get('entityDefs'))
                definition_source = point_name
                if not definitions and grouped and parent_names:
                    parent_definitions = list_items(record(parent_names[0])['edit'].get('entityDefs'))
                    if len(parent_definitions) == 1:
                        definition_source = parent_names[0]
                        definitions = parent_definitions
                native_selection = len(definitions) > 1
                _require(point['class'] == 'idTarget_Spawn', f'Invalid concrete source point: {point_name}')
                admission_parents = None
                if not definitions:
                    ai_name, admission_parents = source_definitions.select_admitted_point(point, group_names,
                        spawn_type, edit.get('aiTypeDefAssignments'), record, context_layers=row['layers'])
                else:
                    definition_row = record(definition_source)
                    ai_name = source_definitions.select(definitions, spawn_type, edit.get('aiTypeDefAssignments'), record,
                        initial_definitions=list_items(definition_row['edit'].get('initialEntityDefs')))
                parent = point['edit'].get('targetSpawnParent')
                for parent_name in (parent_names + ([parent] if parent else [])) if admission_parents is None else []:
                    parent_row = record(parent_name)
                    _require(parent_row['class'] == 'idTarget_Spawn_Parent', f'Invalid spawn parent: {parent_name}')
                    parent_definitions = list_items(parent_row['edit'].get('entityDefs'))
                    selected_parent = source_definitions.select(parent_definitions, spawn_type, edit.get('aiTypeDefAssignments'), record,
                        initial_definitions=list_items(parent_row['edit'].get('initialEntityDefs'))) if native_selection else None
                    _require(selected_parent == ai_name if native_selection else parent_definitions == [dict(name=ai_name)],
                        f'Source parent override needs review: {point_name}/{parent_name}')
                if spawn_type == 'ENCOUNTER_SPAWN_ANY':
                    related = [point, record(definition_source)]
                    _require(definitions == [dict(name=ai_name)] and all(list_items(value['edit'].get('initialEntityDefs'))
                        in ([], [dict(name=ai_name)]) for value in related), f'ANY source needs one stable definition: {point_name}')
                ai = record(ai_name)
                matched = [key for key, value in species.items() if value['status'] == 'qualified'
                    and ai['inherit'] in value['inherits']
                    and (spawn_type == 'ENCOUNTER_SPAWN_ANY' or spawn_type in value['source_spawn_types']
                        or any(source_mask & enemy_spawn_mask(mask)
                        for mask in value['source_spawn_types'] if mask != 'ENCOUNTER_SPAWN_ANY'))]
                _require(ai['class'] == 'idAI2' and len(matched) == 1, f'Unqualified actor identity: {ai_name}')
                health = ai['edit'].get('aiHealth', {}).get('components', {}).get('components[0]', {})
                if 'max' not in health or 'starting' not in health:
                    if attribute_baselines is None:
                        path = Path(root or Path(__file__).resolve().parents[2]) / 'content/catalog/enemy_attribute_baselines.json'
                        raw = path.read_bytes()
                        attribute_baselines = json.loads(raw)
                        attribute_baselines_sha256 = hashlib.sha256(raw).hexdigest()
                    health = resolve_enemy_attribute_baselines(blocks[ai_name], catalog=attribute_baselines).get('aiHealth', {}).get('components', {}).get('components[0]', {})
                    pins[ai_name]['health_source'] = 'qualified_attribute_baseline'
                _require(positive_enemy_hitpoints(health), f'Nonpositive source health: {ai_name}')
                _require(not any(re.search(r'invul|immortal|invinc|undamag', path, re.I) and value
                    for path, value in walk_values(ai['edit'])), f'Protected damage state needs review: {ai_name}')
                pins[ai_name]['starting_health'] = health['starting']
                identities.add(matched[0])
                member = dict(spawner=point_name, ai=ai_name)
                if admission_parents is not None:
                    member['definition_sources'] = admission_parents
                elif definition_source != point_name:
                    member['definition_source'] = definition_source
                members.append(member)
            event_path = ('encounterComponent', 'entityEvents', 'item[0]', 'events', f'item[{index}]', 'eventCall')
            left, right = _value_span(blocks[name], event_path)
            label = args[6 if maintenance else 3 if grouped else 2]['string']
            creations.append(dict(event_index=index, event_def=call['eventDef'], event_sha256=text_sha256(blocks[name][left:right].strip()),
                **(dict(species=next(iter(identities))) if len(identities) == 1 else dict(species_candidates=sorted(identities))),
                spawn_type=spawn_type, count=count, label=label, spawner=origin_name,
                members=members, replacement_constraints=typed_consumers(calls, spawn_type, label)))
            if maintenance:
                creations[-1].update(desired_count=args[1]['int'], count_kind='initial_maintenance_batch',
                    replacement_qualification='preserve_source_producer_until_maintenance_materialization_is_qualified')
            if spawn_type == 'ENCOUNTER_SPAWN_ANY':
                creations[-1].update(source_identity='concrete_spawner_definitions',
                    replacement_qualification='preserve_source_event_until_any_materialization_is_qualified')
        total = sum(creation['count'] for creation in creations)
        _require(total == selection['segment_N'] and total > 0, f'Reviewed segment N differs: {name}')
        if 'source_slice' not in selection:
            barrier = list_items(calls[phase_index]['args'])
            for creation in creations:
                source_mask = enemy_spawn_mask(creation['spawn_type'])
                _require(enemy_spawn_mask(barrier[0]['eEncounterSpawnType_t']) & source_mask == source_mask,
                    f'Final barrier omits source species: {name}')
                _require(barrier[2]['string'] in {'', creation['label']}, f'Final barrier omits source cohort: {name}')
        left, right = _value_span(blocks[name], ('encounterComponent', 'entityEvents', 'item[0]', 'events', f'item[{phase_index}]', 'eventCall'))
        segments.append(dict(id=selection['id'], manager=name, script_index=0, segment_N=total,
            qualification='finite_authored_opportunities_if_activated', review=selection['evidence'],
            activation=dict(kind='reviewed_single_script_initial_pass', bindings=bindings, incoming_targets=incoming, event_gates=event_gates,
                required_phase='reviewed_finite_emissions' if 'source_slice' in selection else 'selected_source_population_zero',
                phase_anchor=dict(event_index=phase_index, event_def=calls[phase_index]['eventDef'], event_sha256=text_sha256(blocks[name][left:right].strip())),
                gaps=['physical_commit_route_requires_review']), creations=creations))
        if 'source_slice' in selection:
            segments[-1]['source_slice'] = dict(scope)
        if initial_completion:
            segments[-1]['activation']['initial_maintenance_completion'] = dict(initial_completion)
        if 'logic' in selection:
            segments[-1]['logic'] = dict(selection['logic'])
        if controls:
            segments[-1]['activation']['control_events'] = controls
    selected = {segment['manager'] for segment in segments}
    catalog = dict(schema_version=1, map_key=profile['map_key'], source=profile['source'], species_registry_sha256=hashlib.sha256(registry_bytes).hexdigest(),
        logic_sources={}, counting=dict(unit='finite_authored_combat_opportunity', scope='one_initial_pass_of_each_selected_segment',
            identity_fields=['map_key','manager','script_index','event_index','ordinal'], replays_add_to_N=False, requires_forced_deaths=False, map_N=None, campaign_N=None),
        qualification=dict(source='reviewed_finite_scripts_and_concrete_source_templates', runtime=False,
            replacement_feasibility='qualified_separately_from_original_opportunity_count'), segments=segments, entities=dict(sorted(pins.items())),
        gaps=[dict(manager=name, reason='finite_initial_pass_not_reviewed', map_N=None) for name in sorted(managers-selected)])
    if source_definitions.ancestry_sha256 is not None:
        catalog['definition_ancestry_sha256'] = source_definitions.ancestry_sha256
    if attribute_baselines_sha256 is not None:
        catalog['attribute_baselines_sha256'] = attribute_baselines_sha256
    verify_catalog(catalog, source_bytes, registry_bytes, root=root)
    return catalog


def extract_snapshot_catalogs(root, payload_root, review):
    root, payload_root = Path(root), Path(payload_root)
    _require(review['schema_version'] == 1, 'Unsupported opportunity review schema')
    manifest = json.loads((payload_root/'room_payload_manifest.json').read_bytes())
    _require(manifest['schema_version'] == 2, 'Source snapshots require payload schema 2')
    registry = (root/'content/catalog/enemy_species.json').read_bytes()
    catalogs = {}
    with ZipFile(payload_root/'room_payloads.zip') as archive:
        for map_key, profile in sorted(review['maps'].items()):
            _require(map_key == profile['map_key'], 'Reviewed map key differs')
            snapshot = manifest['source_snapshots'][map_key]
            source_bytes = archive.read(snapshot['member'])
            _require(len(source_bytes) == snapshot['size'] and hashlib.sha256(source_bytes).hexdigest() == snapshot['sha256'], f'Snapshot pin differs: {map_key}')
            descriptor = json.loads((root/'content/maps'/map_key/'descriptor.json').read_bytes())
            _require(profile['source'] == dict(path=descriptor['source_owner'], sha256=descriptor['source_sha256'])
                and descriptor['source_sha256'] == snapshot['sha256'], f'Canonical source descriptor differs: {map_key}')
            catalog = extract_reviewed_catalog(source_bytes, registry, profile, root=root)
            if profile.get('source_alternatives'):
                catalog['source_alternatives'] = profile['source_alternatives']
                verify_catalog(catalog, source_bytes, registry, root=root)
            catalog['source_snapshot'] = snapshot
            catalogs[map_key] = catalog
    return catalogs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--payload-root', type=Path, required=True)
    parser.add_argument('--review', type=Path, required=True)
    parser.add_argument('--output-root', type=Path, required=True)
    args = parser.parse_args()
    review_bytes = args.review.read_bytes()
    catalogs = extract_snapshot_catalogs(args.root, args.payload_root, json.loads(review_bytes))
    args.output_root.mkdir(parents=True, exist_ok=True)
    for map_key, catalog in catalogs.items():
        catalog['review_source'] = dict(path=args.review.resolve().relative_to(args.root.resolve()).as_posix(),
            sha256=hashlib.sha256(review_bytes).hexdigest())
        path = args.output_root/f'{map_key}.json'
        path.write_text(json.dumps(catalog, indent=2) + '\n', encoding='utf-8')
    print(json.dumps([dict(map_key=key, segments=len(value['segments']), scoped_opportunities=sum(s['segment_N'] for s in value['segments']),
        entities=len(value['entities']), map_N=None, campaign_N=None) for key,value in catalogs.items()]))


if __name__ == '__main__':
    main()
