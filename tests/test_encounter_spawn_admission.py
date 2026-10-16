import copy
import json
from pathlib import Path

import pytest

from tools.maps.encounter_source_definitions import SourceDefinitions


ROOT = Path(__file__).resolve().parents[1]
MASK = 'ENCOUNTER_SPAWN_IMP'
POPULATION = 'actorpopulation/default/campaign'


def items(values):
    return {'num': len(values), **{f'item[{index}]': value for index, value in enumerate(values)}}


@pytest.fixture
def records():
    def entity(name, kind, inherit, edit, layers=None):
        return {'name': name, 'class': kind, 'inherit': inherit, 'edit': edit, 'layers': layers or []}

    return {row['name']: row for row in [
        entity('point', 'idTarget_Spawn', 'target/spawn', {'spawnPosition': {'x': 1}}),
        entity('group', 'idTargetSpawnGroup', 'encounter/spawn_group/zone',
            {'targetSpawnParent': 'parent', 'spawners': items(['point'])}),
        entity('parent', 'idTarget_Spawn_Parent', 'encounter/spawn_group/parent',
            {'entityDefs': items([{'name': 'imp'}, {'name': 'soldier'}])}),
        entity('imp', 'idAI2', 'ai/fodder/imp', {}),
        entity('soldier', 'idAI2', 'ai/fodder/soldier_blaster', {}),
    ]}


def resolve(records, *, layers=(), seen=None):
    registry = json.loads((ROOT / 'content/catalog/enemy_species.json').read_bytes())
    def record(name):
        if seen is not None:
            seen.add(name)
        return records[name]
    return SourceDefinitions(registry, ROOT).select_admitted_point(records['point'],
        [name for name, row in records.items() if row['class'] == 'idTargetSpawnGroup'],
        MASK, POPULATION, record, context_layers=list(layers))


@pytest.mark.parametrize('membership', ['explicit', 'saved', 'cylinder'])
def test_empty_point_uses_complete_native_membership_and_pins_discarded_actor(records, membership):
    edit = records['group']['edit']
    if membership != 'explicit':
        edit['spawners'] = items([])
        if membership == 'saved':
            edit['spawnersTouchingPrimitives'] = items(['point'])
        else:
            records['group']['inherit'] = 'encounter/spawn_group/cylinder'
    seen = set()
    assert resolve(records, seen=seen) == ('imp', ['parent'])
    assert {'group', 'parent', 'imp', 'soldier'} <= seen


@pytest.mark.parametrize(('axis', 'value', 'admitted'), [
    ('x', 1, True), ('x', 1 + 2 ** -25, True), ('x', 1 + 2 ** -22, False),
    ('z', 1, True), ('z', 1 + 2 ** -22, False), ('z', -1, True),
])
def test_cylinder_rounds_each_coordinate_to_float32_with_inclusive_edges(records, axis, value, admitted):
    records['point']['edit']['spawnPosition'] = {axis: value}
    records['group']['inherit'] = 'encounter/spawn_group/cylinder'
    records['group']['edit'].update(spawners=items([]), radius=1, floorHeight=-1, ceilingHeight=1)
    if admitted:
        assert resolve(records)[0] == 'imp'
    else:
        with pytest.raises(ValueError, match='needs one definition'):
            resolve(records)


def test_same_actor_from_multiple_groups_is_one_concrete_selection(records):
    records['other_group'] = {**copy.deepcopy(records['group']), 'name': 'other_group'}
    assert resolve(records) == ('imp', ['parent'])


def test_unrelated_missing_member_does_not_replace_the_concrete_point(records):
    records['group']['edit']['spawners'] = items(['point', 'absent_other_point'])
    assert resolve(records) == ('imp', ['parent'])


@pytest.mark.parametrize('missing_index', [0, 1])
def test_omitted_fresh_managed_member_does_not_hide_the_concrete_point(records, missing_index):
    records['group']['edit']['spawners'] = {'num': 2, f'item[{1 - missing_index}]': 'point'}
    assert resolve(records) == ('imp', ['parent'])


def test_omitted_fresh_managed_member_cannot_supply_a_point(records):
    records['group']['edit']['spawners'] = {'num': 1}
    with pytest.raises(ValueError, match='needs one definition'):
        resolve(records)


@pytest.mark.parametrize('members', [
    {'num': 1, 'item[2]': 'point'}, {'num': 1, 'other': 'point'}, {'num': 1, 'item[0]': None},
])
def test_native_null_admission_keeps_rejecting_unqualified_authored_members(records, members):
    records['group']['edit']['spawners'] = members
    with pytest.raises(ValueError):
        resolve(records)


def test_other_layer_remains_in_upper_bound_and_cannot_supply_only_active_witness(records):
    records['group']['layers'] = ['master']
    with pytest.raises(ValueError, match='no guaranteed active group'):
        resolve(records, layers=['main'])
    records['active_group'] = {**copy.deepcopy(records['group']), 'name': 'active_group', 'layers': ['main']}
    assert resolve(records, layers=['main'])[0] == 'imp'
    records['other_imp'] = {**copy.deepcopy(records['imp']), 'name': 'other_imp'}
    records['other_parent'] = {**copy.deepcopy(records['parent']), 'name': 'other_parent',
        'edit': {'entityDefs': items([{'name': 'other_imp'}])}}
    records['group']['edit']['targetSpawnParent'] = 'other_parent'
    with pytest.raises(ValueError, match='no unique native selection'):
        resolve(records, layers=['main'])


@pytest.mark.parametrize('fault', ['touch', 'reset', 'bind', 'exclusive', 'subgroup', 'sparse', 'negative_count', 'inherit', 'parent', 'fields'])
def test_unqualified_admission_is_rejected(records, fault):
    if fault == 'touch':
        records['point']['edit']['flags'] = {'touchesTriggers': True}
    elif fault == 'reset':
        records['point']['edit']['initialEntityDefs'] = items([{'name': 'imp'}])
    elif fault == 'bind':
        records['point']['edit']['bindInfo'] = {'bindParent': 'moving_platform'}
    elif fault == 'exclusive':
        records['group']['edit']['exclusive'] = True
    elif fault == 'subgroup':
        records['group']['edit']['targetSpawnSubGroups'] = items(['other'])
    elif fault == 'sparse':
        records['group']['edit']['spawnersTouchingPrimitives'] = {'num': 2, 'item[0]': 'point'}
    elif fault == 'negative_count':
        records['group']['edit']['spawnersTouchingPrimitives'] = {'num': -1}
    elif fault == 'inherit':
        records['group']['inherit'] = 'encounter/spawn_group/unknown'
    elif fault == 'parent':
        records['parent']['class'] = 'idLogicRelay'
    else:
        records['parent']['edit']['entityDefs']['item[0]']['weight'] = 2
    with pytest.raises(ValueError):
        resolve(records)
