"""Recoverable Core pair replacement, serialized with session preparation."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets

NAMES = ("sentinel_core.dll", "msimg32.dll", "sentinel-distribution.json")


@contextmanager
def game_write_lock(root):
    from .launcher_platform import _acquire_windows_mutex, _release_windows_mutex
    key = hashlib.sha256(os.path.normcase(str(Path(root).resolve())).encode()).hexdigest()
    if os.name == "nt":
        handle, _ = _acquire_windows_mutex("Local\\DoomEAP.GameWrite." + key)
        try:
            yield
        finally:
            _release_windows_mutex(handle)
    else:
        import fcntl
        with (Path(root) / ".sentinel-game-write.lock").open("ab") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
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
        raise RuntimeError("Core recovery belongs to another installation")
    for name in NAMES:
        current = _read(root / name)
        accepted = (record["original"][name], record["incoming"][name])
        digest = hashlib.sha256(current).hexdigest() if current is not None else None
        if digest not in accepted:
            raise RuntimeError(f"Core recovery preserves an unrecognized file: {root / name}")
        original = _read(backup / "original" / name)
        if (hashlib.sha256(original).hexdigest() if original is not None else None) != record["original"][name]:
            raise ValueError(f"Core backup hash mismatch: {name}")
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
            raise ValueError("Invalid Core recovery record")
        restore(root, directory / token)
        active.unlink()
        (root / "sentinel-core-update.txt").unlink(missing_ok=True)
    elif (root / "sentinel-core-update.txt").exists():
        marker = root / "sentinel-core-update.txt"
        token = json.loads((directory / "previous.json").read_text())["backup"]
        if len(token) != 32 or any(char not in "0123456789abcdef" for char in token):
            raise ValueError("Invalid completed Core recovery record")
        record = json.loads((directory / token / "record.json").read_text())
        if record["game_root"] != str(root.resolve()) or marker.read_bytes() != b"sentinel-update-v1\n":
            raise RuntimeError("Unknown Core update marker was preserved")
        for name in NAMES:
            data = _read(root / name)
            if data is None or hashlib.sha256(data).hexdigest() != record["incoming"][name]:
                raise RuntimeError("Completed Core update differs from its recovery record")
        marker.unlink()


def replace_pair(root, state_dir, incoming):
    from .launcher_platform import _atomic_write_bytes, detect_doom_processes, probe_meathook
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
    _atomic_write_bytes(directory / "active.json", json.dumps({"backup": backup.name}).encode())
    _atomic_write_bytes(root / "sentinel-core-update.txt", b"sentinel-update-v1\n")
    try:
        for name in NAMES:
            if detect_doom_processes():
                raise RuntimeError("DOOM Eternal started during Core replacement; close it before recovery")
            _atomic_write_bytes(root / name, incoming[name])
        if not probe_meathook(root).ok:
            raise RuntimeError("Core pair verification failed after installation")
    except Exception:
        if not detect_doom_processes():
            restore(root, backup)
            (directory / "active.json").unlink()
            (root / "sentinel-core-update.txt").unlink()
        raise
    (directory / "active.json").replace(directory / "previous.json")
    (root / "sentinel-core-update.txt").unlink()
    return backup


def rollback_core(root, state_dir):
    from .launcher_platform import detect_doom_processes
    with game_write_lock(root):
        if detect_doom_processes():
            raise RuntimeError("Close DOOM Eternal before restoring Core")
        recover_interrupted(root, state_dir)
        directory = backup_root(root, state_dir)
        token = json.loads((directory / "previous.json").read_text())["backup"]
        if len(token) != 32 or any(char not in "0123456789abcdef" for char in token):
            raise ValueError("Invalid Core backup record")
        target = directory / token
        if not all((target / "original" / name).is_file() for name in NAMES):
            raise RuntimeError("A complete previous Core installation is unavailable")
        replace_pair(root, state_dir, {name: (target / "original" / name).read_bytes() for name in NAMES})
        (directory / "previous.json").unlink()
        return target
