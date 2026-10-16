import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from doom_eap.content import enemy_resources
from doom_eap.contracts.challenge_registry import load_challenge_registry
from tools.decls.mastery_decl_builder import _reward_free_override
from tools.release import build_room_resources as builder
from tools.release.room_payloads import (
    assemble_room_files,
    read_source_snapshots,
    read_zip,
    validate_room_payload_manifest,
    write_deterministic_zip,
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def resources(tmp_path):
    targets = {key: f"fixture/maps/{key}.entities" for key in ("e1m1_intro", "e2m1_nest")}
    raw = {"e1m1_intro": b"World\r\nentity {\r\n}\r\n", "e2m1_nest": b"Other\r\nentity {\r\n}\r\n"}
    base_files = {target: key.encode() for key, target in targets.items()}
    base_files.update({
        "gameresources/generated/decls/" + entry["decls"]["unlockable"]["path"]: _reward_free_override(entry)[0].encode()
        for entry in load_challenge_registry()["weapon_masteries"]
    })
    replacement = "replacements/e1m1_intro/chainsaw.entities"
    payload_files = {replacement: b"replacement", **{f"sources/{key}.entities": value for key, value in raw.items()}}
    manifest = {
        "schema_version": 2, "model": "dependent_map_payloads",
        "physical_option_keys": ["randomize_chainsaw", "randomize_dash", "randomize_first_battery"],
        "base_members": sorted(base_files), "context_targets": {"fixture": targets["e1m1_intro"]},
        "source_snapshots": {key: {"member": f"sources/{key}.entities", "sha256": digest(value), "size": len(value)} for key, value in raw.items()},
        "maps": {
            "e1m1_intro": {"option_keys": ["randomize_chainsaw"], "state_policy": "cartesian", "target_member": targets["e1m1_intro"], "states": [
                {"options": {"randomize_chainsaw": False}, "source": "base", "member": None, "sha256": digest(base_files[targets["e1m1_intro"]])},
                {"options": {"randomize_chainsaw": True}, "source": "replacement", "member": replacement, "sha256": digest(payload_files[replacement])},
            ]},
            "e2m1_nest": {"option_keys": [], "state_policy": "off_only", "target_member": targets["e2m1_nest"], "states": [
                {"options": {}, "source": "base", "member": None, "sha256": digest(base_files[targets["e2m1_nest"]])},
            ]},
        },
    }
    base, payload = tmp_path / "base.zip", tmp_path / "payload.zip"
    write_deterministic_zip(base_files, base)
    write_deterministic_zip(payload_files, payload)
    return SimpleNamespace(base=base, payload=payload, manifest=manifest, base_files=base_files,
                           payload_files=payload_files, raw=raw, targets=targets, replacement=replacement)


def record_reads(monkeypatch, payload):
    original = zipfile.ZipFile.read
    reads = []

    def read(archive, member, *args, **kwargs):
        if Path(archive.filename) == payload:
            reads.append(member.filename if isinstance(member, zipfile.ZipInfo) else member)
        return original(archive, member, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "read", read)
    return reads


@pytest.mark.parametrize("physical_on", [False, True])
def test_assembly_reads_only_selected_replacement_and_never_installs_sources(resources, monkeypatch, physical_on):
    reads = record_reads(monkeypatch, resources.payload)
    assembled, _ = assemble_room_files(resources.base, resources.payload, resources.manifest,
                                     {"randomize_chainsaw": physical_on, "mastery_requirements": "Original"})
    assert reads == ([resources.replacement] if physical_on else [])
    assert set(assembled) == set(resources.base_files)
    assert not any(member.startswith("sources/") for member in assembled)
    expected = b"replacement" if physical_on else b"e1m1_intro"
    assert assembled[resources.targets["e1m1_intro"]] == expected


def test_sources_are_selective_raw_utf8_without_authoring_tree(resources, monkeypatch):
    original = Path.read_bytes

    def read_bytes(path):
        assert "vanillamaps" not in path.parts
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    reads = record_reads(monkeypatch, resources.payload)
    selected = read_source_snapshots(resources.payload, resources.manifest, ["e1m1_intro"])
    assert reads == ["sources/e1m1_intro.entities"]
    assert selected["e1m1_intro"].encode("utf-8") == resources.raw["e1m1_intro"]
    assert "\r\n" in selected["e1m1_intro"]
    assert read_source_snapshots(resources.payload, resources.manifest, []) == {}
    assert reads == ["sources/e1m1_intro.entities"]


@pytest.mark.parametrize("defect", ["hash", "size", "utf8"])
def test_source_corruption_is_refused(resources, defect):
    record = resources.manifest["source_snapshots"]["e1m1_intro"]
    if defect == "hash":
        resources.payload_files[record["member"]] = resources.raw["e1m1_intro"].replace(b"World", b"Wrong")
    elif defect == "size":
        record["size"] += 1
    else:
        resources.payload_files[record["member"]] = b"\xff" * record["size"]
        record["sha256"] = digest(resources.payload_files[record["member"]])
    write_deterministic_zip(resources.payload_files, resources.payload)
    with pytest.raises(ValueError, match="snapshot"):
        read_source_snapshots(resources.payload, resources.manifest, ["e1m1_intro"])


@pytest.mark.parametrize("defect", ["old_schema", "missing_map", "extra_map", "alias", "installed_source", "replacement_source"])
def test_manifest_refuses_unsupported_or_aliased_sources(resources, defect):
    manifest = resources.manifest
    snapshots = manifest["source_snapshots"]
    if defect == "old_schema":
        manifest["schema_version"] = 1
    elif defect == "missing_map":
        del snapshots["e2m1_nest"]
    elif defect == "extra_map":
        snapshots["extra"] = copy.deepcopy(snapshots["e1m1_intro"])
    elif defect == "alias":
        snapshots["e1m1_intro"]["member"] = "sources/./e1m1_intro.entities"
    elif defect == "installed_source":
        manifest["base_members"].append(snapshots["e1m1_intro"]["member"])
    else:
        manifest["maps"]["e1m1_intro"]["states"][1]["member"] = snapshots["e1m1_intro"]["member"]
    with pytest.raises(ValueError):
        validate_room_payload_manifest(manifest)


@pytest.mark.parametrize("defect", ["missing", "extra", "duplicate", "alias", "parent", "drive"])
def test_archive_paths_and_exact_members_are_checked_even_off(resources, defect):
    entries = list(resources.payload_files.items())
    if defect == "missing":
        entries = entries[:-1]
    elif defect == "extra":
        entries.append(("extra.entities", b"extra"))
    elif defect == "duplicate":
        entries.append(entries[-1])
    else:
        bad = {"alias": "sources/./e2m1_nest.entities", "parent": "../escape.entities", "drive": "C:/escape.entities"}[defect]
        entries[-1] = (bad, entries[-1][1])
    with zipfile.ZipFile(resources.payload, "w") as archive:
        for name, data in entries:
            if defect == "duplicate" and name in archive.namelist():
                with pytest.warns(UserWarning, match="Duplicate name"):
                    archive.writestr(name, data)
            else:
                archive.writestr(name, data)
    with pytest.raises(ValueError):
        assemble_room_files(resources.base, resources.payload, resources.manifest, {"mastery_requirements": "Original"})
    with pytest.raises(ValueError):
        read_source_snapshots(resources.payload, resources.manifest, ["e1m1_intro"])


def test_read_zip_refuses_aliases_and_duplicate_names(resources):
    with zipfile.ZipFile(resources.payload, "w") as archive:
        archive.writestr("a/../escape", b"no")
    with pytest.raises(ValueError, match="unsafe"):
        read_zip(resources.payload)


@pytest.mark.parametrize("defect", [None, "missing", "size", "alias", "installed"])
def test_enemy_snapshots_participate_in_exact_archive_inventory_without_being_installed(resources, defect):
    name, data = "gameresources_patch3/native.asset", b"native asset"
    member = f"enemy_resources/stone_imp/{name}"
    snapshot = {"member": member, "size": len(data), "sha256": digest(data)}
    resources.manifest["enemy_resources"] = {"stone_imp": {"members": {name: snapshot}}}
    resources.payload_files[member] = data
    if defect == "missing":
        del resources.payload_files[member]
    elif defect == "size":
        snapshot["size"] += 1
    elif defect == "alias":
        snapshot["member"] = f"enemy_resources/stone_imp/./{name}"
    elif defect == "installed":
        resources.manifest["base_members"].append(member)
    write_deterministic_zip(resources.payload_files, resources.payload)
    if defect:
        with pytest.raises(ValueError):
            assemble_room_files(resources.base, resources.payload, resources.manifest,
                                {"mastery_requirements": "Original"})
    else:
        assembled, _ = assemble_room_files(resources.base, resources.payload, resources.manifest,
                                           {"mastery_requirements": "Original"})
        assert set(assembled) == set(resources.base_files)
        assert read_source_snapshots(resources.payload, resources.manifest, ["e1m1_intro"]) == {
            "e1m1_intro": resources.raw["e1m1_intro"].decode("utf-8")}


@pytest.fixture
def build_inputs(tmp_path, monkeypatch):
    root, staged = tmp_path / "authoring", tmp_path / "staged"
    (root / "vanillamaps").mkdir(parents=True)
    staged.mkdir()
    key = "e1m1_intro"
    raw = b"authored\r\nentity {\r\n}\r\n"
    (root / "vanillamaps/e1m1_intro.map").write_bytes(raw)
    source_registry = root / "map_sources.json"
    source_registry.write_text(json.dumps({"maps": {key: {"enabled": True, "source_file": "e1m1_intro.map",
        "resource_path": "game/sp/e1m1_intro/e1m1_intro_patch3.resources",
        "relative_entities_path": "game/sp/e1m1_intro/e1m1_intro.entities"}}}), encoding="utf-8")
    spec = SimpleNamespace(key=key, source_file="e1m1_intro.map", source_sha256=digest(raw),
                           resource_path="game/sp/e1m1_intro/e1m1_intro_patch3.resources")
    catalog = SimpleNamespace(maps={key: spec}, enabled_maps=lambda: [spec])
    calls = []

    class Compiler:
        def __init__(self, _root):
            pass

        def active_location_ids(self, _options):
            return []

        def compile_map(self, manifest, _vanilla, destination, map_key):
            calls.append(map_key)
            destination.write_bytes(json.dumps(manifest["options"], sort_keys=True).encode())

    def compress(command, **_kwargs):
        Path(command[-1]).write_bytes(b"packed:" + Path(command[-2]).read_bytes())

    monkeypatch.setattr(builder, "ModCompiler", Compiler)
    monkeypatch.setattr(builder, "SeedManifest", SimpleNamespace(create=lambda **kwargs: kwargs))
    monkeypatch.setattr(builder, "load_content_catalog", lambda _root: catalog)
    monkeypatch.setattr(builder, "compute_room_resource_input_fingerprint", lambda _root: ("compiler", {}))
    monkeypatch.setattr(builder, "patch_mission_complete_maps", lambda *args: {"unrelated_generated_entity_diff_count": 0})
    monkeypatch.setattr(builder, "dlc_contexts", lambda: [SimpleNamespace(identity="fixture", map_keys=(key,), runtime_maps=("game/sp/e1m1_intro/e1m1_intro",))])
    monkeypatch.setattr(builder.subprocess, "run", compress)
    compressor = tmp_path / "compressor.exe"
    compressor.write_bytes(b"MZfixture")
    return SimpleNamespace(root=root, staged=staged, work=tmp_path / "work", output=tmp_path / "output",
                           cache=tmp_path / "cache", compressor=compressor, sources=source_registry,
                           raw=raw, spec=spec, calls=calls)


def build(inputs):
    builder.build_room_resources(inputs.root, inputs.staged, inputs.work, inputs.compressor,
                                 inputs.sources, inputs.output, inputs.cache)


def test_builder_embeds_raw_sources_and_preserves_descriptor_identity(build_inputs):
    build(build_inputs)
    manifest = json.loads((build_inputs.output / "room_payload_manifest.json").read_bytes())
    snapshot = manifest["source_snapshots"]["e1m1_intro"]
    assert snapshot["sha256"] == build_inputs.spec.source_sha256 == digest(build_inputs.raw)
    assert snapshot["size"] == len(build_inputs.raw)
    with zipfile.ZipFile(build_inputs.output / "room_payloads.zip") as archive:
        assert archive.read(snapshot["member"]) == build_inputs.raw
    text = read_source_snapshots(build_inputs.output / "room_payloads.zip", manifest, ["e1m1_intro"])
    assert text["e1m1_intro"].encode("utf-8") == build_inputs.raw
    assert build_inputs.calls == ["e1m1_intro", "e1m1_intro"]


def test_builder_refuses_source_drift_before_compiling_or_writing_payloads(build_inputs):
    build_inputs.spec.source_sha256 = "0" * 64
    with pytest.raises(ValueError, match="descriptor"):
        build(build_inputs)
    assert not build_inputs.calls
    assert not list(build_inputs.output.iterdir())


def test_builder_embeds_verified_enemy_zip_for_cold_room_compilation(build_inputs, monkeypatch):
    payload = b"native declaration"
    name = "generated/decls/entitydef/ai/stone.decl"
    catalog = build_inputs.root / "enemy_resources.json"
    profile = {"resource_container": "gameresources_patch3", "streams": [], "assets": [{
        "name": name, "metadata": {"map_resource_name": "ai/stone", "map_resource_type": "entityDef",
            "resource_type": "rs_streamfile", "stream_hash": 0, "version": 0, "special_bytes": [0, 0, 0]},
        "sources": [{"bank": "native.resources", "size": len(payload), "sha256": digest(payload)}]}]}
    catalog.write_text(json.dumps({"revision": 1, "species": {"stone_imp": profile}}))
    payloads = {"gameresources_patch3/" + name: payload,
        "gameresources_patch3/EternalMod/assetsinfo/stone_imp.json": enemy_resources._assetsinfo_payload(profile)}
    native_zip = build_inputs.root / "stone.zip"
    write_deterministic_zip(payloads, native_zip)
    monkeypatch.setattr(builder, "embed_enemy_resource_payloads", lambda *args:
        enemy_resources.embed_enemy_resource_payloads(*args, catalog_path=catalog))
    builder.build_room_resources(build_inputs.root, build_inputs.staged, build_inputs.work,
        build_inputs.compressor, build_inputs.sources, build_inputs.output, build_inputs.cache,
        {"stone_imp": native_zip})
    manifest = json.loads((build_inputs.output / "room_payload_manifest.json").read_bytes())
    assert enemy_resources.read_enemy_resource_payloads(build_inputs.output / "room_payloads.zip", manifest,
        ["stone_imp"], catalog_path=catalog) == payloads
    assert read_source_snapshots(build_inputs.output / "room_payloads.zip", manifest, ["e1m1_intro"])
