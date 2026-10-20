import hashlib
import json
from types import SimpleNamespace

import pytest

from doom_eap.launcher.campaign_resources import installed_resource_fingerprints, verify_installed_resources
from doom_eap.launcher.launcher_core import RoomCompiler
from doom_eap.launcher.launcher_doctor import LauncherDoctor
from doom_eap.launcher.launcher_integration import IntegratedLaunchWorkflow


@pytest.fixture
def installed(tmp_path, monkeypatch):
    game = tmp_path / 'game'
    (game / 'base').mkdir(parents=True)
    (game / 'Mods').mkdir()
    for name in ('DOOMEternalx64vk.exe', 'base/meta.resources', 'base/idRehash.map', 'base/packagemapspec.json', 'base/build-manifest.bin'):
        (game / name).write_bytes(b'injected ' + name.encode())
    package = game / 'Mods/room.zip'
    package.write_bytes(b'room package')
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    receipt = dict(staged_mod=str(package), staged_sha256=digest, manifest_hash='room',
                   adapter_state='applied', generated_mod=str(package), generated_sha256=digest,
                   room_zip_sha256=digest, installed_resources=installed_resource_fingerprints(game))
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'launcher_setup.json').write_text(json.dumps(receipt))
    workflow = IntegratedLaunchWorkflow.__new__(IntegratedLaunchWorkflow)
    workflow.state_dir = state
    workflow.platform_name = 'windows'
    workflow.application_dir = tmp_path
    workflow._manifest_for = lambda _: SimpleNamespace(manifest_hash='room', static_content_digest='')
    workflow._config = lambda: {}
    workflow._game_root = lambda _: game
    monkeypatch.setattr(RoomCompiler, 'validate_cached_package', lambda *args: True)
    monkeypatch.setattr('doom_eap.launcher.launcher_integration.probe_runtime_prerequisites',
                        lambda *args: SimpleNamespace(ok=True))
    return game, state, receipt, workflow


@pytest.mark.parametrize('name', ['DOOMEternalx64vk.exe', 'base/meta.resources', 'base/idRehash.map', 'base/packagemapspec.json', 'base/build-manifest.bin'])
def test_restored_game_file_requires_reinstall_even_with_valid_room_zip(installed, name):
    game, state, receipt, workflow = installed
    assert workflow.install_state(None).state == 'already_installed'
    (game / name).write_bytes(b'Steam restored original contents')
    with pytest.raises(ValueError, match='Installed game files changed'):
        verify_installed_resources(game, receipt)
    result = workflow.install_state(None)
    assert result.state == 'update_required' and name in result.reason
    doctor = LauncherDoctor(config={'game_root': str(game)}, paths=SimpleNamespace(state_dir=state))
    assert any(action.action_id == 'reinstall_room_mod' for action in doctor.repair_actions())
    assert json.loads((state / 'launcher_setup.json').read_bytes()) == receipt


def test_missing_install_evidence_requires_reinstall_and_preserves_explicit_uninstall(installed):
    game, state, receipt, workflow = installed
    del receipt['installed_resources']
    path = state / 'launcher_setup.json'
    path.write_text(json.dumps(receipt))
    assert workflow.install_state(None).state == 'update_required'
    receipt['adapter_state'] = 'uninstalled'
    path.write_text(json.dumps(receipt))
    assert workflow.install_state(None).state == 'uninstalled'
    doctor = LauncherDoctor(config={'game_root': str(game)}, paths=SimpleNamespace(state_dir=state))
    assert not any(action.action_id == 'reinstall_room_mod' for action in doctor.repair_actions())
