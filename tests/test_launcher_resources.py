import json
import struct
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from doom_eap.launcher import campaign_resources, launcher_platform as platform
from doom_eap.launcher import launcher_integration as integration
from test_resource_bank import write_bank


TARGET = "game/sp/e1m1_intro/e1m1_intro"
HUB = "game/hub/hub"
DONOR = "game/dlc2/e5m1_spear/e5m1_spear"
HORDE = "game/horde/e6m3_mcity_horde/e6m3_mcity_horde"


@pytest.fixture
def installed_room(tmp_path, monkeypatch):
    from doom_eap.launcher import build_manifest, resource_bank
    from doom_eap.runtime.save_decrypt import _aes_gcm_encrypt

    monkeypatch.setattr(platform, "detect_doom_processes", lambda: ())
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


@pytest.mark.parametrize("damage", ["empty", "missing", "outside", "truncated", "duplicate", "valid"])
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
        monkeypatch.setattr(adapter, "_prepare_tools", lambda _: game / "injector")
        monkeypatch.setattr(platform, "subprocess", SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired))
    else:
        adapter = platform.WindowsModInjectorAdapter(None, opener=run,
            event_sink=lambda kind, **payload: logs.append(payload.get("message", "")))
        monkeypatch.setattr(platform, "stage_windows_injector_toolchain", lambda *a, **kw: game / "injector.bat")
    result = adapter.run(game)
    assert result.state == "applied"
    assert (banks / "cached.resources").read_bytes() == b"cache"
    assert any("Injector before:" in line for line in logs)
    assert any("Injector after:" in line for line in logs)


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


@pytest.mark.parametrize("step", ["prepare banks", "register banks", "manifest", "publish", "cleanup", "verify"])
def test_install_failure_reaches_receipt_and_support(installed_room, monkeypatch, tmp_path, step):
    from doom_eap.launcher import build_manifest, resource_bank, launcher_doctor as doctor

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
        "cleanup": (resource_bank, "cleanup_banks"),
        "verify": (platform, "verify_linux_mod_installation"),
    }[step]
    if isinstance(target, str):
        monkeypatch.setattr(target + "." + name, fail)
    else:
        monkeypatch.setattr(target, name, fail)
    if step == "cleanup":
        monkeypatch.setattr(resource_bank, "obsolete_banks", lambda *a: {"obsolete.resources": {}})

    state = tmp_path / "state"
    state.mkdir()
    config = {"doom_base_dir": str(game / "base")}
    config_path = state / "config.json"
    config_path.write_text(json.dumps(config))
    workflow = integration.IntegratedLaunchWorkflow(tmp_path, state, config_path, platform_name="linux")
    manifest = SimpleNamespace(manifest_hash="room", seed_name="seed", team=0, slot=1,
                               options={"randomize_dash": False}, static_content_digest="")
    monkeypatch.setattr(workflow, "install_state", lambda _: integration.InstallState("install_needed", "room"))
    monkeypatch.setattr(workflow, "_cached_install", lambda *a: None)
    monkeypatch.setattr(workflow, "ensure_game_link", lambda *a, **kw: SimpleNamespace(state="verified"))
    monkeypatch.setattr(integration, "probe_runtime_prerequisites", lambda *a: SimpleNamespace(ok=True))
    monkeypatch.setattr(workflow, "_manifest_for", lambda _: manifest)
    monkeypatch.setattr(workflow, "_idfile_manager", lambda: None)
    monkeypatch.setattr(workflow, "_room_compiler", lambda **kw: SimpleNamespace(build=lambda *a: source))
    monkeypatch.setattr(workflow, "_publish_client_config", lambda **kw: config_path)
    monkeypatch.setattr(workflow, "_adapter", lambda config, root, staged: (adapter.activate(root, source), "", ""))
    record = workflow.execute(None)
    receipt = json.loads((state / "launcher_setup.json").read_text())
    assert record.adapter_state == "failed"
    assert step in receipt["adapter_message"] and "forced resource failure" in receipt["adapter_message"]
    assert integration.setup_failure_payload(RuntimeError(record.adapter_message))["user_message"] == record.adapter_message
    details = receipt["adapter_details"]
    assert details["failure_step"] == step and details["exception_type"] == "OSError"
    assert "forced resource failure" in details["verification_error"] and "Traceback" in details["traceback"]
    assert {item["label"] for item in details["required_resources"]} == {"hub", "e1m1", "shell"}
    logs = [payload["message"] for kind, payload in events if kind == "log"]
    assert any(step in line for line in logs) and any("OSError" in line for line in logs)
    assert any("Traceback" in line for line in logs)
    summary = doctor._room_install_receipt_summary(state)
    assert summary["adapter_details"] == details
    monkeypatch.setattr(doctor, "_support_log_tails", lambda *a, **kw: ({"launcher.log": "saved launcher output"}, {}))
    report = doctor.write_support_bundle(tmp_path / "support.zip", doctor.DoctorReport("test", ()),
        config=config, logs=logs, support_diagnostics={"room_install_receipt": summary},
        injector_output=record.adapter_message + "\ninjector stdout\ninjector stderr")
    with zipfile.ZipFile(report) as archive:
        assert step.encode() in archive.read("launcher.log")
        assert b"forced resource failure" in archive.read("doctor.json")
        assert b"injector stdout" in archive.read("install/injector.log")
