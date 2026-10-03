"""Checkout views, emitted integrity and compiler inputs have distinct byte contracts."""
import hashlib
import json
import struct
from pathlib import Path

import pytest



from tools.release.prebuilt_room_resources import (
    CANONICAL_RESOURCE_FILENAMES, METADATA_FILENAMES, compute_room_resource_input_fingerprint,
    get_frozen_bundle_dir, validate_room_resource_integrity,
)
from tools.release.room_resource_checkout import stage_room_resource_files
from tools.release.source_bytes import compiler_source_bytes


ROOT = Path(__file__).resolve().parents[1]
NAMES = (*CANONICAL_RESOURCE_FILENAMES, *METADATA_FILENAMES)


def test_every_hud_counter_is_a_root_sibling():
    recipes = json.loads((ROOT / "packaging/presentation-swf-patches.json").read_text())
    variants = [entry for entry in recipes if entry["resource"].endswith("/swf/hud/hud_score.swf")]
    assert any("e1m1_intro" in entry["resource"] for entry in variants)
    assert any("e4m1" in entry["resource"] for entry in variants)
    assert any("e5m1" in entry["resource"] for entry in variants)
    for entry in variants:
        data = (ROOT / "packaging/mod_assets" / entry["resource"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["result_sha256"]
        position = 27

        def integer():
            nonlocal position
            value = struct.unpack_from(">I", data, position)[0]
            position += 4
            return value

        count = integer()
        position += 4 * count
        for _ in range(integer()):
            integer()
            length = struct.unpack_from("<I", data, position)[0]
            position += 4 + length
        placements = []
        for _ in range(integer()):
            tag, length = integer(), integer()
            placements.append((tag, data[position:position + length]))
            position += length
        assert sum(tag in (26, 70) and b"apFoundLabel\0" in payload for tag, payload in placements) == 1, entry["resource"]




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
