import json
from dataclasses import replace

import pytest

from doom_eap.launcher import launcher_platform as platform
from doom_eap.launcher.install_journal import InstallationRecoveryRequired, InstallJournal, InstallPhase, file_digest
from doom_eap.launcher.installation import (
    InstallationActions,
    InstallationOutcome,
    InstallationPlan,
    InstallationService,
)
from doom_eap.launcher.launcher_core import LaunchWorkflow


@pytest.fixture
def installation(tmp_path, monkeypatch):
    from doom_eap.launcher import installation_adapters

    monkeypatch.setattr(installation_adapters, "_read_processes", lambda: ())
    monkeypatch.setattr(platform, "detect_doom_processes", lambda: ())
    root = tmp_path / "game"
    (root / "base").mkdir(parents=True)
    (root / "Mods").mkdir()
    (root / "DOOMEternalx64vk.exe").write_bytes(b"game")
    generated = tmp_path / "room.zip"
    import zipfile

    with zipfile.ZipFile(generated, "w") as archive:
        archive.writestr("seed_manifest.json", json.dumps({"manifest_hash": "room"}))
    app = tmp_path / "app"
    app.mkdir()
    config = app / "ap_config.json"
    config.write_text(json.dumps({"seed_manifest_hash": "previous"}))
    state = tmp_path / "state"
    state.mkdir()
    receipt = state / "launcher_setup.json"
    receipt.write_bytes(b"previous receipt")
    plan = InstallationPlan(
        root,
        generated,
        receipt,
        config,
        state,
        {
            "manifest_hash": "room",
            "generated_sha256": file_digest(generated),
            "executable_sha256": file_digest(root / "DOOMEternalx64vk.exe"),
        },
    )
    calls = []

    def prepare(journal):
        platform.stage_room_mod(generated, root, receipt, manifest_hash="room", journal=journal)

    def inject():
        calls.append("inject")
        return InstallationOutcome(platform.AdapterResult("applied", "installed", returncode=0))

    def session(journal):
        calls.append("session")
        LaunchWorkflow.write_client_config(app, manifest_hash="room", journal=journal)

    actions = InstallationActions(
        prepare,
        inject,
        lambda: True,
        lambda result: result,
        lambda result: result,
        session,
        lambda result: {"manifest_hash": "room", "adapter_state": "applied"},
        lambda: None,
    )
    return plan, actions, calls


@pytest.mark.parametrize(
    "phase",
    [
        InstallPhase.PREPARED,
        InstallPhase.INJECTING,
        InstallPhase.AWAITING_CONFIRMATION,
        InstallPhase.CONFIRMED,
        InstallPhase.FINALIZING_RESOURCES,
        InstallPhase.VERIFYING,
        InstallPhase.PREPARING_SESSION,
    ],
)
def test_restart_at_transaction_boundaries(installation, monkeypatch, phase):
    plan, actions, calls = installation
    transition = InstallJournal.transition

    def crash_after_write(journal, current, **evidence):
        transition(journal, current, **evidence)
        if current is phase:
            raise SystemExit("process stopped")

    monkeypatch.setattr(InstallJournal, "transition", crash_after_write)
    with pytest.raises(SystemExit):
        InstallationService().execute(plan, actions)
    assert plan.receipt_path.read_bytes() == b"previous receipt"
    monkeypatch.setattr(InstallJournal, "transition", transition)
    if phase is InstallPhase.INJECTING:
        with pytest.raises(InstallationRecoveryRequired):
            InstallationService().execute(plan, actions)
        assert not calls
    else:
        InstallationService().execute(plan, actions)
        assert calls.count("inject") == 1
        assert json.loads(plan.receipt_path.read_text())["adapter_state"] == "applied"


def test_save_failure_resumes_session_without_reinjection(installation):
    plan, actions, calls = installation

    def fail_session(journal):
        actions.session(journal)
        raise RuntimeError("save unavailable")

    with pytest.raises(RuntimeError):
        InstallationService().execute(plan, replace(actions, session=fail_session))
    journal = plan.journal()
    journal.load()
    assert journal.phase is InstallPhase.PREPARING_SESSION
    assert plan.receipt_path.read_bytes() == b"previous receipt"
    InstallationService().execute(plan, actions)
    assert calls == ["inject", "session", "session"]


@pytest.mark.parametrize("point", ["backup", "marker"])
def test_completed_installation_resumes_interrupted_cleanup(installation, monkeypatch, point):
    from pathlib import Path

    plan, actions, calls = installation
    unlink = Path.unlink

    def interrupted(path, *args, **kwargs):
        unlink(path, *args, **kwargs)
        if path.parent.name.startswith(".doomeap-installation-") and (path.suffix == ".json") == (point == "marker"):
            raise SystemExit("cleanup interrupted")

    monkeypatch.setattr(Path, "unlink", interrupted)
    with pytest.raises(SystemExit):
        InstallationService().execute(plan, actions)
    monkeypatch.setattr(Path, "unlink", unlink)
    InstallationService().execute(plan, actions)
    journal = plan.journal()
    assert journal.load()
    assert journal.phase is InstallPhase.COMPLETE
    assert journal.document.get("cleanup_complete") is True
    assert "cleanup_error" not in journal.document["evidence"]
    assert all(not Path(entry["backup"]).parent.exists() for entry in journal.document["files"])
    assert calls == ["inject", "session"]


def test_rollback_restores_launcher_writes_without_copying_injector_files(installation, monkeypatch):
    from doom_eap.launcher import install_journal

    plan, actions, calls = installation
    root = plan.game_root
    untouched = root / 'base/large.resources'
    with untouched.open('wb') as stream:
        stream.truncate(128 * 1024 * 1024)
    injected = [root / 'base' / ('native' + suffix) for suffix in ('.resources', '.streamdb', '.backup')]
    for path in injected:
        path.write_bytes(b'original game file')
    settings = root / 'EternalModInjector Settings.txt'
    core = root / 'sentinel_core.dll'
    staged = root / 'Mods' / plan.generated.name
    metadata = root / 'base/packagemapspec.json'
    owned = [settings, core, staged, metadata, plan.config_path, plan.receipt_path]
    for path in (settings, core, staged, metadata):
        path.write_bytes(b'previous launcher file')
    plan.receipt_path.write_text(json.dumps({'staged_mod': str(staged), 'staged_sha256': file_digest(staged)}))
    originals = {path: path.read_bytes() for path in owned}
    bank = root / 'base/ap_enemy_resources/new.resources'
    digest = install_journal.file_digest

    def checked_digest(path):
        assert path not in [untouched, *injected]
        return digest(path)

    monkeypatch.setattr(install_journal, 'file_digest', checked_digest)

    def prepare(journal):
        actions.prepare(journal)
        journal.replace(settings, b'launcher settings', operation='injector_settings')
        journal.replace(core, b'new core', operation='test_publish')

    def inject():
        for path in injected:
            path.write_bytes(b'injector changed it')
        settings.write_bytes(b'injector updated settings')
        return actions.inject()

    def finalize(outcome):
        journal = plan.journal()
        journal.load()
        journal.replace(metadata, b'new metadata', operation='resource_activation')
        journal.replace(bank, b'new bank', operation='resource_bank')
        return outcome

    def session(journal):
        actions.session(journal)
        raise OSError('session unavailable')

    with pytest.raises(OSError):
        InstallationService().execute(plan, replace(actions, prepare=prepare, inject=inject,
                                                   finalize=finalize, session=session))
    journal = plan.journal()
    journal.load()
    assert {entry['path'] for entry in journal.document['files']} == {str(path) for path in [*owned[:-1], bank]}
    journal.rollback()
    assert {path: path.read_bytes() for path in owned} == originals
    assert not bank.exists()
    assert untouched.stat().st_size == 128 * 1024 * 1024
    assert all(path.read_bytes() == b'injector changed it' for path in injected)
    assert calls == ['inject', 'session']


def test_late_confirmation_and_repeated_click_do_not_reinject(installation):
    plan, actions, calls = installation

    def inject():
        calls.append("inject")
        return InstallationOutcome(
            platform.AdapterResult(
                "applied", "installed", returncode=0, details={"installation_mode": "windows_injector_assisted"}
            )
        )

    actions = replace(actions, inject=inject, confirm=lambda: False)
    outcome = InstallationService().execute(plan, actions)
    assert outcome.adapter.state == "manual_install_required"
    journal = plan.journal()
    journal.load()
    token = journal.transaction_id
    with pytest.raises(InstallationRecoveryRequired):
        InstallationService().execute(plan, actions, manual=True, transaction_id="old")
    assert calls == ["inject"]
    InstallationService().execute(plan, actions, manual=True, transaction_id=token)
    with pytest.raises(InstallationRecoveryRequired):
        InstallationService().execute(plan, actions, manual=True, transaction_id=token)
    assert calls == ["inject", "session"]


def test_surviving_injector_blocks_every_new_transaction(installation):
    plan, actions, calls = installation

    def survived():
        calls.append("inject")
        raise SystemExit("launcher terminated while external process lives")

    with pytest.raises(SystemExit):
        InstallationService().execute(plan, replace(actions, inject=survived))
    before = {path: path.read_bytes() for path in plan.game_root.rglob("*") if path.is_file()}
    journal = plan.journal()
    journal.load()
    for manual in (False, True):
        with pytest.raises(InstallationRecoveryRequired):
            InstallationService().execute(plan, actions, manual=manual, transaction_id=journal.transaction_id)
    assert before == {path: path.read_bytes() for path in plan.game_root.rglob("*") if path.is_file()}
    assert calls == ["inject"]


def test_preparation_failure_restores_core_and_staging(installation, tmp_path):
    from doom_eap.launcher.launcher_core_install import backup_root
    from tests.test_core_distribution import fixture

    plan, actions, calls = installation
    core = tmp_path / "core"
    core.mkdir()
    fixture(core)

    def prepare(journal):
        platform.install_meathook(
            plan.game_root,
            None,
            state_dir=plan.state_dir,
            consent=lambda _: False,
            local_artifact=core / "distribution.json",
            journal=journal,
        )
        actions.prepare(journal)
        raise OSError("disk full before injector")

    with pytest.raises(OSError):
        InstallationService().execute(plan, replace(actions, prepare=prepare))
    assert not (plan.game_root / "sentinel_core.dll").exists()
    assert not (plan.game_root / "msimg32.dll").exists()
    assert not (backup_root(plan.game_root, plan.state_dir) / "previous.json").exists()
    assert not (plan.game_root / "Mods" / plan.generated.name).exists()
    assert plan.receipt_path.read_bytes() == b"previous receipt"
    assert not calls


def test_external_change_is_not_overwritten_on_restart(installation):
    plan, actions, calls = installation
    journal = plan.journal()
    journal.begin(plan.identity)
    target = plan.game_root / "Mods" / plan.generated.name
    journal.replace(target, b"owned", operation="test_publish")
    target.write_bytes(b"foreign")
    with pytest.raises(InstallationRecoveryRequired):
        InstallationService().execute(plan, actions)
    assert target.read_bytes() == b"foreign"
    assert not calls


@pytest.fixture
def workflow_installation(tmp_path, monkeypatch):
    import subprocess
    import zipfile
    from types import SimpleNamespace
    from unittest.mock import Mock

    from doom_eap.launcher import installation_adapters
    from doom_eap.launcher import launcher_integration as integration
    from tests.test_core_distribution import fixture
    from tests.test_install_workflow import _snapshot

    monkeypatch.setattr(platform, "detect_doom_processes", lambda: ())
    monkeypatch.setattr(installation_adapters, "_read_processes", lambda: ())
    game = tmp_path / "game"
    (game / "base").mkdir(parents=True)
    (game / "DOOMEternalx64vk.exe").write_bytes(b"game")
    app = tmp_path / "app"
    app.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    core = tmp_path / "core"
    core.mkdir()
    fixture(core)
    monkeypatch.setattr(integration.CoreRuntime, "resolve", lambda *a, **kw: core / "distribution.json")
    monkeypatch.setattr(integration, "probe_runtime_prerequisites", lambda *a: SimpleNamespace(ok=True))

    class Compiler:
        static_content_digest = ""

        def __init__(self, *a, **kw):
            pass

        def build(self, manifest, destination):
            destination.mkdir(parents=True, exist_ok=True)
            path = destination / "DoomEternalArchipelago-0123456789abcdef.zip"
            with zipfile.ZipFile(path, "w") as package:
                package.writestr("seed_manifest.json", json.dumps(manifest.document()))
            return path

        @staticmethod
        def validate_cached_package(path, manifest):
            with zipfile.ZipFile(path) as package:
                return json.loads(package.read("seed_manifest.json"))["manifest_hash"] == manifest.manifest_hash

    monkeypatch.setattr(integration, "RoomCompiler", Compiler)
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in (*platform.WINDOWS_INJECTOR_REQUIRED_MEMBERS, "InjectorShell"):
        path = tools / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test injector")

    def acquire(manager, spec, **kwargs):
        executable = tools / ("EternalModInjector.bat" if spec is platform.WINDOWS_MOD_INJECTOR else "InjectorShell")
        return platform.InstalledDependency(spec.name, spec.version, "a" * 64, "fixture", str(tools), str(executable))

    monkeypatch.setattr(platform.DependencyManager, "acquire", acquire)
    calls = []

    def run(*args, **kwargs):
        calls.append("inject")
        return subprocess.CompletedProcess(args[0], 0, "output", "")

    class Process:
        pid = 123456
        returncode = 0
        stdout = stderr = None

        def __init__(self, *a, **kw):
            calls.append("inject")

        def wait(self, **kwargs):
            return 0

        def communicate(self, **kwargs):
            return "output", ""

    monkeypatch.setattr(platform.subprocess, "run", run)
    monkeypatch.setattr(platform.subprocess, "Popen", Process)
    monkeypatch.setattr(integration, "finalize_mod_installation", lambda root, staged, result, **kw: result)

    def verified(root, staged, result, **kwargs):
        return platform.AdapterResult(
            "applied",
            result.message,
            command=result.command,
            returncode=result.returncode,
            details={
                **result.details,
                "post_install_verification": "verified",
                "user_confirmed": True,
                "required_resources": [{"status": "verified"}] * 3,
                "installed_resources": {"resource": "digest"},
            },
        )

    monkeypatch.setattr(integration, "verify_mod_installation", verified)
    from doom_eap.launcher import campaign_resources

    monkeypatch.setattr(campaign_resources, "installed_resource_fingerprints", lambda root: {"resource": "digest"})
    monkeypatch.setattr(platform, "verify_linux_mod_installation", lambda *a, **kw: {"state": "verified"})
    monkeypatch.setattr(integration, "verify_linux_mod_installation", platform.verify_linux_mod_installation)
    snapshot = _snapshot()
    config = state / "config.json"
    config.write_text(json.dumps({"doom_base_dir": str(game / "base")}))
    owner = Mock()
    return integration, game, app, state, config, snapshot, owner, calls


@pytest.mark.parametrize("system", ["windows", "linux"])
def test_workflow_runs_complete_transaction_and_reuses_installation(workflow_installation, system):
    integration, game, app, state, config, snapshot, owner, calls = workflow_installation
    events = []
    workflow = integration.IntegratedLaunchWorkflow(
        app,
        state,
        config,
        platform_name=system,
        consent=lambda _: True,
        confirmation=lambda: True,
        session_owner=owner,
        event_sink=lambda kind, payload: events.append((kind, payload)),
    )
    record = workflow.execute(snapshot)
    assert record.adapter_state == "applied"
    journal = InstallJournal(game, (state / "launcher_setup.json", app / "ap_config.json"))
    journal.load()
    assert journal.phase is InstallPhase.COMPLETE
    assert json.loads((state / "launcher_setup.json").read_text())["transaction_id"] == record.transaction_id
    assert calls == ["inject"]
    if system == "linux":
        finished = next(payload for kind, payload in events if kind == "injector_finished")
        assert finished["stdout"] == "output"
        assert finished["returncode"] == 0
    workflow.execute(snapshot)
    assert calls == ["inject"]
    assert owner.prepare.call_count == 2


@pytest.mark.parametrize("process", ["injector", "child", "inspection_error"])
def test_workflow_waits_for_injector_before_recovering_prepared_installation(
    workflow_installation, monkeypatch, process
):
    from doom_eap.launcher import installation_adapters

    integration, game, app, state, config, snapshot, owner, calls = workflow_installation
    workflow = integration.IntegratedLaunchWorkflow(
        app, state, config, platform_name="windows", consent=lambda _: True,
        confirmation=lambda: True, session_owner=owner,
    )
    transition = InstallJournal.transition

    def interrupted(journal, phase, **evidence):
        transition(journal, phase, **evidence)
        if phase is InstallPhase.PREPARED:
            raise SystemExit("preparation interrupted")

    monkeypatch.setattr(InstallJournal, "transition", interrupted)
    with pytest.raises(SystemExit):
        workflow.execute(snapshot)
    monkeypatch.setattr(InstallJournal, "transition", transition)
    journal = InstallJournal(game)
    assert journal.load()
    journal.transition(InstallPhase.PREPARED, injector_pid="100")
    injected = game / "base/native.resources"
    injected.write_bytes(b"injector changed it")
    before = {path: file_digest(path) for root in (game, app, state) for path in root.rglob("*") if path.is_file()}

    def processes():
        if process == "inspection_error":
            raise OSError("process inspection unavailable")
        if process == "child":
            return [{"pid": "101", "parent_pid": "100", "name": "worker"}]
        return [{"pid": "100", "parent_pid": "1", "name": "EternalModInjector.exe"}]

    monkeypatch.setattr(installation_adapters, "_read_processes", processes)
    with pytest.raises(InstallationRecoveryRequired):
        workflow.execute(snapshot)
    assert before == {path: file_digest(path) for root in (game, app, state) for path in root.rglob("*") if path.is_file()}
    assert not calls and not owner.prepare.called
    monkeypatch.setattr(installation_adapters, "_read_processes", lambda: ())
    assert workflow.execute(snapshot).adapter_state == "applied"
    assert injected.read_bytes() == b"injector changed it"
    assert calls == ["inject"]


def test_workflow_manual_confirmation_preserves_previous_receipt(workflow_installation):
    integration, game, app, state, config, snapshot, owner, calls = workflow_installation
    receipt = state / "launcher_setup.json"
    receipt.write_bytes(b"previous receipt")
    workflow = integration.IntegratedLaunchWorkflow(
        app,
        state,
        config,
        platform_name="windows",
        consent=lambda _: True,
        confirmation=lambda: False,
        session_owner=owner,
    )
    record = workflow.execute(snapshot)
    assert record.adapter_state == "manual_install_required"
    assert receipt.read_bytes() == b"previous receipt"
    assert not owner.prepare.called
    with pytest.raises(InstallationRecoveryRequired):
        workflow.confirm_manual_installation(snapshot, transaction_id="stale")
    confirmed = workflow.confirm_manual_installation(snapshot, transaction_id=record.transaction_id)
    assert confirmed.adapter_state == "applied"
    assert calls == ["inject"]
    assert owner.prepare.call_count == 1


@pytest.mark.parametrize("operation", ["room_mod_stage", "client_config_publish", "setup_receipt_publish"])
@pytest.mark.parametrize("after", [False, True])
def test_publication_failure_can_resume_without_false_completion(installation, monkeypatch, operation, after):
    from doom_eap.launcher import install_journal

    plan, actions, calls = installation
    publish = install_journal.publish_file

    def interrupted(source, target, *, operation: str):
        if operation == refused and not after:
            raise OSError("publication failed")
        publish(source, target, operation=operation)
        if operation == refused:
            raise OSError("publication interrupted")

    refused = operation
    monkeypatch.setattr(install_journal, "publish_file", interrupted)
    with pytest.raises(OSError):
        InstallationService().execute(plan, actions)
    journal = plan.journal()
    journal.load()
    assert journal.phase is not InstallPhase.COMPLETE
    if operation == "room_mod_stage":
        assert not (plan.game_root / "Mods" / plan.generated.name).exists()
    monkeypatch.setattr(install_journal, "publish_file", publish)
    InstallationService().execute(plan, actions)
    assert calls.count("inject") == 1
    assert json.loads(plan.receipt_path.read_text())["adapter_state"] == "applied"


@pytest.mark.parametrize("name", ["active.json", "sentinel_core.dll", "msimg32.dll", "sentinel-distribution.json"])
@pytest.mark.parametrize("another_profile", [False, True])
def test_restart_during_core_replacement(installation, monkeypatch, tmp_path, name, another_profile):
    from tests.test_core_distribution import fixture

    plan, actions, calls = installation
    core = tmp_path / "core"
    core.mkdir()
    fixture(core)

    def prepare(journal):
        platform.install_meathook(
            plan.game_root,
            None,
            state_dir=plan.state_dir,
            consent=lambda _: False,
            local_artifact=core / "distribution.json",
            journal=journal,
        )
        actions.prepare(journal)

    publish = platform.publish_file

    def interrupted(source, path, *, operation):
        publish(source, path, operation=operation)
        if path.name == name:
            raise SystemExit("process interrupted")

    monkeypatch.setattr(platform, "publish_file", interrupted)
    with pytest.raises(SystemExit):
        InstallationService().execute(plan, replace(actions, prepare=prepare))
    monkeypatch.setattr(platform, "publish_file", publish)
    if another_profile:
        plan = replace(plan, state_dir=tmp_path / "another-profile")
    InstallationService().execute(plan, replace(actions, prepare=prepare))
    assert platform.probe_meathook(plan.game_root).ok
    assert calls.count("inject") == 1
    assert not (plan.game_root / "sentinel-core-update.txt").exists()


@pytest.mark.parametrize("record_pid", [False, True])
def test_interrupted_injector_requires_retry_after_processes_stop(installation, monkeypatch, record_pid):
    from doom_eap.launcher import installation_adapters

    plan, actions, calls = installation
    tool = plan.game_root / "EternalModInjector.bat"
    banks = plan.game_root / "base" / "ap_enemy_resources"
    banks.mkdir()
    (banks / "bank.resources").write_bytes(b"bank")

    original = actions

    def prepare(journal):
        original.prepare(journal)
        journal.replace(tool, b"tool", operation="injector_stage")
        journal.document["evidence"]["tool"] = {"path": str(tool), "sha256": file_digest(tool)}
        if record_pid:
            journal.document["evidence"]["injector_pid"] = 123
        journal.save()

    def inject():
        calls.append("inject")
        with platform.prepare_injector_resources(plan.game_root) as hold:
            hold.start_injector()
            raise SystemExit("launcher exited")

    actions = replace(actions, prepare=prepare, inject=inject)
    with pytest.raises(SystemExit):
        InstallationService().execute(plan, actions)
    journal = plan.journal()
    journal.load()
    token = journal.transaction_id
    monkeypatch.setattr(installation_adapters, "injector_processes", lambda evidence: ({"pid": "123"},))
    with pytest.raises(InstallationRecoveryRequired):
        InstallationService().execute(plan, actions, manual=True, transaction_id=token)
    assert not banks.exists()
    with pytest.raises(InstallationRecoveryRequired):
        InstallationService().execute(plan, actions, retry=True)
    assert not banks.exists() and calls == ['inject']
    with pytest.raises(InstallationRecoveryRequired):
        platform.install_meathook(plan.game_root, None, consent=lambda _: False)
    with pytest.raises(InstallationRecoveryRequired):
        platform.stage_room_mod(plan.generated, plan.game_root, plan.receipt_path, manifest_hash="room")
    before = plan.config_path.read_bytes()
    with pytest.raises(InstallationRecoveryRequired):
        LaunchWorkflow.write_client_config(
            plan.config_path.parent, runtime_config={"doom_base_dir": str(plan.game_root / "base")}
        )
    assert plan.config_path.read_bytes() == before
    monkeypatch.setattr(installation_adapters, "injector_processes", lambda evidence: ())
    with pytest.raises(InstallationRecoveryRequired):
        InstallationService().execute(plan, actions, manual=True, transaction_id=token)
    assert not banks.exists()
    InstallationService().execute(plan, replace(actions, inject=original.inject), retry=True)
    assert (banks / "bank.resources").read_bytes() == b"bank"
    assert calls == ["inject", "inject", "session"]


def test_explicit_retry_runs_only_a_finished_failed_injector(installation):
    plan, actions, calls = installation

    def failed():
        calls.append("failed")
        return InstallationOutcome(platform.AdapterResult("failed", "injector failed", returncode=1))

    InstallationService().execute(plan, replace(actions, inject=failed))
    InstallationService().execute(plan, replace(actions, inject=failed))
    assert calls == ["failed"]
    InstallationService().execute(plan, actions, retry=True)
    assert calls == ["failed", "inject", "session"]
    InstallationService().execute(plan, actions)
    assert calls == ["failed", "inject", "session"]


@pytest.mark.parametrize("point", ["prepared", "injected"])
def test_cancellation_stops_at_the_next_safe_boundary(installation, point):
    from doom_eap.launcher.launcher_workers import LauncherWorkCancelled

    plan, actions, calls = installation
    stopped = []

    def prepare(journal):
        actions.prepare(journal)
        if point == "prepared":
            stopped.append(True)

    def inject():
        result = actions.inject()
        stopped.append(True)
        return result

    def check():
        if stopped:
            raise LauncherWorkCancelled()

    with pytest.raises(LauncherWorkCancelled):
        InstallationService().execute(plan, replace(actions, prepare=prepare, inject=inject, check=check))
    journal = plan.journal()
    journal.load()
    expected = InstallPhase.ROLLED_BACK if point == "prepared" else InstallPhase.AWAITING_CONFIRMATION
    assert journal.phase is expected
    assert calls == ([] if point == "prepared" else ["inject"])
    assert plan.receipt_path.read_bytes() == b"previous receipt"


def test_process_inspection_finds_children_even_when_parent_has_exited():
    from doom_eap.launcher.installation_adapters import injector_processes

    evidence = {"tool": {"path": "InjectorShell"}, "injector_pid": "100"}
    processes = [
        {"pid": "102", "parent_pid": "101", "name": "worker"},
        {"pid": "101", "parent_pid": "100", "name": "worker"},
        {"pid": "200", "parent_pid": "1", "name": "InjectorShell"},
        {"pid": "300", "parent_pid": "1", "name": "other"},
        {"pid": "400", "parent_pid": "1", "group_id": "100", "name": "worker"},
    ]
    evidence["injector_group"] = "100"
    assert {row["pid"] for row in injector_processes(evidence, process_list=processes)} == {"101", "102", "200", "400"}


@pytest.mark.parametrize(('processes', 'returncode', 'uncertain', 'expected'), [
    (({'pid': '123'},), 0, False, 'running'),
    ((), 0, False, 'finished'),
    ((), 1, False, 'failed'),
    ((), None, False, 'unknown'),
    ((), 0, True, 'unknown'),
    (OSError('inspection unavailable'), 0, False, 'unknown'),
])
def test_injector_state_distinguishes_process_lifetime_from_success(monkeypatch, processes, returncode, uncertain, expected):
    from doom_eap.launcher import installation_adapters as adapters

    def inspect(evidence):
        if isinstance(processes, OSError):
            raise processes
        return processes

    monkeypatch.setattr(adapters, 'injector_processes', inspect)
    evidence = {'injector_outcome': {'adapter': {'returncode': returncode, 'details': {'recovery_required': uncertain}}}}
    assert adapters.injector_state(evidence).value == expected


def test_same_name_replacement_does_not_claim_an_unplanned_removal(installation):
    plan, actions, calls = installation
    target = plan.game_root / "Mods" / plan.generated.name
    target.write_bytes(b"previous")
    plan.receipt_path.write_text(json.dumps({"staged_mod": str(target), "staged_sha256": file_digest(target)}))

    def prepare(journal):
        actions.prepare(journal)
        target.unlink()
        raise OSError("external removal")

    with pytest.raises(InstallationRecoveryRequired):
        InstallationService().execute(plan, replace(actions, prepare=prepare))
    assert not target.exists()
    assert not calls


def test_workflow_blocks_unknown_injector_result_until_reinstallation(workflow_installation, monkeypatch):
    from doom_eap.launcher import installation_adapters

    integration, game, app, state, config, snapshot, owner, calls = workflow_installation

    class Process:
        pid = 123456

        def __init__(self, *a, **kw):
            calls.append("inject")

        def wait(self):
            raise SystemExit("launcher exited")

    opener = platform.subprocess.Popen
    monkeypatch.setattr(platform.subprocess, "Popen", Process)
    workflow = integration.IntegratedLaunchWorkflow(
        app,
        state,
        config,
        platform_name="windows",
        consent=lambda _: True,
        confirmation=lambda: True,
        session_owner=owner,
    )
    with pytest.raises(SystemExit):
        workflow.execute(snapshot)
    restarted = integration.IntegratedLaunchWorkflow(
        app,
        state,
        config,
        platform_name="windows",
        consent=lambda _: True,
        confirmation=lambda: True,
        session_owner=owner,
    )
    monkeypatch.setattr(installation_adapters, 'injector_processes', lambda evidence: ({'pid': '123456'},))
    pending = restarted.execute(snapshot)
    assert pending.adapter_details['injector_state'] == 'running'
    assert 'still running' in pending.adapter_message
    with pytest.raises(InstallationRecoveryRequired):
        restarted.confirm_manual_installation(snapshot, transaction_id=pending.transaction_id)
    assert restarted.execute(snapshot, retry_injector=True).adapter_details['injector_state'] == 'running'
    assert calls == ['inject'] and not owner.prepare.called
    monkeypatch.setattr(installation_adapters, 'injector_processes', lambda evidence: ())
    pending = restarted.execute(snapshot)
    assert pending.adapter_state == "manual_install_required"
    assert pending.adapter_details['injector_state'] == 'unknown'
    assert 'INSTALL AGAIN' in pending.adapter_message
    assert not (state / "launcher_setup.json").exists()
    assert not owner.prepare.called
    with pytest.raises(InstallationRecoveryRequired):
        restarted.confirm_manual_installation(snapshot, transaction_id=pending.transaction_id)
    monkeypatch.setattr(platform.subprocess, "Popen", opener)
    confirmed = restarted.execute(snapshot, retry_injector=True)
    assert confirmed.adapter_state == "applied"
    assert calls == ["inject", "inject"]


def test_configuration_writer_refuses_a_busy_installation_without_waiting(installation):
    import threading

    from doom_eap.launcher.launcher_core_install import game_write_lock

    plan, _, _ = installation
    acquired = threading.Event()
    release = threading.Event()

    def hold():
        with game_write_lock(plan.game_root):
            acquired.set()
            release.wait(10)

    worker = threading.Thread(target=hold, daemon=True)
    worker.start()
    try:
        assert acquired.wait(5)
        before = plan.config_path.read_bytes()
        with pytest.raises(InstallationRecoveryRequired):
            LaunchWorkflow.write_client_config(
                plan.config_path.parent, runtime_config={"doom_base_dir": str(plan.game_root / "base")}
            )
        assert plan.config_path.read_bytes() == before
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    LaunchWorkflow.write_client_config(
        plan.config_path.parent, runtime_config={"doom_base_dir": str(plan.game_root / "base")}
    )
    assert json.loads(plan.config_path.read_text())["doom_base_dir"] == str(plan.game_root / "base")


def test_game_start_blocks_rollback_until_a_safe_restart(installation, monkeypatch):
    plan, actions, calls = installation
    target = plan.game_root / "sentinel_core.dll"

    def prepare(journal):
        journal.replace(target, b"new core", operation="test_publish")
        monkeypatch.setattr(
            platform, "detect_doom_processes", lambda: ({"name": "doometernalx64vk.exe", "pid": "123"},)
        )
        raise OSError("game started")

    with pytest.raises(RuntimeError):
        InstallationService().execute(plan, replace(actions, prepare=prepare))
    assert target.read_bytes() == b"new core"
    journal = plan.journal()
    journal.load()
    assert journal.phase is InstallPhase.RECOVERY_REQUIRED
    assert plan.receipt_path.read_bytes() == b"previous receipt"
    monkeypatch.setattr(platform, "detect_doom_processes", lambda: ())
    InstallationService().execute(plan, actions)
    assert not target.exists()
    assert calls == ["inject", "session"]


def test_injector_start_blocks_failure_rollback_until_a_safe_restart(installation, monkeypatch):
    from doom_eap.launcher import installation_adapters

    plan, actions, calls = installation
    target = plan.game_root / "sentinel_core.dll"

    def prepare(journal):
        journal.replace(target, b"new core", operation="test_publish")
        monkeypatch.setattr(
            installation_adapters, "_read_processes",
            lambda: [{"pid": "100", "parent_pid": "1", "name": "EternalModInjector.exe"}],
        )
        raise OSError("injector started")

    with pytest.raises(InstallationRecoveryRequired):
        InstallationService().execute(plan, replace(actions, prepare=prepare))
    assert target.read_bytes() == b"new core"
    journal = plan.journal()
    assert journal.load()
    assert journal.phase is InstallPhase.RECOVERY_REQUIRED
    assert plan.receipt_path.read_bytes() == b"previous receipt"
    monkeypatch.setattr(installation_adapters, "_read_processes", lambda: ())
    InstallationService().execute(plan, actions)
    assert not target.exists()
    assert calls == ["inject", "session"]



@pytest.mark.parametrize("windows", [False, True])
@pytest.mark.parametrize("running", [False, True])
def test_external_recovery_uses_platform_appropriate_guidance(tmp_path, monkeypatch, windows, running):
    from types import SimpleNamespace

    from doom_eap.launcher import installation_adapters
    from doom_eap.launcher.launcher_integration import IntegratedLaunchWorkflow

    journal = InstallJournal(tmp_path)
    journal.begin({})
    journal.transition(InstallPhase.INJECTING)
    plan = SimpleNamespace(journal=lambda: journal)
    workflow = SimpleNamespace(platform_name="windows" if windows else "linux")
    monkeypatch.setattr(installation_adapters, "injector_processes", lambda _: ({"pid": "123"},) if running else ())
    result = IntegratedLaunchWorkflow._external_recovery_outcome(workflow, plan, InstallationRecoveryRequired("uncertain"))
    assert result.adapter.state == "manual_install_required"
    assert result.adapter.details["recovery_required"]
    assert "INSTALL AGAIN" in result.adapter.message
    assert ("window" in result.adapter.message) is windows
    assert result.adapter.details["injector_state"] == ("running" if running else "unknown")
