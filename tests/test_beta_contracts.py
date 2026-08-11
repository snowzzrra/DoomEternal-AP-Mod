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
    assert manifest.options == {
        "randomize_chainsaw": False,
        "randomize_dash": True,
        "randomize_first_battery": False,
    }
    assert json.loads((tmp_path / "ap_config.json").read_text())["seed_manifest_hash"] == manifest.manifest_hash


def test_native_runtime_config_updates_without_persisting_password(tmp_path):
    client_dir = tmp_path / "client"
    remote_a = client_dir / "userdata" / "111111" / "782330" / "remote"
    remote_b = client_dir / "userdata" / "222222" / "782330" / "remote"
    remote_a.mkdir(parents=True)
    remote_b.mkdir(parents=True)

    LaunchWorkflow.write_client_config(
        client_dir,
        endpoint="room-a:38281",
        manifest_hash="manifest-a",
        runtime_config={
            "steam_remote_dir": str(remote_a),
            "doom_base_dir": "game-a/base",
            "save_games_dir": "saves-a",
            "password": "must-not-persist",
        },
    )
    first = json.loads((client_dir / "ap_config.json").read_text())
    assert first["steam_remote_dir"] == str(remote_a)
    assert first["steam_id3"] == 111111
    assert first["server_address"] == "room-a:38281"
    assert "password" not in first

    LaunchWorkflow.write_client_config(
        client_dir,
        endpoint="room-b:38281",
        manifest_hash="manifest-b",
        runtime_config={
            "steam_remote_dir": str(remote_b),
            "doom_base_dir": "game-b/base",
            "save_games_dir": "saves-b",
        },
    )
    second = json.loads((client_dir / "ap_config.json").read_text())
    assert second["steam_remote_dir"] == str(remote_b)
    assert second["steam_id3"] == 222222
    assert second["server_address"] == "room-b:38281"
    assert second["seed_manifest_hash"] == "manifest-b"
    assert second["doom_base_dir"] == "game-b/base"
    assert second["save_games_dir"] == "saves-b"
    assert "password" not in second


def test_release_identity_has_required_revisions():
    identity = release_identity()
    assert identity["game"] == "DOOM Eternal"
    assert identity["bridge_protocol_version"] == 4
    assert identity["compiler_revision"] == 2
