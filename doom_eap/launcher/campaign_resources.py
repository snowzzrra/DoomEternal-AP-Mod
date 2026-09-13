"""Enable only the installed room's new campaign streamfiles after idRehash.

The upstream rehasher updates container hashes but retains the vanilla resource
bitmap. Appended records remain disabled even when their mapresources entries
exist. Native OpenContainer/FindStreamFile apply that bitmap before name lookup.
"""
from __future__ import annotations

import hashlib
import mmap
from pathlib import Path
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


def _u32(data, offset):
    return struct.unpack_from("<I", data, offset)[0]


def _u64(data, offset):
    return struct.unpack_from("<Q", data, offset)[0]


def _records(path: Path, wanted: set[str]) -> dict:
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
            if size != decoded or offset + size > len(data):
                raise ValueError(f"Expected decoded post-inject resource: {name}")
            result[name] = (index, kind, offset, data[offset:offset + size], _u32(data, row + 112))
    if result.keys() != wanted:
        raise ValueError(f"Missing installed campaign resources: {sorted(wanted - result.keys())}")
    return result


def enable_campaign_resources(game_root: Path, mod_zip: Path) -> dict:
    """Finalize an injected room package; never enable unrelated resource records."""
    from doom_eap.launcher.launcher_platform import detect_doom_processes, publish_file

    with zipfile.ZipFile(mod_zip) as package:
        expected = {name: package.read("gameresources_patch2/" + name)
                    for name in STREAMFILES if "gameresources_patch2/" + name in package.namelist()}
    if not expected:
        return {"state": "not_applicable"}
    if expected.keys() != set(STREAMFILES):
        raise ValueError("Incomplete unified campaign resource package")
    if any(process["name"] == "doometernalx64vk.exe" for process in detect_doom_processes()):
        raise RuntimeError("Close DOOM before finalizing campaign resources")

    base = game_root / "base"
    records = _records(base / CONTAINER, set(STREAMFILES))
    for name, (_, kind, _, payload, flags) in records.items():
        if kind != "rs_streamfile" or flags != 0 or payload != expected[name]:
            raise ValueError(f"Installed campaign streamfile differs from the room package: {name}")

    meta_path = base / "meta.resources"
    original = meta_path.read_bytes()
    _, _, payload_at, mask, _ = _records(meta_path, {"generated/buildgame/container.mask"})[
        "generated/buildgame/container.mask"]
    # Resource checksum occupies four bytes; the native mask reader starts at +4.
    boundaries = {}
    position = 8
    for _ in range(_u32(mask, 4)):
        words = _u32(mask, position + 8)
        boundaries[position] = words
        position += 12 + words * 8
    if position != len(mask):
        raise ValueError("Invalid installed container mask table")
    offsets = {}
    for line in (base / "idRehash.map").read_text(encoding="utf-8-sig").splitlines():
        name, offset = line.rsplit(";", 1)
        name = name.replace("\\", "/").removeprefix("./")
        if name in offsets:
            raise ValueError("Duplicate idRehash container mapping")
        offsets[name] = int(offset)
    mask_at = offsets[CONTAINER]
    words = boundaries[mask_at]
    updated = bytearray(original)
    entries = []
    for name, (index, _, _, _, _) in records.items():
        if index >= words * 64:
            raise ValueError("Campaign records exceed the installed mask capacity; installation not finalized")
        offset, bit = payload_at + mask_at + 12 + index // 8, 1 << (index % 8)
        entries.append({"name": name, "index": index, "previously_enabled": bool(original[offset] & bit)})
        updated[offset] |= bit
    if updated != original:
        if meta_path.read_bytes() != original:
            raise RuntimeError("Container mask changed during campaign installation")
        with tempfile.NamedTemporaryFile(dir=base, prefix=".doomeap-mask-", delete=False) as temporary:
            temporary.write(updated)
            incoming = Path(temporary.name)
        try:
            publish_file(incoming, meta_path, operation="campaign_resource_mask")
        finally:
            incoming.unlink(missing_ok=True)
    if meta_path.read_bytes() != updated:
        raise RuntimeError("Campaign resource mask publication did not match readback")
    return {"state": "enabled", "container": CONTAINER, "entries": entries,
            "changed_bits": sum(not entry["previously_enabled"] for entry in entries),
            "meta_sha256": hashlib.sha256(updated).hexdigest()}
