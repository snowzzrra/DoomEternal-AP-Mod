"""Load frozen bodies through the native resource closure of their donor maps."""

import hashlib
import json
from collections import Counter
from importlib.resources import files
from pathlib import PurePosixPath


_MARKER = "doomEAPEnemyResourceBanks"


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


def prepare_native_resource_banks(base, plan):
    requested = native_resource_maps(plan)
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
    by_map = {}
    for ref in spec["mapFileRefs"]:
        by_map.setdefault(ref["map"], set()).add(ref["file"])
    donor_files, targets, consolidated = set(), {}, {}
    masks = None
    shared = None
    source_hashes = {path.name: hashlib.sha256(original).hexdigest()}
    for target, donors in requested.items():
        if target not in maps or not donors <= maps.keys():
            raise ValueError("Native enemy resource map is absent from the installation")
        destination = by_map.get(maps[target], set())
        selected = set()
        for donor in donors:
            candidates = by_map.get(maps[donor], set())
            if not candidates:
                raise ValueError("Native enemy resource donor has no banks")
            for index in candidates:
                value = spec["files"][index]
                name = value.get("name") if isinstance(value, dict) else None
                if not isinstance(name, str):
                    raise ValueError("Invalid native resource bank")
                bank = PurePosixPath(name)
                if "\\" in name or bank.is_absolute() or ".." in bank.parts or ":" in name:
                    raise ValueError("Invalid native resource bank path")
                if bank.suffix not in {".resources", ".streamdb"}:
                    continue
                source = base.joinpath(*bank.parts)
                if not source.is_file() or not source.stat().st_size:
                    raise ValueError(f"Missing native enemy resource bank: {name}")
                if index not in destination:
                    selected.add(index)
        targets[target] = selected
    for target, selected in targets.items():
        destination = by_map.get(maps[target], set())
        common = by_map.get(maps.get('common'), set())
        closure = destination | common | selected
        names = {spec['files'][index]['name'] for index in closure
                 if PurePosixPath(spec['files'][index]['name']).suffix in {'.resources', '.streamdb'}}
        if len(names) > 64:
            from .resource_bank import ResourceIndex, consolidate_bank, read_masks
            if masks is None:
                source_hashes.update({name: hashlib.sha256((base / name).read_bytes()).hexdigest()
                                      for name in ('meta.resources', 'idRehash.map')})
                masks = read_masks(base)
                from .resource_bank import shared_donor_bank
                shared = shared_donor_bank(base, spec, requested, by_map, maps, masks)
            resources = [index for index in sorted(selected, reverse=True)
                         if spec['files'][index]['name'].endswith('.resources')]
            native = [index for index in sorted(destination | common, reverse=True)
                      if spec['files'][index]['name'].endswith('.resources')]
            if not resources or not native:
                raise ValueError('Native resource closure cannot fit the database limit')
            if shared is not None:
                bank, info = shared
            else:
                paths = {index: base / spec['files'][index]['name'] for index in native + resources}
                enabled = {str(path): masks[spec['files'][index]['name']] for index, path in paths.items()}
                excluded = set()
                for index in native:
                    bits = enabled[str(paths[index])]
                    for name, (_, _, number) in ResourceIndex(paths[index]).records:
                        if number // 8 < len(bits) and bits[number // 8] & (1 << (number % 8)):
                            excluded.add(name)
                smallest = min(native, key=lambda index: paths[index].stat().st_size)
                bank, info = consolidate_bank(paths[smallest], [paths[index] for index in resources], excluded,
                                              base / 'ap_enemy_resources', enabled)
            name = bank.relative_to(base).as_posix()
            known = name in consolidated
            consolidated[name] = info
            selected.difference_update(resources)
            if known:
                selected.add(next(i for i, value in enumerate(spec['files']) if value['name'] == name))
            else:
                selected.add(len(spec['files']))
                spec['files'].append({'name': name})
            closure_names = {spec['files'][index]['name'] for index in destination | common | selected
                             if PurePosixPath(spec['files'][index]['name']).suffix in {'.resources', '.streamdb'}}
            if len(closure_names) > 64:
                raise ValueError('Native enemy resource closure exceeds the database limit')
        targets[target] = sorted(selected)
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
