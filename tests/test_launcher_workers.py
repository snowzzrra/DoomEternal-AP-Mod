"""Real worker ownership and scoped launcher adapter contracts, without game writes."""
from dataclasses import asdict
import json
import queue
import threading
from types import SimpleNamespace

import pytest

from doom_eap.launcher.launcher_workers import LauncherWorkers, LauncherWorkCancelled
from doom_eap.launcher.launcher_interactions import LauncherInteractions
from doom_eap.launcher.launcher_integration import IntegratedLaunchWorkflow, RoomSetupCoordinator
from test_install_workflow import _snapshot


def test_serialization_cancelled_queue_and_same_key_replacement():
    errors, calls = [], []
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    workers = LauncherWorkers(lambda job, error: errors.append(error))

    def old(job):
        entered.set()
        assert release.wait(3)
        job.check()
        calls.append("old")

    try:
        assert workers.submit("room", old)
        assert entered.wait(3)
        assert not workers.submit("room", old)
        assert workers.submit("queued", lambda job: calls.append("queued"))
        workers.invalidate()
        assert not workers.submit("stale-input", lambda job: calls.append("stale-input"), generation=0)
        assert workers.submit("room", lambda job: (calls.append("new"), completed.set()))
        assert calls == []
        release.set()
        assert completed.wait(3)
        assert calls == ["new"] and errors == []
    finally:
        release.set()
        workers.close(3)
    assert workers.active == frozenset()
    assert not workers.submit("closed", lambda job: None)


def test_file_publication_finishes_before_a_new_scope_can_write(tmp_path):
    workers = LauncherWorkers(lambda job, error: pytest.fail(str(error)))
    entered, release, retiring, retired = (threading.Event() for _ in range(4))
    path = tmp_path / "runtime.json"

    def operation(job):
        def publish():
            entered.set()
            assert release.wait(3)
            path.write_text("old")
        job.publish(publish)

    def retire():
        retiring.set()
        workers.invalidate()
        path.write_text("new")
        retired.set()

    thread = threading.Thread(target=retire)
    try:
        assert workers.submit("old-publication", operation)
        assert entered.wait(3)
        thread.start()
        assert retiring.wait(3)
        assert not retired.wait(0.03)
        release.set()
        assert retired.wait(3)
        assert path.read_text() == "new"
    finally:
        release.set()
        thread.join(3)
        workers.close(3)


def test_cancelled_problem_report_cannot_publish_preview(tmp_path):
    from doom_eap.launcher import launcher_reporting as reporting
    from doom_eap.launcher.launcher_workers import LauncherJob
    cancelled = threading.Event()
    job = LauncherJob("report", 0, cancelled)

    def generate(destination, *, logs):
        cancelled.set()
        return tmp_path / "report.zip"

    with pytest.raises(LauncherWorkCancelled):
        reporting.report_problem(SimpleNamespace(create_support_bundle=generate), logs=[], job=job)


def test_question_kind_scope_and_cancellation():
    events, errors, answers = queue.Queue(), [], []
    workers = LauncherWorkers(lambda job, error: errors.append(error))
    interactions = LauncherInteractions(lambda kind, payload: events.put((kind, payload)))
    finished = threading.Event()

    def operation(job):
        try:
            answers.append(interactions.for_job(job).confirmation())
        finally:
            finished.set()

    try:
        assert workers.submit("confirmation", operation)
        kind, payload = events.get(timeout=3)
        assert kind == "installation_confirmation_required"
        assert payload["launcher_job_generation"] == workers.generation
        interactions.resolve("uninstall_confirmation_required", payload["request_id"], True)
        assert not finished.is_set()
        interactions.resolve(kind, payload["request_id"], True)
        assert finished.wait(3) and answers == [True]
        finished.clear()
        # A distinct key avoids depending on the previous thread's finalizer timing.
        assert workers.submit("cancelled-confirmation", operation)
        _, cancelled = events.get(timeout=3)
        workers.invalidate()
        interactions.cancel_all()
        assert finished.wait(3)
        interactions.resolve(kind, cancelled["request_id"], True)
        assert answers == [True] and errors == []
        assert not workers.accepts(cancelled["launcher_job_generation"])
    finally:
        interactions.cancel_all()
        workers.close(3)


def test_adapter_configuration_is_captured_and_cancelled_before_reuse(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"doom_base_dir": "old/base", "nested": {"value": 1}}))
    events, errors = [], []
    workers = LauncherWorkers(lambda job, error: errors.append(error))
    interactions = LauncherInteractions(lambda *args: events.append(args))
    workflow = IntegratedLaunchWorkflow(tmp_path, tmp_path, config_path, platform_name="windows")
    done = threading.Event()

    def operation(job):
        try:
            scoped = workflow.for_job(job, lambda *args: events.append(args), interactions.for_job(job))
            config_path.write_text(json.dumps({"doom_base_dir": "new/base"}))
            first = scoped._config()
            first["nested"]["value"] = 2
            assert scoped._config() == {"doom_base_dir": "old/base", "nested": {"value": 1}}
            workers.invalidate()
            with pytest.raises(LauncherWorkCancelled):
                scoped._config()
            with pytest.raises(LauncherWorkCancelled):
                scoped._emit("setup_ready")
            assert workflow._config() == {"doom_base_dir": "new/base"}
        finally:
            done.set()

    try:
        assert workers.submit("configuration", operation)
        assert done.wait(3)
        assert errors == [] and events == []
    finally:
        workers.close(3)


def room_event(seed="install-seed"):
    snapshot = _snapshot()
    # Exercise the actual launcher packet-to-snapshot producer.
    return {
        "type": "connected", "seed_name": seed, "team": snapshot.team, "slot": snapshot.slot,
        "slot_data": dict(snapshot.slot_data),
        "missing_locations": list(snapshot.missing_locations), "checked_locations": list(snapshot.checked_locations),
        "placements": [asdict(value) for value in snapshot.placements],
    }


@pytest.mark.parametrize("fail", [False, True])
def test_retired_setup_cannot_publish_result_or_complete_new_room(tmp_path, fail):
    events, results, errors = [], [], []
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    workers = LauncherWorkers(lambda job, error: errors.append(error))
    interactions = LauncherInteractions(lambda *args: events.append(args))

    class Workflow:
        state_dir = tmp_path

        def for_job(self, job, sink, prompts):
            return self

        def execute(self, snapshot, endpoint):
            assert snapshot.seed_name == "install-seed"
            entered.set()
            assert release.wait(3)
            if fail:
                raise OSError("retired installer")
            return SimpleNamespace(adapter_state="applied", manifest_hash="old")

    coordinator = RoomSetupCoordinator(Workflow(), lambda *args: events.append(args),
                                       lambda record, generation: results.append(record), workers, interactions)
    first = room_event()
    try:
        assert coordinator.submit(first)
        assert entered.wait(3)
        assert not coordinator.submit(first)
        next_room = room_event("replacement")
        coordinator.observe(next_room)
        before = list(events)
        release.set()
        # The sentinel is ordered behind the old operation by the actual worker lane.
        assert workers.submit("sentinel", lambda job: done.set())
        assert done.wait(3)
        assert events == before and results == [] and errors == []
        assert coordinator.last_event["seed_name"] == "replacement"
        assert coordinator.room_key(first) not in coordinator._completed
    finally:
        release.set()
        workers.close(3)


def test_successful_setup_is_deduplicated_and_explicit_retry_remains_available(tmp_path):
    events, results, errors = [], [], []
    workers = LauncherWorkers(lambda job, error: errors.append(error))
    interactions = LauncherInteractions(lambda *args: events.append(args))
    done = threading.Event()

    class Workflow:
        state_dir = tmp_path
        calls = 0

        def for_job(self, job, sink, prompts):
            return self

        def execute(self, snapshot, endpoint):
            self.calls += 1
            return SimpleNamespace(
                adapter_state="applied", manifest_hash="current", randomize_dash=False,
                new_install=True, adapter_message="applied", steam_launch_option="",
            )

    workflow = Workflow()
    coordinator = RoomSetupCoordinator(workflow, lambda *args: events.append(args),
                                       lambda record, generation: results.append((record, generation)), workers, interactions)
    event = room_event()
    try:
        assert coordinator.submit(event)
        assert workers.submit("first-sentinel", lambda job: done.set())
        assert done.wait(3)
        assert workflow.calls == 1 and len(results) == 1
        assert not coordinator.submit(event)
        done.clear()
        assert coordinator.submit(event, force=True)
        assert workers.submit("second-sentinel", lambda job: done.set())
        assert done.wait(3)
        assert workflow.calls == 2 and len(results) == 2 and errors == []
        assert all(payload["launcher_job_generation"] == workers.generation for _, payload in events)
        coordinator.invalidate()
        assert coordinator.last_event["seed_name"] == event["seed_name"]
        assert not coordinator.start(force=True)
    finally:
        workers.close(3)


@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("trigger", ["reconnect", "game_exit", "delayed_exit", "disconnected_exit"])
def test_room_readiness_prepares_session_before_starting_native_client(fail, trigger, monkeypatch):
    from doom_eap.launcher.launcher_controller import LauncherController

    events, errors, calls = [], [], []
    done = threading.Event()
    workers = LauncherWorkers(lambda job, error: errors.append(error))
    interactions = LauncherInteractions(lambda *args: None)
    owner = SimpleNamespace(status={"state": "not_prepared", "ready": False})

    def prepare(snapshot, config):
        calls.append("prepare")
        assert snapshot.seed_name == "install-seed" and config == {"room": "existing"}
        if fail:
            raise RuntimeError("admission fixture blocked")
        owner.status = {"state": "prelaunch_ready", "ready": False, "intent": "create"}
        return owner.status

    owner.prepare = prepare
    workflow = SimpleNamespace(session_owner=owner, install_state=lambda snapshot: SimpleNamespace(
        state="already_installed", manifest_hash="same-package", staged_mod="room.zip", steam_launch_option="",
        reason="", readiness="ready", readiness_reason=""))
    workflow.for_job = lambda *args: workflow
    controller = SimpleNamespace(workers=workers, config={"room": "existing"}, workflow=workflow,
                                 setup=SimpleNamespace(room_key=RoomSetupCoordinator.room_key), interactions=interactions,
                                 _setup_event=lambda kind, payload: events.append((kind, payload)),
                                 _ensure_native_client=lambda **kwargs: calls.append("native"))
    try:
        if trigger == "reconnect":
            assert LauncherController._queue_room_readiness(controller, room_event())
        else:
            from doom_eap.launcher import launcher_controller
            from doom_eap.runtime import observer_lifecycle
            monkeypatch.setattr(launcher_controller, "os", SimpleNamespace(name="nt"))
            processes = iter((None, (), ()) if trigger == "delayed_exit" else ((), (), ()))
            monkeypatch.setattr(observer_lifecycle, "windows_game_processes", lambda: next(processes))
            owner.observe = lambda processes: {"state": "game_exited", "ready": False}
            controller._lifecycle_lock = threading.Lock()
            controller.setup.current_event = None if trigger == "disconnected_exit" else room_event()
            controller._queue_room_readiness = lambda event: LauncherController._queue_room_readiness(controller, event)
            controller.emit = lambda *args, **kwargs: None
            iterations = iter((False, False, False, True))
            controller._game_lifecycle_stop = SimpleNamespace(is_set=lambda: next(iterations), wait=lambda _: None)
            controller._game_lifecycle_sample_lock = threading.Lock()
            LauncherController._sample_game_lifecycle(controller)
        assert workers.submit("sentinel", lambda job: done.set())
        assert done.wait(3)
        if trigger == "disconnected_exit":
            assert calls == [] and events == [] and errors == []
            return
        if fail:
            assert calls == ["prepare"] and errors == []
            assert len(events) == 1 and events[0][0] == "room_install_state"
            payload = events[0][1]
            assert payload["state"] == "already_installed" and payload["readiness"] == "blocked"
            assert payload["readiness_reason"] == "admission fixture blocked"
            assert payload["failure_domain"] == "campaign_session"
        else:
            assert calls == ["prepare", "native"] and errors == []
            assert events[0][0] == "ap_session_status" and events[0][1]["state"] == "prelaunch_ready"
            assert events[1][0] == "room_install_state" and events[1][1]["state"] == "already_installed"
    finally:
        workers.close(3)
