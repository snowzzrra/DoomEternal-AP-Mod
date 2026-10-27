"""Collect bounded native diagnostics for one qualified process identity."""
import json
import os
from pathlib import Path


def diagnostics_directory(config):
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        return Path(local) / "SentinelCore/diagnostics" if local else None
    selected = config.get("game_root") or config.get("doom_base_dir")
    prefix = config.get("proton_compat_data_dir")
    if not prefix:
        if not selected:
            return None
        game = Path(selected).expanduser().resolve()
        if game.name.casefold() == "base":
            game = game.parent
        prefix = game.parent.parent / "compatdata/782330"
    return Path(prefix).expanduser() / "pfx/drive_c/users/steamuser/AppData/Local/SentinelCore/diagnostics"


def collect(config, session):
    pid, created = session.get("pid"), str(session.get("process_created", ""))
    if type(pid) is not int or pid <= 0 or not created.isdecimal():
        return {"status": "unavailable", "reason": "process_identity_unknown"}
    root = diagnostics_directory(config)
    if root is None:
        return {"status": "unavailable", "reason": "native_log_location_unavailable"}
    records = []
    issues = []
    files = []
    for suffix in (".jsonl.previous", ".jsonl", ".latest.json"):
        path = root / f"{pid}-{created}{suffix}"
        try:
            with path.open("rb") as stream:
                size = stream.seek(0, 2)
                start = max(0, size - 1024 * 1024)
                stream.seek(start)
                if start:
                    stream.readline(131073)
                raw = stream.read(1024 * 1024)
            retained = []
            skipped = 0
            for line in raw.splitlines():
                if len(line) > 131072:
                    skipped += 1
                    continue
                record = json.loads(line)
                if (not isinstance(record, dict) or record.get("schema") != "sentinel-startup-v1"
                        or record.get("pid") != pid or record.get("process_created") != created):
                    continue
                native_session = record.get("session")
                record["correlation"] = {"build_matches": record.get("build_id") == session.get("build_id"),
                                         "namespace_matches": isinstance(native_session, dict) and native_session.get("namespace_id") == session.get("namespace_id")}
                retained.append(record)
            files.append({"source_filename": path.name, "source_size": size,
                          "truncated": bool(start or skipped or len(retained) > 64),
                          "records": retained[-64:]})
            records.extend(retained[-64:])
        except FileNotFoundError:
            continue
        except (OSError, ValueError, UnicodeError) as error:
            issues.append({"file": path.name, "error": type(error).__name__})
    return {"status": "collected" if records else "unavailable", "records": records[-64:],
            "files": files, "issues": issues}
