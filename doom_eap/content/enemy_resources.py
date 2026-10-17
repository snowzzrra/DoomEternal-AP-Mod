from __future__ import annotations

from collections import defaultdict
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import struct
import zipfile

from doom_eap.content.enemy_options import DLC_SPECIES
from doom_eap.launcher.campaign_resources import _records, prepare_resource_activation


CATALOG = Path(__file__).resolve().parents[2] / "content" / "catalog" / "enemy_resources.json"


def _load_enemy_resource_catalog(catalog_path: Path) -> dict:
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    if catalog["revision"] != 1:
        raise ValueError("Unsupported enemy resource catalog revision")
    return catalog


def required_enemy_resource_species(plan, *, catalog_path: Path = CATALOG) -> set[str]:
    profiles = _load_enemy_resource_catalog(catalog_path)["species"]
    species_ids = {instance["species"]
                   for map_key, row in (plan or {}).get("maps", {}).items()
                   for operation in row["operations"] for wave in operation["waves"] for instance in wave["instances"]
                   if instance["ai"]["map_key"] != map_key}
    return {species_id for species_id in species_ids if species_id in DLC_SPECIES or species_id in profiles}


def load_enemy_resource_profile(species_id: str, *, catalog_path: Path = CATALOG) -> dict:
    catalog = _load_enemy_resource_catalog(catalog_path)
    try:
        return catalog["species"][species_id]
    except KeyError as exc:
        raise ValueError(f"No local resource profile for {species_id}") from exc


def _verify(payload: bytes, record: dict, name: str) -> None:
    if len(payload) != record["size"] or hashlib.sha256(payload).hexdigest() != record["sha256"]:
        raise ValueError(f"Enemy resource differs from the catalog: {name}")


def _resource_sources(profile: dict, variants: dict[str, str] | None) -> dict:
    variants = {**profile.get("variants", {}), **(variants or {})}
    result = {}
    for asset in profile["assets"]:
        sources = asset["sources"]
        if len(sources) > 1:
            selected = variants.get(asset["name"])
            source = next((source for source in sources if source["bank"] == selected), None)
            if source is None:
                raise ValueError(f"Select an explicit native variant for {asset['name']}")
        else:
            source = sources[0]
        result[asset["name"]] = (asset, source)
    return result


def _assetsinfo_payload(profile: dict) -> bytes:
    assets_info = []
    # binary filenames can match a declaration's map resource name.
    for asset in sorted(profile["assets"], key=lambda asset: asset["metadata"]["resource_type"] == "rs_streamfile"):
        metadata = asset["metadata"]
        # installed loader reads this JSON number as a signed Int64.
        stream_hash = metadata["stream_hash"]
        if stream_hash >= 1 << 63:
            stream_hash -= 1 << 64
        assets_info.append({
            "name": metadata["map_resource_name"] or asset["name"],
            "resourceType": metadata["resource_type"],
            "mapResourceType": metadata["map_resource_type"],
            "streamDbHash": stream_hash,
            "version": metadata["version"],
            **{f"specialByte{index + 1}": value
               for index, value in enumerate(metadata["special_bytes"])},
        })
    return (json.dumps({"assets": assets_info}, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _payload_records(species_id: str, profile: dict) -> dict:
    container = profile["resource_container"]
    records = {f"{container}/{name}": source
               for name, (_, source) in _resource_sources(profile, None).items()}
    records.update({stream["name"]: stream for stream in profile["streams"]})
    metadata = _assetsinfo_payload(profile)
    records[f"{container}/EternalMod/assetsinfo/{species_id}.json"] = {
        "size": len(metadata), "sha256": hashlib.sha256(metadata).hexdigest()}
    return records


def embed_enemy_resource_payloads(
    members: dict[str, bytes], manifest: dict, resources: dict[str, dict[str, bytes]], *, catalog_path: Path = CATALOG,
) -> None:
    staged_members, staged_species = {}, {}
    for species_id, payloads in sorted(resources.items()):
        profile = load_enemy_resource_profile(species_id, catalog_path=catalog_path)
        expected = _payload_records(species_id, profile)
        if set(payloads) != set(expected):
            raise ValueError(f"Enemy resource payload is incomplete: {species_id}")
        snapshots = {}
        for name, record in sorted(expected.items()):
            payload = payloads[name]
            _verify(payload, record, name)
            member = f"enemy_resources/{species_id}/{name}"
            if member in members:
                raise ValueError(f"Enemy resource snapshot already exists: {member}")
            staged_members[member] = payload
            snapshots[name] = {"member": member, "size": len(payload), "sha256": record["sha256"]}
        staged_species[species_id] = {"members": snapshots}
    existing = manifest.get("enemy_resources", {})
    if set(existing) & set(staged_species):
        raise ValueError("Enemy resource species is already embedded")
    members.update(staged_members)
    manifest["enemy_resources"] = {**existing, **staged_species}


def read_enemy_resource_payloads(
    payload_resource: Path, manifest: dict, species_ids, *, catalog_path: Path = CATALOG,
) -> dict[str, bytes]:
    requested = sorted(set(species_ids))
    if not requested:
        return {}
    snapshots = {}
    for species_id in requested:
        profile = load_enemy_resource_profile(species_id, catalog_path=catalog_path)
        expected = _payload_records(species_id, profile)
        packaged = manifest.get("enemy_resources", {}).get(species_id, {}).get("members", {})
        if set(packaged) != set(expected):
            raise ValueError(f"Enemy resource snapshot is incomplete: {species_id}")
        for name, record in expected.items():
            member = f"enemy_resources/{species_id}/{name}"
            snapshot = {"member": member, "size": record["size"], "sha256": record["sha256"]}
            if packaged[name] != snapshot:
                raise ValueError(f"Enemy resource snapshot identity drifted: {name}")
            snapshots[member] = (name, record)
    result = {}
    with zipfile.ZipFile(payload_resource) as archive:
        entries = [entry for entry in archive.infolist() if entry.filename in snapshots]
        if len(entries) != len(snapshots) or len({entry.filename for entry in entries}) != len(snapshots):
            raise ValueError("Enemy resource archive has missing or duplicate snapshots")
        for entry in entries:
            name, record = snapshots[entry.filename]
            payload = archive.read(entry)
            _verify(payload, record, name)
            if name in result and result[name] != payload:
                raise ValueError(f"Enemy resource payloads conflict: {name}")
            result[name] = payload
    return result


def build_enemy_resources(
    game_base: Path, species_id: str, *, variants: dict[str, str] | None = None,
    decode=None, catalog_path: Path = CATALOG,
) -> dict[str, bytes]:
    """Read verified native payloads; profile qualification records unresolved native dependencies."""
    profile = load_enemy_resource_profile(species_id, catalog_path=catalog_path)
    container = profile["resource_container"]
    by_bank = defaultdict(dict)
    for name, (asset, source) in _resource_sources(profile, variants).items():
        by_bank[source["bank"]][asset["name"]] = (asset, source)

    payloads = {}
    for bank, assets in sorted(by_bank.items()):
        records = _records(game_base / bank, set(assets), decode=decode)
        for name, (asset, source) in sorted(assets.items()):
            _, kind, _, payload, _ = records[name]
            if kind != asset["metadata"]["resource_type"]:
                raise ValueError(f"Enemy resource type differs from the catalog: {name}")
            _verify(payload, source, name)
            payloads[f"{container}/{name}"] = payload

    payloads[f"{container}/EternalMod/assetsinfo/{species_id}.json"] = _assetsinfo_payload(profile)

    with ExitStack() as stack:
        streams = {}
        for bank, source in profile["stream_sources"].items():
            stream = stack.enter_context((game_base / bank).open("rb"))
            index = stream.read(source["index_size"])
            if hashlib.sha256(index).hexdigest() != source["index_sha256"]:
                raise ValueError(f"Enemy StreamDB index differs from the catalog: {bank}")
            streams[bank] = stream
        for wrapper in profile["streams"]:
            chunks = []
            for index, chunk in enumerate(wrapper["chunks"]):
                if chunk["file_id"] != wrapper["first_file_id"] + index:
                    raise ValueError("Enemy StreamDB wrapper requires consecutive native IDs")
                stream = streams[chunk["bank"]]
                stream.seek(32 + 16 * chunk["index_row"])
                row = struct.unpack("<QII", stream.read(16))
                if row != (chunk["file_id"], chunk["offset"] // 16, chunk["size"]):
                    raise ValueError(f"Enemy StreamDB index row differs: {chunk['file_id']}")
                stream.seek(chunk["offset"])
                payload = stream.read(chunk["size"])
                _verify(payload, chunk, str(chunk["file_id"]))
                chunks.append(payload)
            offset = 12 + 8 * len(chunks)
            header = bytearray(b"STREAMDB" + struct.pack("<I", len(chunks)))
            for chunk in chunks:
                header.extend(struct.pack("<II", offset, len(chunk)))
                offset += len(chunk)
            payload = bytes(header) + b"".join(chunks)
            _verify(payload, wrapper, wrapper["name"])
            payloads[wrapper["name"]] = payload
    return payloads


def prepare_enemy_resource_activation(
    game_base: Path, species_id: str, *, variants: dict[str, str] | None = None,
    decode=None, catalog_path: Path = CATALOG,
) -> tuple[dict[str, bytes], dict]:
    """Prepare mask updates after native import and idRehash; publish neither file here."""
    profile = load_enemy_resource_profile(species_id, catalog_path=catalog_path)
    sources = _resource_sources(profile, variants)
    container = f"{profile['resource_container']}.resources"
    records = _records(game_base / container, set(sources), decode=decode)
    indices = []
    for name, (asset, source) in sources.items():
        index, kind, _, payload, _ = records[name]
        if kind != asset["metadata"]["resource_type"]:
            raise ValueError(f"Enemy resource type differs from the catalog: {name}")
        _verify(payload, source, name)
        indices.append(index)

    updates, report = prepare_resource_activation(game_base, {container: set(indices)})
    table = report["containers"][container]
    return updates, {
        "container": container, "verified_resources": len(indices), "changed_bits": table["changed_bits"],
        "added_mask_words": table["added_mask_words"], "source_meta_sha256": report["source_meta_sha256"],
        "updated_meta_sha256": report["updated_meta_sha256"],
    }
