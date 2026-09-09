"""File adapters for save candidates and the native gameplay handshake."""

from pathlib import Path
import re
import os
import shutil
import subprocess

from doom_eap.runtime.save_decrypt import decrypt, steam_id64

from doom_eap.contracts.runtime_context import canonical_map_name
from doom_eap.contracts.save_observation import GameplaySaveEvidence, PrimarySaveSelection


def primary_save_candidates(remote_directory, steam_id, filename="game_duration.dat", slot_prefix=None):
    """Enumerate candidates, without asserting which slot is active."""
    if remote_directory is None or steam_id <= 0 or not remote_directory.is_dir():
        return []
    candidates = []
    glob_pattern = f"{slot_prefix}*/{filename}" if slot_prefix else f"*-AUTOSAVE*/{filename}"
    for path in remote_directory.glob(glob_pattern):
        if not re.fullmatch(r"(?:GAME|DLC[12]|HORDE)-AUTOSAVE\d+", path.parent.name):
            continue
        if slot_prefix and not path.parent.name.startswith(slot_prefix):
            continue
        try:
            stat = path.stat()
            full_path = path.resolve()
        except OSError:
            continue
        if not path.is_file() or stat.st_size <= 0:
            continue
        candidates.append(PrimarySaveSelection(path.parent.name, full_path, stat.st_mtime_ns))

    def _slot_sort_key(selected):
        digits = re.search(r"\d+$", selected.slot_directory)
        slot_num = int(digits.group(0)) if digits else 0
        return (selected.mtime_ns, slot_num)

    return sorted(candidates, key=_slot_sort_key, reverse=True)


def read_gameplay_save_evidence(path):
    """Read native evidence; active-slot proof belongs to the observer."""
    path = Path(path)
    try:
        values = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        state = values.get("state", "")
        epoch = int(values.get("epoch", "-1"))
        slot_directory = values.get("slot", "")
        map_name = canonical_map_name(values.get("map_name", "")) or ""
    except (OSError, UnicodeError, ValueError):
        return None
    if state == "menu":
        return GameplaySaveEvidence(state, epoch, "", "")
    if (
        state != "gameplay"
        or epoch < 0
        or not re.fullmatch(r"(?:GAME|DLC[12]|HORDE)-AUTOSAVE\d+", slot_directory)
    ):
        return None
    return GameplaySaveEvidence(
        state, epoch, slot_directory, map_name,
        values.get("provisional", "false").lower() == "true",
        values.get("native_safe", "false").lower() == "true",
    )


def unpack_game_duration(path, *, steam_id, runtime_directory, probe_path, oodle_path,
                         compat_data, proton_path, steam_install, host_exec):
    """Return checkpoint-death and native unlockable records from one save."""
    runtime_directory.mkdir(parents=True, exist_ok=True)
    runtime_probe = runtime_directory / probe_path.name
    runtime_oodle = runtime_directory / oodle_path.name
    if not runtime_probe.exists():
        shutil.copy2(probe_path, runtime_probe)
    if not runtime_oodle.exists():
        shutil.copy2(oodle_path, runtime_oodle)

    encrypted = path.read_bytes()
    aad = f"{steam_id64(steam_id)}MANCUBUS{path.name}"
    runtime_save = runtime_directory / "game_duration.dat"
    runtime_save.write_bytes(decrypt(encrypted, aad))

    runtime_unpacked = runtime_directory / "game_duration.full.bin"
    if os.name == "nt":
        command = [
            str(runtime_probe), runtime_oodle.name, runtime_save.name,
            runtime_unpacked.name,
        ]
        environment = None
    else:
        compat_data.mkdir(parents=True, exist_ok=True)
        proton_command = [
            str(proton_path),
            "run",
            runtime_probe.name,
            runtime_oodle.name,
            runtime_save.name,
            runtime_unpacked.name,
        ]
        if host_exec:
            command = [
                host_exec,
                "env",
                f"STEAM_COMPAT_DATA_PATH={compat_data}",
                f"STEAM_COMPAT_CLIENT_INSTALL_PATH={steam_install}",
                *proton_command,
            ]
            environment = None
        else:
            command = proton_command
            environment = os.environ.copy()
            environment["STEAM_COMPAT_DATA_PATH"] = str(compat_data)
            environment["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(steam_install)

    result = subprocess.run(
        command,
        cwd=runtime_directory,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode in {0, 20}:
        return runtime_unpacked.read_bytes(), result.returncode, result.stdout

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    raise RuntimeError(
        "save_death_probe exited with code "
        f"{result.returncode}; stdout={stdout!r}; stderr={stderr!r}"
    )



def read_game_details_for_selection(selected, steam_id, logger):
    if selected is None:
        return None
    path = selected.path.parent / "game.details"
    if not path.is_file():
        return None

    aad = f"{steam_id64(steam_id)}MANCUBUS{path.name}"
    try:
        plaintext = decrypt(path.read_bytes(), aad).decode("utf-8")
        values = {}
        for line in plaintext.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        values["_path"] = str(path)
        values["_mtime_ns"] = path.stat().st_mtime_ns
        if "mapName" in values:
            values["mapName"] = canonical_map_name(values["mapName"])
        return values
    except Exception as error:
        logger.error(f"[Save] Failed to decrypt {path}: {error}")
        return None


