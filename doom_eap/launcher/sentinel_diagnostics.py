"""Collect bounded native diagnostics for one qualified process identity."""
import json
import os
from pathlib import Path


def collect(config, session):
    pid, created = session.get("pid"), str(session.get("process_created", ""))
    if type(pid) is not int or pid <= 0 or not created.isdecimal():
        return {"status": "unavailable", "reason": "process_identity_unknown"}
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", "")) / "SentinelCore/diagnostics"
    else:
        game = Path(config.get("game_root") or config["doom_base_dir"]).resolve()
        if game.name == "base":
            game = game.parent
        prefix = Path(config.get("proton_compat_data_dir") or game.parents[1] / "compatdata/782330")
        root = prefix / "pfx/drive_c/users/steamuser/AppData/Local/SentinelCore/diagnostics"
    records = []
    issues = []
    for suffix in (".jsonl.previous", ".jsonl", ".latest.json"):
        path = root / f"{pid}-{created}{suffix}"
        try:
            with path.open("rb") as stream:
                size = stream.seek(0, 2)
                start = max(0, size - 1024 * 1024)
                stream.seek(start)
                if start:
                    stream.readline(65537)
                raw = stream.read(1024 * 1024)
            for line in raw.splitlines():
                if len(line) > 65536:
                    continue
                record = json.loads(line)
                if (not isinstance(record, dict) or record.get("schema") != "sentinel-startup-v1"
                        or record.get("pid") != pid or record.get("process_created") != created):
                    continue
                native_session = record.get("session")
                record["correlation"] = {"build_matches": record.get("build_id") == session.get("build_id"),
                                         "namespace_matches": isinstance(native_session, dict) and native_session.get("namespace_id") == session.get("namespace_id")}
                records.append(record)
        except FileNotFoundError:
            continue
        except (OSError, ValueError, UnicodeError) as error:
            issues.append({"file": path.name, "error": type(error).__name__})
    return {"status": "collected" if records else "unavailable", "records": records[-64:], "issues": issues}
