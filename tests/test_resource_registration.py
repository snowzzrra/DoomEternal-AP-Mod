"""Check bank registration without publishing damaged resources."""

import hashlib
import json
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_resource_bank import write_bank

from doom_eap.launcher import campaign_resources, resource_bank

MASK_NAME = 'generated/buildgame/container.mask'
pytest_plugins = ['test_launcher_resources']


@pytest.fixture(autouse=True)
def mock_rehasher(monkeypatch):
    def rehash(command, *, cwd, **kwargs):
        directory = Path(cwd)
        assert command == [str((directory.parent / ('idRehash.exe' if resource_bank.os.name == 'nt' else 'idRehash')).resolve())]
        assert kwargs == {'capture_output': True, 'timeout': 120, 'creationflags': 0}
        path = directory / 'meta.resources'
        row = campaign_resources._records(path, {MASK_NAME})[MASK_NAME]
        data = bytearray(path.read_bytes())
        for line in (directory / 'idRehash.map').read_text().splitlines():
            name, offset = line.rsplit(';', 1)
            assert Path(name).is_file()
            struct.pack_into('<Q', data, row[2] + int(offset), 123)
        path.write_bytes(data)
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')

    monkeypatch.setattr(resource_bank, 'subprocess', SimpleNamespace(run=rehash, CREATE_NO_WINDOW=0))


def staged_bank(tmp_path, count=1):
    donor = tmp_path / 'donor.resources'
    write_bank(donor, [('model', f'model/{i}', b'body', ()) for i in range(count)])
    index = resource_bank.ResourceIndex(donor)
    selected = {key: (index, row, number) for key, (_, row, number) in index.records}
    return resource_bank.plan_resource_bank(selected, tmp_path / 'staging')


def mask_payload(path):
    return campaign_resources._records(path, {MASK_NAME})[MASK_NAME][3]


def snapshot(base):
    return {path.relative_to(base): path.read_bytes() for path in base.rglob('*') if path.is_file()}


@pytest.mark.parametrize('count', [1, 63, 64, 65, 128, 129])
def test_bank_registration_relocates_mask_and_preserves_foreign_tables(tmp_path, count):
    bank, info = staged_bank(tmp_path, count)
    base = tmp_path / 'base'
    base.mkdir()
    foreign = struct.pack('<QIQ', 0x123456789abcdef, 1, 0x1020304050607080)
    unlisted = struct.pack('<QIQQ', 0xfedcba9876543210, 2, 0, 0x1111111111111111)
    mask = struct.pack('<II', 0, 2) + foreign + unlisted
    write_bank(base / 'meta.resources', [('rs_streamfile', MASK_NAME, mask, ())])
    mapping = b'\xef\xbb\xbf./foreign.resources;8\r\n'
    (base / 'idRehash.map').write_bytes(mapping)
    before = snapshot(base)
    name = 'ap_enemy_resources/' + bank.name
    banks, paths = {name: info}, {name: bank}
    updates = {}
    resource_bank.register_banks(base, banks, updates, bank_paths=paths)
    assert snapshot(base) == before
    assert updates['idRehash.map'] == mapping + f'{name};{len(mask)}\n'.encode()
    for filename, data in updates.items():
        (base / filename).write_bytes(data)
    actual = mask_payload(base / 'meta.resources')
    assert actual[8:len(mask)] == foreign + unlisted
    assert struct.unpack_from('<I', actual, 4)[0] == 3
    assert struct.unpack_from('<Q', actual, len(mask))[0] == 123
    bits = resource_bank.mask_tables(actual)[len(mask)]
    assert len(bits) == (count + 63) // 64 * 8
    assert int.from_bytes(bits, 'little') == (1 << count) - 1
    row = campaign_resources._records(base / 'meta.resources', {MASK_NAME})[MASK_NAME]
    assert row[2] >= len(before[Path('meta.resources')]) and row[2] % 16 == 0
    repeated = {}
    resource_bank.register_banks(base, banks, repeated, bank_paths=paths)
    assert repeated == {}
    removed = {}
    resource_bank.register_banks(base, {}, removed, obsolete={name})
    (base / 'meta.resources').write_bytes(removed['meta.resources'])
    assert mask_payload(base / 'meta.resources') == mask
    assert removed['idRehash.map'] == mapping


def test_obsolete_table_removal_remaps_foreign_offsets(tmp_path):
    base = tmp_path / 'base'
    base.mkdir()
    old = 'ap_enemy_resources/' + 'a' * 64 + '.resources'
    foreign = struct.pack('<QIQ', 0xfeed, 1, 0x5)
    mask = struct.pack('<IIQIQ', 0, 2, 0xbeef, 1, 1) + foreign
    write_bank(base / 'meta.resources', [('rs_streamfile', MASK_NAME, mask, ())])
    (base / 'idRehash.map').write_bytes(f'{old};8\nforeign.resources;28\n'.encode())
    updates = {}
    resource_bank.register_banks(base, {}, updates, obsolete={old})
    (base / 'meta.resources').write_bytes(updates['meta.resources'])
    assert mask_payload(base / 'meta.resources') == struct.pack('<II', 0, 1) + foreign
    assert updates['idRehash.map'] == b'foreign.resources;8\n'


@pytest.mark.parametrize('damage', ['bank', 'hash', 'bits', 'capacity', 'map_name', 'map_offset', 'spec', 'target'])
def test_damaged_registration_stops_before_publication(installed_room, tmp_path, damage):
    game, mod = installed_room(False)
    base = game / 'base'
    staging = tmp_path / 'staging'
    plan = campaign_resources.plan_campaign_resources(game, mod, staging)
    name, = plan['banks']['consolidated']
    filename = 'meta.resources'
    path = staging / filename
    if damage == 'bank':
        path = Path(plan['banks']['bank_paths'][name])
        data = bytearray(path.read_bytes())
        data[-1] ^= 1
    elif damage in {'hash', 'bits', 'capacity'}:
        data = bytearray(path.read_bytes())
        row = campaign_resources._records(path, {MASK_NAME})[MASK_NAME]
        mapping = resource_bank._bank_mapping(row[3], (staging / 'idRehash.map').read_text())
        at = row[2] + mapping[name]
        if damage == 'hash':
            data[at:at + 8] = bytes(8)
        else:
            data[at + {'bits': 12, 'capacity': 8}[damage]] ^= 1
    elif damage in {'map_name', 'map_offset'}:
        filename = 'idRehash.map'
        path = staging / filename
        data = path.read_bytes()
        lines = data.decode().splitlines()
        duplicate = lines[-1] if damage == 'map_name' else 'foreign.resources;' + lines[-1].rsplit(';', 1)[1]
        data += (duplicate + '\n').encode()
    else:
        filename = 'packagemapspec.json'
        path = staging / filename
        spec = json.loads(path.read_bytes())
        refs = spec['mapFileRefs']
        if damage == 'spec':
            spec['mapFileRefs'] = [ref for ref in refs if spec['files'][ref['file']]['name'] != name]
        else:
            ref = next(ref for ref in refs if spec['files'][ref['file']]['name'] == name)
            ref['map'] = 0
        data = json.dumps(spec).encode()
    path.write_bytes(data)
    if damage != 'bank':
        plan['updates'][filename]['sha256'] = hashlib.sha256(data).hexdigest()
    before = snapshot(base)
    with pytest.raises(ValueError):
        campaign_resources.publish_campaign_resources(game, plan, staging)
    assert snapshot(base) == before


def test_changed_metadata_stops_before_publication(installed_room, tmp_path):
    game, mod = installed_room(False)
    base, staging = game / 'base', tmp_path / 'staging'
    plan = campaign_resources.plan_campaign_resources(game, mod, staging)
    (base / 'idRehash.map').write_bytes(b'external change')
    before = snapshot(base)
    with pytest.raises(RuntimeError, match='changed during installation'):
        campaign_resources.publish_campaign_resources(game, plan, staging)
    assert snapshot(base) == before


def test_repeat_install_keeps_verified_registration_files(installed_room, tmp_path):
    game, mod = installed_room(False)
    staging = tmp_path / 'staging'
    plan = campaign_resources.plan_campaign_resources(game, mod, staging)
    campaign_resources.publish_campaign_resources(game, plan, staging)
    campaign_resources.verify_campaign_resources(game, plan)
    repeated = campaign_resources.plan_campaign_resources(game, mod, tmp_path / 'repeated')
    assert not {'meta.resources', 'idRehash.map'} & repeated['updates'].keys()
    campaign_resources.publish_campaign_resources(game, repeated, tmp_path / 'repeated')
    campaign_resources.verify_campaign_resources(game, repeated)


@pytest.mark.parametrize('failure', ['error', 'empty', 'unrelated'])
def test_rehasher_failure_preserves_installed_files(installed_room, tmp_path, monkeypatch, failure):
    game, mod = installed_room(False)
    base = game / 'base'
    run = resource_bank.subprocess.run

    def rehash(command, *, cwd, **kwargs):
        if failure == 'error':
            return SimpleNamespace(returncode=1, stderr=b'rehasher failed')
        if failure == 'empty':
            return SimpleNamespace(returncode=0)
        result = run(command, cwd=cwd, **kwargs)
        path = Path(cwd) / 'meta.resources'
        row = campaign_resources._records(path, {MASK_NAME})[MASK_NAME]
        data = bytearray(path.read_bytes())
        data[row[2] + 8] ^= 1
        path.write_bytes(data)
        return result

    monkeypatch.setattr(resource_bank.subprocess, 'run', rehash)
    before = snapshot(base)
    with pytest.raises((RuntimeError, ValueError), match={
        'error': 'rehasher failed', 'empty': 'registration is empty', 'unrelated': 'unrelated mask records',
    }[failure]):
        campaign_resources.plan_campaign_resources(game, mod, tmp_path / 'staging')
    assert snapshot(base) == before
