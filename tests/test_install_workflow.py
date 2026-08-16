from doom_eap.launcher.launcher_core import LaunchWorkflow, ModCompiler, RoomSnapshot, release_identity


def _snapshot() -> RoomSnapshot:
    ids = ModCompiler().active_location_ids(False)
    identity = release_identity()
    return RoomSnapshot.from_packets(
        {"seed_name": "install-seed"},
        {
            "team": 1,
            "slot": 2,
            "slot_data": {
                "randomize_chainsaw": False,
                "randomize_dash": False,
                "randomize_first_battery": False,
                "bridge_protocol": 4,
                "content_revision": identity["content_revision"],
            },
            "missing_locations": ids[::2],
            "checked_locations": ids[1::2],
        },
    )


def test_same_manifest_install_is_idempotent(tmp_path):
    workflow = LaunchWorkflow()
    target = tmp_path / "active"
    first = workflow.execute(_snapshot(), target)
    before = (target / "seed_manifest.json").stat().st_mtime_ns
    second = workflow.execute(_snapshot(), target)
    assert second.manifest_hash == first.manifest_hash
    assert (target / "seed_manifest.json").stat().st_mtime_ns == before
