"""Headless Join/Create launcher adapter."""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from .launcher_core import LaunchWorkflow
from .launcher_supervisor import BridgeSupervisor


def main() -> None:
    parser = argparse.ArgumentParser(description="DOOM Eternal Archipelago seed compiler")
    command = parser.add_subparsers(dest="command", required=True)
    join = command.add_parser("simulate-join", help="run an offline room fixture")
    join.add_argument("room_json", type=Path, help="Room/slot data exported by Archipelago")
    join.add_argument("--client-dir", type=Path, required=True)
    join.add_argument("--endpoint", required=True)
    create = command.add_parser("create", help="write starter options without compiling a room")
    create.add_argument("--output", type=Path, required=True)
    run = command.add_parser("run", help="supervise bridge, consume RoomSnapshot, compile and install")
    run.add_argument("--client", type=Path, required=True)
    run.add_argument("--install-root", type=Path, required=True)
    run.add_argument("--endpoint", required=True)
    run.add_argument("--name", required=True)
    run.add_argument("--profile", required=True)
    run.add_argument("--vanilla-exultia", type=Path, required=True, help="legal local Exultia .entities source")
    args = parser.parse_args()
    if args.command == "create":
        args.output.write_text("game: DOOM Eternal\nDOOM Eternal:\n  randomize_dash: false\n", encoding="utf-8")
        return
    if args.command == "run":
        supervisor = BridgeSupervisor(
            client=args.client,
            workflow=LaunchWorkflow(vanilla_exultia=args.vanilla_exultia),
            install_root=args.install_root,
            profile_id=args.profile,
        )
        supervisor.start(
            endpoint=args.endpoint,
            player=args.name,
            password=os.environ.get("DOOM_AP_PASSWORD", ""),
        )
        try:
            while supervisor.state.value not in {"FAILED", "STOPPED"}:
                time.sleep(0.2)
        except KeyboardInterrupt:
            supervisor.stop()
        if supervisor.last_error:
            raise SystemExit(json.dumps(supervisor.last_error, sort_keys=True))
        return
    room = json.loads(args.room_json.read_text(encoding="utf-8"))
    manifest = LaunchWorkflow().join(room, args.client_dir, args.endpoint)
    print(manifest.manifest_hash)


if __name__ == "__main__":
    main()
