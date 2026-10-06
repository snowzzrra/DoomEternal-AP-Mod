import json
from types import SimpleNamespace
import urllib.error

import pytest

from doom_eap.contracts.core_distribution import verify_runtime
from doom_eap.launcher.launcher_core_runtime import CoreRuntime, RELEASES_URL
from doom_eap.launcher.launcher_session import archive_prelaunch
from tests.test_core_distribution import fixture


def test_official_selection_pagination_integrity_and_offline_reuse(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    manifest = fixture(source)
    manifest.update(version="1.0.12", base_version="1.0.12", channel="stable", rc_number=0,
                    mod_version_range=">=0.6.0,<0.7.0", minimum_launcher_version="0.6.1",
                    abi={**manifest["abi"], "deathlink": 2, "optional_future_interface": 99})
    base = "https://github.com/snowzzrra/Sentinel-Core/releases/download/v1.0.12/"
    release = {"draft": False, "prerelease": False, "assets": [
        {"name": name, "browser_download_url": base + name} for name in ("distribution.json", "sentinel-runtime.zip")]}
    second = RELEASES_URL + "&page=2"
    documents = {RELEASES_URL: [{**release, "prerelease": True}], second: [release], base + "distribution.json": manifest}

    class Transport:
        def fetch(self, url, path, *, headers=None):
            assert headers is None or headers["Accept"] == "application/vnd.github+json"
            if url.endswith("sentinel-runtime.zip"):
                path.write_bytes((source / "sentinel-runtime.zip").read_bytes())
            else:
                path.write_text(json.dumps(documents[url]))
            return SimpleNamespace(headers={"Link": f'<{second}>; rel="next"'} if url == RELEASES_URL else {})

    monkeypatch.setattr("doom_eap.launcher.launcher_core_runtime.probe_meathook", lambda root: SimpleNamespace(ok=False))
    runtime = CoreRuntime(tmp_path / "cache", transport=Transport())
    selected = runtime.resolve({}, tmp_path / "game")
    assert verify_runtime(selected)[0]["version"] == "1.0.12"
    (source / "sentinel-runtime.zip").unlink()
    assert runtime.resolve({}, tmp_path / "game") == selected
    offline = CoreRuntime(tmp_path / "cache", transport=SimpleNamespace(fetch=lambda *a, **kw: pytest.fail("network required for verified cache")))
    assert offline.resolve({}, tmp_path / "game") == selected
    (selected.parent / "sentinel_core.dll").write_bytes(b"damaged")
    with pytest.raises(RuntimeError, match="Repair Core"):
        runtime.resolve({}, tmp_path / "game")


def test_metadata_conditional_request_and_rate_limit_preserve_cache(tmp_path):
    calls = []

    class Transport:
        def fetch(self, url, path, *, headers=None):
            calls.append(headers)
            if len(calls) == 2:
                raise urllib.error.HTTPError(url, 304, "unchanged", {}, None)
            if len(calls) == 3:
                raise urllib.error.HTTPError(url, 429, "limited", {"Retry-After": "120"}, None)
            path.write_text("[]")
            return SimpleNamespace(headers={"ETag": '"verified"'})

    runtime = CoreRuntime(tmp_path, transport=Transport())
    runtime.check()
    runtime.check()
    assert calls[1]["If-None-Match"] == '"verified"'
    runtime.check()
    runtime.check()
    assert len(calls) == 3
    assert list(tmp_path.glob("*.json"))


def test_selected_runtime_is_published_without_stale_override(tmp_path):
    from doom_eap.launcher.launcher_core import LaunchWorkflow
    path = LaunchWorkflow.write_client_config(tmp_path, runtime_config={"core_runtime_manifest": "old"})
    LaunchWorkflow.write_client_config(tmp_path, runtime_config={"selected_core_runtime_manifest": "selected"})
    published = json.loads(path.read_text())
    assert published["selected_core_runtime_manifest"] == "selected"
    assert "core_runtime_manifest" not in published


def test_doctor_offers_core_install_for_configured_base_directory(tmp_path):
    from doom_eap.launcher.launcher_doctor import LauncherDoctor
    (tmp_path / "base").mkdir()
    (tmp_path / "DOOMEternalx64vk.exe").write_bytes(b"MZ")
    actions = LauncherDoctor(config={"doom_base_dir": str(tmp_path / "base")}).repair_actions()
    assert "install_game_link" in {action.action_id for action in actions}


def test_interrupted_pair_replacement_restores_original_bytes(tmp_path, monkeypatch):
    from doom_eap.launcher import launcher_platform
    from doom_eap.launcher.launcher_core_install import NAMES, replace_pair, recover_interrupted
    root = tmp_path / "game"
    root.mkdir()
    original = {name: ("old " + name).encode() for name in NAMES}
    incoming = {name: ("new " + name).encode() for name in NAMES}
    for name, data in original.items():
        (root / name).write_bytes(data)
    write = launcher_platform._atomic_write_bytes
    def interrupt(path, data):
        if path == root / "msimg32.dll":
            raise SystemExit("interrupted")
        write(path, data)
    monkeypatch.setattr(launcher_platform, "detect_doom_processes", lambda: [])
    monkeypatch.setattr(launcher_platform, "_atomic_write_bytes", interrupt)
    with pytest.raises(SystemExit):
        replace_pair(root, tmp_path / "state", incoming)
    assert (root / "sentinel_core.dll").read_bytes() == incoming["sentinel_core.dll"]
    monkeypatch.setattr(launcher_platform, "_atomic_write_bytes", write)
    recover_interrupted(root, tmp_path / "state")
    assert {name: (root / name).read_bytes() for name in NAMES} == original
    assert not (root / "sentinel-core-update.txt").exists()


def test_prelaunch_stale_owner_recovery_preserves_bytes_and_live_owner(tmp_path, monkeypatch):
    marker = tmp_path / "sentinel-prelaunch.txt"
    raw = ("sentinel-run-v1\nrun=" + "a" * 32 + "\nprotection=" + "b" * 64 +
           "\nowner=123\ncreated=456\nsentinel-test-session-v2\nroom=same\n").encode()
    marker.write_bytes(raw)
    monkeypatch.setattr("doom_eap.launcher.launcher_session.prelaunch_owner_state", lambda *args: "alive")
    with pytest.raises(RuntimeError, match="live launcher"):
        archive_prelaunch(marker, tmp_path)
    assert marker.read_bytes() == raw
    monkeypatch.setattr("doom_eap.launcher.launcher_session.prelaunch_owner_state", lambda *args: "unknown")
    with pytest.raises(RuntimeError, match="unknown"):
        archive_prelaunch(marker, tmp_path, explicit=True)
    monkeypatch.setattr("doom_eap.launcher.launcher_session.prelaunch_owner_state", lambda *args: "ended")
    archived = archive_prelaunch(marker, tmp_path)
    assert archived.read_bytes() == raw and not marker.exists()
