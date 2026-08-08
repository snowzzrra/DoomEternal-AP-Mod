"""Headless Join/Create launcher adapter."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from launcher_core import LaunchWorkflow


def main() -> None:
    parser = argparse.ArgumentParser(description="DOOM Eternal Archipelago seed compiler")
    command = parser.add_subparsers(dest="command", required=True)
    join = command.add_parser("join", help="compile room-authoritative seed options")
    join.add_argument("room_json", type=Path, help="Room/slot data exported by Archipelago")
    join.add_argument("--client-dir", type=Path, required=True)
    join.add_argument("--endpoint", required=True)
    create = command.add_parser("create", help="write starter options without compiling a room")
    create.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "create":
        args.output.write_text("game: DOOM Eternal\nDOOM Eternal:\n  randomize_dash: false\n", encoding="utf-8")
        return
    room = json.loads(args.room_json.read_text(encoding="utf-8"))
    manifest = LaunchWorkflow().join(room, args.client_dir, args.endpoint)
    print(manifest.manifest_hash)


if __name__ == "__main__":
    main()
