"""Checkout views, emitted integrity and compiler inputs have distinct byte contracts."""
import hashlib
import json
from pathlib import Path

import pytest


def test_working_tree_handoff_reports_ancestry_without_claiming_clean_sources(tmp_path):
    from tools.release.source_provenance import snapshot_inputs, source_origin
    from scripts.release.assemble_ci_artifact import source_ancestry
    source = tmp_path / "module.py"
    source.write_bytes(b"value = 2\n")
    origin = source_origin("ancestor", snapshot_inputs([("module.py", source)]))
    manifest = {"build": {"source_mode": "working_tree_snapshot"}, "mod": origin}
    assert source_ancestry(manifest, "mod") == "ancestor"
    origin["resolved_sha"] = "ancestor"
    with pytest.raises(ValueError, match="provenance"):
        source_ancestry(manifest, "mod")

from tools.release.prebuilt_room_resources import (
    CANONICAL_RESOURCE_FILENAMES, METADATA_FILENAMES, compute_room_resource_input_fingerprint,
    export_prebuilt_room_resources, get_frozen_bundle_dir, validate_room_resource_integrity,
)
from tools.release.room_resource_checkout import stage_room_resource_files
from tools.release.source_bytes import compiler_source_bytes


ROOT = Path(__file__).resolve().parents[1]
NAMES = (*CANONICAL_RESOURCE_FILENAMES, *METADATA_FILENAMES)


def test_pinned_checkout_materializes_proven_bytes_without_changing_history(tmp_path):
    source = get_frozen_bundle_dir(ROOT)
    before = {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in NAMES}
    stage_room_resource_files(source, tmp_path / "stage", ROOT, NAMES)
    provenance = validate_room_resource_integrity(tmp_path / "stage")
    manifest = (tmp_path / "stage" / "room_payload_manifest.json").read_bytes()
    assert b"\r\n" not in manifest
    assert hashlib.sha256(manifest).hexdigest() == provenance["room_payload_manifest"]["sha256"]
    assert len(manifest) == provenance["room_payload_manifest"]["size"]
    with pytest.raises(ValueError, match="Release version mismatch"):
        validate_room_resource_integrity(tmp_path / "stage", expected_version="0.5.1")
    for name, digest in before.items():
        assert hashlib.sha256((source / name).read_bytes()).hexdigest() == digest
    for name in ("base_mod.zip", "room_payloads.zip"):
        assert (source / name).read_bytes() == (tmp_path / "stage" / name).read_bytes()


def test_download_bytes_are_not_normalized_and_text_changes_are_not_hidden(tmp_path):
    download = tmp_path / "download"
    stage_room_resource_files(get_frozen_bundle_dir(ROOT), download, ROOT, NAMES)
    manifest = download / "room_payload_manifest.json"
    lf = manifest.read_bytes()
    manifest.write_bytes(lf.replace(b"\n", b"\r\n"))
    stage_room_resource_files(download, tmp_path / "copied", ROOT, NAMES)
    assert (tmp_path / "copied" / manifest.name).read_bytes() == manifest.read_bytes()
    with pytest.raises(ValueError, match="Checksum mismatch"):
        validate_room_resource_integrity(tmp_path / "copied")
    manifest.write_bytes(lf + b" ")
    with pytest.raises(ValueError, match="Checksum mismatch"):
        validate_room_resource_integrity(download)


def test_stale_checkout_cannot_replace_destination_or_pinned_files(tmp_path):
    source = get_frozen_bundle_dir(ROOT)
    destination = tmp_path / "export"
    destination.mkdir()
    sentinel = destination / "base_mod.zip"
    sentinel.write_bytes(b"existing artifact")
    with pytest.raises(ValueError, match="STALE ROOM RESOURCES"):
        export_prebuilt_room_resources(source, destination, ROOT)
    assert sentinel.read_bytes() == b"existing artifact"
    with pytest.raises(ValueError, match="pinned checkout"):
        stage_room_resource_files(source, source, ROOT, NAMES)


def test_source_fingerprint_normalizes_only_owned_text_and_preserves_real_changes(tmp_path):
    source = tmp_path / "doom_eap" / "contracts" / "new_contract.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"ANSWER = 1\n")
    baseline, hashes = compute_room_resource_input_fingerprint(tmp_path)
    assert "doom_eap/contracts/new_contract.py" in hashes
    source.write_bytes(b"ANSWER = 1\r\n")
    assert compute_room_resource_input_fingerprint(tmp_path)[0] == baseline
    source.write_bytes(b"ANSWER = 2\r\n")
    assert compute_room_resource_input_fingerprint(tmp_path)[0] != baseline
    for filename in ("download.zip", "vanilla.decl"):
        raw = tmp_path / filename
        raw.write_bytes(b"opaque\r\nbytes")
        assert compiler_source_bytes(raw) == b"opaque\r\nbytes"


def test_new_json_producer_bytes_are_lf_and_semantic_changes_change_identity():
    from tools.release.room_payloads import canonical_json
    first = canonical_json({"room": 1})
    assert b"\r" not in first
    assert json.loads(first) == {"room": 1}
    assert first != canonical_json({"room": 2})


def test_visual_projection_matches_pinned_policies_across_checkout_line_endings(tmp_path):
    from doom_eap.content.automap_visual_registry import _sha256, build_authorial_registry
    source = tmp_path / "locations.json"
    source.write_bytes(b'{"location": 1}\n')
    identity = _sha256(source)
    source.write_bytes(b'{"location": 1}\r\n')
    assert _sha256(source) == identity
    source.write_bytes(b'{"location": 2}\r\n')
    assert _sha256(source) != identity
    assert build_authorial_registry(ROOT) == json.loads(
        (ROOT / "data" / "checked_location_visuals.json").read_text(encoding="utf-8"))


def test_decl_producer_emits_the_complete_lf_catalogue(tmp_path):
    from tools.decls.mastery_decl_builder import build_mastery_overrides
    audit = build_mastery_overrides(tmp_path)
    assert audit
    files = list(tmp_path.rglob("*.decl"))
    assert len(files) == 26
    assert set(audit["written_paths"]) == {path.as_posix() for path in files}
    assert all(b"\r\n" not in path.read_bytes() for path in files)


def test_native_feedback_contract_retains_existing_boundary_and_rejects_implicit_generic():
    from tools.maps.ap_map_generator import resolve_location_feedback_policy
    native = {"native_entity_contract": {"original_targets": []}}
    assert resolve_location_feedback_policy({}, "CHECK", native) == "ap_only"
    assert resolve_location_feedback_policy({"CHECK": {"policy": "vanilla_only"}}, "CHECK", native) == "vanilla_only"
    with pytest.raises(ValueError, match="Missing explicit"):
        resolve_location_feedback_policy({}, "CHECK", {})
    with pytest.raises(ValueError, match="Invalid location feedback policy"):
        resolve_location_feedback_policy({"CHECK": {"policy": "invalid"}}, "CHECK", native)


def test_secondary_check_source_requires_a_declared_primary():
    from tools.maps.automap_baseline_guard import find_check_source
    source = 'entity {\n entityDef pickup {\n class = "idProp2";\n }\n}\n'
    declared = {"AP_CHECK_PICKUP": 1, "AP_CHECK_PICKUP_B": 2}
    name, bounds = find_check_source(source, "AP_CHECK_PICKUP_B", declared)
    assert name == "pickup" and bounds is not None
    assert find_check_source(source, "AP_CHECK_PICKUP_B", {"AP_CHECK_PICKUP_B": 2})[1] is None
    assert find_check_source(source, "AP_CHECK_UNKNOWN_B", declared)[1] is None


def test_frozen_compiler_identity_retains_untracked_dependencies_and_content_changes(tmp_path):
    from doom_eap.content.compiler_identity import load_compiler_source_identity, BUNDLED_IDENTITY_PATH
    source = tmp_path / "source"
    module = source / "doom_eap/runtime/new_owner.py"
    module.parent.mkdir(parents=True)
    module.write_bytes(b"VALUE = 1\r\n")
    document = load_compiler_source_identity(source, frozen=False)
    frozen = tmp_path / "frozen"
    manifest = frozen / BUNDLED_IDENTITY_PATH
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps(document), encoding="utf-8")
    assert load_compiler_source_identity(frozen, frozen=True) == document
    module.write_bytes(b"VALUE = 1\n")
    assert load_compiler_source_identity(source, frozen=False) == document
    module.write_bytes(b"VALUE = 2\n")
    assert load_compiler_source_identity(source, frozen=False)["fingerprint"] != document["fingerprint"]
    document["source_hashes"]["doom_eap/runtime/new_owner.py"] = "0" * 64
    manifest.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="compiler source identity"):
        load_compiler_source_identity(frozen, frozen=True)


def test_working_tree_origin_is_exact_and_does_not_claim_its_ancestor_as_built_commit(tmp_path):
    from tools.release.source_provenance import capture_release_sources, source_origin
    mod, ap = tmp_path / "mod", tmp_path / "ap"
    (mod / "doom_eap").mkdir(parents=True)
    ap.mkdir()
    module = mod / "doom_eap/new_owner.py"
    module.write_bytes(b"VALUE = 1\r\n")
    (ap / "CommonClient.py").write_bytes(b"CLIENT = 1\n")
    state = capture_release_sources(mod, ap)
    record = source_origin("ancestor", state["mod"])
    assert record["base_commit_sha"] == "ancestor" and "resolved_sha" not in record
    assert record["source_state"]["files"]["doom_eap/new_owner.py"]["sha256"] == hashlib.sha256(module.read_bytes()).hexdigest()
    module.write_bytes(b"VALUE = 1\n")
    assert capture_release_sources(mod, ap)["mod"]["fingerprint"] != state["mod"]["fingerprint"]
    state["mod"]["fingerprint"] = "0" * 64
    with pytest.raises(ValueError, match="source evidence"):
        source_origin("ancestor", state["mod"])
