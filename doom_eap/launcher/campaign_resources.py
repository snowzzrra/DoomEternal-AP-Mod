"""Enable only the installed room's new resources after idRehash.

The upstream rehasher updates container hashes but retains the vanilla resource
bitmap. Appended records remain disabled even when their mapresources entries
exist. Native OpenContainer/FindStreamFile apply that bitmap before name lookup.
"""
from __future__ import annotations

import hashlib
import json
import mmap
from pathlib import Path
import re
import struct
import tempfile
import zipfile

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
        raise ValueError("Installed game resource evidence is missing; reinstall the room package")
    actual = installed_resource_fingerprints(game_root)
    if actual != expected:
        changed = ", ".join(name for name, digest in actual.items() if expected.get(name) != digest)
        raise ValueError(f"Installed game files changed: {changed}; reinstall the room package")


def resource_mapping_lines(mask: bytes, mapping: str) -> list[str]:
    if len(mask) < 8:
        raise ValueError("Truncated native resource mask")
    offsets, position = set(), 8
    for _ in range(_u32(mask, 4)):
        if position + 12 > len(mask):
            raise ValueError("Truncated native resource mask")
        offsets.add(position)
        position += 12 + 8 * _u32(mask, position + 8)
    if position != len(mask):
        raise ValueError("Invalid native resource mask boundary")
    result = []
    for line in mapping.splitlines(keepends=True):
        name, offset = line.rsplit(";", 1)
        if int(offset) not in offsets:
            if re.fullmatch(r"ap_enemy_resources/[0-9a-f]{64}\.resources", name.replace("\\", "/")):
                continue
            raise ValueError("Invalid native resource mask mapping")
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
            raise ValueError(f"Unsupported campaign resource container: {path.name}")
        count, names_at, info_at = _u32(data, 32), _u64(data, 64), _u64(data, 80)
        ids = _u64(data, 96) + _u32(data, 40) * 4
        name_count = _u64(data, names_at)
        if info_at + count * 144 > len(data) or names_at + 8 + name_count * 8 > len(data):
            raise ValueError("Truncated resource index")
        names = []
        for index in range(name_count):
            start = names_at + 8 + name_count * 8 + _u64(data, names_at + 8 + index * 8)
            end = data.find(b"\0", start)
            if end < start:
                raise ValueError("Unterminated resource name")
            names.append(data[start:end].decode("utf-8"))
        for index in range(count):
            row = info_at + index * 144
            local_names = ids + _u64(data, row + 32) * 8
            name = names[_u64(data, local_names + _u64(data, row + 8) * 8)]
            if name not in wanted:
                continue
            if name in result:
                raise ValueError(f"Duplicate campaign resource: {name}")
            kind = names[_u64(data, local_names + _u64(data, row) * 8)]
            offset, size, decoded = (_u64(data, row + n) for n in (56, 64, 72))
            if (size != decoded and decode is None) or offset + size > len(data):
                raise ValueError(f"Expected decoded post-inject resource: {name}")
            payload = data[offset:offset + size]
            if size != decoded:
                payload = decode(payload, decoded)
                if len(payload) != decoded:
                    raise ValueError(f"Decoded resource length differs: {name}")
            result[name] = (index, kind, offset, payload, _u32(data, row + 112))
    if result.keys() != wanted:
        raise ValueError(f"Missing installed campaign resources: {sorted(wanted - result.keys())}")
    return result


def prepare_resource_activation(base: Path, targets: dict[str, set[int]]) -> tuple[dict, dict]:
    original = (base / "meta.resources").read_bytes()
    index, _, payload_at, mask, flags = _records(base / "meta.resources", {"generated/buildgame/container.mask"})[
        "generated/buildgame/container.mask"]
    if flags or len(mask) < 8:
        raise ValueError("Resource activation requires an uncompressed native container mask")
    original_mapping = (base / "idRehash.map").read_bytes()
    mapping, selected = [], {}
    lines = resource_mapping_lines(mask, original_mapping.decode("utf-8-sig"))
    for line in lines:
        name, offset = line.rsplit(";", 1)
        normalized = name.replace("\\", "/").removeprefix("./")
        if normalized in selected:
            raise ValueError("Duplicate idRehash container mapping")
        selected[normalized] = int(offset)
        mapping.append((name, int(offset), offset[len(offset.rstrip("\r\n")):]))
    if not targets.keys() <= selected.keys():
        raise ValueError("Resource container lacks an idRehash mapping")
    by_offset = {selected[name]: name for name in targets}
    if len(by_offset) != len(targets):
        raise ValueError("Ambiguous idRehash container mapping")
    updated_mask, offsets, tables = bytearray(mask[:8]), {}, {}
    position = 8
    for _ in range(_u32(mask, 4)):
        if position + 12 > len(mask):
            raise ValueError("Invalid native container mask table")
        words = _u32(mask, position + 8)
        end = position + 12 + words * 8
        if end > len(mask):
            raise ValueError("Invalid native container mask boundary")
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
        raise ValueError("Invalid native container mask mapping boundary")
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


def publish_resource_activation(base: Path, updates: dict[str, bytes], report: dict) -> None:
    from doom_eap.launcher.launcher_platform import publish_file

    originals = {name: (base / name).read_bytes() for name in updates}
    hashes = report.get("source_hashes")
    if hashes is None:
        hashes = {"meta.resources": report["source_meta_sha256"], "idRehash.map": report["source_rehash_sha256"]}
    if set(hashes) != set(originals) or any(hashlib.sha256(originals[name]).hexdigest() != value
                                          for name, value in hashes.items()):
        raise RuntimeError("Resource metadata changed during installation")
    incoming, published = {}, []
    try:
        for name, payload in updates.items():
            if payload != originals[name]:
                with tempfile.NamedTemporaryFile(dir=base, prefix=".doomeap-mask-", delete=False) as temporary:
                    incoming[name] = Path(temporary.name)
                    temporary.write(payload)
        for name, path in incoming.items():
            if (base / name).read_bytes() != originals[name]:
                raise RuntimeError("Resource metadata changed during installation")
            publish_file(path, base / name, operation="resource_activation")
            published.append(name)
        if any((base / name).read_bytes() != payload for name, payload in updates.items()):
            raise RuntimeError("Resource activation publication did not match readback")
    except Exception:
        for name in reversed(published):
            if (base / name).read_bytes() != updates[name]:
                raise RuntimeError("Resource metadata changed externally; rollback cannot overwrite it")
            incoming[name].write_bytes(originals[name])
            publish_file(incoming[name], base / name, operation="resource_activation_rollback")
        raise
    finally:
        for path in incoming.values():
            path.unlink(missing_ok=True)


def enable_campaign_resources(game_root: Path, mod_zip: Path) -> dict:
    """Finalize an injected room package; never enable unrelated resource records."""
    from doom_eap.launcher.launcher_platform import detect_doom_processes
    from doom_eap.launcher.native_enemy_resources import prepare_native_resource_banks

    with zipfile.ZipFile(mod_zip) as package:
        names = set(package.namelist())
        manifest = json.loads(package.read("seed_manifest.json")) if "seed_manifest.json" in names else {}
        required = set(STREAMFILES)
        if "fortress_economy_v1" in manifest.get("required_capabilities", ()):
            required.add(EXCLUDED_LAYER)
        expected = {name: package.read("gameresources_patch2/" + name)
                    for name in required if "gameresources_patch2/" + name in names}
        plan = manifest.get("options", {}).get("enemy_plan")
        roots = json.loads(package.read("seed_receipt.json")).get("native_resource_roots", {}) if "seed_receipt.json" in names else {}
    campaign_required = bool(expected) or "unified_campaign_v1" in manifest.get("required_capabilities", ()) or EXCLUDED_LAYER in required
    base = game_root / "base"
    if any(process["name"].casefold() == "doometernalx64vk.exe" for process in detect_doom_processes()):
        raise RuntimeError("Close DOOM before finalizing campaign resources")
    bank_updates, banks = prepare_native_resource_banks(base, plan, roots)
    from .resource_bank import obsolete_banks, cleanup_banks
    spec = json.loads(bank_updates.get('packagemapspec.json', (base / 'packagemapspec.json').read_bytes())) if (base / 'packagemapspec.json').is_file() else {'files': []}
    obsolete = obsolete_banks(base, spec)
    if not campaign_required and not bank_updates and not obsolete:
        return {"state": "not_applicable"}
    if campaign_required and expected.keys() != required:
        raise ValueError("Incomplete unified campaign resource package")
    records = _records(base / CONTAINER, required) if campaign_required else {}
    for name, (_, kind, _, payload, flags) in records.items():
        if kind != "rs_streamfile" or flags != 0 or payload != expected[name]:
            raise ValueError(f"Installed campaign streamfile differs from the room package: {name}")

    targets = {CONTAINER: {row[0] for row in records.values()}} if records else {}
    updates, report = prepare_resource_activation(base, targets) if targets else ({}, {"containers": {}})
    if bank_updates:
        report["source_hashes"] = {**({"meta.resources": report["source_meta_sha256"],
            "idRehash.map": report["source_rehash_sha256"]} if targets else {}), **banks["source_hashes"]}
        updates.update(bank_updates)
    if banks.get('consolidated') or obsolete:
        from .resource_bank import register_banks
        report.setdefault('source_hashes', {})
        for name in ('meta.resources', 'idRehash.map'):
            original = (base / name).read_bytes()
            expected_hash = banks['source_hashes'].get(name, hashlib.sha256(original).hexdigest())
            if hashlib.sha256(original).hexdigest() != expected_hash:
                raise RuntimeError('Resource metadata changed during bank consolidation')
            report['source_hashes'].setdefault(name, expected_hash)
            updates.setdefault(name, original)
        register_banks(base, banks.get('consolidated', {}), updates, obsolete)
        report['updated_meta_sha256'] = hashlib.sha256(updates['meta.resources']).hexdigest()
    table = report["containers"].get(CONTAINER, {"previously_enabled": {}, "changed_bits": 0, "added_mask_words": 0})
    entries = [{"name": name, "index": row[0], "previously_enabled": table["previously_enabled"][row[0]]}
               for name, row in records.items()]
    from .build_manifest import register_resource_manifest
    register_resource_manifest(base, banks.get("consolidated", {}), updates, report, obsolete)
    publish_resource_activation(base, updates, report)
    cleanup = cleanup_banks(base, obsolete) if obsolete else {'removed': [], 'bytes_freed': 0, 'errors': []}
    return {"state": "enabled", "container": CONTAINER if records else None, "entries": entries,
            "changed_bits": table["changed_bits"], "added_mask_words": table["added_mask_words"],
            "native_enemy_banks": banks["maps"], "meta_sha256": report.get("updated_meta_sha256"), "bank_cleanup": cleanup}
