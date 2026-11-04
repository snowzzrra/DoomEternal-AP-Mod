"""Enable only the installed room's new resources after idRehash.

The upstream rehasher updates container hashes but retains the vanilla resource
bitmap. Appended records remain disabled even when their mapresources entries
exist. Native OpenContainer/FindStreamFile apply that bitmap before name lookup.
"""
from __future__ import annotations

import hashlib
import json
import mmap
import re
import struct
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from doom_eap.launcher.ui.strings import text as player_text

from .launcher_platform import installation_step

CONTAINER = "gameresources_patch2.resources"
DECLS = (
    "devmenuoption/devmenuoption/ap_unified_campaign.decl",
    "missionselectinfolist/missionlist_ap_unified.decl",
    *(f"layer/game/sp/hub/ap_phase_{phase}.decl" for phase in range(1, 8)),
)
STREAMFILES = tuple("generated/decls/" + name for name in DECLS)
EXCLUDED_LAYER = "generated/decls/layer/game/sp/hub/ap_excluded.decl"


def installed_resource_fingerprints(game_root: Path) -> dict[str, str]:
    names = ("DOOMEternalx64vk.exe", "base/meta.resources", "base/idRehash.map", "base/packagemapspec.json",
             "base/build-manifest.bin")
    result = {}
    for name in names:
        with (game_root / name).open("rb") as source:
            result[name] = hashlib.file_digest(source, "sha256").hexdigest()
    return result


def verify_installed_resources(game_root: Path, receipt: dict) -> None:
    expected = receipt.get("installed_resources")
    if not isinstance(expected, dict) or not expected:
        raise ValueError(player_text('launcher.campaign_resources', 'installed_game_resource_evidence_is_missing_reinstall_the_room_p'))
    actual = installed_resource_fingerprints(game_root)
    if actual != expected:
        changed = ", ".join(name for name, digest in actual.items() if expected.get(name) != digest)
        raise ValueError(player_text('launcher.campaign_resources', 'installed_game_files_changed_value_0_reinstall_the_room_package', value_0=f'{changed}'))


def resource_mapping_lines(mask: bytes, mapping: str) -> list[str]:
    if len(mask) < 8:
        raise ValueError(player_text('launcher.campaign_resources', 'truncated_native_resource_mask'))
    offsets, position = set(), 8
    for _ in range(_u32(mask, 4)):
        if position + 12 > len(mask):
            raise ValueError(player_text('launcher.campaign_resources', 'truncated_native_resource_mask'))
        offsets.add(position)
        position += 12 + 8 * _u32(mask, position + 8)
    if position != len(mask):
        raise ValueError(player_text('launcher.campaign_resources', 'invalid_native_resource_mask_boundary'))
    result = []
    for line in mapping.splitlines(keepends=True):
        name, offset = line.rsplit(";", 1)
        if int(offset) not in offsets:
            if re.fullmatch(r"ap_enemy_resources/[0-9a-f]{64}\.resources", name.replace("\\", "/")):
                continue
            raise ValueError(player_text('launcher.campaign_resources', 'invalid_native_resource_mask_mapping'))
        result.append(line)
    return result


def _u32(data, offset):
    return struct.unpack_from("<I", data, offset)[0]


def _u64(data, offset):
    return struct.unpack_from("<Q", data, offset)[0]


def _records(path: Path, wanted: set[str], *, decode=None) -> dict:
    result = {}
    with path.open("rb") as source, mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as data:
        if data[:4] != b"IDCL" or _u32(data, 4) != 12:
            raise ValueError(player_text('launcher.campaign_resources', 'unsupported_campaign_resource_container_value_0', value_0=f'{path.name}'))
        count, names_at, info_at = _u32(data, 32), _u64(data, 64), _u64(data, 80)
        ids = _u64(data, 96) + _u32(data, 40) * 4
        name_count = _u64(data, names_at)
        if info_at + count * 144 > len(data) or names_at + 8 + name_count * 8 > len(data):
            raise ValueError(player_text('launcher.campaign_resources', 'truncated_resource_index'))
        names = []
        for index in range(name_count):
            start = names_at + 8 + name_count * 8 + _u64(data, names_at + 8 + index * 8)
            end = data.find(b"\0", start)
            if end < start:
                raise ValueError(player_text('launcher.campaign_resources', 'unterminated_resource_name'))
            names.append(data[start:end].decode("utf-8"))
        for index in range(count):
            row = info_at + index * 144
            local_names = ids + _u64(data, row + 32) * 8
            name = names[_u64(data, local_names + _u64(data, row + 8) * 8)]
            if name not in wanted:
                continue
            if name in result:
                raise ValueError(player_text('launcher.campaign_resources', 'duplicate_campaign_resource_value_0', value_0=f'{name}'))
            kind = names[_u64(data, local_names + _u64(data, row) * 8)]
            offset, size, decoded = (_u64(data, row + n) for n in (56, 64, 72))
            if (size != decoded and decode is None) or offset + size > len(data):
                raise ValueError(player_text('launcher.campaign_resources', 'expected_decoded_post_inject_resource_value_0', value_0=f'{name}'))
            payload = data[offset:offset + size]
            if size != decoded:
                payload = decode(payload, decoded)
                if len(payload) != decoded:
                    raise ValueError(player_text('launcher.campaign_resources', 'decoded_resource_length_differs_value_0', value_0=f'{name}'))
            result[name] = (index, kind, offset, payload, _u32(data, row + 112))
    if result.keys() != wanted:
        raise ValueError(player_text('launcher.campaign_resources', 'missing_installed_campaign_resources_value_0', value_0=f'{sorted(wanted - result.keys())}'))
    return result


def prepare_resource_activation(base: Path, targets: dict[str, set[int]]) -> tuple[dict, dict]:
    original = (base / "meta.resources").read_bytes()
    index, _, payload_at, mask, flags = _records(base / "meta.resources", {"generated/buildgame/container.mask"})[
        "generated/buildgame/container.mask"]
    if flags or len(mask) < 8:
        raise ValueError(player_text('launcher.campaign_resources', 'resource_activation_requires_an_uncompressed_native_container_ma'))
    original_mapping = (base / "idRehash.map").read_bytes()
    mapping, selected = [], {}
    lines = resource_mapping_lines(mask, original_mapping.decode("utf-8-sig"))
    for line in lines:
        name, offset = line.rsplit(";", 1)
        normalized = name.replace("\\", "/").removeprefix("./")
        if normalized in selected:
            raise ValueError(player_text('launcher.campaign_resources', 'duplicate_idrehash_container_mapping'))
        selected[normalized] = int(offset)
        mapping.append((name, int(offset), offset[len(offset.rstrip("\r\n")):]))
    if not targets.keys() <= selected.keys():
        raise ValueError(player_text('launcher.campaign_resources', 'resource_container_lacks_an_idrehash_mapping'))
    by_offset = {selected[name]: name for name in targets}
    if len(by_offset) != len(targets):
        raise ValueError(player_text('launcher.campaign_resources', 'ambiguous_idrehash_container_mapping'))
    updated_mask, offsets, tables = bytearray(mask[:8]), {}, {}
    position = 8
    for _ in range(_u32(mask, 4)):
        if position + 12 > len(mask):
            raise ValueError(player_text('launcher.campaign_resources', 'invalid_native_container_mask_table'))
        words = _u32(mask, position + 8)
        end = position + 12 + words * 8
        if end > len(mask):
            raise ValueError(player_text('launcher.campaign_resources', 'invalid_native_container_mask_boundary'))
        offsets[position] = len(updated_mask)
        bits = bytearray(mask[position + 12:end])
        if position in by_offset:
            name = by_offset[position]
            indices = targets[name]
            added = max(0, (max(indices) + 64) // 64 - words) if indices else 0
            bits.extend(b"\0" * (8 * added))
            previous = {value: bool(bits[value // 8] & (1 << (value % 8))) for value in indices}
            for value in indices:
                bits[value // 8] |= 1 << (value % 8)
            tables[name] = {"previously_enabled": previous, "added_mask_words": added,
                            "changed_bits": sum(not value for value in previous.values())}
        updated_mask.extend(mask[position:position + 8])
        updated_mask.extend(struct.pack("<I", len(bits) // 8))
        updated_mask.extend(bits)
        position = end
    if position != len(mask) or set(tables) != set(targets) or any(offset not in offsets for _, offset, _ in mapping):
        raise ValueError(player_text('launcher.campaign_resources', 'invalid_native_container_mask_mapping_boundary'))
    updated = bytearray(original)
    incoming_at = payload_at
    if len(updated_mask) != len(mask) and payload_at + len(mask) != len(original):
        incoming_at = (len(updated) + 15) & ~15
        updated.extend(b"\0" * (incoming_at - len(updated)))
    updated[incoming_at:incoming_at + len(updated_mask)] = updated_mask
    info_at = _u64(original, 80) + 144 * index
    struct.pack_into("<QQQ", updated, info_at + 56, incoming_at, len(updated_mask), len(updated_mask))
    encoding = "utf-8-sig" if original_mapping.startswith(b"\xef\xbb\xbf") else "utf-8"
    updated_mapping = "".join(lines).encode(encoding)
    if any(offsets[offset] != offset for _, offset, _ in mapping):
        updated_mapping = "".join(f"{name};{offsets[offset]}{ending}" for name, offset, ending in mapping).encode(encoding)
    return {"meta.resources": bytes(updated), "idRehash.map": updated_mapping}, {
        "containers": tables, "source_meta_sha256": hashlib.sha256(original).hexdigest(),
        "source_rehash_sha256": hashlib.sha256(original_mapping).hexdigest(),
        "updated_meta_sha256": hashlib.sha256(updated).hexdigest(),
    }


def publish_resource_activation(base, updates, report, *, journal=None):
    if journal is not None:
        for name, payload in updates.items():
            journal.replace(base / name, payload, operation='resource_activation')
        return
    _publish_resource_activation(base, updates, report)


def _activation_originals(base, updates, report):
    originals = {name: (base / name).read_bytes() for name in updates}
    hashes = report.get("source_hashes")
    if hashes is None:
        hashes = {"meta.resources": report["source_meta_sha256"], "idRehash.map": report["source_rehash_sha256"]}
    if set(hashes) != set(originals) or any(hashlib.sha256(originals[name]).hexdigest() != value
                                          for name, value in hashes.items()):
        raise RuntimeError(player_text('launcher.campaign_resources', 'resource_metadata_changed_during_installation'))
    return originals


def _publish_resource_activation(base, updates, report):
    from .launcher_platform import publish_file

    originals = _activation_originals(base, updates, report)
    incoming, published = {}, []
    try:
        for name, payload in updates.items():
            if payload != originals[name]:
                with tempfile.NamedTemporaryFile(dir=base, prefix=".doomeap-mask-", delete=False) as temporary:
                    incoming[name] = Path(temporary.name)
                    temporary.write(payload)
        for name, path in incoming.items():
            if (base / name).read_bytes() != originals[name]:
                raise RuntimeError(player_text('launcher.campaign_resources', 'resource_metadata_changed_during_installation'))
            published.append(name)
            publish_file(path, base / name, operation="resource_activation")
        if any((base / name).read_bytes() != payload for name, payload in updates.items()):
            raise RuntimeError(player_text('launcher.campaign_resources', 'resource_activation_publication_did_not_match_readback'))
    except Exception as error:
        _restore_resource_activation(base, updates, originals, incoming, published, error)
        raise
    finally:
        for path in incoming.values():
            path.unlink(missing_ok=True)


def _restore_resource_activation(base, updates, originals, incoming, published, error):
    from .launcher_platform import publish_file

    for name in reversed(published):
        actual = (base / name).read_bytes()
        if actual == originals[name]:
            continue
        if actual != updates[name]:
            raise RuntimeError(player_text('launcher.campaign_resources', 'resource_metadata_changed_externally_rollback_cannot_overwrite_i')) from error
        incoming[name].write_bytes(originals[name])
        publish_file(incoming[name], base / name, operation="resource_activation_rollback")


def _cleanup_resource_banks(base, obsolete):
    from .resource_bank import cleanup_banks

    return cleanup_banks(base, obsolete) if obsolete else {'removed': [], 'bytes_freed': 0, 'errors': []}


def room_enemy_plan(options):
    if not options:
        return None
    from doom_eap.runtime.context_registry import admit_slot_data, load_slot_catalog
    from tools.maps.native_recipe_materializer import expand_resolved_native_recipes

    plan = expand_resolved_native_recipes(admit_slot_data(options), load_slot_catalog())
    return {"kind": "native_recipes", **plan}


@installation_step(player_text('launcher.campaign_resources', 'prepare_banks'))
def plan_campaign_resources(game_root: Path, mod_zip: Path, staging: Path) -> dict:
    """Finalize an injected room package; never enable unrelated resource records."""
    from doom_eap.launcher.native_enemy_resources import prepare_native_resource_banks

    from .install_journal import checked_path

    checked_path(staging)
    manifest, required, expected, huds, plan, roots = _room_resources(mod_zip)
    campaign_required = _campaign_required(manifest, expected, required)
    base = game_root / "base"
    bank_updates, banks = prepare_native_resource_banks(base, plan, roots, staging=staging,
        hub_world_text='unified_campaign_v1' in manifest.get('required_capabilities', ()))
    from .resource_bank import obsolete_banks
    spec = _planned_spec(base, bank_updates)
    obsolete = obsolete_banks(base, spec)
    if not campaign_required and not huds and not bank_updates and not obsolete:
        return {"result": {"state": "not_applicable"}, "updates": {}, "banks": banks, "obsolete": {}}
    targets, records = _activation_targets(base, campaign_required, expected, required, huds, spec)
    updates, report = prepare_resource_activation(base, targets) if targets else ({}, {"containers": {}})
    if bank_updates:
        report["source_hashes"] = {**({"meta.resources": report["source_meta_sha256"],
            "idRehash.map": report["source_rehash_sha256"]} if targets else {}), **banks["source_hashes"]}
        updates.update(bank_updates)
    bank_paths = _bank_paths(banks)
    _register_resource_banks(base, banks, updates, report, obsolete, bank_paths)
    table = report["containers"].get(CONTAINER, {"previously_enabled": {}, "changed_bits": 0, "added_mask_words": 0})
    entries = [{"name": name, "index": row[0], "previously_enabled": table["previously_enabled"][row[0]]}
               for name, row in records.items()]
    from .build_manifest import register_resource_manifest
    with installation_step("manifest"):
        register_resource_manifest(base, banks.get("consolidated", {}), updates, report, obsolete, bank_paths=bank_paths)
    report.setdefault("source_hashes", {"meta.resources": report.get("source_meta_sha256"),
                                         "idRehash.map": report.get("source_rehash_sha256")})
    staged = _stage_resource_metadata(staging, updates, report)
    result = {"state": "enabled", "container": CONTAINER if records else None, "entries": entries,
              "changed_bits": table["changed_bits"], "added_mask_words": table["added_mask_words"],
              "native_enemy_banks": banks["maps"], "meta_sha256": report.get("updated_meta_sha256")}
    return {"result": result, "updates": staged, "banks": banks, "obsolete": obsolete}


def _campaign_required(manifest, expected, required):
    return bool(expected) or "unified_campaign_v1" in manifest.get("required_capabilities", ()) or EXCLUDED_LAYER in required


def _planned_spec(base, updates):
    path = base / 'packagemapspec.json'
    return json.loads(updates.get(path.name, path.read_bytes())) if path.is_file() else {'files': []}


def _bank_paths(banks):
    return {name: Path(value) for name, value in banks.get('bank_paths', {}).items()}


def _room_resources(mod_zip):
    with zipfile.ZipFile(mod_zip) as package:
        names = set(package.namelist())
        manifest = json.loads(package.read("seed_manifest.json")) if "seed_manifest.json" in names else {}
        required = set(STREAMFILES)
        if "fortress_economy_v1" in manifest.get("required_capabilities", ()):
            required.add(EXCLUDED_LAYER)
        expected = {name: package.read("gameresources_patch2/" + name)
                    for name in required if "gameresources_patch2/" + name in names}
        hud_name = "swf/hud/hud_score.swf"
        huds = {name.split('/')[0]: package.read(name) for name in names
                if name.count('/') == 3 and name.endswith('/' + hud_name)}
        plan = room_enemy_plan(manifest.get("options"))
        roots =json.loads(package.read("seed_receipt.json")).get("native_resource_roots", {}) if "seed_receipt.json" in names else {}
    return manifest, required, expected, huds, plan, roots


def _activation_targets(base, campaign_required, expected, required, huds, spec):
    if campaign_required and expected.keys() != required:
        raise ValueError(player_text('launcher.campaign_resources', 'incomplete_unified_campaign_resource_package'))
    records = _records(base / CONTAINER, required) if campaign_required else {}
    for name, (_, kind, _, payload, flags) in records.items():
        if kind != "rs_streamfile" or flags != 0 or payload != expected[name]:
            raise ValueError(player_text('launcher.campaign_resources', 'installed_campaign_streamfile_differs_from_the_room_package_valu', value_0=f'{name}'))

    hud_name = "swf/hud/hud_score.swf"
    targets = {CONTAINER: {row[0] for row in records.values()}} if records else {}
    _hud_targets(base, huds, spec, targets, hud_name)
    return targets, records


def _register_resource_banks(base, banks, updates, report, obsolete, bank_paths):
    if banks.get('consolidated') or obsolete:
        from .resource_bank import register_banks
        with installation_step(player_text('launcher.campaign_resources', 'register_banks')):
            report.setdefault('source_hashes', {})
            for name in ('meta.resources', 'idRehash.map'):
                original = (base / name).read_bytes()
                expected_hash = banks['source_hashes'].get(name, hashlib.sha256(original).hexdigest())
                if hashlib.sha256(original).hexdigest() != expected_hash:
                    raise RuntimeError(player_text('launcher.campaign_resources', 'resource_metadata_changed_during_bank_consolidation'))
                report['source_hashes'].setdefault(name, expected_hash)
                updates.setdefault(name, original)
            register_banks(base, banks.get('consolidated', {}), updates, obsolete, bank_paths=bank_paths)
        report['updated_meta_sha256'] = hashlib.sha256(updates['meta.resources']).hexdigest()


def publish_campaign_resources(game_root, plan, staging, *, journal=None):
    from .install_journal import checked_path, file_digest
    from .native_enemy_resources import publish_native_resource_banks

    base = game_root / 'base'
    updates = {}
    for name, source in plan['banks']['bank_paths'].items():
        if checked_path(Path(source)) != checked_path(staging / Path(name).name):
            raise ValueError(player_text('launcher.campaign_resources', 'invalid_staged_resource_bank_path'))
    for name, info in plan['updates'].items():
        if name not in {'meta.resources', 'idRehash.map', 'packagemapspec.json', 'build-manifest.bin'}:
            raise ValueError(player_text('launcher.campaign_resources', 'invalid_resource_metadata_target'))
        path = checked_path(staging / name)
        if file_digest(path) != info['sha256']:
            raise ValueError(player_text('launcher.campaign_resources', 'staged_resource_metadata_differs'))
        if file_digest(checked_path(base / name)) not in {info['before'], info['sha256']}:
            raise RuntimeError(player_text('launcher.campaign_resources', 'resource_metadata_changed_during_installation'))
        updates[name] = path.read_bytes()
    _verify_bank_registration(base, plan, staging)
    with installation_step('publish'):
        publish_native_resource_banks(base, plan['banks'], journal=journal)
        report = {'source_hashes': {name: info['before'] for name, info in plan['updates'].items()}}
        if updates:
            publish_resource_activation(base, updates, report, journal=journal)


def verify_campaign_resources(game_root, plan):
    from .install_journal import checked_path, file_digest
    from .native_enemy_resources import verify_native_resource_banks

    base = game_root / 'base'
    for name, info in plan['updates'].items():
        if file_digest(checked_path(base / name)) != info['sha256']:
            raise ValueError(player_text('launcher.campaign_resources', 'installed_resource_metadata_differs') + name)
    verify_native_resource_banks(base, plan['banks'])
    _verify_bank_registration(base, plan, base)


def _verify_bank_registration(base, plan, staging):
    from .resource_bank import verify_bank_registration

    if not plan['banks'].get('consolidated'):
        return
    paths = {name: staging / name if name in plan['updates'] else base / name
             for name in ('meta.resources', 'idRehash.map', 'packagemapspec.json')}
    bank_paths = ({name: base / name for name in plan['banks']['consolidated']} if staging == base
                  else _bank_paths(plan['banks']))
    verify_bank_registration(paths['meta.resources'], paths['idRehash.map'].read_text(encoding='utf-8-sig'),
                             json.loads(paths['packagemapspec.json'].read_bytes()), plan['banks'], bank_paths=bank_paths)


def enable_campaign_resources(game_root: Path, mod_zip: Path, *, journal=None) -> dict:
    from .launcher_platform import require_doom_closed

    require_doom_closed(player_text('launcher.campaign_resources', 'finalizing_campaign_resources'))
    if journal is None:
        with tempfile.TemporaryDirectory(dir=game_root, prefix='.doomeap-resources-') as directory:
            staging = Path(directory)
            plan = plan_campaign_resources(game_root, mod_zip, staging)
            publish_campaign_resources(game_root, plan, staging)
            verify_campaign_resources(game_root, plan)
            return {**plan['result'], 'obsolete_banks': plan['obsolete'], 'banks': plan['banks']}
    staging = journal.directory / journal.transaction_id / 'resources'
    plan = journal.document['evidence'].get('resource_plan')
    if plan is None:
        plan = plan_campaign_resources(game_root, mod_zip, staging)
        journal.document['evidence']['resource_plan'] = plan
        journal.save()
    publish_campaign_resources(game_root, plan, staging, journal=journal)
    verify_campaign_resources(game_root, plan)
    return {**plan['result'], 'obsolete_banks': plan['obsolete'], 'banks': plan['banks']}


def _hud_targets(base, huds, spec, targets, hud_name):
    for container, hud in huds.items():
        containers = {row['name'] for row in spec['files'] if PurePosixPath(row['name']).name == container + '.resources'}
        if container == 'hub' or container.startswith('hub_patch'):
            containers = {'game/hub/' + container + '.resources'}
        if len(containers) != 1:
            raise ValueError(player_text('launcher.campaign_resources', 'ambiguous_or_missing_hud_resource_container') + container)
        bank, = containers
        index, kind, _, payload, flags = _records(base / bank, {hud_name})[hud_name]
        if kind != "cswf" or flags or payload != hud:
            raise ValueError(player_text('launcher.campaign_resources', 'installed_hud_differs_from_the_room_package') + container)
        targets.setdefault(bank, set()).add(index)


def _stage_resource_metadata(staging, updates, report):
    from .launcher_platform import _atomic_write_bytes

    staged = {}
    staging.mkdir(parents=True, exist_ok=True)
    for name, payload in updates.items():
        if hashlib.sha256(payload).hexdigest() == report['source_hashes'][name]:
            continue
        path = staging / name
        _atomic_write_bytes(path, payload)
        staged[name] = {"sha256": hashlib.sha256(payload).hexdigest(),
                        "before": report["source_hashes"][name]}
    return staged


def cleanup_resource_staging(journal, plan):
    from .install_journal import checked_path, file_digest

    staging = checked_path(journal.directory / journal.transaction_id / 'resources')
    expected = {name: info['sha256'] for name, info in plan['updates'].items()}
    expected.update({Path(name).name: info['identity'] for name, info in plan['banks']['consolidated'].items()})
    for name, digest in expected.items():
        path = checked_path(staging / name)
        if not path.is_relative_to(staging):
            raise ValueError(player_text('launcher.campaign_resources', 'invalid_staged_resource_path'))
        if path.exists():
            if file_digest(path) != digest:
                raise OSError(player_text('launcher.campaign_resources', 'staged_resources_changed_before_cleanup'))
            path.unlink()
    if staging.exists() and not any(staging.iterdir()):
        staging.rmdir()
