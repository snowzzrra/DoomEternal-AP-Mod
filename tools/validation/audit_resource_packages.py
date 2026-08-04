"""Parameterized asset-package audit derived from ``content_catalog``."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Iterable

from content_catalog import AssetSpec, load_content_catalog
from tools.maps.ap_map_generator import resolve_donor_model_override


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _audit_texture_contract(asset_root: Path) -> None:
    repo_root = asset_root.parent.parent
    contract_path = (
        repo_root / "assets" / "runtime" / "archipelago_logo"
        / "texture_contract.json"
    )
    if not contract_path.is_file():
        return
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    converter = contract["converter"]
    source_png = repo_root / converter["source_png"]
    runtime_tga = repo_root / converter["output"]
    runtime_bytes = runtime_tga.read_bytes()
    if (
        _sha256(source_png) != converter["source_png_sha256"]
        or _sha256(runtime_tga) != converter["output_sha256"]
        or not runtime_bytes.startswith(b"DIVINITY")
        or runtime_bytes.endswith(b"TRUEVISION-XFILE.\x00")
    ):
        raise AssertionError("[ASSET] AUTOHECKIN_OUTPUT_INVALID")
    expected = {
        asset_root / slot["resource_base"] / slot["true_filename"]
        for slot in contract["slots"]
        if slot["packaged"]
    }
    actual = set(asset_root.rglob("*.tga*"))
    if actual != expected:
        raise AssertionError(
            "[ASSET] TEXTURE_CONSUMER_SET_INVALID "
            f"expected={sorted(map(str, expected))} actual={sorted(map(str, actual))}"
        )
    if any(path.read_bytes() != runtime_bytes for path in expected):
        raise AssertionError("[ASSET] AUTOHECKIN_PAYLOAD_MISMATCH")


def _replacement_bundle_paths(
    root: Path,
    asset: AssetSpec,
) -> tuple[Path, Path]:
    slot = asset.replacement_slot
    return (
        root / str(slot["resource_archive"]) / asset.model,
        root / "streamdb" / str(slot["streamdb_payload"]),
    )


def _assert_model_importer_bundle(root: Path, asset: AssetSpec) -> None:
    slot = asset.replacement_slot
    required = {
        "model_path", "resource_archive", "material2", "import_bundle",
        "asset_id", "streamdb_payload", "resource_payload_sha256",
        "streamdb_payload_sha256", "provenance",
    }
    if not required <= set(slot):
        raise AssertionError(
            f"[ASSET] IMPORTER_PROVENANCE_MISSING bundle={asset.key}"
        )
    asset_id = str(slot["asset_id"])
    model = Path(str(slot["model_path"]))
    expected_payload = (
        model.parent / f"{model.stem}_id#{asset_id}{model.suffix}"
    ).as_posix()
    provenance = slot["provenance"]
    if (
        not asset_id.isdecimal()
        or model.as_posix() != asset.model
        or str(slot["resource_archive"]) != asset.resource_base
        or str(slot["import_bundle"]) != f"{model.stem}_id#{asset_id}"
        or str(slot["streamdb_payload"]) != expected_payload
        or not isinstance(provenance, Mapping)
        or provenance.get("producer") != "Doom Eternal Model Importer v1.2"
        or not re.fullmatch(
            r"[0-9a-f]{64}", str(provenance.get("source_obj_sha256", ""))
        )
    ):
        raise AssertionError(
            f"[ASSET] IMPORTER_IDENTITY_INVALID bundle={asset.key}"
        )
    resource_payload, streamdb_payload = _replacement_bundle_paths(root, asset)
    if not resource_payload.is_file() or not streamdb_payload.is_file():
        raise AssertionError(
            f"[ASSET] IMPORTER_BUNDLE_INCOMPLETE bundle={asset.key}"
        )
    resource_bytes = resource_payload.read_bytes()
    streamdb_bytes = streamdb_payload.read_bytes()
    if (
        len(resource_bytes) < 128
        or asset.model.removesuffix(".lwo").encode() not in resource_bytes
        or len(streamdb_bytes) < 1024
        or not streamdb_bytes.startswith(b"STREAMDB")
    ):
        raise AssertionError(
            f"[ASSET] IMPORTER_PAYLOAD_INVALID bundle={asset.key}"
        )
    is_v3_streamdb = (
        len(streamdb_bytes) >= 36
        and struct.unpack_from("<II", streamdb_bytes, 8) == (3, 36)
    )
    if is_v3_streamdb:
        stream_block_size = struct.unpack_from("<I", streamdb_bytes, 16)[0]
        if (
            len(streamdb_bytes) != 36 + stream_block_size
            or resource_bytes.count(struct.pack("<I", stream_block_size)) < 4
            or struct.pack("<I", stream_block_size * 2) not in resource_bytes
            or resource_bytes.count(struct.pack("<I", stream_block_size * 3)) < 2
        ):
            raise AssertionError(
                f"[ASSET] IMPORTER_BUNDLE_LAYOUT_MISMATCH bundle={asset.key}"
            )
    if (
        _sha256(resource_payload) != slot["resource_payload_sha256"]
        or _sha256(streamdb_payload) != slot["streamdb_payload_sha256"]
    ):
        raise AssertionError(
            f"[ASSET] IMPORTER_PAYLOAD_HASH_MISMATCH bundle={asset.key}"
        )


def _model_references(content: str, model: str) -> set[str]:
    references: set[str] = set()
    cursor = 0
    entity_start = re.compile(r"(?m)^\s*entity\s*\{")
    while match := entity_start.search(content, cursor):
        start = match.start()
        index = content.find("{", start) + 1
        depth = 1
        quoted = False
        while index < len(content) and depth:
            char = content[index]
            if char == '"' and content[index - 1] != "\\":
                quoted = not quoted
            elif not quoted:
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
            index += 1
        if depth:
            raise AssertionError("unterminated entity block during asset audit")
        block = content[start:index]
        name_match = re.search(r"\bentityDef\s+([^\s{]+)", block)
        cursor = index
        if name_match is None:
            continue
        if re.search(
            rf'\bmodel\s*=\s*"{re.escape(model)}";',
            block,
            flags=re.IGNORECASE,
        ):
            references.add(name_match.group(1))
    return references


def _audit_visual_presentation(
    asset_root: Path,
    mod_root: Path,
    asset: AssetSpec,
    *,
    generated_maps_root: Path | None,
    zip_names: set[str] | None,
) -> None:
    policy = asset.visual_presentation_policy
    if not policy:
        return
    relative_decl = Path(str(policy["material_decl"]))
    source_decl = asset_root / asset.resource_base / relative_decl
    packaged_decl = mod_root / asset.resource_base / relative_decl
    expected_hash = str(policy["material_sha256"])
    for path in (source_decl, packaged_decl):
        if not path.is_file() or _sha256(path) != expected_hash:
            raise AssertionError(
                f"[ASSET] AP_OPAQUE_MATERIAL_MISMATCH bundle={asset.key} "
                f"path={path}"
            )
    decl = source_decl.read_text(encoding="utf-8")
    forbidden = {
        'inherit = "template/pbr_pickup"',
        'prog = "pickup"',
        "surfaceemissivetiledmask",
        "bloommaskmap",
        "surfacesheencolor",
        "alphatestthreshold",
        "prezdrawalpha",
        "cover =",
    }
    if (
        'inherit = "template/pbr"' not in decl
        or "surfaceemissivescale = 0;" not in decl
        or any(token in decl for token in forbidden)
        or any(
            f'filePath = "{path}";' not in decl
            for path in policy["preserve_maps"].values()
        )
    ):
        raise AssertionError(
            f"[ASSET] AP_OPAQUE_MATERIAL_INVALID bundle={asset.key}"
        )

    map_spec = load_content_catalog().maps.get(asset.map_key)
    staged_entities = None
    if map_spec is not None:
        entities_member = (
            f"{Path(asset.resource_owner).stem}/maps/"
            f"{map_spec.relative_entities_path}"
        )
        staged_entities = (
            generated_maps_root / map_spec.data["generated_output"]
            if generated_maps_root is not None
            else mod_root / entities_member
        )
    if staged_entities is not None and staged_entities.is_file():
        generated = staged_entities.read_text(encoding="utf-8")
        references = _model_references(generated, asset.model)
        ap_references = {
            entity for entity in references
            if entity.startswith("ap_location_visual_")
        }
        for entity in ap_references:
            marker = f"entityDef {entity} {{"
            start = generated.index(marker)
            end = generated.index("\nentity {", start)
            block = generated[start:end]
            if (
                f'thinkComponentDecl = "{policy["think_component"]}";'
                not in block
                or any(
                    token in block
                    for token in (
                        "fxDecl", "updateFX", "renderLight", "particle",
                        "targets =", "renderParms",
                    )
                )
            ):
                raise AssertionError(
                    f"[ASSET] AP_VISUAL_ENTITY_PRESENTATION_INVALID "
                    f"bundle={asset.key} entity={entity}"
                )
    if zip_names is not None:
        member = (Path(asset.resource_base) / relative_decl).as_posix()
        if not any(
            name == member or name.endswith(f"/{member}")
            for name in zip_names
        ):
            raise AssertionError(
                f"[ASSET] AP_OPAQUE_MATERIAL_MISSING bundle={asset.key}"
            )


def audit_source_asset_dependencies(
    asset_root: Path,
    assets: Iterable[AssetSpec],
) -> None:
    """Fail fast when a declared copied model or linked payload is absent."""
    _audit_texture_contract(asset_root)
    for asset in tuple(assets):
        if (
            asset.strategy == "donor_model_override"
            or asset.dependency_policy == "canonical_model_importer_bundle"
        ):
            if asset.dependency_policy == "model_importer_bundle_pending":
                resource_payload = (
                    asset_root / asset.resource_base / asset.model
                )
                if resource_payload.exists():
                    raise AssertionError(
                        f"[ASSET] PENDING_IMPORT_HAS_UNPROVEN_PAYLOAD "
                        f"bundle={asset.key} payload={resource_payload}"
                    )
                continue
            _assert_model_importer_bundle(asset_root, asset)
            if asset.strategy == "donor_model_override":
                continue
        if asset.strategy != "resident_model":
            for member in (asset.model, *asset.dependencies):
                source = asset_root / asset.resource_base / member
                if not source.is_file():
                    raise AssertionError(
                        f"[ASSET] DEPENDENCY_MISSING bundle={asset.key} "
                        f"dependency={member}"
                    )


def audit_resource_packages(
    asset_root: Path,
    mod_root: Path,
    *,
    map_key: str | None = None,
    assets: Iterable[AssetSpec] | None = None,
    source_map_root: Path | None = None,
    zip_path: Path | None = None,
    generated_maps_root: Path | None = None,
) -> list[dict[str, str]]:
    """Audit bundles/resident assets against the resource base and final ZIP."""
    records: list[dict[str, str]] = []
    catalog = load_content_catalog()
    selected_assets = tuple(
        asset
        for asset in (assets if assets is not None else catalog.assets)
        if map_key is None or asset.map_key == map_key
    )
    audit_source_asset_dependencies(asset_root, selected_assets)
    zip_names: set[str] | None = None
    zip_archive: zipfile.ZipFile | None = None
    if zip_path is not None:
        zip_archive = zipfile.ZipFile(zip_path)
        zip_names = set(zip_archive.namelist())
    try:
        for asset in selected_assets:
            _audit_visual_presentation(
                asset_root,
                mod_root,
                asset,
                generated_maps_root=generated_maps_root,
                zip_names=zip_names,
            )
            if asset.strategy == "resident_model":
                if source_map_root is None:
                    source_map_root = Path(__file__).resolve().parents[2] / "vanillamaps"
                source_map = source_map_root / f"{asset.map_key}.map"
                if not source_map.is_file() or f'model = "{asset.model}";' not in source_map.read_text(
                    encoding="utf-8"
                ):
                    raise AssertionError(
                        f"{asset.map_key}: resident model is not referenced by the vanilla map: "
                        f"{asset.model}"
                    )
                wrong_patch_copy = mod_root / Path(asset.resource_owner).stem / asset.model
                if wrong_patch_copy.exists():
                    raise AssertionError(
                        f"{asset.map_key}: resident model was incorrectly copied to patch owner: "
                        f"{wrong_patch_copy}"
                    )
                map_spec = catalog.maps.get(asset.map_key)
                if map_spec is not None:
                    entities_member = (
                        f"{Path(asset.resource_owner).stem}/maps/"
                        f"{map_spec.relative_entities_path}"
                    )
                    staged_entities = (
                        generated_maps_root / map_spec.data["generated_output"]
                        if generated_maps_root is not None
                        else mod_root / entities_member
                    )
                    if not staged_entities.is_file() or f'model = "{asset.model}";' not in staged_entities.read_text(
                        encoding="utf-8"
                    ):
                        raise AssertionError(
                            f"{asset.map_key}: staged entities do not reference resident model "
                            f"{asset.model}"
                        )
                    if zip_archive is not None:
                        matches = [
                            name for name in zip_names or ()
                            if name == entities_member or name.endswith(f"/{entities_member}")
                        ]
                        if len(matches) != 1:
                            raise AssertionError(
                                f"{asset.map_key}: final ZIP does not reference resident model "
                                f"{asset.model}"
                            )
                records.append({
                    "map_key": asset.map_key,
                    "strategy": asset.strategy,
                    "resource_base": asset.resource_base,
                    "resource_owner": asset.resource_owner,
                    "asset": asset.model,
                    "sha256": "resident",
                })
                continue

            if asset.strategy == "donor_model_override":
                if asset.dependency_policy == "model_importer_bundle_pending":
                    raise AssertionError(
                        f"[ASSET] MODEL_IMPORT_PENDING bundle={asset.key}"
                    )
                maps_root = source_map_root or (
                    Path(__file__).resolve().parents[2] / "vanillamaps"
                )
                source_map = maps_root / f"{asset.map_key}.map"
                if not source_map.is_file():
                    raise AssertionError(
                        f"{asset.map_key}: donor source map is missing"
                    )
                source_text = source_map.read_text(encoding="utf-8")
                try:
                    resolve_donor_model_override(
                        source_text,
                        source_text,
                        {
                            "strategy": asset.strategy,
                            "donor": dict(asset.donor),
                            "replacement_slot_policy":
                                asset.replacement_slot_policy,
                            "replacement_slot": dict(asset.replacement_slot),
                            "model": asset.model,
                        },
                    )
                except ValueError as error:
                    raise AssertionError(
                        f"{asset.map_key}: donor unresolved: {error}"
                    ) from error
                source_references = _model_references(
                    source_text, asset.model
                )
                expected_references = set(
                    asset.replacement_slot.get(
                        "vanilla_reference_allowlist", ()
                    )
                )
                if source_references != expected_references:
                    raise AssertionError(
                        f"{asset.map_key}: replacement slot vanilla references "
                        f"expected={sorted(expected_references)} "
                        f"actual={sorted(source_references)}"
                    )
                map_spec = catalog.maps.get(asset.map_key)
                staged_entities = None
                entities_member = None
                if map_spec is not None:
                    if asset.usage_policy == "removed_vanilla_entity_allowlist":
                        config = json.loads(
                            map_spec.level_config_path.read_text(encoding="utf-8")
                        )
                        policies = config.get("target_policies", {})
                        not_removed = sorted(
                            entity for entity in expected_references
                            if not policies.get(entity, {}).get(
                                "remove_original", False
                            )
                        )
                        if not_removed:
                            raise AssertionError(
                                f"{asset.map_key}: allowlisted vanilla references "
                                f"are not removed: {not_removed}"
                            )
                    entities_member = (
                        f"{Path(asset.resource_owner).stem}/maps/"
                        f"{map_spec.relative_entities_path}"
                    )
                    staged_entities = (
                        generated_maps_root / map_spec.data["generated_output"]
                        if generated_maps_root is not None
                        else mod_root / entities_member
                    )
                if staged_entities is not None and staged_entities.is_file():
                    generated = staged_entities.read_text(encoding="utf-8")
                    generated_references = _model_references(
                        generated, asset.model
                    )
                    if not generated_references or any(
                        not entity.startswith("ap_location_visual_")
                        for entity in generated_references
                    ):
                        raise AssertionError(
                            f"{asset.map_key}: replacement slot is referenced by "
                            "non-AP generated entities"
                        )
                    if "modelDecl" in generated:
                        raise AssertionError(
                            f"{asset.map_key}: global model override is forbidden"
                        )
                if zip_names is not None and entities_member is not None:
                    matches = [
                        name for name in zip_names
                        if name == entities_member
                        or name.endswith(f"/{entities_member}")
                    ]
                    if len(matches) != 1:
                        raise AssertionError(
                            f"{asset.map_key}: final ZIP lacks generated donor override"
                        )
                source_resource, source_streamdb = _replacement_bundle_paths(
                    asset_root, asset
                )
                packaged_resource, packaged_streamdb = _replacement_bundle_paths(
                    mod_root, asset
                )
                for source, packaged in (
                    (source_resource, packaged_resource),
                    (source_streamdb, packaged_streamdb),
                ):
                    if not packaged.is_file():
                        raise AssertionError(
                            f"[ASSET] IMPORTER_BUNDLE_INCOMPLETE "
                            f"bundle={asset.key} packaged={packaged}"
                        )
                    if _sha256(packaged) != _sha256(source):
                        raise AssertionError(
                            f"{asset.map_key}: packaged importer payload hash "
                            f"mismatch: {packaged.name}"
                        )
                    if zip_names is not None:
                        relative = packaged.relative_to(mod_root).as_posix()
                        if not any(
                            name == relative or name.endswith(f"/{relative}")
                            for name in zip_names
                        ):
                            raise AssertionError(
                                f"[ASSET] IMPORTER_BUNDLE_INCOMPLETE "
                                f"bundle={asset.key} zip={zip_path} "
                                f"missing={relative}"
                            )
                records.append({
                    "map_key": asset.map_key,
                    "strategy": asset.strategy,
                    "resource_base": asset.resource_base,
                    "resource_owner": asset.resource_owner,
                    "asset": asset.model,
                    "asset_id": str(asset.replacement_slot["asset_id"]),
                    "sha256": _sha256(source_streamdb),
                })
                continue

            members = [
                (asset.resource_base, member)
                for member in (asset.model, *asset.dependencies)
            ]
            if asset.dependency_policy == "canonical_model_importer_bundle":
                members.append((
                    "streamdb",
                    str(asset.replacement_slot["streamdb_payload"]),
                ))
            for resource_base, member in members:
                source = asset_root / resource_base / member
                packaged = mod_root / resource_base / member
                if not source.is_file():
                    raise AssertionError(
                        f"[ASSET] DEPENDENCY_MISSING bundle={asset.key} dependency={member} source={source}"
                    )
                if not packaged.is_file():
                    raise AssertionError(
                        f"[ASSET] DEPENDENCY_MISSING bundle={asset.key} dependency={member} packaged={packaged}"
                    )
                source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
                if hashlib.sha256(packaged.read_bytes()).hexdigest() != source_hash:
                    raise AssertionError(
                        f"{asset.map_key}: packaged asset hash mismatch: {member}"
                    )
                if zip_names is not None and not any(
                    name.endswith(f"/{resource_base}/{member}")
                    or name == f"{resource_base}/{member}"
                    for name in zip_names
                ):
                    raise AssertionError(
                        f"[ASSET] DEPENDENCY_MISSING bundle={asset.key} dependency={member} zip={zip_path}"
                    )
            source_hash = hashlib.sha256(
                (asset_root / asset.resource_base / asset.model).read_bytes()
            ).hexdigest()
            records.append({
                "map_key": asset.map_key,
                "strategy": asset.strategy,
                "resource_base": asset.resource_base,
                "resource_owner": asset.resource_owner,
                "asset": asset.model,
                "sha256": source_hash,
            })
    finally:
        if zip_archive is not None:
            zip_archive.close()
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-root", type=Path, required=True)
    parser.add_argument("--mod-root", type=Path, required=True)
    parser.add_argument("--map")
    parser.add_argument("--source-map-root", type=Path)
    parser.add_argument("--zip", dest="zip_path", type=Path)
    parser.add_argument("--generated-maps", dest="generated_maps_root", type=Path)
    args = parser.parse_args()
    print(json.dumps(audit_resource_packages(
        args.asset_root,
        args.mod_root,
        map_key=args.map,
        source_map_root=args.source_map_root,
        zip_path=args.zip_path,
        generated_maps_root=args.generated_maps_root,
    ), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
