import hashlib
import json
import struct
import zipfile

import pytest

from doom_eap.content import enemy_resources
from doom_eap.launcher import campaign_resources


def _hash(payload):
    return hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize(("requested", "expected"), [
    ({"zombie_maykr", "imp"}, {"zombie_maykr"}),
    ({"imp"}, set()),
    ({"blood_angel"}, {"blood_angel"}),
])
def test_required_resources_include_base_supplements_and_missing_dlc(tmp_path, requested, expected):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"revision": 1, "species": {"zombie_maykr": {}}}), encoding="utf-8")
    assert enemy_resources.required_enemy_resource_species(requested, catalog_path=catalog) == expected


def test_resource_selection_rejects_unsupported_catalog_revision(tmp_path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"revision": 2, "species": {"zombie_maykr": {}}}), encoding="utf-8")
    with pytest.raises(ValueError, match="catalog revision"):
        enemy_resources.required_enemy_resource_species({"zombie_maykr"}, catalog_path=catalog)


@pytest.fixture
def native_resources(tmp_path, monkeypatch):
    name = "generated/decls/entitydef/ai/fodder/imp_stone.decl"
    resource = {"payload": b"native patch declaration", "kind": "rs_streamfile"}
    chunks = [b"native first chunk", b"native second chunk"]
    data = bytearray(128 + len(chunks[1]))
    struct.pack_into("<Q6i", data, 0, 0x61C7F32E29C2A550, 0, 0, 0, 0, 0, 2)
    offsets = [80, 128]
    for index, payload in enumerate(chunks):
        struct.pack_into("<QII", data, 32 + index * 16, 41 + index, offsets[index] // 16, len(payload))
        data[offsets[index]:offsets[index] + len(payload)] = payload
    stream_path = tmp_path / "native.streamdb"
    stream_path.write_bytes(data)
    wrapper = b"STREAMDB" + struct.pack("<I4I", 2, 28, len(chunks[0]), 28 + len(chunks[0]), len(chunks[1])) + b"".join(chunks)
    catalog = {"revision": 1, "species": {"stone_imp": {
        "resource_container": "gameresources_patch3",
        "assets": [{"name": name, "metadata": {"map_resource_name": "ai/fodder/imp_stone",
            "map_resource_type": "entityDef", "resource_type": "rs_streamfile",
            "stream_hash": (1 << 64) - 1234, "version": 0, "special_bytes": [0, 1, 2]}, "sources": [
                {"bank": "base.resources", "size": 4, "sha256": _hash(b"base")},
                {"bank": "patch.resources", "size": len(resource["payload"]), "sha256": _hash(resource["payload"])},
            ]}],
        "stream_sources": {"native.streamdb": {"index_size": 64, "index_sha256": _hash(bytes(data[:64]))}},
        "streams": [{"name": "streamdb/stone_id#41.bin", "first_file_id": 41,
            "size": len(wrapper), "sha256": _hash(wrapper), "chunks": [
                {"bank": "native.streamdb", "index_row": index, "file_id": 41 + index,
                 "offset": offsets[index], "size": len(payload), "sha256": _hash(payload)}
                for index, payload in enumerate(chunks)]}],
    }}}
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

    def records(path, wanted, *, decode=None):
        assert path.name == "patch.resources" and wanted == {name}
        return {name: (0, resource["kind"], 0, resource["payload"], 0)}

    monkeypatch.setattr(enemy_resources, "_records", records)
    return tmp_path, name, resource, stream_path, catalog_path, wrapper


def test_native_variant_selection_preserves_resource_names_and_stream_bytes(native_resources):
    base, name, resource, _, catalog, wrapper = native_resources
    with pytest.raises(ValueError, match="explicit native variant"):
        enemy_resources.build_enemy_resources(base, "stone_imp", catalog_path=catalog)
    payloads = enemy_resources.build_enemy_resources(
        base, "stone_imp", variants={name: "patch.resources"}, catalog_path=catalog)
    assert payloads["gameresources_patch3/" + name] == resource["payload"]
    assert payloads["streamdb/stone_id#41.bin"] == wrapper
    asset = json.loads(payloads["gameresources_patch3/EternalMod/assetsinfo/stone_imp.json"])["assets"][0]
    assert f"generated/decls/{asset['mapResourceType'].lower()}/{asset['name']}.decl" == name
    assert asset["resourceType"] == "rs_streamfile" and asset["specialByte2"] == 1
    assert asset["streamDbHash"] == -1234


@pytest.mark.parametrize("corruption", ["resource_payload", "resource_type", "stream_index", "stream_payload"])
def test_modified_native_resources_are_rejected(native_resources, corruption):
    base, name, resource, stream, catalog, _ = native_resources
    if corruption == "resource_payload":
        resource["payload"] = b"different bytes"
    elif corruption == "resource_type":
        resource["kind"] = "image"
    else:
        data = bytearray(stream.read_bytes())
        data[32 if corruption == "stream_index" else 80] ^= 1
        stream.write_bytes(data)
    with pytest.raises(ValueError, match="differs from the catalog"):
        enemy_resources.build_enemy_resources(
            base, "stone_imp", variants={name: "patch.resources"}, catalog_path=catalog)


def test_binary_and_declaration_names_keep_distinct_native_types(native_resources, monkeypatch):
    base, name, resource, _, catalog, _ = native_resources
    data = json.loads(catalog.read_text())
    binary_name = "ai/fodder/imp_stone"
    assets = data["species"]["stone_imp"]["assets"]
    assets.append({"name": binary_name, "metadata": {**assets[0]["metadata"],
        "map_resource_name": "", "map_resource_type": "binaryGoreContainer",
        "resource_type": "binaryGoreContainer"}, "sources": [assets[0]["sources"][1]]})
    catalog.write_text(json.dumps(data))

    def records(path, wanted, *, decode=None):
        return {key: (index, "rs_streamfile" if key == name else "binaryGoreContainer", 0, resource["payload"], 0)
                for index, key in enumerate(sorted(wanted))}

    monkeypatch.setattr(enemy_resources, "_records", records)
    payloads = enemy_resources.build_enemy_resources(
        base, "stone_imp", variants={name: "patch.resources"}, catalog_path=catalog)
    assets_info = json.loads(payloads["gameresources_patch3/EternalMod/assetsinfo/stone_imp.json"])["assets"]
    for resource_name, kind in [(binary_name, "binaryGoreContainer"), (name, "rs_streamfile")]:
        matched = next(asset for asset in assets_info if resource_name in (
            asset["name"], f"generated/decls/{asset['mapResourceType'].lower()}/{asset['name']}.decl"))
        assert matched["resourceType"] == kind


@pytest.fixture
def packaged_resources(native_resources):
    base, name, _, _, catalog, _ = native_resources
    document = json.loads(catalog.read_text())
    document["species"]["stone_imp"]["variants"] = {name: "patch.resources"}
    catalog.write_text(json.dumps(document))
    payloads = enemy_resources.build_enemy_resources(base, "stone_imp", catalog_path=catalog)
    members, manifest = {}, {}
    enemy_resources.embed_enemy_resource_payloads(members, manifest, {"stone_imp": payloads}, catalog_path=catalog)
    archive = base / "payloads.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for member, data in members.items():
            output.writestr(member, data)
    return archive, catalog, payloads, members, manifest


def test_packaged_native_resources_work_without_game_banks_or_streams(packaged_resources, monkeypatch):
    archive, catalog, payloads, _, manifest = packaged_resources
    (archive.parent / "native.streamdb").unlink()
    monkeypatch.setattr(enemy_resources, "_records", lambda *args, **kwargs: pytest.fail("game bank read"))
    assert enemy_resources.read_enemy_resource_payloads(archive, manifest, ["stone_imp"], catalog_path=catalog) == payloads
    assert enemy_resources.read_enemy_resource_payloads(archive.parent / "absent.zip", {}, []) == {}


@pytest.mark.parametrize("defect", ["profile", "inventory", "variant", "payload", "missing", "duplicate"])
def test_packaged_resources_refuse_missing_corrupt_or_resigned_native_bytes(packaged_resources, defect):
    archive, catalog, _, members, manifest = packaged_resources
    name, snapshot = next(iter(manifest["enemy_resources"]["stone_imp"]["members"].items()))
    if defect == "profile":
        manifest["enemy_resources"].clear()
    elif defect == "inventory":
        del manifest["enemy_resources"]["stone_imp"]["members"][name]
    elif defect == "variant":
        members[snapshot["member"]] = b"resigned different native variant"
        snapshot.update(size=len(members[snapshot["member"]]), sha256=_hash(members[snapshot["member"]]))
    elif defect == "payload":
        members[snapshot["member"]] = b"corrupt"
    elif defect == "missing":
        del members[snapshot["member"]]
    with zipfile.ZipFile(archive, "w") as output:
        for member, data in members.items():
            output.writestr(member, data)
        if defect == "duplicate":
            with pytest.warns(UserWarning, match="Duplicate name"):
                output.writestr(snapshot["member"], members[snapshot["member"]])
    with pytest.raises(ValueError, match="snapshot|catalog"):
        enemy_resources.read_enemy_resource_payloads(archive, manifest, ["stone_imp"], catalog_path=catalog)


def test_embedding_publishes_nothing_if_another_species_has_no_native_profile(packaged_resources):
    _, catalog, payloads, _, _ = packaged_resources
    members, manifest = {"unrelated": b"kept"}, {"unrelated": True}
    with pytest.raises(ValueError, match="No local resource profile"):
        enemy_resources.embed_enemy_resource_payloads(
            members, manifest, {"stone_imp": payloads, "unknown": payloads}, catalog_path=catalog)
    assert members == {"unrelated": b"kept"} and manifest == {"unrelated": True}


@pytest.fixture(params=[False, True])
def installed_mask(native_resources, monkeypatch, request):
    base, name, resource, _, catalog, _ = native_resources
    mask_is_last = request.param
    mask = b"HASH" + struct.pack("<I", 3) + b"".join(
        struct.pack("<QIQ", native_hash, 1, bits)
        for native_hash, bits in [(11, 3), (22, 4), (33, 8)])
    meta = bytearray(512)
    struct.pack_into("<Q", meta, 80, 128)
    struct.pack_into("<QQQ", meta, 128 + 56, 512, len(mask), len(mask))
    meta.extend(mask)
    if not mask_is_last:
        meta.extend(b"another native payload")
    (base / "meta.resources").write_bytes(meta)
    mapping = b"before.resources;8\n./gameresources_patch3.resources;28\nafter.resources;48\n"
    (base / "idRehash.map").write_bytes(mapping)

    def records(path, wanted, *, decode=None):
        if path.name == "gameresources_patch3.resources":
            return {name: (64, resource["kind"], 0, resource["payload"], 0)}
        data = path.read_bytes()
        at, size, _ = struct.unpack_from("<QQQ", data, 128 + 56)
        return {"generated/buildgame/container.mask": (0, "file", at, data[at:at + size], 0)}

    monkeypatch.setattr(enemy_resources, "_records", records)
    monkeypatch.setattr(campaign_resources, "_records", records)
    return base, name, catalog, mask, meta, mapping, mask_is_last


def test_activation_grows_mask_preserves_other_resources_and_rehash_offsets(installed_mask):
    base, name, catalog, mask, meta, mapping, mask_is_last = installed_mask
    updates, report = enemy_resources.prepare_enemy_resource_activation(
        base, "stone_imp", variants={name: "patch.resources"}, catalog_path=catalog)
    assert (base / "meta.resources").read_bytes() == meta
    assert (base / "idRehash.map").read_bytes() == mapping
    assert report["changed_bits"] == 1 and report["added_mask_words"] == 1
    updated = updates["meta.resources"]
    at, size, decoded_size = struct.unpack_from("<QQQ", updated, 128 + 56)
    assert size == decoded_size == len(mask) + 8
    result = updated[at:at + size]
    assert result[:28] == mask[:28]
    assert struct.unpack_from("<Q", result, 28)[0] == 22
    assert struct.unpack_from("<IQQ", result, 36) == (2, 4, 1)
    assert result[56:] == mask[48:]
    assert updates["idRehash.map"] == b"before.resources;8\n./gameresources_patch3.resources;28\nafter.resources;56\n"
    if not mask_is_last:
        assert updated[512:len(meta)] == meta[512:]
    for filename, payload in updates.items():
        (base / filename).write_bytes(payload)
    repeated, second_report = enemy_resources.prepare_enemy_resource_activation(
        base, "stone_imp", variants={name: "patch.resources"}, catalog_path=catalog)
    assert repeated == updates
    assert second_report["changed_bits"] == second_report["added_mask_words"] == 0


def test_resource_activation_rolls_back_second_publication_failure_then_retries(installed_mask, monkeypatch):
    from doom_eap.launcher import launcher_platform

    base, _, _, _, meta, mapping, _ = installed_mask
    updates, report = campaign_resources.prepare_resource_activation(base, {"gameresources_patch3.resources": {64}})
    attempts, fail = [], True

    def publish(source, destination, *, operation):
        attempts.append((destination.name, operation))
        if fail and destination.name == "idRehash.map" and operation == "resource_activation":
            raise OSError("injected second publication failure")
        source.replace(destination)

    monkeypatch.setattr(launcher_platform, "publish_file", publish)
    with pytest.raises(OSError, match="second publication"):
        campaign_resources.publish_resource_activation(base, updates, report)
    assert (base / "meta.resources").read_bytes() == meta
    assert (base / "idRehash.map").read_bytes() == mapping
    assert attempts[-1] == ("meta.resources", "resource_activation_rollback")
    assert not list(base.glob(".doomeap-mask-*"))
    fail = False
    campaign_resources.publish_resource_activation(base, updates, report)
    assert all((base / name).read_bytes() == payload for name, payload in updates.items())
    attempts.clear()
    repeated, repeated_report = campaign_resources.prepare_resource_activation(base, {"gameresources_patch3.resources": {64}})
    campaign_resources.publish_resource_activation(base, repeated, repeated_report)
    assert not attempts


def test_resource_activation_rejects_metadata_drift_before_any_publication(installed_mask):
    base, _, _, _, meta, mapping, _ = installed_mask
    updates, report = campaign_resources.prepare_resource_activation(base, {"gameresources_patch3.resources": {64}})
    (base / "idRehash.map").write_bytes(mapping + b"external change")
    with pytest.raises(RuntimeError, match="metadata changed"):
        campaign_resources.publish_resource_activation(base, updates, report)
    assert (base / "meta.resources").read_bytes() == meta
    assert (base / "idRehash.map").read_bytes() == mapping + b"external change"
    assert not list(base.glob(".doomeap-mask-*"))


def test_resource_activation_expands_two_tables_and_preserves_mapping_encoding(installed_mask):
    base, _, _, mask, _, mapping, _ = installed_mask
    mapping = b"\xef\xbb\xbf" + mapping.replace(b"\n", b"\r\n")
    (base / "idRehash.map").write_bytes(mapping)
    updates, report = campaign_resources.prepare_resource_activation(
        base, {"before.resources": {130}, "gameresources_patch3.resources": {64}})
    assert report["containers"]["before.resources"]["added_mask_words"] == 2
    assert report["containers"]["gameresources_patch3.resources"]["added_mask_words"] == 1
    assert updates["idRehash.map"] == b"\xef\xbb\xbfbefore.resources;8\r\n./gameresources_patch3.resources;44\r\nafter.resources;72\r\n"
    at, size, _ = struct.unpack_from("<QQQ", updates["meta.resources"], 128 + 56)
    result = updates["meta.resources"][at:at + size]
    assert result[-20:] == mask[-20:]


@pytest.mark.parametrize("corruption", [None, "payload", "type", "population"])
@pytest.mark.parametrize("species_id", ["stone_imp", "zombie_maykr"])
def test_room_finalizer_verifies_packaged_enemies_and_enables_both_containers(
    installed_mask, native_resources, monkeypatch, corruption, species_id,
):
    from doom_eap.launcher import launcher_platform
    from tools.maps.encounter_populations import PRIVATE_POPULATION_PREFIX

    population_member = PRIVATE_POPULATION_PREFIX + "fixture.decl"
    population_name = population_member.split("/", 1)[1]
    population_payload = b"private population assignment"

    base, name, catalog, _, meta, mapping, _ = installed_mask
    _, _, resource, _, _, _ = native_resources
    game_base = base / "game" / "base"
    game_base.mkdir(parents=True)
    for filename in ("meta.resources", "idRehash.map"):
        (base / filename).replace(game_base / filename)
    base = game_base
    mapping = mapping.replace(b"before.resources", b"gameresources_patch2.resources")
    (base / "idRehash.map").write_bytes(mapping)
    catalog_document = json.loads(catalog.read_text())
    profile = catalog_document["species"]["stone_imp"]
    if species_id != "stone_imp":
        catalog_document["species"][species_id] = catalog_document["species"].pop("stone_imp")
        catalog.write_text(json.dumps(catalog_document))
    required_species = enemy_resources.required_enemy_resource_species
    monkeypatch.setattr(enemy_resources, "required_enemy_resource_species",
                        lambda species: required_species(species, catalog_path=catalog))

    def load_profile(species):
        assert species == species_id
        return profile

    records = campaign_resources._records

    def imported(path, wanted, *, decode=None):
        if path.name == "gameresources_patch2.resources":
            return {key: ((60 + len(campaign_resources.STREAMFILES)) if key == population_name else 60 + index,
                          "rs_streamfile", 0,
                          (b"corrupted" if corruption == "population" else population_payload)
                          if key == population_name else b"campaign:" + key.encode(), 0)
                    for index, key in enumerate(sorted(wanted))}
        return records(path, wanted, decode=decode)

    monkeypatch.setattr(campaign_resources, "_records", imported)
    monkeypatch.setattr(enemy_resources, "load_enemy_resource_profile", load_profile)
    monkeypatch.setattr(launcher_platform, "detect_doom_processes", lambda: [])
    monkeypatch.setattr(launcher_platform, "publish_file", lambda source, destination, **_: source.replace(destination))
    package = base / "room.zip"
    manifest = {"required_capabilities": ["unified_campaign_v1"], "options": {"enemy_plan": {
        "maps": {"e1m1_intro": {"operations": [{"waves": [{"instances": [{"species": species_id}]}]}]}}}}}
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("seed_manifest.json", json.dumps(manifest))
        archive.writestr(population_member, population_payload)
        archive.writestr("gameresources_patch3/" + name, b"corrupted" if corruption == "payload" else resource["payload"])
        for key in campaign_resources.STREAMFILES:
            archive.writestr("gameresources_patch2/" + key, b"campaign:" + key.encode())
    if corruption == "type":
        resource["kind"] = "wrong native type"
    if corruption:
        with pytest.raises(ValueError, match="enemy resource differs"):
            campaign_resources.enable_campaign_resources(base.parent, package)
        assert (base / "meta.resources").read_bytes() == meta
        assert (base / "idRehash.map").read_bytes() == mapping
    else:
        result = campaign_resources.enable_campaign_resources(base.parent, package)
        assert result["state"] == "enabled"
        assert len(result["entries"]) == len(campaign_resources.STREAMFILES)
        assert result["added_mask_words"] == 1
        assert result["enemy_resources"] == {
            "gameresources_patch2.resources": {
                "verified_resources": 1, "changed_bits": len(campaign_resources.STREAMFILES) + 1, "added_mask_words": 1},
            "gameresources_patch3.resources": {
                "verified_resources": 1, "changed_bits": 1, "added_mask_words": 1}}
        again = campaign_resources.enable_campaign_resources(base.parent, package)
        assert again["changed_bits"] == 0
        assert all(row["changed_bits"] == 0 for row in again["enemy_resources"].values())
