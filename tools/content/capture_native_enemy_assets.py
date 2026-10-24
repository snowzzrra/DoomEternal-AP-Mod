"""Capture native cache associations for enemies and powerup pickups on Windows."""
import argparse
import ctypes
import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path

from doom_eap.launcher.resource_bank import ResourceIndex, read_masks, resource_records, select_resources, _u64
from tools.maps.ap_map_generator import find_entity_block_bounds
from tools.maps.encounter_catalog import entity_records


def capture(game_root, output):
    root = Path(__file__).resolve().parents[2]
    base = game_root.resolve() / 'base'
    catalog = json.loads((root / 'doom_eap/content/native_enemy_catalog.json').read_bytes())
    spec = json.loads((base / 'packagemapspec.json').read_bytes())
    by_map = {}
    for ref in spec['mapFileRefs']:
        name = spec['files'][ref['file']]['name']
        if name.endswith('.resources') and not name.startswith('ap_enemy_resources/'):
            by_map.setdefault(spec['maps'][ref['map']]['name'], set()).add(ref['file'])
    bodies = []
    for species, row in catalog['species'].items():
        ref = row['ai']
        source = (root / 'vanillamaps' / (ref['map_key'] + '.map')).read_text(encoding='utf-8')
        start, end = find_entity_block_bounds(source, ref['entity'])
        block = source[start:end]
        if hashlib.sha256(block.strip().encode()).hexdigest() != ref['sha256']:
            raise ValueError('Frozen enemy body source differs: ' + species)
        actor = next(entity_records(block))
        tokens = {json.loads(m[0]) for m in re.finditer(r'"(?:[^"\\]|\\.)*"', block)}
        models = {v for v in tokens if v.startswith('md6def/') and v.endswith('.md6')}
        scopes = {actor['inherit'].rsplit('/', 1)[-1]} | {m.split('/')[-3] for m in models}
        bodies.append((species, catalog['maps'][ref['map_key']]['runtime_map'], models, tokens, scopes, ref))
    powerups = {}
    source = (root / 'vanillamaps/e2m2_base.map').read_text(encoding='utf-8')
    for effect, entity in (('berserk', 'pickups_pickup_powerup_berserk_2'),
                           ('onslaught', 'pickups_pickup_powerup_onslaught_2'),
                           ('overdrive', 'pickups_pickup_powerup_overdrive_1')):
        start, end = find_entity_block_bounds(source, entity)
        block = source[start:end]
        ref = dict(map_key='e2m2_base', entity=entity, sha256=hashlib.sha256(block.strip().encode()).hexdigest())
        tokens = {json.loads(m[0]) for m in re.finditer(r'"(?:[^"\\]|\\.)*"', block)}
        powerups[effect] = ref['sha256']
        bodies.append(('powerup/' + effect, catalog['maps']['e2m2_base']['runtime_map'], set(), tokens, {effect}, ref))
    needed = set().union(by_map['common'], *(by_map[runtime] for _, runtime, _, _, _, _ in bodies))
    indices = {n: ResourceIndex(base / spec['files'][n]['name']) for n in sorted(needed)}
    raw_masks = read_masks(base)
    masks = {index.path: raw_masks[spec['files'][n]['name']] for n, index in indices.items()}
    common = resource_records(indices, by_map['common'], masks)
    dll = ctypes.WinDLL(str(game_root.resolve() / 'oo2core_8_win64.dll'))
    decode = dll.OodleLZ_Decompress
    decode.restype = ctypes.c_longlong
    decode.argtypes = [ctypes.c_void_p, ctypes.c_longlong, ctypes.c_void_p, ctypes.c_longlong,
                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_longlong,
                       ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_longlong, ctypes.c_int]
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.native-enemy-assets-', dir=output.parent) as temporary:
        work = Path(temporary)
        documents = {}
        for runtime in sorted({'common'} | {r for _, r, _, _, _, _ in bodies}):
            effective = resource_records(indices, by_map[runtime], masks)
            name = 'generated/buildgame/' + runtime + '.mapresources'
            index, row, _ = next(v for k, v in effective.items() if k[1] == name)
            with index.path.open('rb') as source:
                source.seek(_u64(row, 56))
                data = source.read(_u64(row, 64))
            if _u64(row, 64) != _u64(row, 72):
                decoded = ctypes.create_string_buffer(_u64(row, 72))
                if decode(data, len(data), decoded, len(decoded), 1, 0, 0, None, 0, None, None, None, 0, 3) != len(decoded):
                    raise ValueError('Native map resource decompression failed: ' + runtime)
                data = decoded.raw
            path = work / (runtime.replace('/', '_') + '.mapresources')
            path.write_bytes(data)
            documents[runtime] = {'path': str(path), 'bank': index.path.relative_to(base).as_posix(),
                                  'sha256': hashlib.sha256(data).hexdigest()}
        inputs, exported = work / 'inputs.json', work / 'assets.json'
        inputs.write_text(json.dumps(documents), encoding='utf-8')
        subprocess.run(['powershell', '-NoProfile', '-File', str(Path(__file__).with_name('read_native_asset_tables.ps1')),
                        '-GameBase', str(base), '-InputFile', str(inputs), '-OutputFile', str(exported)], check=True)
        tables = json.loads(exported.read_text(encoding='utf-8-sig'))
    profiles = {}
    common_pairs = {(r['mapResourceType'], r['name']) for r in tables['common']}
    for species, runtime, models, tokens, scopes, ref in bodies:
        available = resource_records(indices, by_map['common'] | by_map[runtime], masks)
        assets = tables['common'] + tables[runtime]
        pairs = {(r['mapResourceType'], r['name']) for r in assets}
        roots = tokens | {r['name'] for r in assets if scopes.intersection(r['name'].split('/'))}
        selected, _ = select_resources(available, common, roots)
        wanted = {p for p in pairs if p[1] in roots}
        for key in selected:
            kind, name = key[:2]
            if kind == 'rs_streamfile' and name.startswith('generated/decls/') and name.endswith('.decl'):
                _, _, kind, name = name.split('/', 3)
                name = name[:-5]
            matches = {p for p in pairs if p[0].lower() == kind.lower() and p[1] == name}
            if not matches:
                raise ValueError('Missing native cache association: ' + kind + '/' + name)
            wanted.update(matches)
        wanted.update((kind, model) for model in models for kind in ('md6Def', 'binarymd6def'))
        wanted.difference_update(common_pairs)
        ordered = dict.fromkeys((r['mapResourceType'], r['name']) for r in assets if (r['mapResourceType'], r['name']) in wanted)
        if set(ordered) != wanted:
            raise ValueError('Native cache model association is incomplete: ' + species)
        profiles[ref['sha256']] = {'source': ref, 'models': sorted(models),
                                  'assets': [{'mapResourceType': kind, 'name': name} for kind, name in ordered]}
        print(species, 'cache entries', len(ordered), flush=True)
    document = {'schema_version': 1, 'game_image_sha256': hashlib.sha256((game_root / 'DOOMEternalx64vk.exe').read_bytes()).hexdigest(),
                'source_tables': {key: {'bank': v['bank'], 'sha256': v['sha256']} for key, v in documents.items()},
                'powerups': {effect: profiles.pop(sha) for effect, sha in powerups.items()},
                'bodies': dict(sorted(profiles.items()))}
    output.write_text(json.dumps(document, sort_keys=True, separators=(',', ':')) + '\n', encoding='utf-8', newline='\n')
    print('Native enemy asset snapshot:', len(profiles), 'bodies;', output.stat().st_size, 'bytes')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--game-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    capture(args.game_root, args.output)
