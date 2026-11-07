import io
import json
import threading
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from doom_eap.launcher.events import (
    ActionId,
    EventOrigin,
    InstallationChanged,
    InstallationState,
    RoomAccepted,
    Tone,
    adapt_event,
)
from doom_eap.launcher.launcher_supervisor import BridgeSupervisor
from doom_eap.launcher.presenters.play_presenter import PlayPresenter
from doom_eap.launcher.setup_progress import SetupStep
from tests.test_slot_admission import snapshot_for

pytest_plugins = ("tests.test_slot_admission",)


ROOM = ("fixture", 0, 1, "a" * 64)


def event(kind, **payload):
    return adapt_event({"type": kind, "launcher_job_generation": 3, "attempt_id": 2,
                        "setup_progress_room": ROOM, **payload})


@pytest.fixture
def play():
    now = [100.0]
    presenter = PlayPresenter(windows=True, installer_wait_seconds=900, clock=lambda: now[0])
    presenter.activate(3, ROOM, 2)
    return presenter, now


def _assert_typed_room_projection(accepted, source):
    assert isinstance(accepted, RoomAccepted)
    assert accepted.context.origin is EventOrigin.BRIDGE
    assert accepted.projection() == source
    assert deepcopy(accepted) is accepted


def test_supervisor_emits_typed_snapshot_without_changing_projection(tmp_path):
    events, observed, release = [], threading.Event(), threading.Event()
    def accept(event):
        events.append(event)
        if isinstance(event, RoomAccepted):
            observed.set()
    supervisor = BridgeSupervisor(entrypoint=tmp_path / "worker", application_dir=tmp_path,
        config_path=tmp_path / "config", profile_id="fixture", event_sink=accept, log_sink=lambda _: None)
    source = {"type": "connected", "seed_name": "fixture", "team": 0, "slot": 1,
              "slot_data": {"native_generation_fingerprint": "a" * 64}}
    process = Mock(stdin=io.StringIO(), stdout=io.StringIO("AP_EVENT " + json.dumps(source) + "\n"), stderr=io.StringIO())
    process.poll.return_value = None
    process.wait.side_effect = lambda: release.wait(3) and 0
    try:
        with patch("doom_eap.launcher.launcher_supervisor.subprocess.Popen", return_value=process):
            supervisor.start(endpoint="fixture:1", player="fixture")
            assert observed.wait(3)
    finally:
        supervisor.stop()
        release.set()
        assert supervisor.wait_stopped(3)
    accepted, = [event for event in events if isinstance(event, RoomAccepted)]
    _assert_typed_room_projection(accepted, source)
    accepted["slot_data"]["native_generation_fingerprint"] = "changed"
    assert accepted.projection() == source
    with pytest.raises(FrozenInstanceError):
        accepted.kind = "error"


@pytest.mark.parametrize("state,readiness,expected", [
    ("already_installed", "ready", InstallationState.VERIFIED),
    ("already_installed", "blocked", InstallationState.BLOCKED),
    ("update_required", "ready", InstallationState.REQUIRED),
    ("install_needed", "ready", InstallationState.REQUIRED),
])
def test_play_requires_verified_installation_and_prepared_session(play, state, readiness, expected):
    presenter, _ = play
    presenter.observe(event("connected"))
    installation = event("room_install_state", state=state, readiness=readiness, steam_launch_option="%command%", meathook_ok=True)
    assert isinstance(installation, InstallationChanged)
    projection = presenter.observe(installation)
    assert projection["state"] == state and projection["readiness"] == readiness
    assert presenter.present().installation is expected
    assert not presenter.present().ready_to_play
    presenter.observe(event("ap_session_status", state="prelaunch_ready", ready=False))
    assert presenter.present().ready_to_play == (expected is InstallationState.VERIFIED)
    assert not presenter.present().game_confirmed
    presenter.observe(event("ap_session_status", state="admission_refused", ready=False))
    assert not presenter.present().ready_to_play
    assert presenter.present().tone is Tone.ATTENTION


@pytest.mark.parametrize("stale", [{"attempt_id": 1}, {"launcher_job_generation": 2},
                                   {"setup_progress_room": ("other", 0, 1, "b" * 64)}])
def test_play_ignores_events_from_retired_operation_or_room(play, stale):
    presenter, _ = play
    presenter.observe(event("connected"))
    presenter.observe(event("mod_building"))
    before = presenter.present()
    assert presenter.observe(event("setup_failed", **stale)) is None
    assert presenter.present() == before


def test_play_preserves_installer_instruction_and_escalates_save_wait(play):
    presenter, now = play
    presenter.observe(event("connected"))
    presenter.observe(event("mod_staged"))
    presenter.observe(event("injector_started"))
    now[0] += 1000
    model = presenter.present()
    assert model.action is ActionId.FINISH_INSTALL
    assert model.progress.last_completed is SetupStep.STAGING
    assert not model.progress.long_wait
    presenter.observe(event("installation_confirmation_required", request_id="fixture"))
    assert presenter.present().action is ActionId.CONFIRM_INSTALL
    presenter.observe(event("injector_finished", state="applied"))
    now[0] += 300
    model = presenter.present()
    assert model.action is ActionId.DIAGNOSE and model.progress.long_wait
    assert not model.ready_to_play
    presenter.observe(event("inventory_resync", status="error"))
    assert presenter.present().tone is Tone.FAILURE


def _assert_single_new_save_offer(blocked, presenter):
    assert presenter.observe(blocked)["offer_new_save"]
    assert not presenter.observe(blocked)["offer_new_save"]
    assert not presenter.present().ready_to_play
    assert presenter.present().recovery_message and presenter.present().new_save_available


def test_play_waits_for_recovery_and_resume_and_offers_new_save_once(play):
    presenter, _ = play
    presenter.observe(event("connected"))
    presenter.observe(event("room_install_state", state="already_installed", readiness="ready", meathook_ok=True))
    for state, recovery in [("awaiting_recovery", "recovering"), ("recovering", "recovering"),
                            ("awaiting_resume", "previous_checkpoint_restored")]:
        projection = presenter.observe(event("ap_session_status", state=state, recovery_state=recovery, intent="resume"))
        assert projection["recovery_message"]
        assert not presenter.present().ready_to_play
    presenter.observe(event("ap_session_status", state="admitted", intent="resume", ready=True, resume_verified=False))
    assert not presenter.present().ready_to_play
    presenter.observe(event("ap_session_status", state="admitted", intent="resume", ready=True, resume_verified=True,
                            recovery_state="interrupted_save_recovered"))
    assert presenter.present().ready_to_play and presenter.present().game_confirmed
    blocked = event("ap_session_status", state="recovery_blocked", recovery_state="blocked",
                    recovery_reason="save_interrupted", new_save_available=True)
    _assert_single_new_save_offer(blocked, presenter)


def test_play_blocks_readiness_when_game_connection_becomes_unavailable(play):
    presenter, _ = play
    presenter.observe(event("connected"))
    presenter.observe(event("room_install_state", state="already_installed", readiness="ready", meathook_ok=True))
    presenter.observe(event("ap_session_status", state="prelaunch_ready"))
    assert presenter.present().ready_to_play
    presenter.observe(event("integration_status", meathook_ok=False, session={"state": "prelaunch_ready"}))
    assert presenter.present().session_prepared
    assert not presenter.present().ready_to_play


def test_controller_accepts_typed_room_and_publishes_installation_state(tmp_path, monkeypatch, resolved_room):
    from doom_eap.launcher import launcher_controller as application
    paths = SimpleNamespace(config_dir=tmp_path / "config", state_dir=tmp_path / "state", data_dir=tmp_path / "data")
    paths.config_dir.mkdir()
    (paths.config_dir / "launcher.json").write_text(json.dumps({
        "core_auto_update": False, "client_state_file": str(tmp_path / "receipts.json")}))
    monkeypatch.setattr(application, "launcher_user_paths", lambda: paths)
    monkeypatch.setattr(application, "migrate_legacy_launcher_data", lambda *args: None)
    controller = application.LauncherController(Path(__file__).parents[1])
    state = SimpleNamespace(state="install_needed", readiness="ready", readiness_reason="",
                            manifest_hash="fixture", staged_mod="", steam_launch_option="", reason="")
    workflow = SimpleNamespace(install_state=lambda snapshot: state)
    monkeypatch.setattr(controller.workflow, "for_job", lambda *args: workflow)
    snapshot = snapshot_for(resolved_room[0])
    room = asdict(snapshot)
    room["missing_locations"] = list(snapshot.missing_locations)
    room["checked_locations"] = list(snapshot.checked_locations)
    room["placements"] = list(room["placements"])
    try:
        controller.emit("connected", **room)
        accepted = controller.events.get(timeout=3)
        assert isinstance(accepted, RoomAccepted)
        assert controller.process_event(accepted) is not False
        assert controller.connected_room
        assert controller.setup.current_event["seed_name"] == snapshot.seed_name
        installed = controller.events.get(timeout=3)
        assert isinstance(installed, InstallationChanged)
        assert installed.state is InstallationState.REQUIRED
        controller.disconnect()
        assert controller.process_event(installed) is False
    finally:
        controller.close()
