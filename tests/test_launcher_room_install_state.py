import hashlib
import json
import zipfile
from pathlib import Path
from typing import cast

from launcher_core import ModCompiler, RoomSnapshot, release_identity
from launcher_integration import IntegratedLaunchWorkflow, RoomSetupCoordinator


def _snapshot(seed_name: str = "room-seed", dash: bool = False) -> RoomSnapshot:
    options = {"randomize_chainsaw": False, "randomize_dash": dash, "randomize_first_battery": False}
    ids = ModCompiler().active_location_ids(options)
    identity = release_identity()
    return RoomSnapshot.from_packets(
        {"seed_name": seed_name},
        {
            "team": 1,
            "slot": 2,
            "slot_data": {
                "randomize_chainsaw": False,
                "randomize_dash": dash,
                "randomize_first_battery": False,
                "bridge_protocol": identity["bridge_protocol_version"],
                "content_revision": identity["content_revision"],
            },
            "missing_locations": ids[::2],
            "checked_locations": ids[1::2],
        },
    )


def _workflow(tmp_path: Path) -> IntegratedLaunchWorkflow:
    root = Path(__file__).resolve().parents[1]
    return IntegratedLaunchWorkflow(root, tmp_path / "state", tmp_path / "launcher.json")


def _write_matching_install(workflow: IntegratedLaunchWorkflow, snapshot: RoomSnapshot) -> Path:
    manifest = workflow.base_workflow.manifest_for(snapshot)
    staged = workflow.state_dir / "Mods" / "room-bound.zip"
    staged.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(staged, "w") as package:
        package.writestr("seed_manifest.json", json.dumps(manifest.document()))
    workflow.state_dir.mkdir(parents=True, exist_ok=True)
    workflow.state_dir.joinpath("launcher_setup.json").write_text(
        json.dumps(
            {
                "manifest_hash": manifest.manifest_hash,
                "staged_mod": str(staged),
                "staged_sha256": hashlib.sha256(staged.read_bytes()).hexdigest(),
                "adapter_state": "applied",
                "steam_launch_option": "launch option",
            }
        )
    )
    return staged


def test_matching_room_install_is_already_installed(tmp_path):
    workflow = _workflow(tmp_path)
    snapshot = _snapshot()
    _write_matching_install(workflow, snapshot)

    state = workflow.install_state(snapshot)

    assert state.state == "already_installed"
    assert state.steam_launch_option == "launch option"


def test_missing_or_different_package_needs_install(tmp_path):
    workflow = _workflow(tmp_path)
    snapshot = _snapshot()
    staged = _write_matching_install(workflow, snapshot)
    staged.unlink()
    assert workflow.install_state(snapshot).state == "install_needed"

    _write_matching_install(workflow, snapshot)
    assert workflow.install_state(_snapshot(seed_name="another-room")).state == "install_needed"


def test_connect_observation_does_not_start_setup():
    started = []
    events = []

    class FakeWorkflow:
        def execute(self, *_args, **_kwargs):
            started.append(True)

    coordinator = RoomSetupCoordinator(cast(IntegratedLaunchWorkflow, FakeWorkflow()), lambda kind, payload: events.append((kind, payload)), lambda _record: None)
    event = {
        "type": "connected",
        "seed_name": "room-seed",
        "team": 1,
        "slot": 2,
        "slot_data": {
            "randomize_chainsaw": False,
            "randomize_dash": False,
            "randomize_first_battery": False,
        },
    }

    assert coordinator.observe(event)
    assert not started
    assert events == []
