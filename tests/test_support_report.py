import hashlib
import json
import threading
import zipfile
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from doom_eap.launcher import launcher_doctor as doctor
from doom_eap.launcher import launcher_reporting as reporting
from doom_eap.launcher import sentinel_diagnostics as sentinel
from doom_eap.launcher.launcher_controller import LauncherController
from tests.test_core_distribution import fixture as core_distribution_fixture


@pytest.mark.parametrize("proton", [False, True])
def test_bundle_keeps_incompatible_install_history_and_removes_secrets(tmp_path, monkeypatch, proton):
    game = tmp_path / "game"
    (game / "base").mkdir(parents=True)
    (game / "DOOMEternalx64vk.exe").write_bytes(b"test executable")
    manifest = core_distribution_fixture(game)
    manifest.update(version="1.0.0", base_version="1.0.0", channel="stable", rc_number=0)
    manifest["ownership_private"] = {"identity": "platform-canary"}
    (game / "sentinel-distribution.json").write_text(json.dumps(manifest))
    (game / "XINPUT1_3.dll").write_bytes(b"foreign proxy")
    (game / "version.dll").write_bytes(b"another proxy")
    (game / "Mods").mkdir()
    mod = game / "Mods/room.zip"
    mod.write_bytes(b"room package")
    config = {"game_root": str(game), "client_state_file": str(tmp_path / "receipts.json"),
              "doom_config_path": str(tmp_path / "game.cfg")}
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    if proton:
        config["proton_compat_data_dir"] = str(tmp_path / "compatdata/782330")
        monkeypatch.setattr(sentinel, "os", SimpleNamespace(name="posix", environ={}))
    directory = sentinel.diagnostics_directory(config)
    directory.mkdir(parents=True)
    session = {"pid": 71, "process_created": 123, "build_id": manifest["build_id"], "namespace_id": "room"}
    record = {"schema": "sentinel-startup-v1", "pid": 71, "process_created": "123",
              "build_id": manifest["build_id"], "session": {"namespace_id": "room"}, "admission": {"namespace_id": "room"},
              "private": {"pointer": "pointer-canary"}, "nested": [{"Ownership_Private": {"handle": "handle-canary"}}],
              "password": "password-canary", "token": "token-canary"}
    for suffix in (".jsonl.previous", ".jsonl", ".latest.json"):
        (directory / ("71-123" + suffix)).write_text(json.dumps(record))
    (directory / "71-123.darklord.log").write_text('AP_DARKLORD event=summary token=token-canary\n')
    history = sentinel.collect(config, session)
    monkeypatch.setattr(doctor, "_support_log_tails", lambda *a, **kw: ({}, {}))
    paths = SimpleNamespace(state_dir=tmp_path / "state", data_dir=tmp_path / "data")
    live = {"ap_session": session, "native_diagnostics": history,
            "native_helper_startup": {"state": "failed", "ownership_private": "helper-canary"}}
    support = doctor.build_support_diagnostics(config, tmp_path / "launcher.json", paths, [], {}, {}, live=live)
    support["session"] = {"connected": False, "identity_source": "last_known", "seed_name": "test-room", "slot": 2,
                          "slot_data": {"apworld_revision": "0.7.0"}}
    support["game_link"].update(queue={"status": "available", "pending": 3, "processing": 1, "failed": 2},
                                 telemetry_safety={"safety": {"status": "blocked"}})
    console = game / "base/qconsole.log"
    console.write_text("old console\n" * 30000 + '2026-10-27T14:30:00Z ERROR password=password-canary\n')
    controller = LauncherController.__new__(LauncherController)
    controller._lifecycle_lock = threading.Lock()
    controller.workers = SimpleNamespace(accepts=lambda generation: True)
    controller.emit = lambda *args, **kwargs: None
    controller._setup_event("injector_started", {})
    controller._setup_event("injector_finished", {"stdout": "old-output\n" * 10000,
                            "stderr": 'ERROR token=token-canary\n' + json.dumps({"ownership_private": "injector-canary"})})
    assert len(controller._injector_output.encode()) <= 64 * 1024
    result = doctor.write_support_bundle(tmp_path / "report.zip", doctor.DoctorReport("test", ()),
        config=config, support_diagnostics=support, injector_output=controller._injector_output,
        logs=['2026-10-27T14:31:00Z WARNING ' + json.dumps({"nested": {"ownership_private": "log-canary"}}),
              'ownership_private={"handle": "assignment-canary"}'],
        last_setup_failure={"failure_domain": "core_runtime", "message": "Update refused"})
    with zipfile.ZipFile(result) as archive:
        install = json.loads(archive.read("install.json"))
        assert install["manifest"]["parses"] is True
        assert install["manifest"]["document"]["version"] == "1.0.0"
        assert install["structure"]["status"] == "valid"
        assert install["ownership"]["status"] == "owned"
        assert install["compatibility"]["status"] == "incompatible"
        dlls = {item["name"]: item for item in install["dlls"]}
        assert dlls["version.dll"]["sha256"] == hashlib.sha256(b"another proxy").hexdigest()
        assert dlls["XINPUT1_3.dll"]["size"] == len(b"foreign proxy")
        assert install["mods"] == [{"name": "room.zip", "status": "available", "size": mod.stat().st_size,
                                    "sha256": hashlib.sha256(mod.read_bytes()).hexdigest()}]
        summary = archive.read("summary.txt").decode()
        assert "FAIL Core installed and compatible" in summary
        assert "FAIL Item delivery: blocked=yes, pending=3, processing=1, .failed=2" in summary
        assert "core_runtime | Update refused" in summary
        assert "Room seed: test-room | Slot: 2" in summary and "Core 1.0.0" in summary
        assert "last 256 KiB" in summary and "last 64 KiB" in summary
        assert len(archive.read("logs/qconsole.log")) <= 256 * 1024
        assert len(archive.read("install/injector.log")) <= 64 * 1024
        assert json.loads(archive.read("native_startup.json"))["status"] == "available"
        assert "darklord/71-123.darklord.log" in archive.namelist()
        for suffix in (".jsonl.previous", ".jsonl", ".latest.json"):
            assert b"sentinel-startup-v1" in archive.read("sentinel/71-123" + suffix)
        for name in archive.namelist():
            content = archive.read(name)
            assert b"-canary" not in content, name
            if name.endswith(".json"):
                def check_keys(value):
                    if isinstance(value, dict):
                        assert all(not key.casefold().endswith("private") for key in value)
                        for child in value.values():
                            check_keys(child)
                    elif isinstance(value, list):
                        for child in value:
                            check_keys(child)
                check_keys(json.loads(content))
    preview = reporting.report_problem(SimpleNamespace(create_support_bundle=lambda *a, **kw: result),
                                       logs=[], draft_path=tmp_path / "draft.json")
    assert "Core 1.0.0" in preview.payload["diagnostics"]
    assert not preview.submitted
    assert json.loads((tmp_path / "draft.json").read_text())["submitted"] is False


def test_bundle_budget_lists_omissions_and_truncations(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(doctor, "_support_log_tails", lambda *a, **kw:
                        ({f"log-{index}.log": "x" * (1024 * 1024) for index in range(12)}, {}))
    result = doctor.write_support_bundle(tmp_path / "report.zip", doctor.DoctorReport("test", ()))
    with zipfile.ZipFile(result) as archive:
        assert sum(item.file_size for item in archive.infolist()) < doctor.SUPPORT_BUNDLE_MAX_BYTES
        assert result.stat().st_size < doctor.SUPPORT_BUNDLE_MAX_BYTES
        summary = archive.read("summary.txt").decode()
        assert "truncated" in summary and "omitted" in summary and "10 MiB bundle budget" in summary
        assert "summary.txt" in summary and "doctor.json" in summary and "install.json" in summary


def test_issue_url_prefills_summary_or_copies_full_report_under_budget():
    payload = {"title": "Installation failure", "description": "Update refused", "diagnostics": "Core 1.0.0"}
    url, clipboard = reporting.github_issue_url(payload)
    params = parse_qs(urlsplit(url).query)
    assert len(url.encode()) < 2000 and clipboard is None
    assert params["template"] == ["problem.yml"]
    assert params["title"] == [payload["title"]]
    assert "Update refused" in params["summary"][0]
    payload.update(title="🔥" * 500, diagnostics="Unicode report 🔥\n" * 200 + "password=password-canary")
    url, clipboard = reporting.github_issue_url(payload)
    assert len(url.encode()) < 2000
    assert "summary" not in parse_qs(urlsplit(url).query)
    assert clipboard == reporting.report_text(payload)
    assert "password-canary" not in clipboard
