"""Native initial spawn-group admission for empty concrete source points."""

import math
import re
import struct

from tools.maps.encounter_catalog import list_items, walk_values


# the native constructor uses this float32 value for radius and signed heights.
RADIUS = struct.unpack('<f', bytes.fromhex('bf0e1c40'))[0]


def _f32(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('Unqualified spawn admission coordinate')
    try:
        result = struct.unpack('<f', struct.pack('<f', value))[0]
    except OverflowError as error:
        raise ValueError('Unqualified spawn admission coordinate') from error
    if not math.isfinite(result):
        raise ValueError('Unqualified spawn admission coordinate')
    return result


def _items(edit, field, *, native_null_members=False):
    value = edit.get(field)
    if value is not None and (not isinstance(value, dict) or type(value.get('num')) is not int or value['num'] < 0):
        raise ValueError(f'Unqualified spawn admission list: {field}')
    if value is not None and native_null_members:
        if set(value) - {'num', *(f'item[{index}]' for index in range(value['num']))}:
            raise ValueError(f'Unqualified spawn admission list: {field}')
        # fresh omitted managed references have invalid handles and add no concrete member.
        return [value[f'item[{index}]'] for index in range(value['num']) if f'item[{index}]' in value]
    try:
        return list_items(value)
    except (KeyError, TypeError) as error:
        raise ValueError(f'Unqualified spawn admission list: {field}') from error


def spawn_position(edit):
    if any(re.search(r'bind|transform', path, re.I) and value for path, value in walk_values(edit)):
        raise ValueError('Spawn admission with bound transforms needs review')
    position = edit.get('spawnPosition', {})
    if not isinstance(position,dict) or set(position)-set('xyz'):
        raise ValueError('Unqualified spawn admission coordinate')
    return [_f32(position.get(axis, 0)) for axis in 'xyz']


def _cylinder_contains(position, edit):
    origin = spawn_position(edit)
    floor = _f32(edit.get('floorHeight', -RADIUS))
    ceiling = _f32(edit.get('ceilingHeight', RADIUS))
    radius = _f32(edit.get('radius', RADIUS))
    center_z = _f32(origin[2] + _f32(_f32(floor + ceiling) * .5))
    half_height = _f32(_f32(ceiling - floor) * .5)
    dx, dy = (_f32(position[index] - origin[index]) for index in (0, 1))
    horizontal = _f32(_f32(dx * dx) + _f32(dy * dy))
    vertical = abs(_f32(position[2] - center_z))
    return horizontal <= _f32(radius * radius) and vertical <= half_height


def source_group_points(name, point_names, group_names, record, context_layers):
    group = record(name)
    if group['class'] != 'idTargetSpawnGroup' or group['inherit'] not in (
            'encounter/spawn_group/zone', 'encounter/spawn_group/cylinder'):
        raise ValueError('Unqualified source spawn group defaults')
    if group['layers'] and group['layers'] != context_layers:
        raise ValueError('Source group layer activation needs review')
    edit = group['edit']
    subgroups = _items(edit, 'targetSpawnSubGroups', native_null_members=True)
    if any(not isinstance(child, str) or child == name
           or _items(record(child)['edit'], 'targetSpawnSubGroups', native_null_members=True) for child in subgroups):
        raise ValueError('Nested source subgroup expansion needs activation-order review')
    members = _items(edit, 'spawners', native_null_members=True)
    touching = edit.get('includeTouchingSpawners', True)
    cylinder = edit.get('usesCylinderForBounds', group['inherit'] == 'encounter/spawn_group/cylinder')
    if touching:
        if cylinder:
            members += [point for point in point_names if _cylinder_contains(spawn_position(record(point)['edit']), edit)]
        else:
            members += _items(edit, 'spawnersTouchingPrimitives', native_null_members=True)
    if any(not isinstance(member, str) for member in members):
        raise ValueError('Unqualified spawn-group member reference')
    members = list(dict.fromkeys(members))
    for point in members:
        row = record(point)
        if row['class'] != 'idTarget_Spawn' or row['inherit'] != 'target/spawn':
            raise ValueError('Source group member is not a qualified spawn point')
        if row['layers'] and row['layers'] != context_layers:
            raise ValueError('Source point layer activation needs review')
        if row['edit'].get('flags', {}).get('touchesTriggers', False):
            raise ValueError('Physical spawn-group touches need review')
    if not edit.get('exclusive', False):
        for other in group_names:
            candidate = record(other)
            if other == name or not candidate['edit'].get('exclusive', False):
                continue
            if candidate['edit'].get('includeTouchingSpawners', True):
                raise ValueError('Exclusive spawn groups need admission review')
            claimed = _items(candidate['edit'], 'spawners', native_null_members=True)
            if set(members) & set(claimed):
                if candidate['layers'] and candidate['layers'] != context_layers:
                    raise ValueError('Exclusive spawn group layer activation needs review')
                members = [point for point in members if point not in claimed]
    for child in subgroups:
        members += source_group_points(child, point_names, group_names, record, context_layers)
    return list(dict.fromkeys(members))


def admitted_parent_definitions(point, group_names, record, context_layers):
    edit = point['edit']
    if point['class'] != 'idTarget_Spawn' or point['inherit'] != 'target/spawn':
        raise ValueError('Unqualified empty source point defaults')
    if edit.get('targetSpawnParent') or _items(edit, 'initialEntityDefs'):
        raise ValueError('Empty source point parent or reset needs review')
    if edit.get('flags', {}).get('touchesTriggers', False):
        raise ValueError('Physical spawn-group touches need review')
    if point['layers'] and point['layers'] != context_layers:
        raise ValueError('Source point layer activation needs review')
    position = spawn_position(edit)
    parents = set()
    guaranteed = set()
    admitted = []
    # all map groups apply parents to their own members before subgroup merging.
    for name in sorted(group_names):
        group = record(name)
        if group['class'] != 'idTargetSpawnGroup' or group['inherit'] not in (
                'encounter/spawn_group/zone', 'encounter/spawn_group/cylinder'):
            raise ValueError('Unqualified source spawn group defaults')
        properties = group['edit']
        members = _items(properties, 'spawners', native_null_members=True)
        primitives = _items(properties, 'spawnersTouchingPrimitives')
        if any(not isinstance(member, str) for member in members + primitives):
            raise ValueError('Unqualified spawn-group member reference')
        explicit = point['name'] in members
        saved = point['name'] in primitives
        spawn_position(properties)
        touching = properties.get('includeTouchingSpawners', True)
        cylinder = properties.get('usesCylinderForBounds', group['inherit'] == 'encounter/spawn_group/cylinder')
        if explicit or (touching and (_cylinder_contains(position, properties) if cylinder else saved)):
            exclusive=properties.get('exclusive',False)
            if exclusive and touching:
                raise ValueError('Exclusive spawn groups need admission review')
            admitted.append((exclusive,properties.get('targetSpawnParent'),group['layers'] in (point['layers'],context_layers)))
    exclusive=[entry for entry in admitted if entry[0]]
    if exclusive:
        # native exclusive groups remove their points from nonexclusive groups before applying parents.
        if any(not active for _,_,active in exclusive) or len({parent for _,parent,_ in exclusive if parent})>1:
            raise ValueError('Exclusive spawn groups have uncertain parent selection')
        admitted=exclusive
    for _,parent,active in admitted:
        if parent:
            parents.add(parent)
            if active:
                guaranteed.add(parent)
    definitions = {}
    guaranteed_definitions = set()
    for name in sorted(parents):
        parent = record(name)
        if parent['class'] != 'idTarget_Spawn_Parent':
            raise ValueError('Admitted group parent is not idTarget_Spawn_Parent')
        for definition in _items(parent['edit'], 'entityDefs'):
            if not isinstance(definition, dict) or set(definition) != {'name'}:
                raise ValueError('Unqualified source definition fields')
            definitions[definition['name']] = definition
            if name in guaranteed:
                guaranteed_definitions.add(definition['name'])
    return list(definitions.values()), sorted(parents), guaranteed_definitions
