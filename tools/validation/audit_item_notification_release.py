#!/usr/bin/env python3
"""Audit notifier entities in the actual mod payload carried by the final ZIP."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
import zipfile
from pathlib import Path

from map_registry import load_map_registry, release_plan
from item_classification import load_item_classification_identity
from tools.validation.validate_item_notification_package import (
    HEADER_RE,
    LOCATION_NOTIFICATION_RE,
    NOTIFICATION_RE,
    capability,
    entity_block,
    string_table_names,
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
        entity_block(content, f"ap_rpc_v3_{suffix.split('_', 1)[1]}")
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
            generated_notifications = set(NOTIFICATION_RE.findall(generated.decode("utf-8")))
            packaged_notifications = set(NOTIFICATION_RE.findall(packaged.decode("utf-8")))
            packaged_locations = set(
                LOCATION_NOTIFICATION_RE.findall(packaged.decode("utf-8"))
            )
            if generated != packaged:
                raise AssertionError(f"generated and packaged map contents diverge: {plan.map_key}")
            if enabled:
                if not packaged_notifications:
                    raise AssertionError(f"packaged notifier entities missing: {plan.map_key}")
                if generated_notifications != packaged_notifications:
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
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("item_notifications", {}).get("enabled") is not enabled:
        raise AssertionError("RELEASE_MANIFEST notification capability diverges from audit mode")
    _audit_locales(enabled, mod_root)
    classification_path = client_dir / "data" / "item_classifications.json"
    load_item_classification_identity(classification_path)
    classification_audit = {
        "schema_version": 1,
        "sha256": hashlib.sha256(
            classification_path.read_bytes()
        ).hexdigest(),
    }
    records = audit_mod_payload(
        enabled, generated_maps, mod_root, map_registry, decompressor
    )
    if update_manifest:
        manifest["item_notification_payload"] = {"maps": records}
        manifest["item_classification_identity"] = classification_audit
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    elif manifest.get("item_notification_payload", {}).get("maps") != records:
        raise AssertionError("RELEASE_MANIFEST packaged notifier map audit diverges")
    elif manifest.get("item_classification_identity") != classification_audit:
        raise AssertionError(
            "RELEASE_MANIFEST item classification audit diverges"
        )
    return records


def _extract_playable_zip(playable_zip: Path, destination: Path) -> tuple[Path, Path, Path]:
    with zipfile.ZipFile(playable_zip) as archive:
        archive.extractall(destination)
    mod_zip = destination / "DoomEternalArchipelagoAlpha.zip"
    if not mod_zip.is_file():
        raise AssertionError("playable ZIP lacks its injector mod ZIP")
    mod_root = destination / "mod"
    with zipfile.ZipFile(mod_zip) as archive:
        archive.extractall(mod_root)
    return mod_root, destination / "client", destination / "RELEASE_MANIFEST.json"


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
            mod_root, client_dir, manifest = _extract_playable_zip(args.playable_zip, Path(directory))
            audit_release(args.enabled == "1", args.generated_maps, mod_root, client_dir,
                          manifest, args.map_registry, args.decompressor)
        return 0
    if not all((args.mod_root, args.client_dir, args.release_manifest)):
        parser.error("local audit requires --mod-root, --client-dir, and --release-manifest")
    audit_release(args.enabled == "1", args.generated_maps, args.mod_root, args.client_dir,
                  args.release_manifest, args.map_registry, args.decompressor,
                  args.update_manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
