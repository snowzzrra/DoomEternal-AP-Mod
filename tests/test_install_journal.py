import hashlib
import json
from pathlib import Path

import pytest

from doom_eap.launcher import install_journal as journal_module
from doom_eap.launcher.install_journal import InstallationRecoveryRequired, InstallJournal, InstallPhase


@pytest.mark.parametrize("point", ["intent", "effect", "recorded"])
@pytest.mark.parametrize("original", [None, b"previous"])
def test_restart_restores_proven_file_changes(tmp_path, point, original):
    game = tmp_path / "game"
    game.mkdir()
    target = game / "room.zip"
    if original is not None:
        target.write_bytes(original)
    journal = InstallJournal(game)
    journal.begin({"manifest_hash": "room"})
    journal.expect(target, hashlib.sha256(b"replacement").hexdigest())
    if point != "intent":
        target.write_bytes(b"replacement")
    if point == "recorded":
        journal.record_publication(target)
    restarted = InstallJournal(game)
    assert restarted.load()
    restarted.rollback()
    assert restarted.phase is InstallPhase.ROLLED_BACK
    assert (target.read_bytes() if target.exists() else None) == original
    restarted.rollback()
    assert restarted.phase is InstallPhase.ROLLED_BACK


@pytest.mark.parametrize("damage", ["target", "backup"])
def test_recovery_preserves_external_changes_and_damaged_backups(tmp_path, damage):
    target = tmp_path / "room.zip"
    target.write_bytes(b"previous")
    journal = InstallJournal(tmp_path)
    journal.begin({})
    journal.replace(target, b"replacement", operation="test_publish")
    entry = json.loads(journal.path.read_text())["files"][0]
    changed = (
        target if damage == "target" else tmp_path / InstallJournal.DIRECTORY / journal.transaction_id / entry["key"]
    )
    changed.write_bytes(b"foreign")
    restarted = InstallJournal(tmp_path)
    restarted.load()
    with pytest.raises(InstallationRecoveryRequired):
        restarted.rollback()
    assert changed.read_bytes() == b"foreign"
    assert target.read_bytes() == (b"foreign" if damage == "target" else b"replacement")
    assert restarted.phase is InstallPhase.RECOVERY_REQUIRED
    assert target.name in restarted.document["evidence"]["recovery_error"]


def test_external_config_is_restored_after_publication_failure(tmp_path, monkeypatch):
    game = tmp_path / "game"
    game.mkdir()
    config = tmp_path / "profile" / "ap_config.json"
    config.parent.mkdir()
    config.write_bytes(b"previous")
    journal = InstallJournal(game, (config,))
    journal.begin({})
    publish = journal_module.publish_file

    def fail_after_publish(source, target, *, operation):
        publish(source, target, operation=operation)
        raise OSError("interrupted publication")

    monkeypatch.setattr(journal_module, "publish_file", fail_after_publish)
    with pytest.raises(OSError):
        journal.replace(config, b"replacement", operation="test_publish")
    monkeypatch.setattr(journal_module, "publish_file", publish)
    restarted = InstallJournal(game, (config,))
    restarted.load()
    restarted.rollback()
    assert config.read_bytes() == b"previous"
    assert not list(config.parent.glob("*.incoming"))


def test_another_profile_restores_external_config_with_matching_local_intent(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    config = tmp_path / "ap_config.json"
    journal = InstallJournal(game, (config,))
    journal.begin({})
    journal.replace(config, b"replacement", operation="test_publish")
    restarted = InstallJournal(game)
    restarted.load()
    restarted.rollback()
    assert not config.exists()


def test_another_profile_preserves_external_file_without_matching_intent(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    config = tmp_path / "ap_config.json"
    journal = InstallJournal(game, (config,))
    journal.begin({})
    journal.replace(config, b"replacement", operation="test_publish")
    entry = journal.document["files"][0]
    from pathlib import Path

    Path(entry["backup"]).with_suffix(".json").write_text("{}")
    with pytest.raises(InstallationRecoveryRequired):
        InstallJournal(game).load()
    assert config.read_bytes() == b"replacement"


def test_invalid_journal_cannot_choose_a_backup_path(tmp_path):
    target = tmp_path / "room.zip"
    target.write_bytes(b"previous")
    journal = InstallJournal(tmp_path)
    journal.begin({})
    journal.replace(target, b"replacement", operation="test_publish")
    document = json.loads(journal.path.read_text())
    document["files"][0]["backup"] = str(tmp_path / "foreign")
    journal.path.write_text(json.dumps(document))
    with pytest.raises(InstallationRecoveryRequired):
        InstallJournal(tmp_path).load()
    assert target.read_bytes() == b"replacement"


def test_rollback_can_resume_after_restoring_one_file(tmp_path, monkeypatch):
    targets = [tmp_path / name for name in ("core.dll", "room.zip")]
    journal = InstallJournal(tmp_path)
    journal.begin({})
    for target in targets:
        target.write_bytes(b"previous")
        journal.replace(target, b"replacement", operation="test_publish")
    publish = journal_module.publish_file

    def interrupted(source, target, *, operation):
        publish(source, target, operation=operation)
        raise SystemExit("rollback interrupted")

    monkeypatch.setattr(journal_module, "publish_file", interrupted)
    with pytest.raises(SystemExit):
        journal.rollback()
    monkeypatch.setattr(journal_module, "publish_file", publish)
    restarted = InstallJournal(tmp_path)
    restarted.load()
    restarted.rollback()
    assert all(target.read_bytes() == b"previous" for target in targets)
    assert restarted.phase is InstallPhase.ROLLED_BACK


def test_support_summary_excludes_configuration_and_preimages(tmp_path):
    from doom_eap.launcher.launcher_doctor import installation_diagnostics

    journal = InstallJournal(tmp_path)
    journal.begin({"configuration": {"password": "secret"}})
    target = tmp_path / "ap_config.json"
    target.write_bytes(b"private configuration")
    journal.replace(target, b"replacement", operation="test_publish")
    journal.transition(InstallPhase.PREPARED)
    summary = installation_diagnostics({"doom_base_dir": str(tmp_path)})
    assert summary["transaction_id"] == journal.transaction_id
    assert "secret" not in json.dumps(summary)
    assert "private configuration" not in json.dumps(summary)
    assert "backup" not in summary


def test_backup_capture_preserves_an_existing_unowned_file(tmp_path):
    import hashlib

    target = tmp_path / "room.zip"
    target.write_bytes(b"previous")
    journal = InstallJournal(tmp_path)
    journal.begin({})
    backup = journal.directory / journal.transaction_id / hashlib.sha256(str(target.resolve()).encode()).hexdigest()
    backup.parent.mkdir(parents=True)
    backup.write_bytes(b"foreign")
    with pytest.raises(FileExistsError):
        journal.replace(target, b"replacement", operation="test_publish")
    assert target.read_bytes() == b"previous"
    assert backup.read_bytes() == b"foreign"


def test_pending_game_file_backups_require_inspection_without_restoring_them(tmp_path):
    journal = InstallJournal(tmp_path)
    journal.begin({})
    target = tmp_path / 'native.resources'
    target.write_bytes(b'game data')
    journal.document['evidence']['injector_files'] = [str(target)]
    journal.save()
    with pytest.raises(InstallationRecoveryRequired):
        InstallJournal(tmp_path).load()
    assert target.read_bytes() == b'game data'


@pytest.mark.parametrize("phase", [InstallPhase.COMPLETE, InstallPhase.ROLLED_BACK])
@pytest.mark.parametrize("original", [None, b"previous"])
def test_external_cleanup_resumes_after_marker_removal(tmp_path, monkeypatch, phase, original):
    game = tmp_path / "game"
    game.mkdir()
    config = tmp_path / "config.json"
    if original is not None:
        config.write_bytes(original)
    journal = InstallJournal(game, (config,))
    journal.begin({})
    journal.replace(config, b"replacement", operation="test_publish")
    if phase is InstallPhase.ROLLED_BACK:
        journal.rollback()
    else:
        journal.transition(phase)
    entry = journal.document["files"][0]
    marker = Path(entry["backup"]).with_suffix(".json")
    unlink = Path.unlink

    def interrupted(path, *args, **kwargs):
        unlink(path, *args, **kwargs)
        if path == marker:
            raise SystemExit("cleanup interrupted")

    monkeypatch.setattr(Path, "unlink", interrupted)
    with pytest.raises(SystemExit):
        journal.cleanup()
    monkeypatch.setattr(Path, "unlink", unlink)
    restarted = InstallJournal(game)
    assert restarted.load()
    restarted.cleanup()
    assert restarted.document["cleanup_complete"] is True
    assert not marker.parent.exists()
    expected = original if phase is InstallPhase.ROLLED_BACK else b"replacement"
    assert (config.read_bytes() if config.exists() else None) == expected


def test_cleanup_requires_external_intent_without_persisted_progress(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    config = tmp_path / "config.json"
    config.write_bytes(b"previous")
    journal = InstallJournal(game, (config,))
    journal.begin({})
    journal.replace(config, b"replacement", operation="test_publish")
    journal.transition(InstallPhase.COMPLETE)
    entry = journal.document["files"][0]
    backup = Path(entry["backup"])
    backup.with_suffix(".json").unlink()
    restarted = InstallJournal(game, (config,))
    assert restarted.load()
    with pytest.raises(OSError):
        restarted.cleanup()
    assert backup.read_bytes() == b"previous"
    assert config.read_bytes() == b"replacement"
