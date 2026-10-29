import pytest

from doom_eap.launcher.setup_progress import SetupAction, SetupProgress, SetupStep


def presenter(*, windows=False):
    now = [100.0]
    progress = SetupProgress(windows=windows, installer_wait_seconds=900, clock=lambda: now[0])
    progress.activate(1, ("room", 0, 1))
    return progress, now


def event(kind, **fields):
    return {"type": kind, "launcher_job_generation": 1, "setup_progress_room": ("room", 0, 1), **fields}


def test_waiting_phase_reports_elapsed_and_next_action():
    progress, now = presenter(windows=True)
    progress.observe(event("mod_staged"))
    progress.observe(event("injector_started"))
    now[0] += 72
    progress.observe(event("injector_started"))
    view = progress.present()
    assert view.step is SetupStep.INSTALLER_INPUT
    assert view.elapsed_seconds == 72
    assert view.last_completed is SetupStep.STAGING
    assert view.next_action is SetupAction.FINISH_INSTALL
    assert "1:12" in view.text
    assert "Last completed step: Copying the room mod" in view.text
    assert "EternalModInjector, then close its window" in view.text
    progress.observe(event("setup_started"))
    assert progress.present().elapsed_seconds == 0
    assert progress.present().last_completed is None


@pytest.mark.parametrize(("kind", "seconds"), [("injector_started", 900), ("runtime_config_ready", 300)])
def test_long_wait_offers_help_without_claiming_success(kind, seconds):
    progress, now = presenter()
    progress.observe(event(kind))
    now[0] += seconds - 1
    assert not progress.present().long_wait
    now[0] += 1
    view = progress.present()
    assert view.long_wait
    assert view.next_action is SetupAction.DIAGNOSE
    assert view.last_completed is None
    assert "Check Setup" in view.text
    assert "long time" in view.text
    assert all(word not in view.text.casefold() for word in ("success", "complete", "remaining", "estimate"))
    now[0] += 10000
    assert progress.present().step is view.step


@pytest.mark.parametrize(("generation", "room"), [
    (0, ("room", 0, 1)), (1, ("retired", 0, 1)), (None, None),
])
def test_retired_room_progress_is_ignored(generation, room):
    progress, now = presenter()
    progress.observe(event("mod_building"))
    now[0] += 20
    before = progress.present()
    for kind in ("injector_started", "mod_staged", "setup_ready", "setup_failed"):
        progress.observe(event(kind, launcher_job_generation=generation, setup_progress_room=room))
    assert progress.present() == before
    progress.activate(2, ("next room", 0, 1))
    progress.observe(event("mod_staged"))
    assert progress.present() is None


def test_installer_input_wait_is_distinct_from_background_work():
    windows, win_now = presenter(windows=True)
    linux, linux_now = presenter()
    for progress in (windows, linux):
        progress.observe(event("injector_started"))
    win_now[0] += 10000
    linux_now[0] += 10000
    assert windows.present().step is SetupStep.INSTALLER_INPUT
    assert windows.present().next_action is SetupAction.FINISH_INSTALL
    assert not windows.present().long_wait
    assert linux.present().step is SetupStep.INSTALLING
    assert linux.present().next_action is SetupAction.DIAGNOSE
    windows.observe(event("installation_confirmation_required"))
    assert windows.present().step is SetupStep.CONFIRMING
    windows.observe(event("injector_finished", state="applied"))
    assert windows.present().step is SetupStep.SAVE
    assert windows.present().elapsed_seconds == 0
    windows.observe(event("setup_ready", adapter_state="applied"))
    assert windows.present() is None


def test_config_event_only_completes_a_build_that_was_observed():
    progress, _ = presenter()
    progress.observe(event("runtime_config_ready"))
    assert progress.present().step is SetupStep.SAVE
    assert progress.present().last_completed is None
    progress.observe(event("mod_building"))
    progress.observe(event("runtime_config_ready"))
    assert progress.present().last_completed is SetupStep.BUILDING
    progress.observe(event("injector_started"))
    progress.observe(event("injector_finished", state="failed"))
    assert progress.present() is None


def test_existing_room_reports_save_preparation_until_session_is_prepared():
    progress, now = presenter()
    progress.observe(event("core_runtime_selected", setup_progress_step=SetupStep.SAVE))
    now[0] += 300
    assert progress.present().step is SetupStep.SAVE
    assert progress.present().long_wait
    progress.observe(event("integration_status", session={"state": "not_prepared"}))
    assert progress.present().elapsed_seconds == 300
    progress.observe(event("ap_session_status", state="prelaunch_ready"))
    assert progress.present() is None
