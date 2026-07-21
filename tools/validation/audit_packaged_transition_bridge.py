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
    sys.path.insert(0, str(client_dir))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def assert_packaged_manifest(client_dir: Path, manifest_path: Path) -> str:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bridge = client_dir / "bridge_client.py"
    actual = __import__("hashlib").sha256(bridge.read_bytes()).hexdigest()
    recorded = manifest.get("mission_bridge", {})
    if recorded.get("sha256") != actual:
        raise AssertionError("unpacked bridge SHA diverges from RELEASE_MANIFEST")
    if recorded.get("revision") != f"mission-unified-{actual[:12]}":
        raise AssertionError("unpacked bridge revision diverges from RELEASE_MANIFEST")
    if recorded.get("transition_handler") != "unified":
        raise AssertionError("RELEASE_MANIFEST does not declare unified transition handler")
    if recorded.get("protocol") != 3:
        raise AssertionError("RELEASE_MANIFEST does not declare bridge protocol 3")
    identity = json.loads((client_dir / "bridge_identity.json").read_text(encoding="utf-8"))
    if identity.get("protocol") != 3:
        raise AssertionError("unpacked bridge identity protocol diverges")
    if identity.get("sha256") != actual:
        raise AssertionError("unpacked bridge identity sha256 diverges")
    if identity.get("revision") != f"mission-unified-{actual[:12]}":
        raise AssertionError("unpacked bridge identity revision diverges")
    if "Ignoring unexpected goal transition event" in bridge.read_text(encoding="utf-8"):
        raise AssertionError("unpacked bridge still contains old goal-only handler")
    return actual


def assert_packaged_launcher(apworld_path: Path, client_dir: Path, bridge_sha256: str) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with zipfile.ZipFile(apworld_path) as archive:
            archive.extractall(root)
        client_path = root / "doometernal" / "Client.py"
        if not client_path.is_file():
            raise AssertionError("unpacked APWorld lacks normal DOOM launcher")
        settings = types.ModuleType("settings")
        settings.get_settings = lambda: {
            "doometernal_options": {"client_directory": str(client_dir)}
        }
        utils = types.ModuleType("Utils")
        utils.messagebox = lambda *args, **kwargs: None
        old_settings = sys.modules.get("settings")
        old_utils = sys.modules.get("Utils")
        sys.modules["settings"] = settings
        sys.modules["Utils"] = utils
        try:
            spec = importlib.util.spec_from_file_location("packaged_launcher", client_path)
            if spec is None or spec.loader is None:
                raise AssertionError("unpacked normal launcher is not importable")
            launcher = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(launcher)
            resolved = launcher._client_directory().resolve()
            actual_sha, revision = launcher._bridge_identity(resolved / "bridge_client.py")
            identity_path = resolved / "bridge_identity.json"
            original_identity = identity_path.read_text(encoding="utf-8")
            bad_identity = json.loads(original_identity)
            bad_identity["protocol"] = 999
            identity_path.write_text(json.dumps(bad_identity), encoding="utf-8")
            try:
                launcher._bridge_identity(resolved / "bridge_client.py")
            except RuntimeError as error:
                if "incompatible" not in str(error):
                    raise AssertionError("launcher protocol rejection is not clear") from error
            else:
                raise AssertionError("launcher accepted incompatible bridge protocol")
            finally:
                identity_path.write_text(original_identity, encoding="utf-8")
        finally:
            if old_settings is None:
                sys.modules.pop("settings", None)
            else:
                sys.modules["settings"] = old_settings
            if old_utils is None:
                sys.modules.pop("Utils", None)
            else:
                sys.modules["Utils"] = old_utils
        if resolved != client_dir.resolve() or resolved.parent != client_dir.resolve().parent:
            raise AssertionError("normal launcher resolved a stale/global bridge path")
        if not (resolved / "bridge_client.py").is_file():
            raise AssertionError("normal launcher did not resolve an installed bridge")
        if actual_sha != bridge_sha256 or revision != f"mission-unified-{bridge_sha256[:12]}":
            raise AssertionError("normal launcher did not validate unpacked bridge identity")


async def consume(
    module, event: Path, expected: int | None, fail_send: bool = False,
    allowed_locations: set[int] | None = None,
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
    if expected is None:
        if event.exists() or sent:
            raise AssertionError("Hub -> mission transition must be ignored and consumed")
    elif fail_send or (
        allowed_locations is not None and expected not in allowed_locations
    ):
        if not event.exists() or sent:
            raise AssertionError("retryable transition must preserve event")
    elif expected == 7770162:
        if event.exists() or sent[:1] != [{"cmd": "LocationChecks", "locations": [expected]}] or (
            len(sent) != 2 or sent[1].get("cmd") != "StatusUpdate"
        ):
            raise AssertionError(f"packaged Fortress goal event drift: {sent!r}")
    elif event.exists() or sent != [{"cmd": "LocationChecks", "locations": [expected]}]:
        raise AssertionError(f"packaged event did not send expected LocationChecks {expected}: {sent!r}")
    return sent


def event(path: Path, from_map: str, to_map: str) -> Path:
    path.write_text(f"sequence=7\nfrom_map={from_map}\nto_map={to_map}\n", encoding="utf-8")
    return path


def main() -> int:
    client_dir = Path(sys.argv[1]).resolve()
    source_registry = Path(sys.argv[2]).resolve()
    manifest_path = Path(sys.argv[3]).resolve()
    apworld_path = Path(sys.argv[4]).resolve()
    bridge_sha256 = assert_packaged_manifest(client_dir, manifest_path)
    assert_packaged_launcher(apworld_path, client_dir, bridge_sha256)
    packaged_registry = client_dir / "data" / "challenge_location_registry.json"
    if not packaged_registry.is_file() or packaged_registry.read_bytes() != source_registry.read_bytes():
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
            "BRIDGE_PROTOCOL=3",
            "TRANSITION_HANDLER=unified",
        ]
        if identity_lines != expected_identity:
            raise AssertionError(f"unpacked bridge startup identity drift: {identity_lines!r}")
        asyncio.run(consume(bridge, event(base / "ap_transition_1_1.evt", "game/sp/e1m1_intro/e1m1_intro", "game/sp/hub/hub"), None))
        asyncio.run(consume(bridge, event(base / "ap_transition_1_2.evt", "game/sp/e1m2_battle/e1m2_battle", "game/hub/hub"), None))
        asyncio.run(consume(bridge, event(base / "ap_transition_1_3.evt", "game/hub/hub", "game/sp/e1m2_war/e1m2_war"), None))
        retry_event = event(base / "ap_transition_1_4.evt", "game/sp/e1m3_cult/e1m3_cult", "game/sp/e1m4_boss/e1m4_boss")
        asyncio.run(consume(bridge, retry_event, 7770124, fail_send=True))
        asyncio.run(consume(bridge, retry_event, 7770124))
        goal_event = base / bridge.FORTRESS_GOAL_EVENT_FILENAME
        goal_event.write_text("AP_GOAL_EVENT_FORTRESS_VISIT_3\n", encoding="utf-8")
        asyncio.run(consume(bridge, goal_event, 7770162, fail_send=True))
        asyncio.run(consume(bridge, goal_event, 7770162))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
