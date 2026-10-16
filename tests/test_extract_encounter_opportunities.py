import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from tools.maps.extract_encounter_opportunities import extract_reviewed_catalog, extract_snapshot_catalogs
from tools.maps.verify_encounter_opportunities import verify_catalog

from tools.maps.encounter_catalog import entity_records


def _items(values):
    return dict(num=len(values), **{f'item[{i}]': value for i, value in enumerate(values)})


def _call(name, args):
    return dict(eventCall=dict(eventDef=name, args=_items(args)))


def _fixture(*, count=2, barrier='ENCOUNTER_SPAWN_ANY', duplicate=False, operation='spawnAI', definitions=None, health=100, parent_definition=None, group_parent=False, controls=False):
    def properties(values):
        return '\n'.join(f'{key} = {{ {properties(value)} }}' if isinstance(value, dict)
            else f'{key} = {json.dumps(value)};' for key, value in values.items())

    def entity(name, kind, inherit, edit):
        return f'entity {{\nentityDef {name} {{\ninherit = "{inherit}";\nclass = "{kind}";\nedit = {{ {properties(edit)} }}\n}}\n}}\n'

    spawn = _call(operation, [dict(eEncounterSpawnType_t='ENCOUNTER_SPAWN_IMP'),dict(int=count),dict(entity='group'),dict(string='cohort')])
    calls = [spawn] * (2 if duplicate else 1) + [_call('waitAIRemaining', [dict(eEncounterSpawnType_t=barrier),dict(int=0),dict(string=''),dict(bool=False)])]
    if controls:
        calls.insert(0, _call('activateTarget', [dict(entity='relay'),dict(string='blocker on')]))
        calls.append(_call('activateTarget', [dict(entity='relay'),dict(string='blocker off')]))
    text = entity('arena','idEncounterManager','encounter/manager',dict(commitTriggers=_items(['commit']),
        encounterComponent=dict(entityEvents=_items([dict(entity='arena',events=_items(calls))]))))
    text += entity('commit','idEncounterTrigger_Commit','encounter/trigger/commit',{})
    group = dict(spawners=_items((['parent'] if group_parent else [])+['point_a','point_b']))
    if group_parent:
        group['targetSpawnParent'] = 'parent'
    text += entity('group','idTargetSpawnGroup','encounter/spawn_group',group)
    for name in ('point_a','point_b'):
        edit = dict(entityDefs=_items([dict(name=value) for value in (definitions if definitions is not None else ['actor'])]),
            spawnPosition=dict(x=0,y=0,z=0),spawnEditable=dict(aiStateOverride='AIOVERRIDE_TELEPORT'))
        if parent_definition is not None and not group_parent:
            edit['targetSpawnParent'] = 'parent'
        text += entity(name,'idTarget_Spawn','target/spawn',edit)
    text += entity('actor','idAI2','ai/fodder/imp',dict(aiHealth=dict(components={'components[0]':dict(max=health,starting=health)})))
    if parent_definition is not None:
        parent_definitions = parent_definition if isinstance(parent_definition,list) else [parent_definition]
        text += entity('parent','idTarget_Spawn_Parent','encounter/spawn_group/parent',dict(entityDefs=_items([dict(name=value) for value in parent_definitions])))
    if controls:
        text += entity('relay','idTarget_Count','target/relay',dict(count=1,repeat=True,targets=_items(['wall'])))
        text += entity('wall','idDynamicEntity','func/dynamic',dict(flags=dict(hidden=True)))
    data = text.encode()
    registry = json.dumps(dict(species=dict(imp=dict(status='qualified',inherits=['ai/fodder/imp'],source_spawn_types=['ENCOUNTER_SPAWN_IMP'])))).encode()
    manager = next(entity_records(text,include_names={'arena'}))
    profile = dict(map_key='test',source=dict(path='vanillamaps/test.map',sha256=hashlib.sha256(data).hexdigest()),segments=[
        dict(id='initial',manager='arena',manager_sha256=manager['sha256'],layers=[],segment_N=count*(2 if duplicate else 1),evidence='isolated finite initial pass')])
    if controls:
        profile['segments'][0]['control_events'] = [dict(event_index=index,event_def='activateTarget',target='relay') for index in (0,len(calls)-1)]
        profile['segments'][0]['control_entities'] = {row['name']:row['sha256'] for row in entity_records(text,include_names={'relay','wall'})}
    return data, registry, profile


def test_reviewed_group_retains_instances_and_keeps_map_totals_unqualified():
    data, registry, profile = _fixture()
    catalog = extract_reviewed_catalog(data,registry,profile)
    segment = catalog['segments'][0]
    assert segment['segment_N'] == 2
    assert segment['creations'][0]['members'] == [dict(spawner='point_a',ai='actor'),dict(spawner='point_b',ai='actor')]
    assert segment['activation']['phase_anchor']['event_index'] == 1
    assert catalog['counting']['map_N'] is None and catalog['counting']['campaign_N'] is None


def test_reviewed_cleanup_keeps_population_barrier_and_control_graph_separate():
    data, registry, profile = _fixture(controls=True)
    catalog = extract_reviewed_catalog(data,registry,profile)
    segment, = catalog['segments']
    assert segment['segment_N'] == 2 and segment['creations'][0]['event_index'] == 1
    assert segment['activation']['phase_anchor']['event_index'] == 2
    assert segment['activation']['control_events'] == profile['segments'][0]['control_events']
    assert {'relay','wall'} <= catalog['entities'].keys()
    assert catalog['counting']['map_N'] is None and catalog['counting']['campaign_N'] is None
    verify_catalog(catalog,data,registry)


@pytest.mark.parametrize('defect', ['missing_review', 'wrong_event', 'missing_child', 'changed_entity'])
def test_unreviewed_cleanup_graph_does_not_become_finite_opportunities(defect):
    data, registry, profile = _fixture(controls=True)
    selection = profile['segments'][0]
    if defect == 'missing_review':
        selection.pop('control_events')
    elif defect == 'wrong_event':
        selection['control_events'][0]['event_index'] = 1
    elif defect == 'missing_child':
        selection['control_entities'].pop('wall')
    else:
        selection['control_entities']['wall'] = '0'*64
    with pytest.raises(ValueError,match='[Cc]ontrol'):
        extract_reviewed_catalog(data,registry,profile)


@pytest.mark.parametrize('options,message', [
    (dict(count=3),'exceeds distinct source points'),
    (dict(duplicate=True),'reuse without an empty-population barrier'),
    (dict(operation='maintainAICount'),'Unsupported cohort'),
    (dict(barrier='ENCOUNTER_SPAWN_ARACHNOTRON'),'barrier omits source species'),
    (dict(definitions=[]),'needs one definition|Unqualified source spawn group defaults'),
    (dict(definitions=['actor','other']),'Unresolved source entity'),
    (dict(health=0),'Nonpositive source health'),
    (dict(parent_definition='other'),'parent override needs review'),
])
def test_unsupported_source_does_not_become_finite_opportunities(options,message):
    data, registry, profile = _fixture(**options)
    with pytest.raises(ValueError,match=message):
        extract_reviewed_catalog(data,registry,profile)


def test_review_pins_reject_changed_source_before_counting():
    data, registry, profile = _fixture()
    with pytest.raises(ValueError,match='Reviewed source hash differs'):
        extract_reviewed_catalog(data+b'\n',registry,profile)


def test_matching_parent_is_included_in_source_pins():
    data, registry, profile = _fixture(parent_definition='actor')
    assert extract_reviewed_catalog(data,registry,profile)['entities']['parent']['class_name'] == 'idTarget_Spawn_Parent'


def test_group_parent_definitions_preserve_count_from_event_not_positions():
    data, registry, profile = _fixture(count=1,definitions=[],parent_definition='actor',group_parent=True)
    catalog = extract_reviewed_catalog(data,registry,profile)
    segment = catalog['segments'][0]
    assert segment['segment_N'] == segment['creations'][0]['count'] == 1
    assert segment['creations'][0]['members'] == [dict(spawner=point,ai='actor',definition_source='parent') for point in ('point_a','point_b')]
    assert catalog['entities']['parent']['class_name'] == 'idTarget_Spawn_Parent'
    assert catalog['counting']['map_N'] is None and catalog['counting']['campaign_N'] is None


def test_parent_control_cannot_supply_an_extra_physical_position():
    data, registry, profile = _fixture(count=3,definitions=[],parent_definition='actor',group_parent=True)
    with pytest.raises(ValueError,match='exceeds distinct source points'):
        extract_reviewed_catalog(data,registry,profile)


@pytest.mark.parametrize('definition_source',[None,'point_a'])
def test_inherited_definition_source_cannot_be_rebound(definition_source):
    data, registry, profile = _fixture(definitions=[],parent_definition='actor',group_parent=True)
    catalog = extract_reviewed_catalog(data,registry,profile)
    member = catalog['segments'][0]['creations'][0]['members'][0]
    if definition_source is None:
        member.pop('definition_source')
    else:
        member['definition_source'] = definition_source
    with pytest.raises(ValueError,match='Definition source differs'):
        verify_catalog(catalog,data,registry)


def test_multiple_parent_definitions_need_separate_identity_qualification():
    data, registry, profile = _fixture(definitions=[],parent_definition=['actor','other'],group_parent=True)
    with pytest.raises(ValueError,match='Empty source point parent selection needs review|Unqualified source spawn group defaults'):
        extract_reviewed_catalog(data,registry,profile)


def test_empty_points_use_native_admission_and_verify_every_map_group_pin():
    data, _, profile = _fixture(count=1, definitions=[], parent_definition=['actor', 'soldier'], group_parent=True)
    text = data.decode().replace('inherit = "encounter/spawn_group";', 'inherit = "encounter/spawn_group/zone";')
    text = text.replace('edit = { commitTriggers', 'edit = { aiTypeDefAssignments = "actorpopulation/default/campaign"; commitTriggers', 1)
    text += ('entity {\nentityDef soldier {\ninherit = "ai/fodder/soldier_blaster";\nclass = "idAI2";\nedit = {}\n}\n}\n'
        'entity {\nentityDef outside {\ninherit = "encounter/spawn_group/zone";\nclass = "idTargetSpawnGroup";\nedit = {}\n}\n}\n')
    data = text.encode()
    profile['source']['sha256'] = hashlib.sha256(data).hexdigest()
    profile['segments'][0]['manager_sha256'] = next(entity_records(text, include_names={'arena'}))['sha256']
    root = Path(__file__).resolve().parents[1]
    registry = (root / 'content/catalog/enemy_species.json').read_bytes()
    catalog = extract_reviewed_catalog(data, registry, profile, root=root)
    creation, = catalog['segments'][0]['creations']
    assert creation['count'] == 1
    assert creation['members'] == [dict(spawner=point, ai='actor', definition_sources=['parent']) for point in ('point_a', 'point_b')]
    assert {'parent', 'soldier', 'outside'} <= catalog['entities'].keys()
    del catalog['entities']['outside']
    with pytest.raises(ValueError, match='not pinned: outside'):
        verify_catalog(catalog, data, registry, root=root)


def _native_fixture():
    data, _, profile = _fixture(definitions=['actor', 'soldier'], parent_definition=['actor', 'soldier'], group_parent=True)
    text = data.decode().replace('ai/fodder/imp', 'ai/fodder/zombie_t1_hell').replace(
        'ENCOUNTER_SPAWN_IMP', 'ENCOUNTER_SPAWN_ZOMBIE_TIER_1')
    text = text.replace('edit = { commitTriggers',
        'edit = { aiTypeDefAssignments = "actorpopulation/default/dlc1"; commitTriggers')
    text += ('entity {\nentityDef soldier {\ninherit = "ai/fodder/soldier_blaster";\n'
        'class = "idAI2";\nedit = {}\n}\n}\n')
    data = text.encode()
    profile['source']['sha256'] = hashlib.sha256(data).hexdigest()
    profile['segments'][0]['manager_sha256'] = next(entity_records(text, include_names={'arena'}))['sha256']
    root = Path(__file__).resolve().parents[1]
    return data, (root/'content/catalog/enemy_species.json').read_bytes(), profile


def test_native_selection_examines_descendants_and_all_population_variants():
    data, registry, profile = _native_fixture()
    catalog = extract_reviewed_catalog(data, registry, profile)
    assert catalog['segments'][0]['segment_N'] == 2
    assert {member['ai'] for member in catalog['segments'][0]['creations'][0]['members']} == {'actor'}
    assert 'soldier' in catalog['entities']
    assert catalog['definition_ancestry_sha256']
    verify_catalog(catalog, data, registry)


@pytest.mark.parametrize('defect', ['discarded_pin', 'ancestry_pin', 'rebind'])
def test_multi_definition_verifier_keeps_discarded_candidates_pinned(defect):
    data, registry, profile = _native_fixture()
    catalog = extract_reviewed_catalog(data, registry, profile)
    if defect == 'discarded_pin':
        catalog['entities'].pop('soldier')
    elif defect == 'ancestry_pin':
        catalog['definition_ancestry_sha256'] = '0'*64
    else:
        catalog['segments'][0]['creations'][0]['members'][0]['ai'] = 'soldier'
    with pytest.raises(ValueError):
        verify_catalog(catalog, data, registry)


def test_group_definition_source_requires_parent_class():
    data, registry, profile = _fixture(definitions=[],parent_definition='actor',group_parent=True)
    data = data.replace(b'"idTarget_Spawn_Parent"',b'"idLogicRelay"')
    profile['source']['sha256'] = hashlib.sha256(data).hexdigest()
    with pytest.raises(ValueError,match='Invalid spawn parent'):
        extract_reviewed_catalog(data,registry,profile)


def test_snapshot_source_must_match_the_canonical_descriptor(tmp_path):
    data, registry, profile = _fixture()
    root, payload = tmp_path/'root',tmp_path/'payload'
    (root/'content/catalog').mkdir(parents=True)
    (root/'content/maps/test').mkdir(parents=True)
    payload.mkdir()
    (root/'content/catalog/enemy_species.json').write_bytes(registry)
    descriptor = dict(source_owner=profile['source']['path'],source_sha256=profile['source']['sha256'])
    descriptor_path = root/'content/maps/test/descriptor.json'
    descriptor_path.write_text(json.dumps(descriptor))
    snapshot = dict(member='sources/test.entities',size=len(data),sha256=hashlib.sha256(data).hexdigest())
    (payload/'room_payload_manifest.json').write_text(json.dumps(dict(schema_version=2,source_snapshots=dict(test=snapshot))))
    with ZipFile(payload/'room_payloads.zip','w') as archive:
        archive.writestr(snapshot['member'],data)
    review = dict(schema_version=1,maps=dict(test=profile))
    assert extract_snapshot_catalogs(root,payload,review)['test']['source_snapshot'] == snapshot
    descriptor['source_sha256'] = '0'*64
    descriptor_path.write_text(json.dumps(descriptor))
    with pytest.raises(ValueError,match='Canonical source descriptor differs'):
        extract_snapshot_catalogs(root,payload,review)
