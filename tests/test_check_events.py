import asyncio
import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_CONFIG_TEMP_DIR = tempfile.TemporaryDirectory()
_CONFIG_ROOT = Path(_CONFIG_TEMP_DIR.name)
_FAKE_DOOM_ROOT = _CONFIG_ROOT / "DOOMEternal"
_FAKE_DOOM_BASE = _CONFIG_ROOT / "DOOMEternal" / "base"
_FAKE_SAVE_BASE = (
    _CONFIG_ROOT / "Saved Games" / "id Software" / "DOOMEternal" / "base"
)
_FAKE_DOOM_BASE.mkdir(parents=True)
_FAKE_SAVE_BASE.mkdir(parents=True)
(_FAKE_DOOM_BASE / "classicwads").mkdir()
(_FAKE_DOOM_ROOT / "DOOMEternalx64vk.exe").write_text("", encoding="utf-8")
_CONFIG_FILE = _CONFIG_ROOT / "ap_config.json"
_CONFIG_FILE.write_text(
    json.dumps(
        {
            "doom_base_dir": str(_FAKE_DOOM_BASE),
            "save_games_dir": str(_FAKE_SAVE_BASE),
        }
    ),
    encoding="utf-8",
)
os.environ["DOOM_AP_CONFIG_FILE"] = str(_CONFIG_FILE)


class _DummyLogger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class _DummyCommonContext:
    def __init__(self, server_address=None, password=None):
        self.server_address = server_address
        self.password = password


def _load_bridge_client():
    sys.modules.setdefault("Utils", types.SimpleNamespace(init_logging=lambda *args, **kwargs: None))
    sys.modules.setdefault("colorama", types.SimpleNamespace(init=lambda: None, deinit=lambda: None))

    common_client = types.ModuleType("CommonClient")
    common_client.CommonContext = _DummyCommonContext
    common_client.server_loop = lambda ctx: None
    common_client.gui_enabled = False
    common_client.ClientCommandProcessor = object
    common_client.get_base_parser = lambda: __import__("argparse").ArgumentParser()
    common_client.logger = _DummyLogger()
    sys.modules.setdefault("CommonClient", common_client)

    net_utils = types.ModuleType("NetUtils")
    net_utils.ClientStatus = types.SimpleNamespace(CLIENT_GOAL=30)
    sys.modules.setdefault("NetUtils", net_utils)

    return importlib.import_module("bridge_client")


bridge_client = _load_bridge_client()


class CheckEventTests(unittest.TestCase):
    def test_doom_status_is_user_facing_only(self):
        outputs = []

        class FakeProcessor:
            def output(self, message):
                outputs.append(message)

        bridge_client.DoomCommandProcessor._cmd_doom_status(FakeProcessor())

        self.assertEqual(
            outputs,
            [
                "DOOM integration: running",
                f"Detailed diagnostics: {bridge_client.BRIDGE_LOG_DIR}",
            ],
        )

    def test_extract_location_id_from_filename(self):
        location_id = bridge_client.extract_location_id_from_event(
            "/tmp/ap_event_7770090.txt"
        )
        self.assertEqual(location_id, 7770090)

    def test_extract_location_id_from_suffixed_filename(self):
        location_id = bridge_client.extract_location_id_from_event(
            "/tmp/ap_event_7770090_1.txt"
        )
        self.assertEqual(location_id, 7770090)

    def test_extract_location_id_from_file_contents_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "unknown_event_name.txt"
            path.write_text("echo AP_CHECK_EVENT_7770456\n", encoding="utf-8")
            location_id = bridge_client.extract_location_id_from_event(str(path))
        self.assertEqual(location_id, 7770456)

    def test_parse_goal_transition_event(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "ap_transition_e1m3_cult_to_e1m4_boss.evt"
            path.write_text(
                "sequence=3\n"
                "timestamp=2026-07-07T15:04:05.000Z\n"
                "from_map=game/sp/e1m3_cult/e1m3_cult\n"
                "to_map=game/sp/e1m4_boss/e1m4_boss\n",
                encoding="utf-8",
            )
            event = bridge_client.parse_goal_transition_event(str(path))

        self.assertEqual(
            event,
            {
                "sequence": "3",
                "timestamp": "2026-07-07T15:04:05.000Z",
                "from_map": "game/sp/e1m3_cult/e1m3_cult",
                "to_map": "game/sp/e1m4_boss/e1m4_boss",
            },
        )

    def test_flush_sends_unique_locations_and_waits_for_server_ack(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_dump_dir = bridge_client.INV_DUMP_DIR
            try:
                bridge_client.INV_DUMP_DIR = tmpdir
                Path(tmpdir, "ap_event_7770001.txt").write_text(
                    "AP_CHECK_EVENT_7770001\n", encoding="utf-8"
                )
                Path(tmpdir, "ap_event_7770001_1.txt").write_text(
                    "AP_CHECK_EVENT_7770001\n", encoding="utf-8"
                )
                Path(tmpdir, "ap_event_7770002.txt").write_text(
                    "AP_CHECK_EVENT_7770002\n", encoding="utf-8"
                )

                sent = []

                class FakeContext:
                    def __init__(self):
                        self.locations_checked = set()
                        self.checked_locations = set()
                        self.server_locations = {7770001, 7770002}

                    async def send_msgs(self, messages):
                        sent.extend(messages)

                ctx = FakeContext()
                asyncio.run(
                    bridge_client.DoomEternalContext.flush_check_event_files(ctx)
                )

                self.assertEqual(
                    sent,
                    [{"cmd": "LocationChecks", "locations": [7770001, 7770002]}],
                )
                self.assertEqual(ctx.locations_checked, {7770001, 7770002})
                self.assertEqual(
                    len(list(Path(tmpdir).glob("ap_event_*.txt"))), 3
                )

                asyncio.run(
                    bridge_client.DoomEternalContext.flush_check_event_files(ctx)
                )
                self.assertEqual(len(sent), 1)
                self.assertEqual(
                    len(list(Path(tmpdir).glob("ap_event_*.txt"))), 3
                )

                ctx.checked_locations = {7770001, 7770002}
                asyncio.run(
                    bridge_client.DoomEternalContext.flush_check_event_files(ctx)
                )
                self.assertEqual(list(Path(tmpdir).glob("ap_event_*.txt")), [])
                self.assertEqual(len(sent), 1)
            finally:
                bridge_client.INV_DUMP_DIR = original_dump_dir

    def test_flush_preserves_files_when_send_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_dump_dir = bridge_client.INV_DUMP_DIR
            try:
                bridge_client.INV_DUMP_DIR = tmpdir
                event_path = Path(tmpdir, "ap_event_7770999.txt")
                event_path.write_text("AP_CHECK_EVENT_7770999\n", encoding="utf-8")

                class FakeContext:
                    def __init__(self):
                        self.locations_checked = set()
                        self.checked_locations = set()
                        self.server_locations = {7770999}

                    async def send_msgs(self, messages):
                        raise RuntimeError("network down")

                ctx = FakeContext()
                asyncio.run(
                    bridge_client.DoomEternalContext.flush_check_event_files(ctx)
                )

                self.assertTrue(event_path.exists())
                self.assertEqual(ctx.locations_checked, set())
            finally:
                bridge_client.INV_DUMP_DIR = original_dump_dir

    def test_flush_preserves_location_unknown_to_connected_slot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_dump_dir = bridge_client.INV_DUMP_DIR
            try:
                bridge_client.INV_DUMP_DIR = tmpdir
                event_path = Path(tmpdir, "ap_event_7770999.txt")
                event_path.write_text("AP_CHECK_EVENT_7770999\n", encoding="utf-8")
                sent = []

                class FakeContext:
                    def __init__(self):
                        self.locations_checked = set()
                        self.checked_locations = set()
                        self.server_locations = {7770001}

                    async def send_msgs(self, messages):
                        sent.extend(messages)

                ctx = FakeContext()
                asyncio.run(
                    bridge_client.DoomEternalContext.flush_check_event_files(ctx)
                )

                self.assertTrue(event_path.exists())
                self.assertEqual(ctx.locations_checked, set())
                self.assertEqual(sent, [])
            finally:
                bridge_client.INV_DUMP_DIR = original_dump_dir

    def test_goal_event_sends_goal_and_removes_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_base_dir = bridge_client.DOOM_BASE_DIR
            try:
                bridge_client.DOOM_BASE_DIR = tmpdir
                event_path = Path(tmpdir, bridge_client.GOAL_EVENT_FILENAME)
                event_path.write_text(
                    "sequence=7\n"
                    "timestamp=2026-07-07T15:04:05.000Z\n"
                    "from_map=game/sp/e1m3_cult/e1m3_cult\n"
                    "to_map=game/sp/e1m4_boss/e1m4_boss\n",
                    encoding="utf-8",
                )
                sent = []
                persisted = []

                class FakeContext:
                    def __init__(self):
                        self.item_state_ready = True
                        self.session_state = {"goal_sent": False}
                        self.locations_checked = set()
                        self.server = types.SimpleNamespace(
                            socket=types.SimpleNamespace(closed=False)
                        )

                    async def send_msgs(self, messages):
                        sent.extend(messages)

                    def persist_session_state(self):
                        persisted.append(True)

                    def output(self, message):
                        pass

                    async def send_campaign_goal(self, source_description):
                        return await bridge_client.DoomEternalContext.send_campaign_goal(
                            self, source_description
                        )

                    async def check_campaign_goal_event(self):
                        return await bridge_client.DoomEternalContext.check_campaign_goal_event(
                            self
                        )

                    async def check_campaign_goal_save_fallback(self):
                        return await bridge_client.DoomEternalContext.check_campaign_goal_save_fallback(
                            self
                        )

                ctx = FakeContext()
                asyncio.run(
                    bridge_client.DoomEternalContext.check_campaign_goal(ctx)
                )

                self.assertEqual(
                    sent,
                    [
                        {"cmd": "LocationChecks", "locations": [7770082]},
                        {"cmd": "StatusUpdate", "status": 30},
                    ],
                )
                self.assertEqual(ctx.session_state["goal_sent"], True)
                self.assertEqual(ctx.locations_checked, {7770082})
                self.assertEqual(persisted, [True])
                self.assertFalse(event_path.exists())
            finally:
                bridge_client.DOOM_BASE_DIR = original_base_dir

    def test_goal_event_is_preserved_while_disconnected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_base_dir = bridge_client.DOOM_BASE_DIR
            try:
                bridge_client.DOOM_BASE_DIR = tmpdir
                event_path = Path(tmpdir, bridge_client.GOAL_EVENT_FILENAME)
                event_path.write_text(
                    "sequence=8\n"
                    "timestamp=2026-07-07T15:04:05.000Z\n"
                    "from_map=game/sp/e1m3_cult/e1m3_cult\n"
                    "to_map=game/sp/e1m4_boss/e1m4_boss\n",
                    encoding="utf-8",
                )

                class FakeContext:
                    def __init__(self):
                        self.item_state_ready = True
                        self.session_state = {"goal_sent": False}
                        self.locations_checked = set()
                        self.server = types.SimpleNamespace(
                            socket=types.SimpleNamespace(closed=True)
                        )

                    async def send_msgs(self, messages):
                        raise AssertionError("send_msgs should not be called")

                    def persist_session_state(self):
                        raise AssertionError(
                            "persist_session_state should not be called"
                        )

                    def output(self, message):
                        pass

                    async def send_campaign_goal(self, source_description):
                        return await bridge_client.DoomEternalContext.send_campaign_goal(
                            self, source_description
                        )

                    async def check_campaign_goal_event(self):
                        return await bridge_client.DoomEternalContext.check_campaign_goal_event(
                            self
                        )

                    async def check_campaign_goal_save_fallback(self):
                        return await bridge_client.DoomEternalContext.check_campaign_goal_save_fallback(
                            self
                        )

                ctx = FakeContext()
                asyncio.run(
                    bridge_client.DoomEternalContext.check_campaign_goal(ctx)
                )

                self.assertTrue(event_path.exists())
                self.assertEqual(ctx.session_state["goal_sent"], False)
            finally:
                bridge_client.DOOM_BASE_DIR = original_base_dir


if __name__ == "__main__":
    unittest.main()
