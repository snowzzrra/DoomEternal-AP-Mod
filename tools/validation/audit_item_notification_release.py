#!/usr/bin/env python3
"""Audit notifier entities in the actual mod payload carried by the final ZIP."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any, cast

from doom_eap.content.map_registry import load_map_registry, release_plan
from doom_eap.content.item_classification import load_item_classification_identity
from doom_eap.content.automap_visual_registry import load_automap_visual_registry, validate_generated_visuals
from tools.validation.validate_item_notification_package import (
    LOCATION_NOTIFICATION_RE,
    NOTIFICATION_RE,
    capability,
    entity_block,
    string_table_names,
)
from tools.validation.release_layout import expected_release_roots
from tools.release.release_manifest import load_release_manifest
from tools.release.room_payloads import (
    BASE_RESOURCE_NAME,
    ROOM_PAYLOAD_MANIFEST_NAME,
    ROOM_PAYLOAD_RESOURCE_NAME,
    assemble_room_files,
    canonical_json,
    load_room_payload_manifest,
    resource_metadata,
    write_deterministic_zip,
)


def _normalized(content: bytes) -> bytes:
    return content.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _read_entities(path: Path, decompressor: Path | None, temporary: Path) -> bytes:
    payload = path.read_bytes()
    if b"entityDef " in payload:
        return payload
    if decompressor is None:
        raise AssertionError(f"compressed payload requires decompressor: {path}")
    output = temporary / f"{path.name}.decoded"
    subprocess.run(
        [str(decompressor), "--decompress", str(path), str(output)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return output.read_bytes()


def _map_payload_path(mod_root: Path, plan) -> Path:
    resource_name = Path(plan.resource_path).stem
    return mod_root / resource_name / "maps" / plan.relative_entities_path


def _assert_notifications(content: str, map_key: str) -> None:
    if "entityDef ap_rpc_item_" in content:
        raise AssertionError(f"forbidden receipt root: {map_key}")
    for suffix in NOTIFICATION_RE.findall(content):
        notification = entity_block(content, f"ap_notify_item_{suffix}")
        rpc_suffix = suffix.split('_', 1)[1].rsplit('_', 1)[0]
        entity_block(content, f"ap_rpc_v3_{rpc_suffix}")
        if any(field in notification for field in (
            'triggerOnce = true;', 'removeAfterActivation = true;',
            'disableAfterActivation = true;', 'startOff = true;',
        )):
            raise AssertionError(f"notification is one-shot: {map_key}/{suffix}")


def audit_mod_payload(
    enabled: bool,
    generated_maps: Path,
    mod_root: Path,
    map_registry: Path,
    decompressor: Path | None,
    *,
    require_generated_identity: bool = True,
    visual_registry: dict[str, object] | None = None,
) -> dict[str, dict[str, int | str]]:
    """Compare every release map against its unpacked, compressed mod payload."""
    records: dict[str, dict[str, int | str]] = {}
    plans = release_plan(load_map_registry(map_registry))
    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        for plan in plans:
            generated_path = generated_maps / plan.generated_output
            packaged_path = _map_payload_path(mod_root, plan)
            if not generated_path.is_file() or not packaged_path.is_file():
                raise AssertionError(f"missing generated or packaged map: {plan.map_key}")
            generated = _normalized(generated_path.read_bytes())
            packaged = _normalized(_read_entities(packaged_path, decompressor, temporary))
            if visual_registry is not None:
                validate_generated_visuals(
                    visual_registry,
                    plan.map_key,
                    generated.decode("utf-8"),
                )
            generated_notifications = set(NOTIFICATION_RE.findall(generated.decode("utf-8")))
            packaged_notifications = set(NOTIFICATION_RE.findall(packaged.decode("utf-8")))
            packaged_locations = set(
                LOCATION_NOTIFICATION_RE.findall(packaged.decode("utf-8"))
            )
            if require_generated_identity and generated != packaged:
                raise AssertionError(f"generated and packaged map contents diverge: {plan.map_key}")
            if enabled:
                if not packaged_notifications:
                    raise AssertionError(f"packaged notifier entities missing: {plan.map_key}")
                if require_generated_identity and generated_notifications != packaged_notifications:
                    raise AssertionError(f"packaged notifier entity set diverges: {plan.map_key}")
                _assert_notifications(packaged.decode("utf-8"), plan.map_key)
            elif "entityDef ap_rpc_item_" in packaged.decode("utf-8") or packaged_notifications:
                raise AssertionError(f"disabled notifier payload contains entities: {plan.map_key}")
            records[plan.map_key] = {
                "generated_source_sha256": hashlib.sha256(generated).hexdigest(),
                "packaged_payload_sha256": hashlib.sha256(packaged).hexdigest(),
                "effect_entity_count": packaged.decode("utf-8").count("entityDef ap_rpc_v3_"),
                "notification_entity_count": len(packaged_notifications),
                "major_notification_count": sum(
                    suffix.startswith("major_")
                    for suffix in packaged_notifications
                ),
                "filler_notification_count": sum(
                    suffix.startswith("filler_")
                    for suffix in packaged_notifications
                ),
                "location_notification_count": len(packaged_locations),
                "receipt_root_count": 0,
            }
    return records


def _audit_locales(enabled: bool, mod_root: Path) -> None:
    tables = [
        mod_root / "gameresources_patch1/EternalMod/strings/english.json",
        mod_root / "gameresources_patch1/EternalMod/strings/portuguese.json",
    ]
    if not all(path.is_file() for path in tables):
        raise AssertionError("notification payload lacks locale strings")
    if string_table_names(tables[0]) != string_table_names(tables[1]):
        raise AssertionError("payload locale string names diverge")


def _audit_room_resources(
    client_dir: Path,
    manifest: dict[str, object],
    map_registry: Path,
) -> None:
    compiler = cast(dict[str, Any], manifest["room_compiler"])
    resources = cast(dict[str, Any], compiler["resources"])
    resource_dir = client_dir / "resources"
    expected = {BASE_RESOURCE_NAME, ROOM_PAYLOAD_RESOURCE_NAME, ROOM_PAYLOAD_MANIFEST_NAME}
    if set(resources) != expected:
        raise AssertionError("release manifest room compiler resource contract is incomplete")
    for name in expected:
        actual = resource_metadata(resource_dir / name)
        actual["path"] = f"client/resources/{name}"
        if actual != resources[name]:
            raise AssertionError(f"room compiler resource metadata drifted: {name}")
    known_maps = {
        plan.map_key: f"{Path(plan.resource_path).stem}/maps/{plan.relative_entities_path}"
        for plan in release_plan(load_map_registry(map_registry))
    }
    payload_manifest = load_room_payload_manifest(
        resource_dir / ROOM_PAYLOAD_MANIFEST_NAME,
        known_maps=known_maps,
    )
    scenarios = (
        {"randomize_chainsaw": False, "randomize_dash": False, "randomize_first_battery": False},
        {"randomize_chainsaw": True, "randomize_dash": False, "randomize_first_battery": False},
        {"randomize_chainsaw": False, "randomize_dash": True, "randomize_first_battery": False},
        {"randomize_chainsaw": False, "randomize_dash": False, "randomize_first_battery": True},
        {"randomize_chainsaw": False, "randomize_dash": True, "randomize_first_battery": True},
        {"randomize_chainsaw": True, "randomize_dash": True, "randomize_first_battery": True},
    )
    assembled = {}
    selected = {}
    for options in scenarios:
        assembled, selected = assemble_room_files(
            resource_dir / BASE_RESOURCE_NAME,
            resource_dir / ROOM_PAYLOAD_RESOURCE_NAME,
            payload_manifest,
            options,
        )
        if len(selected) != len(payload_manifest["maps"]):
            raise AssertionError("synthetic room payload selection is incomplete")
        for map_key, state in selected.items():
            expected = {
                key: options[key]
                for key in payload_manifest["maps"][map_key]["option_keys"]
            }
            if state["options"] != expected:
                raise AssertionError(f"synthetic room payload state selection drifted: {map_key}")
    if not any(state["source"] == "replacement" for state in selected.values()):
        raise AssertionError("synthetic room payload did not select replacements")
    with tempfile.TemporaryDirectory() as directory:
        package = Path(directory) / "synthetic-room.zip"
        assembled["room_config.json"] = canonical_json({"start_with_automap": False})
        write_deterministic_zip(assembled, package)
        with zipfile.ZipFile(package) as archive:
            if set(archive.namelist()) != set(assembled):
                raise AssertionError("synthetic room package member set drifted")


def audit_release(
    enabled: bool,
    generated_maps: Path,
    mod_root: Path,
    client_dir: Path,
    manifest_path: Path,
    map_registry: Path,
    decompressor: Path | None,
    update_manifest: bool = False,
) -> dict[str, dict[str, int | str]]:
    if capability(client_dir / "bridge_identity.json") is not enabled:
        raise AssertionError("bridge_identity notification capability diverges from audit mode")
    manifest = load_release_manifest(
        manifest_path,
        package_root=manifest_path.parent,
        generated_maps=generated_maps,
    )
    _audit_room_resources(client_dir, manifest, map_registry)
    registry_path = client_dir / "data" / "checked_location_visuals.json"
    registry = manifest["checked_location_visuals"]
    visual_registry = load_automap_visual_registry(registry_path)
    expected_visuals = {
        plan.map_key: hashlib.sha256(
            (generated_maps / plan.generated_output).read_bytes()
        ).hexdigest()
        for plan in release_plan(load_map_registry(map_registry, authorial=True))
    }
    if registry["sha256"] != hashlib.sha256(registry_path.read_bytes()).hexdigest():
        raise AssertionError("RELEASE_MANIFEST checked-location visual registry hash drifted")
    if registry["authoritative_fingerprint"] != visual_registry["authoritative_fingerprint"]:
        raise AssertionError("RELEASE_MANIFEST checked-location visual fingerprint drifted")
    if registry["generated_map_sha256"] != expected_visuals:
        raise AssertionError("RELEASE_MANIFEST checked-location visual registry hash drifted")
    _audit_locales(enabled, mod_root)
    classification_path = client_dir / "data" / "item_classifications.json"
    load_item_classification_identity(classification_path)
    records = audit_mod_payload(
        enabled, generated_maps, mod_root, map_registry, decompressor,
        visual_registry=visual_registry,
    )
    return records


def _extract_playable_zip(
    playable_zip: Path,
    destination: Path,
) -> tuple[dict[str, Path], Path, Path]:
    with zipfile.ZipFile(playable_zip) as archive:
        files = {info.filename for info in archive.infolist() if not info.is_dir()}
        if any(Path(name).name == "DoomEternalArchipelagoBeta.zip" for name in files):
            raise AssertionError("playable ZIP contains obsolete universal mod ZIP")
        launchers = files & {
            "DoomEternalArchipelagoLauncher",
            "DoomEternalArchipelagoLauncher.exe",
        }
        if len(launchers) != 1:
            raise AssertionError("playable ZIP must contain exactly one root launcher")
        if any(name.startswith("client/DoomEternalArchipelagoLauncher") for name in files):
            raise AssertionError("playable ZIP contains launcher under client")
        expected_roots = expected_release_roots(next(iter(launchers)))
        roots = {name.split("/", 1)[0] for name in files}
        if roots != expected_roots:
            raise AssertionError(f"playable ZIP root layout is not exact: {sorted(roots)}")
        required = {
            "README.md",
            "INSTALL.md",
            "LICENSE",
            "RELEASE_MANIFEST.json",
            "doometernal.apworld",
        }
        missing = required - files
        if missing:
            raise AssertionError(f"playable ZIP lacks public layout files: {sorted(missing)}")
        if any(
            "licenses" in Path(name).parts
            for name in files
        ):
            raise AssertionError("playable ZIP exposes internal resources")
        nested_candidates = {
            name for name in files
            if name.lower().endswith(".zip")
            and "DoomEternalArchipelagoPlayableTest-" in Path(name).name
        }
        if nested_candidates:
            raise AssertionError(f"playable ZIP contains prior candidate: {sorted(nested_candidates)}")
        archive.extractall(destination)
    client_dir = destination / "client"
    resource_dir = client_dir / "resources"
    payload_manifest = load_room_payload_manifest(resource_dir / ROOM_PAYLOAD_MANIFEST_NAME)
    assembled, _ = assemble_room_files(
        resource_dir / BASE_RESOURCE_NAME,
        resource_dir / ROOM_PAYLOAD_RESOURCE_NAME,
        payload_manifest,
        {
            "randomize_chainsaw": True,
            "randomize_dash": True,
            "randomize_first_battery": True,
        },
    )
    mod_root = destination / "assembled-room-mod"
    for member, content in assembled.items():
        path = mod_root / member
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return {"direct": mod_root}, client_dir, destination / "RELEASE_MANIFEST.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enabled", required=True, choices=("0", "1"))
    parser.add_argument("--generated-maps", required=True, type=Path)
    parser.add_argument("--map-registry", required=True, type=Path)
    parser.add_argument("--decompressor", type=Path)
    parser.add_argument("--mod-root", type=Path)
    parser.add_argument("--client-dir", type=Path)
    parser.add_argument("--release-manifest", type=Path)
    parser.add_argument("--playable-zip", type=Path)
    parser.add_argument("--update-manifest", action="store_true")
    args = parser.parse_args()
    if args.playable_zip:
        if any((args.mod_root, args.client_dir, args.release_manifest, args.update_manifest)):
            parser.error("--playable-zip cannot be combined with local payload arguments")
        with tempfile.TemporaryDirectory() as directory:
            mod_roots, client_dir, manifest = _extract_playable_zip(
                args.playable_zip, Path(directory)
            )
            audit_release(
                args.enabled == "1", args.generated_maps, mod_roots["direct"],
                client_dir, manifest, args.map_registry, args.decompressor,
            )
            _audit_locales(args.enabled == "1", mod_roots["direct"])
            audit_mod_payload(
                args.enabled == "1", args.generated_maps, mod_roots["direct"],
                args.map_registry, args.decompressor,
            )
        return 0
    if not all((args.mod_root, args.client_dir, args.release_manifest)):
        parser.error("local audit requires --mod-root, --client-dir, and --release-manifest")
    audit_release(args.enabled == "1", args.generated_maps, args.mod_root, args.client_dir,
                  args.release_manifest, args.map_registry, args.decompressor,
                  args.update_manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
