#!/usr/bin/env python3
"""Exercise Mission Complete through the bridge copied into a playable client."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import types
import zipfile
from pathlib import Path


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
    sys.modules["NetUtils"] = net


def load_bridge(client_dir: Path, base_dir: Path, state_dir: Path):
    config = base_dir.parent / "ap_config.json"
    config.write_text(json.dumps({"doom_base_dir": str(base_dir), "save_games_dir": str(base_dir)}), encoding="utf-8")
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
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if set(manifest) != {"version"} or not isinstance(manifest["version"], str):
        raise AssertionError("RELEASE_MANIFEST must contain one version field")
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
    allowed_locations: set[int] | None = None,
    expect_goal: bool = False,
) -> list[dict]:
    sent = []

    class Context:
        def __init__(self):
            self.session_state = {"goal_sent": False}
            self.locations_checked = set()
            self.checked_locations = set()
            self.server_locations = (
                {7770122, 7770123, 7770124, 7770162}
                if allowed_locations is None else allowed_locations
            )
            self.server = types.SimpleNamespace(socket=types.SimpleNamespace(closed=False))

        async def send_msgs(self, messages):
            if fail_send:
                raise ConnectionError("test network failure")
            sent.extend(messages)
            for message in messages:
                self.checked_locations.update(message.get("locations", ()))

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
    try:
        await module.DoomEternalContext.check_campaign_goal_event(Context())
    finally:
        module.DOOM_BASE_DIR = original
        module.INV_DUMP_DIR = original_dump_dir
    if expect_goal:
        if fail_send:
            if not event.exists() or sent:
                raise AssertionError("retryable goal must preserve event")
        else:
            goal_publisher = next(
                publisher for publisher in module.PUBLISHERS
                if publisher.key == module.CAMPAIGN_GOAL_CONTRACT["publisher_key"]
            )
            expected_messages = [
                {"cmd": "LocationChecks", "locations": [effect["location_id"]]}
                for effect in goal_publisher.effects
                if effect["strategy"] == "location_check"
            ]
            expected_messages.append(
                {"cmd": "StatusUpdate", "status": module.ClientStatus.CLIENT_GOAL}
            )
            if event.exists() or sent != expected_messages:
                raise AssertionError(
                    f"packaged campaign goal event drift: {sent!r}"
                )
    elif expected is None:
        if event.exists() or sent:
            raise AssertionError("Hub -> mission transition must be ignored and consumed")
    elif fail_send or (
        allowed_locations is not None and expected not in allowed_locations
    ):
        if not event.exists() or sent:
            raise AssertionError("retryable transition must preserve event")
    elif event.exists() or sent != [{"cmd": "LocationChecks", "locations": [expected]}]:
        raise AssertionError(f"packaged event did not send expected LocationChecks {expected}: {sent!r}")
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
        old_state_home = os.environ.get("XDG_STATE_HOME")
        bridge = load_bridge(client_dir, base, Path(directory) / "state")
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
        asyncio.run(consume(
            bridge, goal_event, None, fail_send=True,
            allowed_locations=goal_locations, expect_goal=True,
        ))
        asyncio.run(consume(
            bridge, goal_event, None,
            allowed_locations=goal_locations, expect_goal=True,
        ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
