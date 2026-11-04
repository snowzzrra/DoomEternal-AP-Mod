"""Recoverable Core pair replacement, serialized with session preparation."""

import hashlib
import json
import os
import secrets
import threading
from contextlib import contextmanager
from pathlib import Path

from doom_eap.launcher.ui.strings import text as player_text

NAMES = ("sentinel_core.dll", "msimg32.dll", "sentinel-distribution.json")
_held_locks = threading.local()


@contextmanager
def game_write_lock(root, *, blocking=True):
    key = os.path.normcase(str(Path(root).resolve()))
    held = getattr(_held_locks, "roots", None)
    if held is None:
        held = _held_locks.roots = set()
    if key in held:
        yield
        return
    with _game_write_lock(root, blocking=blocking):
        held.add(key)
        try:
            yield
        finally:
            held.remove(key)


@contextmanager
def _game_write_lock(root, *, blocking=True):
    from .launcher_platform import _acquire_windows_mutex, _release_windows_mutex
    key = hashlib.sha256(os.path.normcase(str(Path(root).resolve())).encode()).hexdigest()
    if os.name == "nt":
        name = "Local\\DoomEAP.GameWrite." + key
        handle, _ = _acquire_windows_mutex(name) if blocking else _acquire_windows_mutex(name, timeout_ms=0)
        try:
            yield
        finally:
            _release_windows_mutex(handle)
    else:
        import fcntl
        with (Path(root) / ".sentinel-game-write.lock").open("ab") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


def backup_root(root, state_dir):
    key = hashlib.sha256(os.path.normcase(str(root.resolve())).encode()).hexdigest()
    return (Path(state_dir) if state_dir else root / ".sentinel") / "core-backups" / key


def _read(path):
    return path.read_bytes() if path.exists() else None


def restore(root, backup):
    from .launcher_platform import _atomic_write_bytes
    record = json.loads((backup / "record.json").read_text())
    if record["game_root"] != str(root.resolve()):
        raise RuntimeError(player_text('launcher.core_install', 'these_recovery_files_belong_to_another_game_folder_select_help_s'))
    for name in NAMES:
        current = _read(root / name)
        accepted = (record["original"][name], record["incoming"][name])
        digest = hashlib.sha256(current).hexdigest() if current is not None else None
        if digest not in accepted:
            raise RuntimeError(player_text('launcher.core_install', 'recovery_stopped_because_a_file_could_not_be_identified_existing'))
        original = _read(backup / "original" / name)
        if (hashlib.sha256(original).hexdigest() if original is not None else None) != record["original"][name]:
            raise ValueError(player_text('launcher.core_install', 'a_backup_file_is_damaged_close_doom_eternal_and_select_help_save'))
    for name in NAMES:
        original = _read(backup / "original" / name)
        if original is None:
            (root / name).unlink(missing_ok=True)
        else:
            _atomic_write_bytes(root / name, original)


def recover_interrupted(root, state_dir):
    directory = backup_root(root, state_dir)
    active = directory / "active.json"
    if active.exists():
        record = json.loads(active.read_text())
        token = record["backup"]
        if len(token) != 32 or any(char not in "0123456789abcdef" for char in token):
            raise ValueError(player_text('launcher.core_install', 'recovery_details_could_not_be_read_close_doom_eternal_and_select'))
        restore(root, directory / token)
        active.unlink()
        (root / "sentinel-core-update.txt").unlink(missing_ok=True)
    elif (root / "sentinel-core-update.txt").exists():
        marker = root / "sentinel-core-update.txt"
        token = json.loads((directory / "previous.json").read_text())["backup"]
        if len(token) != 32 or any(char not in "0123456789abcdef" for char in token):
            raise ValueError(player_text('launcher.core_install', 'recovery_details_could_not_be_read_close_doom_eternal_and_select'))
        record = json.loads((directory / token / "record.json").read_text())
        if record["game_root"] != str(root.resolve()) or marker.read_bytes() != b"sentinel-update-v1\n":
            raise RuntimeError(player_text('launcher.core_install', 'update_details_could_not_be_read_and_were_kept_close_doom_eterna'))
        for name in NAMES:
            data = _read(root / name)
            if data is None or hashlib.sha256(data).hexdigest() != record["incoming"][name]:
                raise RuntimeError(player_text('launcher.core_install', 'updated_files_do_not_match_the_saved_recovery_details_close_doom'))
        marker.unlink()


def replace_pair(root, state_dir, incoming, *, journal=None):
    from .launcher_platform import _atomic_write_bytes, detect_doom_processes, probe_meathook, require_doom_closed
    require_doom_closed(player_text('launcher.core_install', 'replacing_core'))
    directory = backup_root(root, state_dir)
    backup = directory / secrets.token_hex(16)
    backup.mkdir(parents=True)
    original = {name: _read(root / name) for name in NAMES}
    record = {"game_root": str(root.resolve()), "original": {}, "incoming": {}}
    for name in NAMES:
        for label, data in (("original", original[name]), ("incoming", incoming[name])):
            record[label][name] = hashlib.sha256(data).hexdigest() if data is not None else None
            if data is not None:
                target = backup / label / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
    (backup / "record.json").write_text(json.dumps(record))
    pointer = _pair_intents(root, directory, backup, journal)
    _atomic_write_bytes(directory / "active.json", pointer)
    _atomic_write_bytes(root / "sentinel-core-update.txt", b"sentinel-update-v1\n")
    try:
        for name in NAMES:
            if detect_doom_processes():
                raise RuntimeError(player_text('launcher.core_install', 'doom_eternal_started_while_connection_files_were_being_replaced'))
            _atomic_write_bytes(root / name, incoming[name])
        if not probe_meathook(root).ok:
            raise RuntimeError(player_text('launcher.core_install', 'game_connection_files_could_not_be_checked_after_installation_cl'))
    except Exception:
        if not detect_doom_processes():
            restore(root, backup)
            (directory / "active.json").unlink()
            (root / "sentinel-core-update.txt").unlink()
        raise
    (directory / "active.json").replace(directory / "previous.json")
    (root / "sentinel-core-update.txt").unlink()
    return backup


def _pair_intents(root, directory, backup, journal):
    pointer = json.dumps({"backup": backup.name}).encode()
    if journal is not None:
        paths = (directory / "active.json", directory / "previous.json")
        journal.include_paths(paths)
        for path in paths:
            journal.expect(path, hashlib.sha256(pointer).hexdigest())
        journal.expect(paths[0], None)
        marker = root / "sentinel-core-update.txt"
        journal.expect(marker, hashlib.sha256(b"sentinel-update-v1\n").hexdigest())
        journal.expect(marker, None)
    return pointer


def rollback_core(root, state_dir):
    from .launcher_platform import detect_doom_processes
    with game_write_lock(root):
        if detect_doom_processes():
            raise RuntimeError(player_text('launcher.core_install', 'close_doom_eternal_before_restoring_the_previous_game_connection'))
        recover_interrupted(root, state_dir)
        directory = backup_root(root, state_dir)
        token = json.loads((directory / "previous.json").read_text())["backup"]
        if len(token) != 32 or any(char not in "0123456789abcdef" for char in token):
            raise ValueError(player_text('launcher.core_install', 'backup_details_could_not_be_read_close_doom_eternal_and_select_h'))
        target = directory / token
        if not all((target / "original" / name).is_file() for name in NAMES):
            raise RuntimeError(player_text('launcher.core_install', 'no_complete_previous_version_is_available_select_help_save_suppo'))
        replace_pair(root, state_dir, {name: (target / "original" / name).read_bytes() for name in NAMES})
        (directory / "previous.json").unlink()
        return target
