import json

from launcher_core import DASH_ENTITY, DASH_LOCATION_ID, LaunchWorkflow, ModCompiler, SeedManifest, release_identity


def test_seed_manifest_is_deterministic():
    compiler = ModCompiler()
    one = SeedManifest.create(seed_name="beta", team=0, slot=1, options={"randomize_dash": True}, active_location_ids=compiler.active_location_ids(True))
    two = SeedManifest.create(seed_name="beta", team=0, slot=1, options={"randomize_dash": True}, active_location_ids=compiler.active_location_ids(True))
    assert one.manifest_hash == two.manifest_hash


def test_dash_false_removes_compiler_location(tmp_path):
    compiler = ModCompiler()
    manifest = SeedManifest.create(seed_name="beta", team=0, slot=1, options={"randomize_dash": False}, active_location_ids=compiler.active_location_ids(False))
    compiler.compile(manifest, tmp_path)
    config = json.loads((tmp_path / "e1m2_war.locations.json").read_text())
    assert DASH_LOCATION_ID not in manifest.active_location_ids
    assert DASH_ENTITY not in config["entities"]


def test_dash_true_keeps_compiler_mutation(tmp_path):
    compiler = ModCompiler()
    manifest = SeedManifest.create(seed_name="beta", team=0, slot=1, options={"randomize_dash": True}, active_location_ids=compiler.active_location_ids(True))
    compiler.compile(manifest, tmp_path)
    config = json.loads((tmp_path / "e1m2_war.locations.json").read_text())
    assert DASH_LOCATION_ID in manifest.active_location_ids
    assert config["entities"][DASH_ENTITY] == DASH_LOCATION_ID


def test_join_uses_room_options_and_writes_config(tmp_path):
    manifest = LaunchWorkflow().join(
        {"seed_name": "beta", "team": 0, "slot": 1, "options": {"randomize_dash": True}},
        tmp_path, "archipelago.example:38281",
    )
    assert manifest.options == {"randomize_dash": True}
    assert json.loads((tmp_path / "ap_config.json").read_text())["seed_manifest_hash"] == manifest.manifest_hash


def test_release_identity_has_required_revisions():
    identity = release_identity()
    assert identity["game"] == "DOOM Eternal"
    assert identity["bridge_protocol_version"] == 4
    assert identity["compiler_revision"] == 2
