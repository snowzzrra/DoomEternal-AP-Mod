"""Exact working-tree input evidence, separate from commit ancestry and compiler caches."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from tools.release.build_launcher import _source_inputs

MOD_ROOTS = ("doom_eap", "tools", "native", "scripts", "data", "content", "manifests",
             "level_configs", "player_templates", "packaging/mod_assets", "packaging/standalone_runtime",
             "packaging/client", "packaging/shell_menu_assetsinfo.json", "packaging/hub_world_text_assetsinfo.json",
             "requirements-launcher.txt", "requirements-ci.txt")


def snapshot_inputs(inputs) -> dict:
    records = {}
    for name, path in inputs:
        raw = path.read_bytes()
        records[name] = {"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}
    encoded = json.dumps(records, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"kind": "working_tree_snapshot", "byte_contract": "exact_file_bytes",
            "fingerprint": hashlib.sha256(encoded).hexdigest(), "files": records}


def capture_release_sources(mod_root: Path, apworld_root: Path) -> dict:
    mod_inputs = []
    for relative in MOD_ROOTS:
        path = mod_root / relative
        if path.exists():
            mod_inputs.extend(_source_inputs(relative, path))
    return {"mod": snapshot_inputs(mod_inputs), "apworld": snapshot_inputs(_source_inputs("archipelago", apworld_root))}


def source_origin(base_commit: str, snapshot: dict) -> dict:
    encoded = json.dumps(snapshot.get("files"), sort_keys=True, separators=(",", ":")).encode("utf-8")
    if (snapshot.get("kind") != "working_tree_snapshot" or snapshot.get("byte_contract") != "exact_file_bytes"
            or not snapshot.get("files") or snapshot.get("fingerprint") != hashlib.sha256(encoded).hexdigest()):
        raise ValueError("Invalid working-tree source evidence")
    return {"base_commit_sha": base_commit, "source_state": snapshot}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--archipelago-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    state = capture_release_sources(args.repo_root.resolve(), args.archipelago_source.resolve())
    if args.verify:
        if state != json.loads(args.output.read_text(encoding="utf-8")):
            raise ValueError("Working-tree inputs changed during the build")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print("SOURCE_PROVENANCE " + " ".join(f"{key}={value['fingerprint']}" for key, value in state.items()))


if __name__ == "__main__":
    main()
