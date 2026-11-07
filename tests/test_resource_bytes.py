import json
from pathlib import Path

import pytest

from tools.release.prebuilt_room_resources import (
    CANONICAL_RESOURCE_FILENAMES,
    METADATA_FILENAMES,
    compute_room_resource_input_fingerprint,
    get_frozen_bundle_dir,
    validate_room_resource_integrity,
)
from tools.release.room_resource_checkout import stage_room_resource_files
from tools.release.source_bytes import compiler_source_bytes

ROOT = Path(__file__).resolve().parents[1]
NAMES = (*CANONICAL_RESOURCE_FILENAMES, *METADATA_FILENAMES)


@pytest.mark.integration
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


def test_options_projection_accepts_checkout_line_endings_and_rejects_source_edits(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tools.content import compile_options_schema as compiler

    class Toggle:
        default = False

    unused = type("UnusedOption", (), {})
    world = SimpleNamespace(game="DOOM Eternal", options_dataclass=SimpleNamespace(type_hints={"death_link": Toggle}))
    monkeypatch.setattr(compiler, "_load_apworld", lambda _: (world, Toggle, unused, unused, unused, unused, unused))
    source = tmp_path / "worlds/doometernal/options.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"death_link = False\n")
    projection = tmp_path / "options_schema.json"
    compiler.materialize_schema(tmp_path, projection)
    original = projection.read_bytes()
    source.write_bytes(b"death_link = False\r\n")
    compiler.check_schema(tmp_path, projection)
    assert compiler.materialize_schema(tmp_path, projection) == ()
    assert projection.read_bytes() == original
    source.write_bytes(b"death_link = True\r\n")
    with pytest.raises(ValueError, match="stale"):
        compiler.check_schema(tmp_path, projection)


def test_frozen_compiler_identity_retains_untracked_dependencies_and_content_changes(tmp_path):
    from doom_eap.content.compiler_identity import BUNDLED_IDENTITY_PATH, SOURCE_FILES, load_compiler_source_identity
    source = tmp_path / "source"
    for name in (*SOURCE_FILES, "data/strings/en.json", "data/strings/pt-BR.json", "data/strings/languages.json"):
        required = source / name
        required.parent.mkdir(parents=True, exist_ok=True)
        required.write_bytes(b"VALUE = 1\n")
    module = source / "doom_eap/content/new_transform.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_bytes(b"VALUE = 1\r\n")
    document = load_compiler_source_identity(source, frozen=False)
    frozen = tmp_path / "frozen"
    manifest = frozen / BUNDLED_IDENTITY_PATH
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps(document), encoding="utf-8")
    assert load_compiler_source_identity(frozen, frozen=True) == document
    module.write_bytes(b"VALUE = 1\n")
    assert load_compiler_source_identity(source, frozen=False) == document
    for name in ("doom_eap/runtime/new_owner.py", "doom_eap/contracts/core_distribution.py"):
        excluded = source / name
        excluded.parent.mkdir(parents=True, exist_ok=True)
        excluded.write_bytes(b"VALUE = 2\n")
    assert load_compiler_source_identity(source, frozen=False) == document
    module.write_bytes(b"VALUE = 2\n")
    assert load_compiler_source_identity(source, frozen=False)["fingerprint"] != document["fingerprint"]
    document["source_hashes"][module.relative_to(source).as_posix()] = "0" * 64
    manifest.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="compiler source identity"):
        load_compiler_source_identity(frozen, frozen=True)


@pytest.mark.parametrize("relative", [
    "data/strings/en.json", "data/strings/pt-BR.json", "data/strings/languages.json", "doom_eap/strings.py", "tools/release/build_string_table.py",
])
def test_player_text_changes_invalidate_room_resource_and_compiler_caches(tmp_path, relative):
    from doom_eap.content.compiler_identity import SOURCE_FILES, load_compiler_source_identity

    for name in (*SOURCE_FILES, "data/strings/en.json", "data/strings/pt-BR.json", "data/strings/languages.json"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"original\n")
    resources_before = compute_room_resource_input_fingerprint(tmp_path)[0]
    compiler_before = load_compiler_source_identity(tmp_path, frozen=False)["fingerprint"]
    (tmp_path / relative).write_bytes(b"changed\n")
    assert compute_room_resource_input_fingerprint(tmp_path)[0] != resources_before
    assert load_compiler_source_identity(tmp_path, frozen=False)["fingerprint"] != compiler_before
