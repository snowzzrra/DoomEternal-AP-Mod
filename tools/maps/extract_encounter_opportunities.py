"""Extract explicitly reviewed finite source segments from pinned map snapshots."""

import argparse
import hashlib
import json
from pathlib import Path
import re
from zipfile import ZipFile

from .ap_map_generator import find_entity_block_bounds
from .encounter_catalog import ENTITY_HEADER, entity_records, list_items, walk_values
from .encounter_materializer import _value_span, text_sha256
from .encounter_source_definitions import SourceDefinitions
from .verify_encounter_opportunities import verify_catalog, typed_consumers


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
        _require(calls and all(call['eventDef'] in {'spawnSingleAI', 'spawnAI', 'wait', 'waitAIRemaining', 'activateTarget'} for call in calls),
            f'Unsupported cohort or control operation: {name}')
        controls = []
        for index, call in enumerate(calls):
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
        population_indices = [index for index, call in enumerate(calls) if call['eventDef'] != 'activateTarget']
        _require(population_indices, f'No final population barrier: {name}')
        phase_index = population_indices[-1]
        _require(calls[phase_index]['eventDef'] == 'waitAIRemaining' and list_items(calls[phase_index]['args'])[1]['int'] == 0,
            f'No final population barrier: {name}')
        bindings = {}
        for field, class_name in [('commitTriggers', 'idEncounterTrigger_Commit'), ('exitTriggers', 'idEncounterTrigger_Exit'),
                ('userFlagTriggers', 'idEncounterTrigger_RaiseUserFlag')]:
            bindings[field] = []
            for target in list_items(edit.get(field)):
                trigger = record(target)
                _require(trigger['class'] == class_name, f'Invalid trigger class: {target}')
                binding = dict(entity=target)
                if field == 'exitTriggers':
                    binding['force_ai_to_flee'] = trigger['edit'].get('forceAIToFlee', False)
                if field == 'userFlagTriggers':
                    binding['label'] = trigger['edit']['userFlag']
                bindings[field].append(binding)
        _require(bindings['commitTriggers'], f'No reviewed Commit binding: {name}')
        creations = []
        used_points = set()
        for index, call in enumerate(calls):
            args = list_items(call['args'])
            if call['eventDef'] == 'activateTarget':
                continue
            if call['eventDef'] == 'wait':
                _require(type(args[0]['float']) in {int, float} and args[0]['float'] >= 0, f'Invalid source delay: {name}/{index}')
                continue
            if call['eventDef'] == 'waitAIRemaining':
                _require(args[1]['int'] == 0, f'Positive population gate needs review: {name}/{index}')
                if args[0]['eEncounterSpawnType_t'] == 'ENCOUNTER_SPAWN_ANY' and args[2]['string'] == '':
                    used_points.clear()
                continue
            grouped = call['eventDef'] == 'spawnAI'
            count = args[1]['int'] if grouped else 1
            spawn_type = args[0]['eEncounterSpawnType_t']
            origin_name = args[2 if grouped else 1]['entity']
            origin = record(origin_name)
            _require(type(count) is int and count > 0 and len(spawn_type.split()) == 1,
                f'Unqualified finite request: {name}/{index}')
            parent_names = []
            if grouped:
                _require(origin['class'] == 'idTargetSpawnGroup', f'Unsupported group: {origin_name}')
                point_names = [point for point in list_items(origin['edit']['spawners']) if record(point)['class'] != 'idTarget_Spawn_Parent']
                parent = origin['edit'].get('targetSpawnParent')
                if parent:
                    _require(record(parent)['class'] == 'idTarget_Spawn_Parent', f'Invalid spawn parent: {parent}')
                    parent_names.append(parent)
                _require(count <= len(point_names), f'Request exceeds distinct source points: {name}/{index}')
            else:
                point_names = [origin_name]
            _require(len(point_names) == len(set(point_names)) and not used_points.intersection(point_names),
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
                ai = record(ai_name)
                matched = [key for key, value in species.items() if value['status'] == 'qualified'
                    and spawn_type in value['source_spawn_types'] and ai['inherit'] in value['inherits']]
                _require(ai['class'] == 'idAI2' and len(matched) == 1, f'Unqualified actor identity: {ai_name}')
                health = ai['edit'].get('aiHealth', {}).get('components', {}).get('components[0]', {})
                _require(health.get('max', 0) > 0 and health.get('starting', 0) > 0, f'Nonpositive source health: {ai_name}')
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
            _require(len(identities) == 1, f'Mixed source group needs review: {origin_name}')
            event_path = ('encounterComponent', 'entityEvents', 'item[0]', 'events', f'item[{index}]', 'eventCall')
            left, right = _value_span(blocks[name], event_path)
            label = args[-1]['string']
            creations.append(dict(event_index=index, event_def=call['eventDef'], event_sha256=text_sha256(blocks[name][left:right].strip()),
                species=identities.pop(), spawn_type=spawn_type, count=count, label=label, spawner=origin_name,
                members=members, replacement_constraints=typed_consumers(calls, spawn_type, label)))
        total = sum(creation['count'] for creation in creations)
        _require(total == selection['segment_N'] and total > 0, f'Reviewed segment N differs: {name}')
        barrier = list_items(calls[phase_index]['args'])
        for creation in creations:
            _require(barrier[0]['eEncounterSpawnType_t'] == 'ENCOUNTER_SPAWN_ANY'
                or creation['spawn_type'] in barrier[0]['eEncounterSpawnType_t'].split(), f'Final barrier omits source species: {name}')
            _require(barrier[2]['string'] in {'', creation['label']}, f'Final barrier omits source cohort: {name}')
        left, right = _value_span(blocks[name], ('encounterComponent', 'entityEvents', 'item[0]', 'events', f'item[{phase_index}]', 'eventCall'))
        segments.append(dict(id=selection['id'], manager=name, script_index=0, segment_N=total,
            qualification='finite_authored_opportunities_if_activated', review=selection['evidence'],
            activation=dict(kind='reviewed_single_script_initial_pass', bindings=bindings, incoming_targets=[], event_gates=[],
                required_phase='selected_source_population_zero',
                phase_anchor=dict(event_index=phase_index, event_def='waitAIRemaining', event_sha256=text_sha256(blocks[name][left:right].strip())),
                gaps=['physical_commit_route_and_kill_attribution_are_not_established_by_source_population_zero']), creations=creations))
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
