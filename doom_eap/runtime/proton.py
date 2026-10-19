"""Run qualified Windows helpers in DOOM Eternal's selected Steam prefix."""
from pathlib import Path
import os
import json
import subprocess


class ProtonSelectionError(RuntimeError):
    failure_domain = "proton_runtime"

    def __init__(self, reason, *, candidates=(), selected=None):
        super().__init__("Select the Proton version used by DOOM Eternal in Steam. "
                         "Choose the file named proton, not wine, DOOM Eternal or the pfx folder.")
        self.proton_reason = reason
        self.proton_candidates = sorted(str(path) for path in candidates)[:12]
        self.proton_selected = str(selected) if selected else None


def windows_path(path) -> str:
    value = str(Path(path).expanduser().resolve())
    return "Z:" + value.replace("/", "\\") if value.startswith("/") else value


def runtime(configuration):
    root = Path(configuration.get("game_root") or configuration["doom_base_dir"]).resolve()
    if root.name.casefold() == "base":
        root = root.parent
    prefix = Path(configuration.get("proton_compat_data_dir") or root.parents[1] / "compatdata/782330").resolve()
    if prefix.name != "782330" or not (prefix / "pfx").is_dir():
        raise RuntimeError("The selected DOOM Eternal Steam compatibility prefix is unavailable")
    selected = configuration.get("proton_executable")
    if selected:
        proton = Path(selected).resolve()
    else:
        info = prefix / "config_info"
        if not info.is_file():
            raise ProtonSelectionError("missing_config_info")
        if info.stat().st_size > 65536:
            raise ProtonSelectionError("invalid_config_info")
        try:
            lines = info.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as error:
            raise ProtonSelectionError("unreadable_config_info") from error
        candidates = {Path(line) / "proton" for line in lines
                      if line.startswith("/") and (Path(line) / "proton").is_file()}
        if len(candidates) != 1:
            raise ProtonSelectionError("ambiguous" if candidates else "no_candidate", candidates=candidates)
        proton = candidates.pop().resolve()
    if proton.name != "proton" or not proton.is_file():
        raise ProtonSelectionError("invalid_selected_path", selected=proton)
    remote = Path(configuration["steam_remote_dir"]).resolve()
    if remote.name != "remote" or remote.parent.name != "782330" or remote.parents[2].name != "userdata":
        raise RuntimeError("Select DOOM Eternal's Steam userdata directory")
    environment = dict(os.environ, STEAM_COMPAT_DATA_PATH=str(prefix),
                       STEAM_COMPAT_CLIENT_INSTALL_PATH=str(remote.parents[3]), WINEPREFIX=str(prefix / "pfx"))
    return [str(proton), "run"], environment


def windows_processes(configuration, helper):
    """Return qualified guest identities, an empty tuple, or UNKNOWN (None)."""
    try:
        command, environment = runtime(configuration)
        response = subprocess.run([*command, windows_path(helper), "--process-identity"],
                                  env=environment, capture_output=True, timeout=8, check=False)
        rows = json.loads(response.stdout).get("processes")
        if response.returncode or not isinstance(rows, list):
            return None
        if any(not isinstance(row, dict) or type(row.get("pid")) is not int or row["pid"] <= 0
               or type(row.get("created")) is not int or row["created"] <= 0
               or not isinstance(row.get("path"), str) for row in rows):
            return None
        return tuple(rows)
    except (OSError, ValueError, RuntimeError, AttributeError, subprocess.SubprocessError):
        return None
