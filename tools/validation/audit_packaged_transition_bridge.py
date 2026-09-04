#!/usr/bin/env python3
"""Exercise Mission Complete through the bridge copied into a playable client."""

from __future__ import annotations

import asyncio
import enum
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import types
import zipfile
from pathlib import Path

from tools.release.release_manifest import load_release_manifest


def install_ap_stubs() -> None:
    sys.modules["Utils"] = types.SimpleNamespace(init_logging=lambda *args, **kwargs: None)
    sys.modules["colorama"] = types.SimpleNamespace(init=lambda: None, deinit=lambda: None)
    common = types.ModuleType("CommonClient")
    common.CommonContext = object
    common.server_loop = lambda ctx: None
    common.gui_enabled = False
    common.ClientCommandProcessor = object
    common.get_base_parser = lambda: __import__("argparse").ArgumentParser()
    common.logger = types.SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None)
    sys.modules["CommonClient"] = common
    net = types.ModuleType("NetUtils")
    net.ClientStatus = types.SimpleNamespace(CLIENT_GOAL=30)
    net.Hint = __import__("collections").namedtuple(
        "Hint",
        (
            "receiving_player", "finding_player", "item_id", "location_id",
            "found", "entrance", "item_flags", "status",
        ),
        defaults=(0, "", 0, 0),
    )
    net.HintStatus = enum.IntEnum(
        "HintStatus",
        {"HINT_UNSPECIFIED": 0, "HINT_PRIORITY": 10, "HINT_NO_PRIORITY": 20, "HINT_AVOID": 30, "HINT_FOUND": 40},
    )
    net.JSONMessagePart = dict
    net.JSONTypes = enum.Enum(
        "JSONTypes",
        {
            name: name
            for name in (
                "text", "color", "player_id", "player_name", "item_name",
                "item_id", "location_name", "location_id", "hint_status",
            )
        },
        type=str,
    )
    sys.modules["NetUtils"] = net


def load_bridge(client_dir: Path, base_dir: Path, state_dir: Path, saves_base: Path):
    config = base_dir.parent / "ap_config.json"
    # The Saved Games base is its own authority, never the game installation
    # base, even when both exist side by side in a fixture.
    config.write_text(json.dumps({"doom_base_dir": str(base_dir), "save_games_dir": str(saves_base)}), encoding="utf-8")
    os.environ["DOOM_AP_CONFIG_FILE"] = str(config)
    os.environ["XDG_STATE_HOME"] = str(state_dir)
    install_ap_stubs()
    spec = importlib.util.spec_from_file_location("packaged_bridge", client_dir / "bridge_client.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("packaged bridge is not importable")
    module = importlib.util.module_from_spec(spec)
    source_root = Path(__file__).resolve().parents[2]
    original_sys_path = sys.path[:]
    sys.path[:] = [
        str(client_dir),
        *(
            entry
            for entry in original_sys_path
            if Path(entry or os.getcwd()).resolve() != source_root
        ),
    ]
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = original_sys_path
    return module


def assert_packaged_manifest(client_dir: Path, manifest_path: Path) -> str:
    source_root = Path(__file__).resolve().parents[2]
    manifest = load_release_manifest(manifest_path, package_root=manifest_path.parent)
    registry_path = client_dir / "data" / "checked_location_visuals.json"
    registry_record = manifest["checked_location_visuals"]
    if not registry_path.is_file() or not isinstance(registry_record, dict):
        raise AssertionError("packaged checked-location visual registry is missing")
    if registry_record.get("path") != "client/data/checked_location_visuals.json":
        raise AssertionError("packaged checked-location visual registry path drifted")
    if registry_record["sha256"] != hashlib.sha256(registry_path.read_bytes()).hexdigest():
        raise AssertionError("packaged checked-location visual registry hash drifted")
    bridge = client_dir / "bridge_client.py"
    actual = __import__("hashlib").sha256(bridge.read_bytes()).hexdigest()
    content_identity = json.loads(
        (client_dir / "data" / "content_identity.json").read_text(encoding="utf-8")
    )
    expected_protocol = content_identity["bridge_protocol_version"]
    identity = json.loads((client_dir / "bridge_identity.json").read_text(encoding="utf-8"))
    if identity.get("protocol") != expected_protocol:
        raise AssertionError("unpacked bridge identity protocol diverges")
    if identity.get("game") != "DOOM Eternal":
        raise AssertionError("unpacked bridge identity game diverges")
    if identity.get("sha256") != actual:
        raise AssertionError("unpacked bridge identity sha256 diverges")
    if identity.get("revision") != f"mission-unified-{actual[:12]}":
        raise AssertionError("unpacked bridge identity revision diverges")
    if "Ignoring unexpected goal transition event" in bridge.read_text(encoding="utf-8"):
        raise AssertionError("unpacked bridge still contains old goal-only handler")
    if not (client_dir / "doom_eap" / "presentation.py").is_file():
        raise AssertionError("packaged client lacks doom_eap/presentation.py")
    packaged_tools = client_dir / "tools"
    expected_tools = {
        Path("__init__.py"),
        Path("release") / "__init__.py",
        Path("release") / "room_payloads.py",
        *(Path("decls") / path.name for path in (source_root / "tools" / "decls").glob("*.py")),
        *(Path("maps") / path.name for path in (source_root / "tools" / "maps").glob("*.py")),
    }
    actual_tools = {
        path.relative_to(packaged_tools)
        for path in packaged_tools.rglob("*")
        if path.is_file()
    } if packaged_tools.is_dir() else set()
    if actual_tools != expected_tools:
        raise AssertionError(
            "packaged runtime tool surface drifted: "
            f"{sorted(str(path) for path in actual_tools)}"
        )
    return actual


def assert_generation_only_apworld(apworld_path: Path) -> None:
    """Keep launcher/runtime ownership out of generation-only APWorld package."""
    with zipfile.ZipFile(apworld_path) as archive:
        names = set(archive.namelist())
    if "doometernal/__init__.py" not in names:
        raise AssertionError("packaged APWorld lacks DOOM Eternal world module")
    if "doometernal/client.py" in names:
        raise AssertionError("packaged APWorld unexpectedly owns client startup")


async def consume(
    module, event: Path, expected: int | None, fail_send: bool = False,
) -> list[dict]:
    sent = []

    class Context:
        def __init__(self):
            self.session_state = {"goal_sent": False}
            self.locations_checked = set()
            self.checked_locations = set()
            self.server_locations = {7770122, 7770123, 7770124, 7770162}
            if expected is not None:
                self.server_locations.add(expected)
            self.server = types.SimpleNamespace(socket=types.SimpleNamespace(closed=False))

        async def send_msgs(self, messages):
            if fail_send:
                raise ConnectionError("test network failure")
            sent.extend(messages)

        def persist_session_state(self):
            pass

        async def send_mission_complete(self, *args, **kwargs):
            return await module.DoomEternalContext.send_mission_complete(self, *args, **kwargs)

        async def send_campaign_goal(self, *args, **kwargs):
            return await module.DoomEternalContext.send_campaign_goal(self, *args, **kwargs)

    original = module.DOOM_BASE_DIR
    original_dump_dir = module.INV_DUMP_DIR
    module.DOOM_BASE_DIR = str(event.parent)
    module.INV_DUMP_DIR = str(event.parent)
    context = Context()
    try:
        await module.DoomEternalContext.check_campaign_goal_event(context)
        if not fail_send and sent:
            context.checked_locations.update(
                location_id
                for message in sent
                for location_id in message.get("locations", ())
            )
            await module.DoomEternalContext.check_campaign_goal_event(context)
    finally:
        module.DOOM_BASE_DIR = original
        module.INV_DUMP_DIR = original_dump_dir
    if expected is None:
        if event.exists() or sent:
            raise AssertionError("Hub -> mission transition must be ignored and consumed")
    elif fail_send:
        if not event.exists() or sent:
            raise AssertionError("retryable transition must preserve event")
    elif event.exists() or sent != [{"cmd": "LocationChecks", "locations": [expected]}]:
        raise AssertionError(f"packaged event did not send expected LocationChecks {expected}: {sent!r}")
    return sent


async def evaluate_checked_state(module, slot_data, checked_locations, expect_goal):
    sent = []

    class Context:
        def __init__(self):
            self.session_state = {"goal_sent": False}
            self.goal_dispatch_in_flight = False
            self.goal_dispatch_sent = False
            self.server_checked_locations_ready = True
            self._connected_slot_data = slot_data
            self.checked_locations = set(checked_locations)
            self.server = types.SimpleNamespace(socket=types.SimpleNamespace(closed=False))

        async def send_msgs(self, messages):
            sent.extend(messages)

    result = await module.DoomEternalContext.evaluate_campaign_goal(
        Context(), "packaged transition audit"
    )
    saw_goal = any(
        message == {"cmd": "StatusUpdate", "status": module.ClientStatus.CLIENT_GOAL}
        for message in sent
    )
    if result is not expect_goal or saw_goal is not expect_goal:
        raise AssertionError(
            f"central goal predicate drift: result={result!r} sent={sent!r}"
        )
    return sent


def event(path: Path, from_map: str, to_map: str) -> Path:
    path.write_text(
        f"sequence=7\nfrom_map={from_map}\nto_map={to_map}\n",
        encoding="utf-8",
    )
    return path


def main() -> int:
    client_dir = Path(sys.argv[1]).resolve()
    source_registry = Path(sys.argv[2]).resolve()
    manifest_path = Path(sys.argv[3]).resolve()
    apworld_path = Path(sys.argv[4]).resolve()
    bridge_sha256 = assert_packaged_manifest(client_dir, manifest_path)
    assert_generation_only_apworld(apworld_path)
    packaged_registry = client_dir / "data" / "challenge_location_registry.json"
    if (
        not packaged_registry.is_file()
        or packaged_registry.read_bytes() != source_registry.read_bytes()
    ):
        raise SystemExit("packaged challenge registry diverges from source")

    with tempfile.TemporaryDirectory() as directory:
        game = Path(directory) / "DOOMEternal"
        base = game / "base"
        (base / "classicwads").mkdir(parents=True)
        (game / "DOOMEternalx64vk.exe").write_text("", encoding="utf-8")
        saves_base = Path(directory) / "Saved Games" / "id Software" / "DOOMEternal" / "base"
        saves_base.mkdir(parents=True)
        old_state_home = os.environ.get("XDG_STATE_HOME")
        bridge = load_bridge(client_dir, base, Path(directory) / "state", saves_base)
        if old_state_home is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = old_state_home

        if bridge.BRIDGE_FILE != (client_dir / "bridge_client.py").resolve():
            raise AssertionError("loaded bridge is not the unpacked client bridge")
        if bridge.BRIDGE_SHA256 != bridge_sha256:
            raise AssertionError("loaded bridge SHA differs from unpacked bridge SHA")
        if bridge.TRANSITION_HANDLER != "unified":
            raise AssertionError("loaded bridge did not select unified transition handler")

        identity_lines = []
        original_logger = bridge.logger
        bridge.logger = types.SimpleNamespace(
            info=lambda template, *args: identity_lines.append(template % args)
        )
        bridge.log_mission_bridge_identity()
        bridge.logger = original_logger
        expected_identity = [
            f"BRIDGE_REVISION=mission-unified-{bridge_sha256[:12]}",
            f"BRIDGE_FILE={client_dir / 'bridge_client.py'}",
            f"BRIDGE_SHA256={bridge_sha256}",
            f"BRIDGE_PROTOCOL={bridge.BRIDGE_PROTOCOL}",
            "GAME_NAME=DOOM Eternal",
            "TRANSITION_HANDLER=unified",
        ]
        if identity_lines != expected_identity:
            raise AssertionError(
                f"unpacked bridge startup identity drift: {identity_lines!r}"
            )

        asyncio.run(consume(
            bridge,
            event(
                base / "ap_transition_1_1.evt",
                "game/sp/e1m1_intro/e1m1_intro",
                "game/sp/hub/hub",
            ),
            None,
        ))
        asyncio.run(consume(
            bridge,
            event(
                base / "ap_transition_1_2.evt",
                "game/sp/e1m2_battle/e1m2_battle",
                "game/hub/hub",
            ),
            None,
        ))
        asyncio.run(consume(
            bridge,
            event(
                base / "ap_transition_1_3.evt",
                "game/hub/hub",
                "game/sp/e1m2_war/e1m2_war",
            ),
            None,
        ))
        retry_event = event(
            base / "ap_transition_1_4.evt",
            "game/sp/e1m3_cult/e1m3_cult",
            "game/sp/e1m4_boss/e1m4_boss",
        )
        asyncio.run(consume(bridge, retry_event, 7770124, fail_send=True))
        asyncio.run(consume(bridge, retry_event, 7770124))

        goal_event = base / bridge.CAMPAIGN_GOAL_CONTRACT["event_filename"]
        goal_event.write_text(
            f"{bridge.CAMPAIGN_GOAL_CONTRACT['marker']}\n",
            encoding="utf-8",
        )
        goal_publisher = next(
            publisher for publisher in bridge.PUBLISHERS
            if publisher.key == bridge.CAMPAIGN_GOAL_CONTRACT["publisher_key"]
        )
        goal_locations = {
            effect["location_id"]
            for effect in goal_publisher.effects
            if effect["strategy"] == "location_check"
        }
        if goal_locations != {7770414}:
            raise AssertionError(f"Final Sin publisher effects drifted: {goal_locations!r}")
        asyncio.run(consume(bridge, goal_event, 7770414, fail_send=True))
        final_sin_sent = asyncio.run(consume(bridge, goal_event, 7770414))
        if final_sin_sent != [{"cmd": "LocationChecks", "locations": [7770414]}]:
            raise AssertionError(f"Final Sin event emitted unexpected messages: {final_sin_sent!r}")

        required_capabilities = [
            "cross_campaign_materialization_v1",
            "goal_events_v1",
            "goal_endpoint_events_v1",
        ]
        icon_goal = {
            "goal": "Kill the Icon of Sin",
            "goal_endpoint_event": "Internal Goal Endpoint: Kill the Icon of Sin",
            "goal_endpoint_available": True,
            "additional_victory_requirements": ["Complete All Slayer Gates"],
            "use_dlc_content": True,
            "include_dlc_missions": True,
            "required_capabilities": required_capabilities,
        }
        base_missions = {
            location_id
            for location_id, name in bridge.DOOM_LOCATION_NAMES.items()
            if name.endswith(" - Mission Complete")
            and name.split(" - ", 1)[0] not in bridge.DLC_MISSION_PREFIXES
        }
        base_end_checked = base_missions | {7770418, 7770414, 7770419}
        asyncio.run(evaluate_checked_state(
            bridge, dict(icon_goal, goal="Complete the Full Saga",
                         goal_endpoint_event="Internal Goal Endpoint: Complete the Full Saga"),
            base_end_checked,
            False,
        ))
        all_gate_ids = {
            location_id
            for location_id, name in bridge.DOOM_LOCATION_NAMES.items()
            if name.endswith(" - Slayer Gate Complete")
        }
        if len(all_gate_ids) != 8:
            raise AssertionError(f"Slayer gate projection drifted: {all_gate_ids!r}")
        asyncio.run(evaluate_checked_state(
            bridge, icon_goal, {7770414, 7770418, *all_gate_ids}, True
        ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
