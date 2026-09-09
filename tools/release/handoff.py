"""Create a deterministic, checksummed build-handoff manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


def write_handoff_manifest(
    root: Path, *, version: str, mod_sha: str, apworld_sha: str, platform: str = "both",
    mod_ref: str | None = None, apworld_ref: str | None = None,
    source_state: dict | None = None,
) -> Path:
    root = root.resolve()
    required = [
        root / "shared/doometernal.apworld",
        root / "shared/client/ap_client.exe",
        root / "shared/resources/base_mod.zip",
        root / "shared/resources/room_payloads.zip",
        root / "shared/resources/room_payload_manifest.json",
    ]
    if platform in {"windows", "both"}:
        required.append(root / "windows/DoomEternalArchipelagoLauncher.exe")
    if platform in {"linux", "both"}:
        required.append(root / "linux/DoomEternalArchipelagoLauncher")
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise ValueError(f"handoff inputs missing: {missing}")

    records: list[dict[str, object]] = []
    sums: list[str] = []
    for directory, _, filenames in os.walk(root):
        for filename in sorted(filenames):
            if filename in {"BUILD-MANIFEST.json", "SHA256SUMS.txt"}:
                continue
            path = Path(directory) / filename
            relative = path.relative_to(root).as_posix()
            digest = _sha256(path)
            role = "launcher" if "Launcher" in filename else (
                "apworld" if filename.endswith(".apworld") else
                "room-resource" if "resources/" in relative else "native-client"
            )
            records.append({"path": relative, "sha256": digest, "size": path.stat().st_size, "role": role})
            sums.append(f"{digest}  {relative}")
    records.sort(key=lambda record: str(record["path"]))
    manifest = {
        "schema_version": 1,
        "version_label": version,
        "mod": {"requested_ref": mod_ref or mod_sha, "resolved_sha": mod_sha},
        "apworld": {"requested_ref": apworld_ref or apworld_sha, "resolved_sha": apworld_sha},
        "build": {"platform": platform, "native_client_toolchain_identity": "MSVC x64 / MIDL"},
        "files": records,
    }
    path = root / "BUILD-MANIFEST.json"
    if source_state is not None:
        from tools.release.source_provenance import source_origin
        for project, ancestor in (("mod", mod_sha), ("apworld", apworld_sha)):
            manifest[project] = source_origin(ancestor, source_state[project])
        manifest["build"]["source_mode"] = "working_tree_snapshot"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    (root / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n", encoding="utf-8", newline="\n")
    for record in records:
        candidate = root / str(record["path"])
        if _sha256(candidate) != record["sha256"] or candidate.stat().st_size != record["size"]:
            raise RuntimeError(f"handoff verification failed: {candidate}")
    print(f"HANDOFF manifest={path} files={len(records)} platform={platform}")
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--mod-sha", required=True)
    parser.add_argument("--apworld-sha", required=True)
    parser.add_argument("--mod-ref")
    parser.add_argument("--apworld-ref")
    parser.add_argument("--source-state", type=Path)
    parser.add_argument("--platform", choices=("windows", "linux", "both"), default="both")
    args = parser.parse_args()
    write_handoff_manifest(args.root, version=args.version, mod_sha=args.mod_sha,
                           apworld_sha=args.apworld_sha, platform=args.platform,
                           mod_ref=args.mod_ref, apworld_ref=args.apworld_ref,
                           source_state=json.loads(args.source_state.read_text(encoding="utf-8")) if args.source_state else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
