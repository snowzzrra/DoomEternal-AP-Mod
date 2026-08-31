#!/usr/bin/env python3
"""Audit notifier entities in the actual mod payload carried by the final ZIP."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any, cast

from doom_eap.content.physical_options import PHYSICAL_OPTIONS
from doom_eap.content.map_registry import load_map_registry, release_plan
from doom_eap.content.item_classification import load_item_classification_identity
from doom_eap.content.automap_visual_registry import load_automap_visual_registry, validate_generated_visuals
from tools.validation.validate_item_notification_package import (
    LOCATION_NOTIFICATION_RE,
    NOTIFICATION_RE,
    capability,
    progressive_notification_stage_count,
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
    read_zip,
    resource_metadata,
    select_room_payloads,
    sha256_bytes,
    write_deterministic_zip,
)
from tools.maps.ap_map_generator import (
    find_entity_block_bounds,
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


def _read_cached_entities(
    payload: bytes,
    decompressor: Path | None,
    temporary: Path,
    cache: dict[str, bytes],
) -> tuple[str, bytes]:
    digest = sha256_bytes(payload)
    if digest not in cache:
        encoded = temporary / f"{digest}.entities"
        encoded.write_bytes(payload)
        cache[digest] = _read_entities(encoded, decompressor, temporary)
    return digest, cache[digest]


def _map_payload_path(mod_root: Path, plan) -> Path:
    resource_name = Path(plan.resource_path).stem
    return mod_root / resource_name / "maps" / plan.relative_entities_path


def _assert_notifications(
    content: str,
    map_key: str,
    item_definitions: dict[str, Any],
) -> None:
    if "entityDef ap_rpc_item_" in content:
        raise AssertionError(f"forbidden receipt root: {map_key}")
    checked_rpc_ids: set[int] = set()
    for suffix in NOTIFICATION_RE.findall(content):
        notification = entity_block(content, f"ap_notify_item_{suffix}")
        rpc_suffix = suffix.split('_', 1)[1].rsplit('_', 1)[0]
        try:
            item_id = int(rpc_suffix.split("_", 1)[0])
        except ValueError as error:
            raise AssertionError(f"notification has invalid item ID: {map_key}/{suffix}") from error
        definition = item_definitions.get(str(item_id))
        if definition is None:
            raise AssertionError(f"notification has unknown item ID: {map_key}/{suffix}")
        progressive_stage_count = progressive_notification_stage_count(item_id, definition)
        if progressive_stage_count is not None:
            parts = rpc_suffix.split("_")
            if len(parts) != 2 or not parts[1].isdigit() or int(parts[1]) >= progressive_stage_count:
                raise AssertionError(f"notification has invalid progressive stage: {map_key}/{suffix}")
        if item_id not in checked_rpc_ids:
            checked_rpc_ids.add(item_id)
            if isinstance(definition, dict) and definition.get("type") == "transient_effect":
                pass
            elif isinstance(definition, dict) and definition.get("type") in {
                "progressive_perk", "progressive_item",
            }:
                for stage, stage_effects in enumerate(definition["perks"]):
                    effects = [stage_effects] if isinstance(stage_effects, str) else stage_effects
                    for index in range(len(effects)):
                        rpc_name = (
                            f"ap_rpc_v3_{item_id}_{stage}"
                            if isinstance(stage_effects, str)
                            else f"ap_rpc_v3_{item_id}_{stage}_{index}"
                        )
                        entity_block(content, rpc_name)
            else:
                entity_block(content, f"ap_rpc_v3_{item_id}")
        if any(field in notification for field in (
            'triggerOnce = true;', 'removeAfterActivation = true;',
            'disableAfterActivation = true;', 'startOff = true;',
        )):
            raise AssertionError(f"notification is one-shot: {map_key}/{suffix}")


AP_RUNTIME_ENTITY_RE = re.compile(r"\bentityDef\s+((?:ap_|AP_CHECK_)[A-Za-z0-9_]+)\s*\{")
GLOBAL_OPTION_KEYS = tuple(PHYSICAL_OPTIONS)


def _runtime_entity_blocks(content: str) -> dict[str, str]:
    blocks: dict[str, str] = {}
    for name in AP_RUNTIME_ENTITY_RE.findall(content):
        bounds = find_entity_block_bounds(content, name)
        if bounds is None or name in blocks:
            raise AssertionError(f"runtime entity is missing or duplicated: {name}")
        blocks[name] = content[bounds[0]:bounds[1]].replace("\r\n", "\n")
    return blocks


def _physical_entity_family(spec: dict[str, object], name: str) -> bool:
    location_id = str(spec["location_id"])
    projected_names = {
        f"{prefix}{location_id}"
        for prefix in (
            "ap_automap_location_",
            "ap_location_visual_",
            "ap_remove_location_visual_",
            "ap_hide_location_visual_",
            "ap_notify_location_",
            "ap_event_",
        )
    }
    projected_names.add(str(spec["entity"]))
    projected_names.add(f"ap_independent_{spec['vanilla_entity']}")
    return name in projected_names


def _assert_runtime_contract_families(
    map_key: str,
    blocks: dict[str, str],
    fast_travel_maps: set[str],
) -> None:
    required = {
        f"ap_lifecycle_{map_key}",
        "ap_rpc_auto_enable",
        "ap_deathlink",
    }
    if map_key in fast_travel_maps:
        required.add("ap_fast_travel_unlock")
        required.add("ap_fast_travel_unlock_native")
    absent = sorted(required - set(blocks))
    if absent:
        raise AssertionError(f"runtime contract owners absent: map={map_key} entities={absent}")

    lifecycle = blocks[f"ap_lifecycle_{map_key}"]
    if (
        'class = "idTarget_FirstThinkActivate";' not in lifecycle
        or 'item[0] = "ap_rpc_auto_enable";' not in lifecycle
        or re.search(r"\blayers\s*=", lifecycle)
    ):
        raise AssertionError(f"FirstThink lifecycle contract drift: {map_key}")
    active_map = blocks["ap_rpc_auto_enable"]
    if "AP_ACTIVE_MAP_V1" not in active_map or f"map_key={map_key}" not in active_map:
        raise AssertionError(f"active-map publisher contract drift: {map_key}")
    if map_key in fast_travel_maps:
        ft_relay = blocks["ap_fast_travel_unlock"]
        if 'class = "idTarget_Count";' not in ft_relay:
            raise AssertionError(f"Fast Travel relay class drift: {map_key}")
        if 'inherit = "target/relay";' not in ft_relay:
            raise AssertionError(f"Fast Travel relay inherit drift: {map_key}")
        if '"ap_fast_travel_unlock_native"' not in ft_relay:
            raise AssertionError(f"Fast Travel relay missing native target: {map_key}")
        ft_native = blocks["ap_fast_travel_unlock_native"]
        if 'class = "idTarget_FastTravelUnlock";' not in ft_native:
            raise AssertionError(f"Fast Travel native class drift: {map_key}")

    families = {
        "AP checks": lambda name: name.startswith("AP_CHECK_"),
        "AP event owners": lambda name: name.startswith("ap_event_"),
        "item RPC owners": lambda name: name.startswith("ap_rpc_v3_"),
        "notification owners": lambda name: name.startswith("ap_notify_"),
        "Automap presentations": lambda name: name.startswith("ap_automap_location_"),
        "visual cleanup targets": lambda name: name.startswith("ap_remove_location_visual_"),
    }
    empty = [label for label, matches in families.items() if not any(matches(name) for name in blocks)]
    if empty:
        raise AssertionError(f"runtime contract families absent: map={map_key} families={empty}")


def _global_option_states() -> list[dict[str, bool]]:
    return [
        dict(zip(GLOBAL_OPTION_KEYS, values, strict=True))
        for values in itertools.product((False, True), repeat=len(GLOBAL_OPTION_KEYS))
    ]


def _audit_room_selection(
    resource_dir: Path,
    payload_manifest: dict[str, object],
) -> tuple[
    list[tuple[dict[str, bool], dict[str, dict[str, Any]]]],
    dict[str, bytes],
    dict[str, bytes],
]:
    manifest_maps = cast(dict[str, dict[str, Any]], payload_manifest["maps"])
    base_members = read_zip(resource_dir / BASE_RESOURCE_NAME)
    payload_members = read_zip(resource_dir / ROOM_PAYLOAD_RESOURCE_NAME)
    expected_base_members = set(cast(list[str], payload_manifest["base_members"]))
    if set(base_members) != expected_base_members:
        raise AssertionError("room payload base member set drifted")
    expected_payload_members = {
        state["member"]
        for record in manifest_maps.values()
        for state in record["states"]
        if state["source"] == "replacement"
    }
    if set(payload_members) != expected_payload_members:
        raise AssertionError("room payload archive member set drifted")

    selections = []
    for state_index, options in enumerate(_global_option_states()):
        selected = select_room_payloads(payload_manifest, options)
        if len(selected) != len(manifest_maps):
            raise AssertionError(
                f"synthetic room payload selection is incomplete: state={state_index}"
            )
        for map_key, state in selected.items():
            record = manifest_maps[map_key]
            expected_options = {
                key: options[key] for key in record["option_keys"]
            }
            if state["options"] != expected_options:
                raise AssertionError(
                    f"synthetic room payload state selection drifted: {map_key}"
                )
            target = str(record["target_member"])
            if target not in base_members:
                raise AssertionError(f"base room payload target is missing: {map_key}/{target}")
            if state["source"] == "base":
                selected_bytes = base_members[target]
            else:
                member = str(state["member"])
                if member not in payload_members:
                    raise AssertionError(f"room payload member is missing: {map_key}/{member}")
                selected_bytes = payload_members[member]
            if sha256_bytes(selected_bytes) != state["sha256"]:
                raise AssertionError(f"room payload state hash drifted: {map_key}")
        selections.append((options, selected))
    if not any(
        state["source"] == "replacement"
        for _, selected in selections
        for state in selected.values()
    ):
        raise AssertionError("synthetic room payload did not select replacements")
    return selections, base_members, payload_members


def _audit_final_physical_states(
    resource_dir: Path,
    payload_manifest: dict[str, object],
    generated_maps: Path,
    map_registry: Path,
    decompressor: Path | None,
    selections: list[tuple[dict[str, bool], dict[str, dict[str, Any]]]],
    base_members: dict[str, bytes],
    payload_members: dict[str, bytes],
    content_cache: dict[str, bytes],
) -> dict[str, int]:
    plans = {plan.map_key: plan for plan in release_plan(load_map_registry(map_registry))}
    fast_travel = json.loads((map_registry.parent / "fast_travel.json").read_text(encoding="utf-8"))
    fast_travel_maps = set(fast_travel["maps"])
    authoritative_content: dict[str, str] = {}
    authoritative: dict[str, dict[str, str]] = {}
    for map_key, plan in plans.items():
        content = (generated_maps / plan.generated_output).read_text(encoding="utf-8")
        authoritative_content[map_key] = content
        authoritative[map_key] = _runtime_entity_blocks(content)
        _assert_runtime_contract_families(map_key, authoritative[map_key], fast_travel_maps)

    unique_map_states: set[tuple[str, str]] = set()
    unique_payload_hashes: set[str] = set()
    parsed_payloads: dict[str, dict[str, str]] = {}
    expected_payloads: dict[tuple[str, tuple[tuple[str, bool], ...]], dict[str, str]] = {}
    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        manifest_maps = cast(dict[str, dict[str, Any]], payload_manifest["maps"])
        for state_index, (options, selected) in enumerate(selections):
            for map_key, state in selected.items():
                record = manifest_maps[map_key]
                target = str(record["target_member"])
                if state["source"] == "base":
                    encoded_bytes = base_members[target]
                else:
                    encoded_bytes = payload_members[str(state["member"])]
                state_sha256 = sha256_bytes(encoded_bytes)
                if state_sha256 != state["sha256"]:
                    raise AssertionError(f"room payload state hash drifted: {map_key}")
                unique_map_states.add((map_key, state_sha256))
                unique_payload_hashes.add(state_sha256)
                _read_cached_entities(
                    encoded_bytes, decompressor, temporary, content_cache
                )
                if state_sha256 not in parsed_payloads:
                    parsed_payloads[state_sha256] = _runtime_entity_blocks(
                        content_cache[state_sha256].decode("utf-8")
                    )
                actual = parsed_payloads[state_sha256]
                relevant_options = tuple(
                    (option_key, options[option_key])
                    for option_key, spec in PHYSICAL_OPTIONS.items()
                    if spec["map_key"] == map_key
                )
                expected_key = (
                    map_key,
                    relevant_options,
                )
                if expected_key not in expected_payloads:
                    expected_content = authoritative_content[map_key]
                    expected = _runtime_entity_blocks(expected_content)
                    for option_key, spec in PHYSICAL_OPTIONS.items():
                        if spec["map_key"] == map_key and not options[option_key]:
                            expected = {
                                name: block
                                for name, block in expected.items()
                                if not _physical_entity_family(spec, name)
                            }
                    expected_payloads[expected_key] = expected
                expected = expected_payloads[expected_key]
                if set(actual) != set(expected):
                    missing = sorted(set(expected) - set(actual))
                    extra = sorted(set(actual) - set(expected))
                    raise AssertionError(
                        f"final runtime entity inventory drift: state={state_index} "
                        f"map={map_key} missing={missing} extra={extra}"
                    )
                changed = [name for name in expected if actual[name] != expected[name]]
                if changed:
                    raise AssertionError(
                        f"final runtime entity blocks drift: state={state_index} "
                        f"map={map_key} entities={sorted(changed)}"
                    )
                required = set()
                if map_key in fast_travel_maps:
                    required.add("ap_fast_travel_unlock")
                if map_key == "e1m1_intro":
                    required.update({
                        "ap_remove_location_visual_7770008",
                        "ap_remove_location_visual_7770009",
                        "ap_remove_location_visual_7770014",
                        "ap_remove_location_visual_7770017",
                    })
                absent = sorted(required - set(actual))
                if absent:
                    raise AssertionError(
                        f"runtime targets absent in final state: state={state_index} "
                        f"map={map_key} entities={absent}"
                    )
    return {
        "global_states": len(selections),
        "unique_map_states": len(unique_map_states),
        "unique_payload_hashes": len(unique_payload_hashes),
        "decompressed_payloads": len(content_cache),
    }


def audit_mod_payload(
    enabled: bool,
    generated_maps: Path,
    mod_root: Path,
    map_registry: Path,
    decompressor: Path | None,
    item_definitions: dict[str, Any],
    *,
    require_generated_identity: bool = True,
    visual_registry: dict[str, object] | None = None,
    content_cache: dict[str, bytes] | None = None,
) -> dict[str, dict[str, int | str]]:
    """Audit release maps against unpacked payloads and room-selected physical bases."""
    records: dict[str, dict[str, int | str]] = {}
    plans = release_plan(load_map_registry(map_registry))
    physical_map_keys = {str(spec["map_key"]) for spec in PHYSICAL_OPTIONS.values()}
    content_cache = content_cache if content_cache is not None else {}
    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        for plan in plans:
            generated_path = generated_maps / plan.generated_output
            packaged_path = _map_payload_path(mod_root, plan)
            if not generated_path.is_file() or not packaged_path.is_file():
                raise AssertionError(f"missing generated or packaged map: {plan.map_key}")
            generated = _normalized(generated_path.read_bytes())
            _, packaged = _read_cached_entities(
                packaged_path.read_bytes(), decompressor, temporary, content_cache
            )
            packaged = _normalized(packaged)
            generated_text = generated.decode("utf-8")
            packaged_text = packaged.decode("utf-8")
            if visual_registry is not None:
                validate_generated_visuals(
                    visual_registry,
                    plan.map_key,
                    generated_text,
                )
            generated_notifications = set(NOTIFICATION_RE.findall(generated_text))
            packaged_notifications = set(NOTIFICATION_RE.findall(packaged_text))
            packaged_locations = set(
                LOCATION_NOTIFICATION_RE.findall(packaged_text)
            )
            generated_identity_required = (
                require_generated_identity and plan.map_key not in physical_map_keys
            )
            if generated_identity_required and generated != packaged:
                raise AssertionError(f"generated and packaged map contents diverge: {plan.map_key}")
            if enabled:
                if not packaged_notifications:
                    raise AssertionError(f"packaged notifier entities missing: {plan.map_key}")
                if generated_identity_required and generated_notifications != packaged_notifications:
                    raise AssertionError(f"packaged notifier entity set diverges: {plan.map_key}")
                _assert_notifications(packaged_text, plan.map_key, item_definitions)
            elif "entityDef ap_rpc_item_" in packaged_text or packaged_notifications:
                raise AssertionError(f"disabled notifier payload contains entities: {plan.map_key}")
            records[plan.map_key] = {
                "generated_source_sha256": hashlib.sha256(generated).hexdigest(),
                "packaged_payload_sha256": hashlib.sha256(packaged).hexdigest(),
                "effect_entity_count": packaged_text.count("entityDef ap_rpc_v3_"),
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


def _load_item_definitions(client_dir: Path) -> dict[str, Any]:
    payload = json.loads(
        (client_dir / "data/items.json").read_text(encoding="utf-8")
    )
    if not isinstance(payload, dict):
        raise AssertionError("packaged item definitions are not an object")
    return payload


def _audit_room_resources(
    client_dir: Path,
    manifest: dict[str, object],
    map_registry: Path,
) -> tuple[
    dict[str, Any],
    list[tuple[dict[str, bool], dict[str, dict[str, Any]]]],
    dict[str, bytes],
    dict[str, bytes],
]:
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
    selections, base_members, payload_members = _audit_room_selection(
        resource_dir, payload_manifest
    )
    _, selected = selections[-1]
    assembled = dict(base_members)
    manifest_maps = cast(dict[str, dict[str, Any]], payload_manifest["maps"])
    for map_key, state in selected.items():
        if state["source"] == "replacement":
            target = str(manifest_maps[map_key]["target_member"])
            assembled[target] = payload_members[str(state["member"])]
    with tempfile.TemporaryDirectory() as directory:
        package = Path(directory) / "synthetic-room.zip"
        assembled["room_config.json"] = canonical_json(
            {"schema_version": 1, "death_link": False, "death_link_mode": "soft"}
        )
        write_deterministic_zip(assembled, package)
        with zipfile.ZipFile(package) as archive:
            if set(archive.namelist()) != set(assembled):
                raise AssertionError("synthetic room package member set drifted")
    print(
        "Room selection audit: "
        f"{len(selections)} global choices, "
        f"{len({(map_key, state['sha256']) for _, selected in selections for map_key, state in selected.items()})} "
        "unique map payload states"
    )
    return payload_manifest, selections, base_members, payload_members


def _audit_final_content(
    enabled: bool,
    generated_maps: Path,
    mod_root: Path,
    client_dir: Path,
    map_registry: Path,
    decompressor: Path | None,
    payload_manifest: dict[str, Any],
    selections: list[tuple[dict[str, bool], dict[str, dict[str, Any]]]],
    base_members: dict[str, bytes],
    payload_members: dict[str, bytes],
    visual_registry: dict[str, object],
) -> dict[str, dict[str, int | str]]:
    content_cache: dict[str, bytes] = {}
    item_definitions = _load_item_definitions(client_dir)
    stats = _audit_final_physical_states(
        client_dir / "resources",
        payload_manifest,
        generated_maps,
        map_registry,
        decompressor,
        selections,
        base_members,
        payload_members,
        content_cache,
    )
    records = audit_mod_payload(
        enabled,
        generated_maps,
        mod_root,
        map_registry,
        decompressor,
        item_definitions,
        visual_registry=visual_registry,
        content_cache=content_cache,
    )
    print(
        "Final content audit: "
        f"{stats['global_states']} global choices, "
        f"{stats['unique_map_states']} unique map states, "
        f"{stats['unique_payload_hashes']} unique payload SHA-256 values, "
        f"{len(content_cache)} decompressed payloads"
    )
    return records


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
    payload_manifest, selections, base_members, payload_members = _audit_room_resources(
        client_dir, manifest, map_registry
    )
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
    records = _audit_final_content(
        enabled,
        generated_maps,
        mod_root,
        client_dir,
        map_registry,
        decompressor,
        payload_manifest,
        selections,
        base_members,
        payload_members,
        visual_registry,
    )
    return records


def _extract_playable_zip(
    playable_zip: Path,
    destination: Path,
    mod_root: Path,
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
            workspace = Path(directory)
            mod_roots, client_dir, manifest = _extract_playable_zip(
                args.playable_zip,
                workspace / "release",
                workspace / "assembled-room-mod",
            )
            audit_release(
                args.enabled == "1", args.generated_maps, mod_roots["direct"],
                client_dir, manifest, args.map_registry, args.decompressor,
            )
            _audit_locales(args.enabled == "1", mod_roots["direct"])
        return 0
    if not all((args.mod_root, args.client_dir, args.release_manifest)):
        parser.error("local audit requires --mod-root, --client-dir, and --release-manifest")
    audit_release(args.enabled == "1", args.generated_maps, args.mod_root, args.client_dir,
                  args.release_manifest, args.map_registry, args.decompressor,
                  args.update_manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
