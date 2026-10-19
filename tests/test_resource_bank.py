import struct

from doom_eap.launcher.resource_bank import ResourceIndex, consolidate_bank, _u64


def write_bank(path, entries):
    names = list(dict.fromkeys(value for kind, name, payload, types in entries for value in (kind, name)))
    meta_values = list(dict.fromkeys(value for kind, name, payload, types in entries for value in types))
    if meta_values:
        names.extend(['dependency_kind', *(f'value/{value}' for value in meta_values)])
    strings, offsets = bytearray(), []
    for name in names:
        offsets.append(len(strings))
        strings.extend(name.encode() + b'\0')
    strings = struct.pack('<Q', len(names)) + struct.pack('<' + 'Q' * len(names), *offsets) + strings
    rows, types, ids, data = bytearray(), bytearray(), bytearray(), bytearray()
    metadata = b''.join(struct.pack('<QQIIQ', names.index('dependency_kind'), names.index(f'value/{value}'),
                                    5, 1, 1234) for value in meta_values)
    for kind, name, payload, values in entries:
        row = bytearray(144)
        struct.pack_into('<QQQQQ', row, 0, 0, 1, 0xffffffffffffffff, len(types) // 4, len(ids) // 8)
        struct.pack_into('<QQQ', row, 56, len(data), len(payload), len(payload))
        struct.pack_into('<I', row, 132, len(values))
        types.extend(struct.pack('<' + 'I' * len(values), *(meta_values.index(v) for v in values)))
        ids.extend(struct.pack('<QQ', names.index(kind), names.index(name)))
        rows.extend(row)
        data.extend(payload)
    header = bytearray(124)
    header[:8] = b'IDCL\x0c\0\0\0'
    names_at = len(header) + len(rows)
    names_end = names_at + len(strings)
    lookup_at = names_end + len(metadata)
    ids_at = lookup_at + len(types)
    data_at = ids_at + len(ids)
    struct.pack_into('<I', header, 32, len(entries))
    struct.pack_into('<I', header, 36, len(meta_values))
    struct.pack_into('<I', header, 40, len(types) // 4)
    struct.pack_into('<I', header, 44, len(entries) * 2)
    struct.pack_into('<I', header, 56, len(strings))
    for at, value in ((64, names_at), (72, names_end), (80, 124), (88, names_end),
                      (96, lookup_at), (104, data_at), (116, data_at)):
        struct.pack_into('<Q', header, at, value)
    for number in range(len(entries)):
        struct.pack_into('<Q', rows, number * 144 + 56, _u64(rows, number * 144 + 56) + data_at)
    path.write_bytes(header + rows + strings + metadata + types + ids + data)


def payloads(path):
    result = []
    with path.open('rb') as source:
        for key, (_, row, number) in ResourceIndex(path).records:
            source.seek(_u64(row, 56))
            result.append((key, source.read(_u64(row, 64))))
    return result


def test_consolidation_preserves_payloads_types_priority_and_disabled_records(tmp_path):
    base, high, low = (tmp_path / name for name in ('base.resources', 'high.resources', 'low.resources'))
    write_bank(base, [('model', 'native', b'native bytes', (1, 2))])
    write_bank(high, [('model', 'demon', b'chosen bytes', (4, 7)),
                      ('model', 'disabled', b'not loaded', ())])
    write_bank(low, [('model', 'demon', b'lower priority', (4, 7)),
                     ('model', 'native', b'must not replace destination', (1, 2)),
                     ('anim', 'demon/attack', b'animation bytes', (11,))])
    enabled = {str(base): b'\1', str(high): b'\1', str(low): b'\7'}
    target, receipt = consolidate_bank(base, [high, low], set(), tmp_path / 'cache', enabled)
    records = payloads(target)
    assert [(key[:2], payload) for key, payload in records] == [
        (('model', 'native'), b'native bytes'), (('model', 'demon'), b'chosen bytes'),
        (('anim', 'demon/attack'), b'animation bytes')]
    index = ResourceIndex(target)
    def metadata(number):
        row = index.records[number][1][1]
        start, count = _u64(row, 24), struct.unpack_from('<I', row, 132)[0]
        return [index.metadata[value][:2] for value in index.types[start:start + count]]
    assert metadata(1) == [('dependency_kind', 'value/4'), ('dependency_kind', 'value/7')]
    assert metadata(2) == [('dependency_kind', 'value/11')]
    assert receipt['base_mask'] == '01' and receipt['appended'] == 2
    assert consolidate_bank(base, [high, low], set(), tmp_path / 'cache', enabled) == (target, receipt)
    enabled[str(high)] = b'\3'
    changed, _ = consolidate_bank(base, [high, low], set(), tmp_path / 'cache', enabled)
    assert changed != target
    assert any(key[1] == 'disabled' for key, payload in payloads(changed))
