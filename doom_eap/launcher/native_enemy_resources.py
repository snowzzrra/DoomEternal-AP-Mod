"""Load the missing resources referenced by frozen enemy bodies."""

import hashlib
import json
from collections import Counter
from importlib.resources import files
from pathlib import PurePosixPath


_MARKER = "doomEAPEnemyResourceBanks"
_HUB_TEXT_DONORS = {
    'game/dlc2/e5m1_spear/e5m1_spear.resources',
    'game/dlc2/e5m1_spear/e5m1_spear_patch1.resources',
    'game/dlc2/e5m1_spear/e5m1_spear_patch2.resources',
    'game/horde/e6m3_mcity_horde/e6m3_mcity_horde.resources',
    'game/horde/e6m3_mcity_horde/e6m3_mcity_horde_patch1.resources',
}


def native_resource_maps(plan):
    if not plan or plan.get("kind") != "native_recipes":
        return {}
    catalog = json.loads(files("doom_eap.content").joinpath("native_enemy_catalog.json").read_bytes())["maps"]
    result = {}
    for key, stage in plan["maps"].items():
        if key not in catalog or stage["runtime_map"] != catalog[key]["runtime_map"]:
            raise ValueError("Native enemy resource destination differs from the catalog")
        donors = set()
        for recipe in stage["recipes"]:
            for body in recipe["bodies"]:
                donor = body["ai"]["map_key"]
                if donor not in catalog:
                    raise ValueError("Unknown native enemy resource donor")
                if donor != key:
                    donors.add(catalog[donor]["runtime_map"])
        if donors:
            result[stage["runtime_map"]] = donors
    return result


def prepare_native_resource_banks(base, plan, roots=None, *, hub_world_text=False):
    requested = native_resource_maps(plan)
    if hub_world_text:
        requested['game/hub/hub'] = {'game/dlc2/e5m1_spear/e5m1_spear', 'game/horde/e6m3_mcity_horde/e6m3_mcity_horde'}
        roots = {**(roots or {}), 'game/hub/hub': ['gui/text', 'swf/guientity/generic_text.swf']}
    path = base / "packagemapspec.json"
    if not path.is_file() and not requested:
        return {}, {"maps": {}}
    original = path.read_bytes()
    spec = json.loads(original.decode("utf-8-sig"))
    if any(not isinstance(spec.get(key), list) for key in ("files", "maps", "mapFileRefs")):
        raise ValueError("Invalid native package map specification")
    for ref in spec["mapFileRefs"]:
        if (not isinstance(ref, dict) or type(ref.get("file")) is not int or type(ref.get("map")) is not int
                or not 0 <= ref["file"] < len(spec["files"]) or not 0 <= ref["map"] < len(spec["maps"])):
            raise ValueError("Invalid native package resource reference")
    previous = spec.pop(_MARKER, None)
    if previous is not None:
        if (not isinstance(previous, dict) or previous.get("revision") != 1
                or not isinstance(previous.get("files"), list) or not isinstance(previous.get("refs"), list)):
            raise ValueError("Invalid native enemy resource ownership metadata")
        prefix = len(previous["files"])
        if not prefix or spec["files"][:prefix] != previous["files"]:
            raise ValueError("Native enemy resource file order changed externally")
        owned = previous["refs"]
        actual = [ref for ref in spec["mapFileRefs"] if ref["file"] < prefix]
        if Counter(json.dumps(ref, sort_keys=True) for ref in actual) != Counter(json.dumps(ref, sort_keys=True) for ref in owned):
            raise ValueError("Native enemy resource references changed externally")
        spec["files"] = spec["files"][prefix:]
        spec["mapFileRefs"] = [{**ref, "file": ref["file"] - prefix}
                               for ref in spec["mapFileRefs"] if ref["file"] >= prefix]
        generated = previous.get('generated_files', [])
        if generated:
            start = len(spec['files']) - len(generated)
            if spec['files'][start:] != generated or any(ref['file'] >= start for ref in spec['mapFileRefs']):
                raise ValueError('Consolidated native resource ownership changed externally')
            del spec['files'][start:]
    maps = {}
    for index, value in enumerate(spec["maps"]):
        if not isinstance(value, dict) or not isinstance(value.get("name"), str) or value["name"] in maps:
            raise ValueError("Invalid or duplicate native package map")
        maps[value["name"]] = index
    hub = maps.get('game/hub/hub')
    imports = [ref for ref in spec['mapFileRefs'] if ref['map'] == hub
               and spec['files'][ref['file']]['name'] in _HUB_TEXT_DONORS]
    if hub_world_text and imports:
        backup = base / 'packagemapspec.json.backup'
        if not backup.is_file():
            raise ValueError('Fortress resource compaction requires the native package map backup')
        native = json.loads(backup.read_text(encoding='utf-8-sig'))
        native_hub = next(i for i, row in enumerate(native['maps']) if row['name'] == 'game/hub/hub')
        native_files = {native['files'][ref['file']]['name'] for ref in native['mapFileRefs'] if ref['map'] == native_hub}
        spec['mapFileRefs'] = [ref for ref in spec['mapFileRefs'] if ref not in imports
                              or spec['files'][ref['file']]['name'] in native_files]
    by_map = {}
    for ref in spec["mapFileRefs"]:
        by_map.setdefault(ref["map"], set()).add(ref["file"])
    donor_files, targets, consolidated = set(), {}, {}
    source_hashes = {path.name: hashlib.sha256(original).hexdigest()}
    if requested:
        expected = {stage['runtime_map'] for stage in (plan or {}).get('maps', {}).values() if stage['recipes']}
        if hub_world_text: expected.add('game/hub/hub')
        if (not isinstance(roots, dict) or set(roots) != expected
                or any(not isinstance(values, list) or any(not isinstance(v, str) for v in values)
                       for values in roots.values())):
            raise ValueError('Rebuild the room package with native enemy resource roots')
        from .resource_bank import ResourceIndex, read_masks, resource_records, select_resources, resource_signature, write_resource_bank
        common = by_map.get(maps.get('common'), set())
        needed = set(common)
        for target, donors in requested.items():
            if target not in maps or not donors <= maps.keys():
                raise ValueError('Native enemy resource map is absent from the installation')
            needed.update(by_map.get(maps[target], set()))
            for donor in donors:
                if not by_map.get(maps[donor]):
                    raise ValueError('Native enemy resource donor has no banks')
                needed.update(by_map[maps[donor]])
        indices = {}
        for number in sorted(needed):
            name = spec['files'][number]['name']
            bank = PurePosixPath(name)
            if bank.is_absolute() or '..' in bank.parts or '\\' in name or ':' in name:
                raise ValueError('Invalid native resource bank path')
            if bank.suffix not in {'.resources', '.streamdb'}:
                continue
            source = base / name
            if not source.is_file() or not source.stat().st_size:
                raise ValueError('Missing native enemy resource bank: ' + name)
            if bank.suffix == '.resources':
                indices[number] = ResourceIndex(source)
        source_hashes.update({name: hashlib.sha256((base / name).read_bytes()).hexdigest()
                              for name in ('meta.resources', 'idRehash.map')})
        raw_masks = read_masks(base)
        masks = {index.path: raw_masks[spec['files'][number]['name']] for number, index in indices.items()}
        shared, missing = {}, set()
        for target, donors in requested.items():
            native = by_map.get(maps[target], set()) | common
            candidates = common | set().union(*(by_map[maps[name]] for name in donors))
            local = resource_records(indices, native, masks)
            available = resource_records(indices, candidates, masks)
            selected, streamed = select_resources(available, local, roots[target])
            for key, value in selected.items():
                if key in shared and value != shared[key] and resource_signature(value) != resource_signature(shared[key]):
                    raise ValueError('Conflicting native enemy resource: ' + key[1])
                shared.setdefault(key, value)
            missing.update([target] if selected else [])
            targets[target] = {i for i in candidates - native
                               if streamed and spec['files'][i]['name'].endswith('.streamdb')}
            names = {spec['files'][i]['name'] for i in native | targets[target]
                     if PurePosixPath(spec['files'][i]['name']).suffix in {'.resources', '.streamdb'}}
            if len(names) + bool(selected) > 64:
                raise ValueError('Native enemy resource closure exceeds the database limit')
        if shared:
            bank, info = write_resource_bank(shared, base / 'ap_enemy_resources')
            name = bank.relative_to(base).as_posix()
            consolidated[name] = info
            number = len(spec['files'])
            spec['files'].append({'name': name})
            for target in missing:
                targets[target].add(number)
        for selected in targets.values():
            donor_files.update(selected)
    if donor_files:
        ordered = sorted(donor_files)
        cloned = [spec["files"][index] for index in ordered]
        indices = {index: offset for offset, index in enumerate(ordered)}
        prefix = len(cloned)
        # native loading reverses file indices and appends banks; donors follow destination banks.
        spec["files"] = cloned + spec["files"]
        refs = [{"file": indices[index], "map": maps[target]}
                for target, selected in sorted(targets.items()) for index in selected]
        spec["mapFileRefs"] = refs + [{**ref, "file": ref["file"] + prefix} for ref in spec["mapFileRefs"]]
        spec[_MARKER] = {"revision": 1, "files": cloned, "refs": refs,
                         **({'generated_files': [{'name': name} for name in consolidated]} if consolidated else {})}
    encoded = (json.dumps(spec, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    updates = {path.name: encoded} if (requested or previous is not None) and encoded != original else {}
    return updates, {"maps": {target: [spec["files"][indices[index]]["name"] for index in selected]
                              for target, selected in sorted(targets.items()) if selected},
                     "consolidated": consolidated,
                     "source_hashes": source_hashes}
