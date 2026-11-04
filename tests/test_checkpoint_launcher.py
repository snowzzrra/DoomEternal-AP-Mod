import ctypes
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from doom_eap.launcher.launcher_session import APSessionOwner, CheckpointRecoveryBlockedError
from doom_eap.runtime.weapon_points import namespace_id


@pytest.fixture
def campaign(tmp_path, monkeypatch):
    from doom_eap.launcher import launcher_session as session

    slot_data = json.loads((Path(__file__).parent / "fixtures/resolved_room_slot_data.json").read_text())
    snapshot = SimpleNamespace(seed_name="fixture", team=0, slot=1, slot_data=slot_data)
    namespace = namespace_id(snapshot.seed_name, 0, 1, slot_data["native_generation_fingerprint"])
    owner = APSessionOwner(tmp_path / "client", tmp_path / "data", tmp_path / "state")
    game, runtime = tmp_path / "game", tmp_path / "runtime"
    remote, local = tmp_path / "Steam/userdata/1/782330/remote", tmp_path / "local"
    directory = owner.data_dir / "campaigns" / namespace
    native = "ap-" + namespace[:40]
    for folder in (game, runtime, remote / native / "GAME-AUTOSAVE0", local, directory):
        folder.mkdir(parents=True)
    (directory / "native.root").write_text(f"sentinel-native-root-v1\nnamespace={namespace}\nroot={native}\n", newline="\n")
    contract = (f"sentinel-campaign-v2\nnamespace={namespace}\ngeneration={slot_data['native_generation_fingerprint']}\n"
                f"provenance=synthetic-fixture\ncampaign=unified\nstarting_stage=hub\n"
                f"difficulty={slot_data['runtime']['difficulty']['engine']}\nslot=AUTOSAVE0\n")
    (directory / "campaign.contract").write_text(contract, newline="\n")
    (directory / "campaign.checkpoint").write_bytes(b"sentinel-checkpoint-ab-v1\n")
    payload = remote / native / "GAME-AUTOSAVE0/game.details"
    payload.write_bytes(b"interrupted native bytes")
    helper = b'def protect(options):\n    return {"reference_manifest_sha256": "a" * 64}\nclass Refused(RuntimeError):\n    pass\n'
    contents = {"prepare_vanilla_backup.py": helper, "sentinel_core.dll": b"fixture", "msimg32.dll": b"fixture"}
    for name, raw in contents.items():
        (runtime / name).write_bytes(raw)
        if name.endswith(".dll"):
            (game / name).write_bytes(raw)
    monkeypatch.setattr(session, "verify_runtime", lambda *args, **kwargs: ({"build_id": "fixture-build"}, contents))
    monkeypatch.setattr(session, "windows_game_processes", lambda: ())
    api = Mock()
    api.GetProcessTimes.return_value = 1
    api.CreateSemaphoreW.return_value = 77
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: api)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 0)
    plan = dict(schema=1, checkpoint_state="save_interrupted", checkpoint=2, resume_allowed=False,
                preservation_required=True, recovery_state="none", recovery_id=None)
    def probe(command, **kwargs):
        if "--save-session-checkpoint-plan" in command:
            result = dict(plan)
        elif "--save-admission" in command:
            result = dict(state="admitted", accepting_requests=True, namespace_id=namespace,
                          build_id="fixture-build", target_pid=71, server_pid=71, process_created="93", instance_id=15)
        elif "--save-session-verify-backup" in command:
            result = dict(namespace=namespace, archive_verified=True, owner_bound=True, quarantine=False)
        else:
            result = dict(namespace_id=namespace, native_root=native)
        return SimpleNamespace(returncode=0, stdout=json.dumps(result).encode())
    monkeypatch.setattr(session.subprocess, "run", probe)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.delenv("SENTINEL_AP_TEST_SESSION", raising=False)
    config = dict(game_root=str(game), core_runtime_manifest=str(runtime), steam_remote_dir=str(remote), save_games_dir=str(local))
    diagnostics = tmp_path / "appdata/SentinelCore/diagnostics"
    diagnostics.mkdir(parents=True)
    def observe(recovery=None, verified=False, *, namespace_value=namespace, build="fixture-build"):
        record = dict(schema="sentinel-startup-v1", pid=71, process_created="93", build_id=build,
                      session={"namespace_id": namespace_value},
                      campaign=dict(source_verified=verified, parser_completed=verified, parser_result=0),
                      b_diagnostics={"stages": {"checkpoint_recovery": recovery}})
        (diagnostics / "71-93.latest.json").write_text(json.dumps(record))
        return owner.observe((dict(pid=71, created=93, path=str(game / "DOOMEternalx64vk.exe")),))
    return SimpleNamespace(owner=owner, snapshot=snapshot, config=config, plan=plan, payload=payload, observe=observe, game=game)


@pytest.mark.parametrize("result", ["interrupted_save_recovered", "previous_checkpoint_restored"])
@pytest.mark.parametrize("checkpoint_state,recovery_state,number", [
    ("save_interrupted", "none", 2), ("receipt_ready", "none", 1), ("confirmed", "recovering", 2),
])
def test_recovery_preserves_then_waits_for_verified_resume(campaign, result, checkpoint_state, recovery_state, number):
    case = campaign
    case.plan.update(checkpoint_state=checkpoint_state, recovery_state=recovery_state, checkpoint=number,
                     recovery_id="preserved-transaction")
    status = case.owner.prepare(case.snapshot, case.config)
    assert status["state"] == "awaiting_recovery" and not status["ready"]
    assert status["intent"] == "resume"
    assert status["recovery_id"] == case.plan["recovery_id"]
    with zipfile.ZipFile(status["preservation_path"]) as archive:
        assert case.payload.read_bytes() in [archive.read(name) for name in archive.namelist()]
    assert case.owner.prepare(case.snapshot, case.config) == status
    assert case.observe(dict(status=2, predicate="checkpoint_recovery_started"))["state"] == "recovering"
    recovery = dict(status=3, predicate=result)
    assert case.observe(recovery)["state"] == "awaiting_resume"
    assert not case.observe(recovery, True, namespace_value="other")["ready"]
    status = case.observe(recovery, True)
    assert status["ready"] and status["resume_verified"] and status["recovery_state"] == result
    case.owner.retire(())


@pytest.mark.parametrize("state,number,new_save", [("save_interrupted", 0, True), ("conflict", 2, False),
                                                  ("invalid", 2, False), ("identity_mismatch", 2, False)])
def test_unproven_checkpoint_preserves_and_blocks(campaign, state, number, new_save):
    case = campaign
    case.plan.update(checkpoint_state=state, checkpoint=number)
    with pytest.raises(CheckpointRecoveryBlockedError):
        case.owner.prepare(case.snapshot, case.config)
    status = case.owner.observe(())
    assert status["state"] == "recovery_blocked" and not status["ready"]
    assert status["new_save_available"] is new_save
    assert Path(status["preservation_path"]).is_file()
    assert not (case.game / "sentinel-prelaunch.txt").exists()
    assert case.payload.read_bytes() == b"interrupted native bytes"
    assert case.owner.inspect_campaign(case.snapshot, case.config)["new_save_available"] is new_save


def test_recovery_refusal_blocks_even_after_admission(campaign):
    case = campaign
    case.owner.prepare(case.snapshot, case.config)
    status = case.observe(dict(status=4, predicate="provider_mismatch"), True)
    assert status["state"] == "recovery_blocked" and not status["ready"]
    assert status["recovery_reason"] == "provider_mismatch"
    case.owner.retire(())


@pytest.mark.parametrize("schema", [None, True, 2])
def test_unknown_probe_schema_preserves_and_cannot_prepare(campaign, schema):
    case = campaign
    case.plan["schema"] = schema
    with pytest.raises(CheckpointRecoveryBlockedError):
        case.owner.prepare(case.snapshot, case.config)
    status = case.owner.observe(())
    assert status["state"] == "recovery_blocked" and not status["ready"]
    assert Path(status["preservation_path"]).is_file()
    assert not (case.game / "sentinel-prelaunch.txt").exists()


def test_preservation_readback_failure_prevents_prelaunch(campaign, monkeypatch):
    case = campaign
    monkeypatch.setattr(zipfile.ZipFile, "testzip", lambda archive: "corrupt")
    with pytest.raises(RuntimeError):
        case.owner.prepare(case.snapshot, case.config)
    assert not (case.game / "sentinel-prelaunch.txt").exists()
    assert case.payload.read_bytes() == b"interrupted native bytes"


def test_confirmed_checkpoint_still_requires_native_resume(campaign):
    case = campaign
    case.plan.update(checkpoint_state="confirmed", resume_allowed=True, preservation_required=False)
    assert case.owner.prepare(case.snapshot, case.config)["state"] == "prelaunch_ready"
    recovery = dict(status=3, predicate="checkpoint_verified")
    assert case.observe(recovery)["state"] == "awaiting_resume"
    assert case.observe(recovery, verified=True)["ready"]
    case.owner.retire(())


def test_explicit_backup_recovery_preserves_and_waits_for_resume(campaign):
    case = campaign
    status = case.owner.prepare(case.snapshot, case.config, recovery_basename="transport-fixture")
    assert status["state"] == "awaiting_recovery" and status["intent"] == "recover"
    assert Path(status["preservation_path"]).is_file()
    recovery = dict(status=3, predicate="recovery_native_payload_verified")
    assert not case.observe(recovery)["ready"]
    assert case.observe(recovery, verified=True)["ready"]
    case.owner.retire(())


@pytest.mark.parametrize("recovery", [[], {"status": []}, {"status": 9}, {"status": 3, "predicate": None}])
def test_malformed_recovery_diagnostics_cannot_release_resume(campaign, recovery):
    case = campaign
    case.owner.prepare(case.snapshot, case.config)
    assert not case.observe(recovery, verified=True)["ready"]
    case.owner.retire(())


def test_supervisor_reports_preserved_first_save_without_starting_helper(campaign, monkeypatch):
    import io

    from doom_eap.launcher import session_owner_app as supervisor

    case = campaign
    case.plan["checkpoint"] = 0
    case.owner.client_dir.mkdir()
    (case.owner.client_dir / "ap_client.exe").write_bytes(b"fixture")
    request = dict(action="prepare", client_dir=str(case.owner.client_dir), data_dir=str(case.owner.data_dir),
                   state_dir=str(case.owner.state_dir), room={}, config=case.config)
    output = io.StringIO()
    monkeypatch.setattr(supervisor, "restore_standard_streams", lambda **kwargs: None)
    monkeypatch.setattr(supervisor, "APSessionOwner", lambda *args: case.owner)
    monkeypatch.setattr(supervisor.RoomSnapshot, "from_event", lambda event: case.snapshot)
    monkeypatch.setattr(supervisor.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode() + b"\n")))
    monkeypatch.setattr(supervisor.sys, "stdout", output)
    assert supervisor.main() == 0
    response = json.loads(output.getvalue())
    assert response["error"] and response["can_close"]
    assert response["status"]["new_save_available"] and not response["status"]["ready"]
    assert Path(response["status"]["preservation_path"]).is_file()
