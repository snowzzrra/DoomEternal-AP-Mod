import struct

import pytest

from doom_eap.launcher.resource_bank import ResourceIndex, resource_records, select_resources, write_resource_bank, _u64


def test_steam_restored_mask_discards_only_unregistered_generated_banks():
    from doom_eap.launcher.campaign_resources import resource_mapping_lines
    mask = struct.pack('<IIQIQI', 0, 2, 17, 0, 29, 0)
    valid = 'native.resources;8\r\n' + 'ap_enemy_resources/' + 'a' * 64 + '.resources;20\r\n'
    stale = 'ap_enemy_resources/' + 'b' * 64 + '.resources;32\r\n'
    assert ''.join(resource_mapping_lines(mask, valid + stale)) == valid
    with pytest.raises(ValueError, match='mapping'):
        resource_mapping_lines(mask, valid + 'other_mod.resources;32\r\n')


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


def test_selected_dependencies_preserve_priority_types_and_reuse_bank_after_reinstall(tmp_path):
    base, high, low = (tmp_path / name for name in ('base.resources', 'high.resources', 'low.resources'))
    write_bank(base, [('model', 'native', b'native bytes', (1, 2))])
    write_bank(high, [('model', 'demon', b'chosen bytes', (4, 7)),
                      ('model', 'disabled', b'not loaded', ()),
                      ('rs_streamfile', 'generated/decls/entitydef/ai/demon.decl',
                       b'{ model = "demon"; anim = "demon/attack"; }', ()),
                      ('staticShadowGeom', 'unrelated map', b'geometry' * 10000, ())])
    write_bank(low, [('model', 'demon', b'lower priority', (4, 7)),
                     ('model', 'native', b'must not replace destination', (1, 2)),
                     ('anim', 'demon/attack', b'animation bytes', (11,))])
    indices = {i: ResourceIndex(path) for i, path in enumerate((base, low, high))}
    enabled = {base: b'\1', high: b'\x0d', low: b'\7'}
    local = resource_records(indices, {0}, enabled)
    available = resource_records(indices, {0, 1, 2}, enabled)
    selected, streamed = select_resources(available, local, ['ai/demon', 'native', 'disabled'])
    assert not streamed
    target, receipt = write_resource_bank(selected, tmp_path / 'cache')
    records = payloads(target)
    assert [(key[:2], payload) for key, payload in records] == [
        (('anim', 'demon/attack'), b'animation bytes'), (('model', 'demon'), b'chosen bytes'),
        (('rs_streamfile', 'generated/decls/entitydef/ai/demon.decl'),
         b'{ model = "demon"; anim = "demon/attack"; }')]
    index = ResourceIndex(target)
    def metadata(number):
        row = index.records[number][1][1]
        start, count = _u64(row, 24), struct.unpack_from('<I', row, 132)[0]
        return [index.metadata[value][:2] for value in index.types[start:start + count]]
    assert metadata(1) == [('dependency_kind', 'value/4'), ('dependency_kind', 'value/7')]
    assert metadata(0) == [('dependency_kind', 'value/11')]
    assert receipt['base_count'] == 0 and receipt['records'] == 3
    import os
    os.utime(high, ns=(1_000_000_000, 1_000_000_000))
    assert write_resource_bank(selected, tmp_path / 'cache') == (target, receipt)
    assert len(list((tmp_path / 'cache').glob('*.resources'))) == 1


def test_native_declaration_comments_and_shader_strings_preserve_asset_dependencies(tmp_path):
    path = tmp_path / 'native.resources'
    declaration = b'// legacy \xcd\n{ shader = "\\q"; model = "demon"; anim = "demon/attack"; }'
    write_bank(path, [('rs_streamfile', 'generated/decls/entitydef/ai/demon.decl', declaration, ()),
                      ('model', 'demon', b'model', ()), ('anim', 'demon/attack', b'anim', ())])
    index = ResourceIndex(path)
    available = resource_records({0: index}, {0}, {path: b'\7'})
    selected, _ = select_resources(available, {}, ['ai/demon'])
    assert {key[:2] for key in selected} == {key[:2] for key, _ in index.records}


def test_local_material_resolves_native_image_variants_without_copying_unrelated_assets(tmp_path):
    path = tmp_path / 'native.resources'
    write_bank(path, [('rs_streamfile', 'generated/decls/material2/weapon.decl',
                       b'{ image = "Art/Objects/Reaper/reaper.tga"; }', ()),
                      ('image', 'art/objects/reaper/reaper.tga$streamed$mtlkind=albedo', b'albedo', ()),
                      ('image', 'art/objects/reaper/reaper.tga$bc7$streamed', b'variant', ()),
                      ('image', 'art/objects/other.tga$streamed', b'unrelated', ())])
    index = ResourceIndex(path)
    available = resource_records({0: index}, {0}, {path: b'\x0f'})
    local = {key: value for key, value in available.items() if key[0] == 'rs_streamfile'}
    selected, _ = select_resources(available, local, ['weapon'])
    assert {(key[0], payload) for key, payload in payloads(write_resource_bank(selected, tmp_path / 'cache')[0])} == {
        ('image', b'albedo'), ('image', b'variant')}


def test_cleanup_preserves_active_foreign_and_registered_files(tmp_path, monkeypatch):
    import hashlib
    import json
    from doom_eap.launcher import resource_bank as domain
    from doom_eap.launcher.build_manifest import _KEYS
    from doom_eap.runtime.save_decrypt import _aes_gcm_encrypt
    directory = tmp_path / 'ap_enemy_resources'
    directory.mkdir()
    stale, active, foreign = [directory / (letter * 64 + '.resources') for letter in 'abc']
    for path in (stale, active, foreign):
        write_bank(path, [('anim', 'demon', b'animation', ())])
        stat = path.stat()
        path.with_suffix('.json').write_text(json.dumps(dict(identity=path.stem, size=stat.st_size,
            mtime_ns=stat.st_mtime_ns, records=1, base_count=0, appended=1)))
    foreign.with_suffix('.json').write_text('{}')
    names = [path.relative_to(tmp_path).as_posix() for path in (stale, active, foreign)]
    spec = {'files': [{'name': names[1]}]}
    (tmp_path / 'packagemapspec.json').write_text(json.dumps(spec))
    obsolete = domain.obsolete_banks(tmp_path, spec)
    assert set(obsolete) == {names[0]}
    def manifest(files):
        nonce = bytes(12)
        (tmp_path / 'build-manifest.bin').write_bytes(nonce + _aes_gcm_encrypt(_KEYS[0], nonce,
            json.dumps({'files': files}).encode(), b'build-manifest') + bytes(64))
    manifest({names[0]: {}})
    monkeypatch.setattr(domain, 'read_masks', lambda _: {})
    assert domain.cleanup_banks(tmp_path, obsolete)['removed'] == []
    manifest({})
    monkeypatch.setattr(domain, 'read_masks', lambda _: {names[0]: b'\1'})
    assert domain.cleanup_banks(tmp_path, obsolete)['removed'] == []
    monkeypatch.setattr(domain, 'read_masks', lambda _: {})
    report = domain.cleanup_banks(tmp_path, obsolete)
    assert report['removed'] == [names[0]] and report['bytes_freed'] == obsolete[names[0]]['size']
    assert not stale.exists() and not stale.with_suffix('.json').exists()
    assert active.is_file() and foreign.is_file()
