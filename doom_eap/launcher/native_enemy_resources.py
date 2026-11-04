"""Load the missing resources referenced by frozen enemy bodies."""

import hashlib
import json
import re
from collections import Counter
from importlib.resources import files
from pathlib import Path, PurePosixPath

from doom_eap.launcher.ui.strings import text as player_text

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
            raise ValueError(player_text('launcher.native_enemy_resources', 'native_enemy_resource_destination_differs_from_the_catalog'))
        donors = set()
        for recipe in [*stage["recipes"], *stage.get('compatibility_recipes', []), *stage.get('counter_recipes', [])]:
            for body in recipe["bodies"]:
                donor = body["ai"]["map_key"]
                if donor not in catalog:
                    raise ValueError(player_text('launcher.native_enemy_resources', 'unknown_native_enemy_resource_donor'))
                if donor != key:
                    donors.add(catalog[donor]["runtime_map"])
        if donors:
            result[stage["runtime_map"]] = donors
    return result


def prepare_native_resource_banks(base, plan, roots=None, *, staging, hub_world_text=False):
    requested, roots, boon_maps = _requested_resources(plan, roots, hub_world_text)
    bank_paths = {}
    path = base / "packagemapspec.json"
    if not path.is_file() and not requested:
        return {}, {"maps": {}, "consolidated": {}, "bank_paths": {}}
    original, spec, previous = _native_spec(path)
    maps, by_map = _map_files(base, spec, hub_world_text)
    donor_files, targets, consolidated = set(), {}, {}
    source_hashes = {path.name: hashlib.sha256(original).hexdigest()}
    if requested:
        common, indices, masks = _source_banks(base, plan, roots, requested, boon_maps, hub_world_text, spec, maps, by_map)
        source_hashes.update({name: hashlib.sha256((base / name).read_bytes()).hexdigest()
                              for name in ('meta.resources', 'idRehash.map')})
        shared, missing, targets = _select_banks(requested, maps, by_map, common, indices, masks, roots, spec)
        if shared:
            name, info, bank = _consolidated_bank(shared, staging, spec, missing, targets)
            bank_paths[name] = bank
            consolidated[name] = info
        donor_files = set().union(*targets.values())
    indices = {}
    if donor_files:
        indices = _prepend_bank_refs(spec, maps, targets, donor_files, consolidated)
    encoded = (json.dumps(spec, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    updates = {path.name: encoded} if (requested or previous is not None) and encoded != original else {}
    return updates, {"maps": _planned_bank_maps(spec, indices, targets),
                     "consolidated": consolidated, "bank_paths": bank_paths,
                     "source_hashes": source_hashes}


def _native_spec(path):
    original = path.read_bytes()
    spec = json.loads(original.decode("utf-8-sig"))
    if any(not isinstance(spec.get(key), list) for key in ("files", "maps", "mapFileRefs")):
        raise ValueError(player_text('launcher.native_enemy_resources', 'invalid_native_package_map_specification'))
    _check_spec_refs(spec)
    previous = spec.pop(_MARKER, None)
    if previous is not None:
        _restore_native_spec(spec, previous)
    return original, spec, previous


def _requested_resources(plan, roots, hub_world_text):
    requested = native_resource_maps(plan)
    if roots is not None and not isinstance(roots, dict):
        raise ValueError(player_text('launcher.native_enemy_resources', 'native_resource_roots_must_be_a_map_to_resource_mapping'))
    boon_maps = _boon_maps(plan, roots)
    for target in boon_maps:
        requested.setdefault(target, set()).add('game/sp/e2m2_base/e2m2_base')
    if hub_world_text:
        requested.setdefault('game/hub/hub', set()).update(
            {'game/dlc2/e5m1_spear/e5m1_spear', 'game/horde/e6m3_mcity_horde/e6m3_mcity_horde'})
        roots = {**(roots or {}), 'game/hub/hub': [*(roots or {}).get('game/hub/hub', ()),
                                                  'gui/text', 'swf/guientity/generic_text.swf']}
    return requested, roots, boon_maps


def _map_files(base, spec, hub_world_text):
    maps = {}
    for index, value in enumerate(spec["maps"]):
        if not isinstance(value, dict) or not isinstance(value.get("name"), str) or value["name"] in maps:
            raise ValueError(player_text('launcher.native_enemy_resources', 'invalid_or_duplicate_native_package_map'))
        maps[value["name"]] = index
    if hub_world_text:
        _compact_hub_imports(base, spec, maps)
    by_map = {}
    for ref in spec["mapFileRefs"]:
        by_map.setdefault(ref["map"], set()).add(ref["file"])
    return maps, by_map


def _source_banks(base, plan, roots, requested, boon_maps, hub_world_text, spec, maps, by_map):
    _check_roots(plan, roots, boon_maps, hub_world_text)
    from .resource_bank import read_masks
    common = by_map.get(maps.get('common'), set())
    needed = _needed_banks(requested, maps, by_map, common)
    indices = _resource_indices(base, spec, needed)
    raw_masks = read_masks(base)
    masks = {index.path: raw_masks[spec['files'][number]['name']] for number, index in indices.items()}
    return common, indices, masks


def _select_banks(requested, maps, by_map, common, indices, masks, roots, spec):
    from .resource_bank import resource_records, select_resources

    shared, missing, targets = {}, set(), {}
    for target, donors in requested.items():
        native = by_map.get(maps[target], set()) | common
        candidates = common | set().union(*(by_map[maps[name]] for name in donors))
        local = resource_records(indices, native, masks)
        available = resource_records(indices, candidates, masks)
        selected, streamed = select_resources(available, local, roots[target])
        _merge_resources(shared, selected)
        missing.update([target] if selected else [])
        targets[target] = {i for i in candidates - native
                           if streamed and spec['files'][i]['name'].endswith('.streamdb')}
        names = {spec['files'][i]['name'] for i in native | targets[target]
                 if PurePosixPath(spec['files'][i]['name']).suffix in {'.resources', '.streamdb'}}
        if len(names) + bool(selected) > 64:
            raise ValueError(player_text('launcher.native_enemy_resources', 'native_enemy_resource_closure_exceeds_the_database_limit'))
    return shared, missing, targets


def publish_native_resource_banks(base, report, *, journal=None):
    from .resource_bank import publish_resource_bank

    for name, info in report.get('consolidated', {}).items():
        publish_resource_bank(Path(report['bank_paths'][name]), _generated_bank_path(base, name, info), info, journal=journal)


def verify_native_resource_banks(base, report):
    from .resource_bank import verify_resource_bank

    for name, info in report.get('consolidated', {}).items():
        verify_resource_bank(_generated_bank_path(base, name, info), info)


def _generated_bank_path(base, name, info):
    if not re.fullmatch(r'ap_enemy_resources/[0-9a-f]{64}\.resources', name) or Path(name).stem != info['identity']:
        raise ValueError(player_text('launcher.build_manifest', 'invalid_generated_resource_bank_path'))
    return base / name


def _check_spec_refs(spec):
    for ref in spec["mapFileRefs"]:
        if (not isinstance(ref, dict) or type(ref.get("file")) is not int or type(ref.get("map")) is not int
                or not 0 <= ref["file"] < len(spec["files"]) or not 0 <= ref["map"] < len(spec["maps"])):
            raise ValueError(player_text('launcher.native_enemy_resources', 'invalid_native_package_resource_reference'))


def _restore_native_spec(spec, previous):
    _check_ownership(previous)
    prefix = len(previous["files"])
    if not prefix or spec["files"][:prefix] != previous["files"]:
        raise ValueError(player_text('launcher.native_enemy_resources', 'native_enemy_resource_file_order_changed_externally'))
    owned = previous["refs"]
    actual = [ref for ref in spec["mapFileRefs"] if ref["file"] < prefix]
    if Counter(json.dumps(ref, sort_keys=True) for ref in actual) != Counter(json.dumps(ref, sort_keys=True) for ref in owned):
        raise ValueError(player_text('launcher.native_enemy_resources', 'native_enemy_resource_references_changed_externally'))
    spec["files"] = spec["files"][prefix:]
    spec["mapFileRefs"] = [{**ref, "file": ref["file"] - prefix}
                           for ref in spec["mapFileRefs"] if ref["file"] >= prefix]
    _remove_generated_files(spec, previous)


def _check_ownership(previous):
    if (not isinstance(previous, dict) or previous.get("revision") != 1
            or not isinstance(previous.get("files"), list) or not isinstance(previous.get("refs"), list)):
        raise ValueError(player_text('launcher.native_enemy_resources', 'invalid_native_enemy_resource_ownership_metadata'))


def _remove_generated_files(spec, previous):
    generated = previous.get('generated_files', [])
    if generated:
        start = len(spec['files']) - len(generated)
        if spec['files'][start:] != generated or any(ref['file'] >= start for ref in spec['mapFileRefs']):
            raise ValueError(player_text('launcher.native_enemy_resources', 'consolidated_native_resource_ownership_changed_externally'))
        del spec['files'][start:]


def _boon_maps(plan, roots):
    boon_maps = {target for target, values in (roots or {}).items() if isinstance(values, list)
                 and _has_powerup(values)}
    if boon_maps:
        catalog = (plan or {}).get('maps') or json.loads(
            files('doom_eap.content').joinpath('native_enemy_catalog.json').read_bytes())['maps']
        supported = {stage['runtime_map'] for stage in catalog.values()} | {'game/hub/hub'}
        if not boon_maps <= supported:
            raise ValueError(player_text('launcher.native_enemy_resources', 'unknown_powerup_resource_destination'))
    return boon_maps


def _has_powerup(values):
    return any(isinstance(value, str) and value.startswith('pickup/powerup/') for value in values)


def _compact_hub_imports(base, spec, maps):
    hub = maps.get('game/hub/hub')
    imports = [ref for ref in spec['mapFileRefs'] if ref['map'] == hub
               and spec['files'][ref['file']]['name'] in _HUB_TEXT_DONORS]
    if imports:
        backup = base / 'packagemapspec.json.backup'
        if not backup.is_file():
            raise ValueError(player_text('launcher.native_enemy_resources', 'fortress_resource_compaction_requires_the_native_package_map_bac'))
        native = json.loads(backup.read_text(encoding='utf-8-sig'))
        native_files = _hub_files(native)
        spec['mapFileRefs'] = [ref for ref in spec['mapFileRefs'] if ref not in imports
                              or spec['files'][ref['file']]['name'] in native_files]


def _hub_files(spec):
    hub = next(i for i, row in enumerate(spec['maps']) if row['name'] == 'game/hub/hub')
    return {spec['files'][ref['file']]['name'] for ref in spec['mapFileRefs'] if ref['map'] == hub}


def _check_roots(plan, roots, boon_maps, hub_world_text):
    expected = {stage['runtime_map'] for stage in (plan or {}).get('maps', {}).values() if stage['recipes']}
    expected.update(boon_maps)
    if hub_world_text:
        expected.add('game/hub/hub')
    if (not isinstance(roots, dict) or set(roots) != expected
            or any(not _resource_roots(values) for values in roots.values())):
        raise ValueError(player_text('launcher.native_enemy_resources', 'rebuild_the_room_package_with_native_enemy_resource_roots'))


def _resource_roots(values):
    return isinstance(values, list) and all(isinstance(value, str) for value in values)


def _needed_banks(requested, maps, by_map, common):
    needed = set(common)
    for target, donors in requested.items():
        if target not in maps or not donors <= maps.keys():
            raise ValueError(player_text('launcher.native_enemy_resources', 'native_enemy_resource_map_is_absent_from_the_installation'))
        needed.update(by_map.get(maps[target], set()))
        for donor in donors:
            if not by_map.get(maps[donor]):
                raise ValueError(player_text('launcher.native_enemy_resources', 'native_enemy_resource_donor_has_no_banks'))
            needed.update(by_map[maps[donor]])
    return needed


def _resource_indices(base, spec, needed):
    from .resource_bank import ResourceIndex

    indices = {}
    for number in sorted(needed):
        name = spec['files'][number]['name']
        bank = PurePosixPath(name)
        if bank.is_absolute() or '..' in bank.parts or '\\' in name or ':' in name:
            raise ValueError(player_text('launcher.native_enemy_resources', 'invalid_native_resource_bank_path'))
        if bank.suffix not in {'.resources', '.streamdb'}:
            continue
        source = base / name
        if not source.is_file() or not source.stat().st_size:
            raise ValueError(player_text('launcher.native_enemy_resources', 'missing_native_enemy_resource_bank') + name)
        if bank.suffix == '.resources':
            indices[number] = ResourceIndex(source)
    return indices


def _merge_resources(shared, selected):
    from .resource_bank import resource_signature

    for key, value in selected.items():
        if key in shared and value != shared[key] and resource_signature(value) != resource_signature(shared[key]):
            raise ValueError(player_text('launcher.native_enemy_resources', 'conflicting_native_enemy_resource') + key[1])
        shared.setdefault(key, value)


def _prepend_bank_refs(spec, maps, targets, donor_files, consolidated):
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
    return indices


def _planned_bank_maps(spec, indices, targets):
    return {target: [spec['files'][indices[index]]['name'] for index in selected]
            for target, selected in sorted(targets.items()) if selected}


def _consolidated_bank(shared, staging, spec, missing, targets):
    from .resource_bank import plan_resource_bank

    bank, info = plan_resource_bank(shared, staging)
    name = 'ap_enemy_resources/' + bank.name
    number = len(spec['files'])
    spec['files'].append({'name': name})
    for target in missing:
        targets[target].add(number)
    return name, info, str(bank.resolve())
