"""Smoke-test build-time room payload selection against release resources."""

from __future__ import annotations

import argparse
from pathlib import Path

from tools.release.room_payloads import (
    BASE_RESOURCE_NAME,
    ROOM_PAYLOAD_MANIFEST_NAME,
    ROOM_PAYLOAD_RESOURCE_NAME,
    assemble_room_files,
    load_room_payload_manifest,
    read_zip,
)


def smoke_room_payload_assembly(root: Path) -> None:
    resources = root / "build/release/client/resources"
    manifest = load_room_payload_manifest(resources / ROOM_PAYLOAD_MANIFEST_NAME)
    base = read_zip(resources / BASE_RESOURCE_NAME)
    payloads = read_zip(resources / ROOM_PAYLOAD_RESOURCE_NAME)
    cases = (
        (
            "e1m1-off",
            "e1m1_intro",
            {
                "start_with_automap": False,
                "randomize_chainsaw": False,
                "randomize_dash": False,
                "randomize_first_battery": False,
            },
        ),
        (
            "e1m1-on",
            "e1m1_intro",
            {
                "start_with_automap": True,
                "randomize_chainsaw": False,
                "randomize_dash": False,
                "randomize_first_battery": False,
            },
        ),
        (
            "e1m1-on-chainsaw",
            "e1m1_intro",
            {
                "start_with_automap": True,
                "randomize_chainsaw": True,
                "randomize_dash": False,
                "randomize_first_battery": False,
            },
        ),
        (
            "e1m2-on-dash-battery",
            "e1m2_war",
            {
                "start_with_automap": True,
                "randomize_chainsaw": False,
                "randomize_dash": True,
                "randomize_first_battery": True,
            },
        ),
    )
    for name, map_key, options in cases:
        assembled, selected = assemble_room_files(
            resources / BASE_RESOURCE_NAME,
            resources / ROOM_PAYLOAD_RESOURCE_NAME,
            manifest,
            options,
        )
        record = manifest["maps"][map_key]
        expected_state = {key: options[key] for key in record["option_keys"]}
        state = selected[map_key]
        if state["options"] != expected_state:
            raise AssertionError(
                f"physical payload selection drifted: {name}/{state['options']}"
            )
        target = str(record["target_member"])
        if not isinstance(assembled[target], bytes):
            raise AssertionError(f"assembled payload is not bytes: {name}")
        if state["source"] == "base":
            if assembled[target] != base[target]:
                raise AssertionError(f"base payload bytes changed: {name}")
        elif assembled[target] != payloads[str(state["member"])]:
            raise AssertionError(f"replacement payload selection drifted: {name}")
        print(f"PASS {name}: {map_key} state={state['options']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    args = parser.parse_args()
    smoke_room_payload_assembly(args.root)


if __name__ == "__main__":
    main()
