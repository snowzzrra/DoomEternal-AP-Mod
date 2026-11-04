import json
import struct
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_resource_bank import write_bank

from doom_eap.launcher import campaign_resources
from doom_eap.launcher import launcher_platform as platform

TARGET = "game/sp/e1m1_intro/e1m1_intro"
HUB = "game/hub/hub"
DONOR = "game/dlc2/e5m1_spear/e5m1_spear"
HORDE = "game/horde/e6m3_mcity_horde/e6m3_mcity_horde"


@pytest.fixture
def installed_room(tmp_path, monkeypatch):
    from doom_eap.launcher import build_manifest, installation_adapters, resource_bank
    from doom_eap.runtime.save_decrypt import _aes_gcm_encrypt

    monkeypatch.setattr(platform, "detect_doom_processes", lambda: ())
    monkeypatch.setattr(installation_adapters, "injector_processes", lambda evidence: ())
    game = tmp_path / "game"
    base = game / "base"
    base.mkdir(parents=True)
    (game / "Mods").mkdir()
    (game / "DOOMEternalx64vk.exe").write_bytes(b"executable")
    names = [TARGET + ".resources", HUB + ".resources", "gameresources_patch1.resources",
             "gameresources_patch2.resources", DONOR + ".resources", HORDE + ".resources"]
    maps = [TARGET, HUB, "common", DONOR, HORDE]
    spec = {"files": [{"name": name} for name in names], "maps": [{"name": name} for name in maps],
            "mapFileRefs": [{"map": m, "file": f} for m, f in ((0, 0), (1, 1), (2, 2), (2, 3), (3, 4), (4, 5))]}
    (base / "packagemapspec.json").write_text(json.dumps(spec))
    members = {}
    for runtime, owner in ((TARGET, "e1m1_intro"), (HUB, "hub"), (DONOR, "e5m1_spear")):
        member = f"{owner}/maps/{runtime}.entities"
        members[member] = f"room map {runtime}".encode()
    shell = "gameresources_patch1/generated/decls/devmenuoption/shell.decl"
    members[shell] = b"room shell"
    for name in campaign_resources.STREAMFILES:
        members["gameresources_patch2/" + name] = ("room " + name).encode()
    for runtime, owner in ((TARGET, "e1m1_intro"), (HUB, "hub"), (DONOR, "e5m1_spear")):
        path = base / (runtime + ".resources")
        path.parent.mkdir(parents=True, exist_ok=True)
        member = f"{owner}/maps/{runtime}.entities"
        entries = [("map", member.split("/", 1)[1], members[member], ())]
        if runtime == DONOR:
            entries.extend([("model", "gui/text", b"font model", ()),
                            ("cswf", "swf/guientity/generic_text.swf", b"font swf", ())])
        write_bank(path, entries)
        path.with_suffix(".meta").write_bytes(b"resource metadata")
    (base / names[-1]).parent.mkdir(parents=True, exist_ok=True)
    write_bank(base / names[-1], [("model", "unused", b"unused", ())])
    write_bank(base / names[2], [("rs_streamfile", shell.split("/", 1)[1], members[shell], ())])
    (base / names[2]).with_suffix(".meta").write_bytes(b"resource metadata")
    write_bank(base / names[3], [("rs_streamfile", name, members["gameresources_patch2/" + name], ())
                               for name in campaign_resources.STREAMFILES])
    mask = struct.pack("<II", 0, len(names)) + b"".join(struct.pack("<QIQ", i + 1, 1, 0xffffffffffffffff)
                                                       for i in range(len(names)))
    write_bank(base / "meta.resources", [("rs_streamfile", "generated/buildgame/container.mask", mask, ())])
    (base / "idRehash.map").write_text("".join(f"{name};{8 + 20 * i}\n" for i, name in enumerate(names)))
    document = {"hash": "sha256", "files": {name: {} for name in (*names, "meta.resources", "packagemapspec.json")}}
    nonce = bytes(12)
    (base / "build-manifest.bin").write_bytes(nonce + _aes_gcm_encrypt(
        build_manifest._KEYS[0], nonce, json.dumps(document).encode(), b"build-manifest") + bytes(64))

    def rehash(command, *, cwd, **kwargs):
        directory = Path(cwd)
        path = directory / "meta.resources"
        row = campaign_resources._records(path, {"generated/buildgame/container.mask"})["generated/buildgame/container.mask"]
        data = bytearray(path.read_bytes())
        for line in (directory / "idRehash.map").read_text().splitlines():
            name, offset = line.rsplit(";", 1)
            assert Path(name).is_file()
            struct.pack_into("<Q", data, row[2] + int(offset), 123)
        path.write_bytes(data)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(resource_bank, "subprocess", SimpleNamespace(run=rehash, CREATE_NO_WINDOW=0))

    def room(include_dlc):
        mod = tmp_path / "room.zip"
        manifest = {"manifest_hash": "room", "required_capabilities": ["unified_campaign_v1"], "options": {}}
        with zipfile.ZipFile(mod, "w") as archive:
            archive.writestr("seed_manifest.json", json.dumps(manifest))
            for name, payload in members.items():
                if include_dlc or "/maps/game/dlc" not in name:
                    archive.writestr(name, payload)
        return game, mod

    return room


@pytest.mark.parametrize("include_dlc", [False, True])
def test_finalize_then_verify_with_generated_hub_bank(installed_room, include_dlc):
    game, source = installed_room(include_dlc)
    staged = platform._stage_mod(source, game)
    for _ in range(2):
        result = campaign_resources.enable_campaign_resources(game, staged)
        spec = json.loads((game / "base/packagemapspec.json").read_bytes())
        assert spec["doomEAPEnemyResourceBanks"]["refs"]
        assert result["state"] == "enabled"
        verified = platform.verify_linux_mod_installation(game, staged)
        evidence = {item["label"]: item for item in verified["required_resources"]}
        assert set(evidence) == {"e1m1", "hub", "shell"} | ({"tag"} if include_dlc else set())
        assert evidence["hub"]["expected_owner"] == HUB + ".resources"
        assert all(item["status"] == "verified" and not item["failure_reason"] for item in evidence.values())
    checked = subprocess.run([sys.executable, "-S", "-c",
        "import sys; from pathlib import Path; "
        "from doom_eap.launcher import campaign_resources, launcher_platform; "
        "launcher_platform.detect_doom_processes = lambda: (); "
        "game, mod = map(Path, sys.argv[1:]); "
        "campaign_resources.enable_campaign_resources(game, mod); "
        "print(len(launcher_platform.verify_linux_mod_installation(game, mod)['required_resources']))",
        str(game), str(staged)], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr
    assert checked.stdout.strip() == str(4 if include_dlc else 3)


@pytest.mark.parametrize("damage", ["empty", "missing", "outside", "truncated", "duplicate", "invalid", "valid"])
def test_injector_preparation_holds_banks_and_repairs_mapping(installed_room, damage):
    game, _ = installed_room(False)
    base = game / "base"
    bank = base / "ap_enemy_resources/cache.resources"
    bank.parent.mkdir()
    bank.write_bytes(b"cached bank")
    mapping = base / "idRehash.map"
    valid = mapping.read_text()
    mapping.write_text(valid + "ap_enemy_resources/cache.resources;999999\n")
    if damage == "empty":
        mapping.write_text("ap_enemy_resources/cache.resources;999999\n")
    elif damage == "missing":
        mapping.unlink()
    elif damage == "outside":
        mapping.write_text(mapping.read_text() + "other.resources;999999\n")
    elif damage == "truncated":
        mapping.write_text(valid.splitlines()[0] + "\n")
    elif damage == "duplicate":
        mapping.write_text(mapping.read_text() + valid.splitlines()[0] + "\n")
    elif damage == "invalid":
        mapping.write_text("not a mapping row\ninvalid.resources;²\ninvalid.resources;" + "9" * 5000 + "\n", encoding="utf-8")
    settings = game / "EternalModInjector Settings.txt"
    settings.write_text(":HAS_CHECKED_RESOURCES=1\n:RESET_BACKUPS=0\n:ASSET_VERSION=fixture\nkeep.backup\n")
    (base / "native.backup").write_bytes(b"backup")
    logs = []
    with pytest.raises(RuntimeError, match="injector failure"):
        with platform.prepare_injector_resources(game, lambda kind, **payload: logs.append(payload["message"])):
            assert not bank.parent.exists()
            held, = game.glob(".doomeap-injector-*/ap_enemy_resources/cache.resources")
            assert base not in held.parents and held.read_bytes() == b"cached bank"
            if mapping.exists():
                assert "ap_enemy_resources/" not in mapping.read_text()
                assert "999999" not in mapping.read_text()
            assert f":HAS_CHECKED_RESOURCES={int(damage == 'valid')}" in settings.read_text()
            assert ":RESET_BACKUPS=0" in settings.read_text()
            raise RuntimeError("injector failure")
    assert bank.read_bytes() == b"cached bank"
    assert not list(game.glob(".doomeap-injector-*"))
    assert any("Injector before:" in line for line in logs)
    assert any("Injector after:" in line for line in logs)
    state = platform.injector_state_summary(game)
    assert state["settings_backup_lines"] == 1 and state["backup_files"] == 1
    assert state["meta_decoded"] is True


@pytest.mark.parametrize("system", ["linux", "windows"])
def test_adapters_prepare_banks_before_invoking_injector(installed_room, monkeypatch, system):
    game, _ = installed_room(False)
    banks = game / "base/ap_enemy_resources"
    banks.mkdir()
    (banks / "cached.resources").write_bytes(b"cache")
    logs = []

    def run(*args, **kwargs):
        assert not banks.exists()
        assert not list((game / "base").rglob("cached.resources"))
        return SimpleNamespace(returncode=0, stdout="full stdout", stderr="full stderr")

    if system == "linux":
        adapter = platform.LinuxModManagerAdapter(SimpleNamespace(version="fixture"),
            event_sink=lambda kind, **payload: logs.append(payload.get("message", "")))
        monkeypatch.setattr(adapter, "prepare", lambda *a, **kw: game / "injector")
        class Process:
            pid = 123
            returncode = 0
            stdout = stderr = None

            def communicate(self, **kwargs):
                result = run()
                return result.stdout, result.stderr

        monkeypatch.setattr(platform.subprocess, "Popen", lambda *a, **kw: Process())
    else:
        adapter = platform.WindowsModInjectorAdapter(None, opener=run,
            event_sink=lambda kind, **payload: logs.append(payload.get("message", "")))
        monkeypatch.setattr(platform, "stage_windows_injector_toolchain", lambda *a, **kw: game / "injector.bat")
    result = adapter.run(game)
    assert result.state == "applied"
    assert (banks / "cached.resources").read_bytes() == b"cache"
    assert any("Injector before:" in line for line in logs)
    assert any("Injector after:" in line for line in logs)


def crash_injector_hold(game, point):
    script = """
import os, sys
from pathlib import Path
from doom_eap.launcher import launcher_platform as platform
platform.detect_doom_processes = lambda: ()
root, point = Path(sys.argv[1]), sys.argv[2]
rename = Path.rename
def interrupted_rename(path, target):
    result = rename(path, target)
    if point == 'moved' and path == root / 'base/ap_enemy_resources':
        os._exit(77)
    if point == 'restored' and path == root / platform.INJECTOR_HOLD / 'ap_enemy_resources':
        os._exit(77)
    return result
Path.rename = interrupted_rename
with platform.prepare_injector_resources(root) as hold:
    if point in {'injecting', 'restored'}:
        hold.start_injector()
    if point == 'restored':
        (root / 'base/idRehash.map').write_bytes(b'post-injector map')
        hold.finish_injector()
    else:
        os._exit(77)
"""
    child = subprocess.run([sys.executable, "-c", script, str(game), point], capture_output=True, text=True)
    assert child.returncode == 77, child.stderr


@pytest.mark.parametrize("point", ["moved", "prepared", "restored"])
def test_injector_hold_recovers_after_process_crash(installed_room, point):
    game, _ = installed_room(False)
    bank = game / "base/ap_enemy_resources/cached.resources"
    bank.parent.mkdir()
    bank.write_bytes(b"preserved bank")
    mapping = game / "base/idRehash.map"
    mapping.write_bytes(mapping.read_bytes() + b"ap_enemy_resources/cached.resources;999999\n")
    settings = game / "EternalModInjector Settings.txt"
    settings.write_bytes(b":HAS_CHECKED_RESOURCES=1\n:RESET_BACKUPS=0\n")
    originals = {path: path.read_bytes() for path in (bank, mapping, settings)}
    crash_injector_hold(game, point)
    assert platform.recover_injector_resources(game)
    for path, content in originals.items():
        assert path.read_bytes() == (b"post-injector map" if point == "restored" and path == mapping else content)
    assert not (game / platform.INJECTOR_HOLD).exists()
    assert not platform.recover_injector_resources(game)


@pytest.mark.parametrize("change", ["injecting", "mapping", "bank", "journal"])
def test_injector_recovery_preserves_ambiguous_or_changed_files(installed_room, monkeypatch, change):
    from unittest.mock import Mock
    game, _ = installed_room(False)
    bank = game / "base/ap_enemy_resources/cached.resources"
    bank.parent.mkdir()
    bank.write_bytes(b"original bank")
    crash_injector_hold(game, "injecting" if change == "injecting" else "prepared")
    held = game / platform.INJECTOR_HOLD
    if change != "injecting":
        target = {"mapping": game / "base/idRehash.map", "bank": held / "ap_enemy_resources/cached.resources",
                  "journal": held / "journal.json"}[change]
        target.write_bytes(b"external change")
    with pytest.raises(platform.InjectorRecoveryRequired):
        platform.recover_injector_resources(game)
    assert held.exists()
    if change != "injecting":
        assert target.read_bytes() == b"external change"
    opener, staging = Mock(), Mock()
    monkeypatch.setattr(platform, "stage_windows_injector_toolchain", staging)
    adapter = platform.WindowsModInjectorAdapter(None, opener=opener)
    assert adapter.run(game).state == "failed"
    staging.assert_not_called()
    opener.assert_not_called()


@pytest.mark.parametrize("termination_fails", [False, True])
def test_injector_timeout_keeps_output_and_blocks_recovery(installed_room, monkeypatch, termination_fails):
    from doom_eap.launcher import installation_adapters

    game, _ = installed_room(False)
    bank = game / "base/ap_enemy_resources/cached.resources"
    bank.parent.mkdir()
    bank.write_bytes(b"preserved bank")
    adapter = platform.LinuxModManagerAdapter(SimpleNamespace(version="fixture"))
    monkeypatch.setattr(adapter, "prepare", lambda *a, **kw: game / "injector")
    class Process:
        pid = 123
        stdout = stderr = None

        def communicate(self, **kwargs):
            raise subprocess.TimeoutExpired("injector", 900, output=b"partial output", stderr=b"partial error")

        def wait(self, **kwargs):
            return -1

    monkeypatch.setattr(platform.subprocess, "Popen", lambda *a, **kw: Process())
    killed = []
    monkeypatch.setattr(installation_adapters.signal, "SIGKILL", 9, raising=False)
    def kill_group(group, sig):
        killed.append((group, sig))
        if termination_fails:
            raise PermissionError("group termination refused")

    monkeypatch.setattr(installation_adapters.os, "killpg", kill_group, raising=False)
    result = adapter.run(game)
    assert killed == [(123, 9)]
    assert result.state == "timed_out"
    assert result.stdout == "partial output" and result.stderr == "partial error"
    assert result.details["recovery_required"] is True
    assert result.details["injector_state"] == "unknown"
    assert ("termination_error" in result.details) is termination_fails
    assert not bank.exists()
    assert (game / platform.INJECTOR_HOLD / "ap_enemy_resources/cached.resources").read_bytes() == b"preserved bank"
    with pytest.raises(platform.InjectorRecoveryRequired):
        platform.recover_injector_resources(game)


def test_verify_reports_expected_owner_and_missing_evidence(installed_room):
    game, source = installed_room(True)
    staged = platform._stage_mod(source, game)
    campaign_resources.enable_campaign_resources(game, staged)
    (game / "base" / (HUB + ".meta")).unlink()
    with pytest.raises(platform.LinuxVerificationError) as failure:
        platform.verify_linux_mod_installation(game, staged)
    details = failure.value.details
    assert details["failure_step"] == "verify" and details["exception_type"] == "LinuxVerificationError"
    hub, = [item for item in details["required_resources"] if item["label"] == "hub"]
    assert hub["expected_owner"] == HUB + ".resources" and hub["failure_reason"] == "missing meta file"
    assert "Traceback" in details["traceback"]


@pytest.mark.parametrize("step", ["prepare banks", "register banks", "manifest", "publish", "verify"])
def test_install_failure_reaches_journal_and_support(installed_room, monkeypatch, tmp_path, step):
    from doom_eap.launcher import build_manifest, resource_bank
    from doom_eap.launcher import launcher_doctor as doctor
    from doom_eap.launcher.install_journal import file_digest
    from doom_eap.launcher.installation import (
        InstallationActions,
        InstallationOutcome,
        InstallationPlan,
        InstallationService,
    )

    game, source = installed_room(False)
    events = []
    adapter = platform.LinuxModManagerAdapter(None, event_sink=lambda kind, **payload: events.append((kind, payload)))
    monkeypatch.setattr(adapter, "run", lambda _: platform.AdapterResult(
        state="applied", message="injected", stdout="injector stdout", stderr="injector stderr", returncode=0))

    def fail(*args, **kwargs):
        raise OSError("forced resource failure")

    target, name = {
        "prepare banks": ("doom_eap.launcher.native_enemy_resources", "prepare_native_resource_banks"),
        "register banks": (resource_bank, "register_banks"),
        "manifest": (build_manifest, "register_resource_manifest"),
        "publish": (campaign_resources, "publish_resource_activation"),
        "verify": (platform, "verify_linux_mod_installation"),
    }[step]
    if isinstance(target, str):
        monkeypatch.setattr(target + "." + name, fail)
    else:
        monkeypatch.setattr(target, name, fail)

    state = tmp_path / "state"
    state.mkdir()
    config = {"doom_base_dir": str(game / "base")}
    receipt_path = state / "launcher_setup.json"
    receipt_path.write_bytes(b"previous installation")
    plan = InstallationPlan(game, source, receipt_path, tmp_path / "ap_config.json", state,
        {"manifest_hash": "room", "generated_sha256": file_digest(source),
         "executable_sha256": file_digest(game / "DOOMEternalx64vk.exe")})
    staged = game / "Mods" / source.name
    def sink(kind, **payload):
        events.append((kind, payload))
    port = []
    def prepare(journal):
        port.append(journal)
        platform.stage_room_mod(source, game, receipt_path, manifest_hash="room", journal=journal)
    actions = InstallationActions(
        prepare=prepare,
        inject=lambda: InstallationOutcome(adapter.run(game)), confirm=lambda: True,
        finalize=lambda outcome: InstallationOutcome(platform.finalize_mod_installation(game, staged, outcome.adapter, event_sink=sink, journal=port[0])),
        verify=lambda outcome: InstallationOutcome(platform.verify_mod_installation(game, staged, outcome.adapter, platform_name="linux", event_sink=sink)),
        session=lambda journal: None, receipt=lambda outcome: {}, check=lambda: None)
    result = InstallationService().execute(plan, actions).adapter
    journal = plan.journal()
    journal.load()
    assert result.state == "failed"
    assert receipt_path.read_bytes() == b"previous installation"
    assert step in result.message and "forced resource failure" in result.message
    details = journal.document["evidence"]["outcome"]["adapter"]["details"]
    assert details["failure_step"] == step and details["exception_type"] == "OSError"
    assert "forced resource failure" in details["verification_error"] and "Traceback" in details["traceback"]
    assert {item["label"] for item in details["required_resources"]} == {"hub", "e1m1", "shell"}
    logs = [payload["message"] for kind, payload in events if kind == "log"]
    assert any(step in line for line in logs) and any("OSError" in line for line in logs)
    assert any("Traceback" in line for line in logs)
    monkeypatch.setattr(doctor, "_support_log_tails", lambda *a, **kw: ({"launcher.log": "saved launcher output"}, {}))
    report = doctor.write_support_bundle(tmp_path / "support.zip", doctor.DoctorReport("test", ()),
        config=config, logs=logs, support_diagnostics={"installation": journal.document},
        injector_output=result.message + "\ninjector stdout\ninjector stderr")
    with zipfile.ZipFile(report) as archive:
        assert step.encode() in archive.read("launcher.log")
        assert b"forced resource failure" in archive.read("doctor.json")
        assert b"injector stdout" in archive.read("install/injector.log")


@pytest.fixture
def resource_transaction(installed_room, tmp_path):
    from doom_eap.launcher.install_journal import file_digest
    from doom_eap.launcher.installation import InstallationActions, InstallationOutcome, InstallationPlan

    game, source = installed_room(False)
    state = tmp_path / 'state'
    state.mkdir()
    receipt = state / 'launcher_setup.json'
    receipt.write_bytes(b'previous receipt')
    plan = InstallationPlan(game, source, receipt, tmp_path / 'config.json', state,
        {'manifest_hash': 'room', 'generated_sha256': file_digest(source),
         'executable_sha256': file_digest(game / 'DOOMEternalx64vk.exe')})
    staged = game / 'Mods' / source.name
    calls = []

    def prepare(journal):
        platform.stage_room_mod(source, game, receipt, manifest_hash='room', journal=journal)

    def inject():
        calls.append('inject')
        return InstallationOutcome(platform.AdapterResult('applied', 'injected'))

    def finalize(outcome):
        journal = plan.journal()
        journal.load()
        return InstallationOutcome(platform.finalize_mod_installation(
            game, staged, outcome.adapter, journal=journal))

    def session(journal):
        calls.append('session')

    actions = InstallationActions(prepare, inject, lambda: True, finalize,
        lambda outcome: InstallationOutcome(platform.verify_mod_installation(
            game, staged, outcome.adapter, platform_name='windows')),
        session, lambda outcome: {'installed': True}, lambda: None)
    return plan, actions, calls


@pytest.mark.parametrize('target', ['bank', 'receipt', 'meta.resources', 'idRehash.map',
                                    'packagemapspec.json', 'build-manifest.bin'])
@pytest.mark.parametrize('after', [False, True])
def test_resource_publication_restart_reuses_plan_without_reinjecting(resource_transaction, monkeypatch, target, after):
    from doom_eap.launcher import install_journal
    from doom_eap.launcher.installation import InstallationService

    plan, actions, calls = resource_transaction
    publish = install_journal.publish_file

    def interrupted(source, path, *, operation):
        hit = ((target == 'bank' and operation == 'resource_bank')
               or (target == 'receipt' and operation == 'resource_bank_receipt')
               or (operation == 'resource_activation' and path.name == target))
        if hit and not after:
            raise SystemExit('interrupted before write')
        publish(source, path, operation=operation)
        if hit:
            raise SystemExit('interrupted after write')

    monkeypatch.setattr(install_journal, 'publish_file', interrupted)
    with pytest.raises(SystemExit):
        InstallationService().execute(plan, actions)
    assert plan.receipt_path.read_bytes() == b'previous receipt'
    monkeypatch.setattr(install_journal, 'publish_file', publish)
    monkeypatch.setattr(campaign_resources, 'plan_campaign_resources',
                        lambda *a: pytest.fail('the persisted plan must be reused'))
    result = InstallationService().execute(plan, actions)
    assert result.adapter.state == 'applied' and calls == ['inject', 'session']
    journal = plan.journal()
    journal.load()
    assert journal.summary()['resources_verified']
    assert not journal.summary()['session_pending']


@pytest.mark.parametrize('damage', [None, 'bank', 'meta.resources'])
def test_verified_resources_survive_save_failure_and_are_checked_on_resume(resource_transaction, monkeypatch, damage):
    from dataclasses import replace

    from doom_eap.launcher.installation import InstallationService

    plan, actions, calls = resource_transaction

    def fail(journal):
        calls.append('save failed')
        raise OSError('save unavailable')

    with pytest.raises(OSError, match='save unavailable'):
        InstallationService().execute(plan, replace(actions, session=fail))
    journal = plan.journal()
    journal.load()
    assert journal.summary()['resources_verified'] and journal.summary()['session_pending']
    assert plan.receipt_path.read_bytes() == b'previous receipt'
    monkeypatch.setattr(campaign_resources, 'plan_campaign_resources', lambda *a: pytest.fail('must not replan'))
    if damage:
        path = (next((plan.game_root / 'base/ap_enemy_resources').glob('*.resources')) if damage == 'bank'
                else plan.game_root / 'base' / damage)
        path.write_bytes(b'external damage')
        with pytest.raises(ValueError):
            InstallationService().execute(plan, actions)
        assert path.read_bytes() == b'external damage'
        assert calls == ['inject', 'save failed']
    else:
        assert InstallationService().execute(plan, actions).adapter.state == 'applied'
        assert calls == ['inject', 'save failed', 'session']


def test_obsolete_bank_cleanup_failure_is_pending_after_commit(resource_transaction, monkeypatch):
    from doom_eap.launcher import resource_bank
    from doom_eap.launcher.installation import InstallationService

    plan, actions, calls = resource_transaction
    seen = []

    def cleanup(base, obsolete):
        journal = plan.journal()
        journal.load()
        seen.append(journal.phase.value)
        assert json.loads(plan.receipt_path.read_bytes())['installed']
        return {'removed': [], 'bytes_freed': 0, 'errors': ['cleanup unavailable']}

    monkeypatch.setattr(campaign_resources, '_cleanup_resource_banks', cleanup)
    assert InstallationService().execute(plan, actions).adapter.state == 'applied'
    journal = plan.journal()
    journal.load()
    assert seen == ['COMPLETE'] and journal.summary()['resources_verified']
    assert 'cleanup unavailable' in journal.summary()['evidence']['cleanup_error']
    monkeypatch.setattr(campaign_resources, '_cleanup_resource_banks', lambda *a: resource_bank.cleanup_banks(*a))
    assert InstallationService().execute(plan, actions).adapter.state == 'applied'
    journal.load()
    assert 'cleanup_error' not in journal.document['evidence']
    assert calls == ['inject', 'session']


@pytest.mark.parametrize('target', ['bank', 'meta.resources'])
def test_resource_rollback_restores_metadata_and_removes_new_banks(resource_transaction, monkeypatch, target):
    from doom_eap.launcher import install_journal
    from doom_eap.launcher.installation import InstallationService

    plan, actions, calls = resource_transaction
    base = plan.game_root / 'base'
    before = {path: path.read_bytes() for path in base.rglob('*') if path.is_file()}
    publish = install_journal.publish_file

    def interrupted(source, path, *, operation):
        publish(source, path, operation=operation)
        if ((target == 'bank' and operation == 'resource_bank')
                or (operation == 'resource_activation' and path.name == target)):
            raise SystemExit('resource publication interrupted')

    monkeypatch.setattr(install_journal, 'publish_file', interrupted)
    with pytest.raises(SystemExit):
        InstallationService().execute(plan, actions)
    monkeypatch.setattr(install_journal, 'publish_file', publish)
    journal = plan.journal()
    journal.load()
    journal.rollback()
    assert before == {path: path.read_bytes() for path in base.rglob('*') if path.is_file()}
    assert plan.receipt_path.read_bytes() == b'previous receipt' and calls == ['inject']


@pytest.mark.parametrize("output,code,prompt", [
    ("Old Doom Eternal backups detected!\nReset backups now? [y/N]", 1, "old_backups"),
    ("Reset backups now? [y/N]", 1, "reset_backups"),
    ("ETERNALMODMANAGER: read: end of file", 1, "modmanager"),
    ("read: end of file", 1, None),
    ("undefined symbol: rl_print_keybinding", 127, None),
])
def test_linux_nonzero_output_explains_residual_prompt(installed_room, monkeypatch, output, code, prompt):
    game, _ = installed_room(False)
    adapter = platform.LinuxModManagerAdapter(SimpleNamespace(version="fixture"))
    options = {}

    class Process:
        pid = 123
        returncode = code
        stdout = stderr = None

        def communicate(self, **kwargs):
            assert kwargs["timeout"] == 900
            return "", output

    def opener(*args, **kwargs):
        options.update(kwargs)
        return Process()

    monkeypatch.setattr(platform.subprocess, "Popen", opener)
    result = adapter.run(game, prepared=game / "fake-injector")
    assert result.state == "failed" and result.returncode == code
    assert result.stderr == output and result.details["residual_prompt"] == prompt
    assert options["stdin"] == subprocess.DEVNULL and options["start_new_session"]
    if code == 127:
        assert result.details["failure_category"] == "linux_loader_symbol_failure"
        assert "symbol lookup failed" in result.message
    else:
        assert "INSTALL AGAIN" in result.message
        assert "without input" in result.message or "end of input" in result.message


@pytest.mark.parametrize("system", ["linux", "windows"])
def test_close_requests_stop_without_abandoning_injector(installed_room, monkeypatch, system):
    import threading
    from unittest.mock import Mock

    from doom_eap.launcher.launcher_controller import LauncherController
    from doom_eap.launcher.launcher_workers import LauncherWorkers

    game, _ = installed_room(False)
    started, finish = threading.Event(), threading.Event()
    results, errors = [], []

    class Process:
        pid = 123
        returncode = 0
        stdout = stderr = None

        def wait(self):
            started.set()
            assert finish.wait(5)

        def communicate(self, **kwargs):
            self.wait()
            return "", ""

    if system == "windows":
        adapter = platform.WindowsModInjectorAdapter(None, opener=lambda *a: Process())
    else:
        adapter = platform.LinuxModManagerAdapter(SimpleNamespace(version="fixture"))
        monkeypatch.setattr(platform.subprocess, "Popen", lambda *a, **kw: Process())
    controller = LauncherController.__new__(LauncherController)
    controller.workers = LauncherWorkers(lambda job, error: errors.append(error))
    controller.workflow = Mock()
    controller.workflow.session_owner.can_close.return_value = True
    controller._game_lifecycle_stop = threading.Event()
    controller._game_lifecycle_thread = None
    controller._stop_native_client = Mock()
    controller.disconnect = Mock()

    def install(job):
        results.append(adapter.run(game, prepared=game / "fake-injector"))
        job.check()
        errors.append("continued after stop")

    try:
        assert controller.workers.submit("install", install)
        assert started.wait(3)
        assert controller.close() is False
        assert controller.workers.active == {"install"}
        controller.workflow.session_owner.retire.assert_not_called()
        controller.disconnect.assert_not_called()
    finally:
        finish.set()
        controller.workers.close(5)
    assert not errors and results[0].state == "applied"
    assert controller.close() is True
    controller.workflow.session_owner.retire.assert_called_once()
    assert not (game / platform.INJECTOR_HOLD).exists()
