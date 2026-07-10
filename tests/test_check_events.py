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
    def _make_item_context(self):
        ctx = bridge_client.DoomEternalContext(None, None)
        ctx.session_state = {}
        ctx.client_state = {"version": 1, "sessions": {}}
        ctx.item_state_ready = True
        ctx.items_received = []
        return ctx

    def test_string_mapping_spools_one_map_side_activation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_queue_dir = bridge_client.QUEUE_DIR
            original_state_file = bridge_client.CLIENT_STATE_FILE
            try:
                bridge_client.QUEUE_DIR = tmpdir
                bridge_client.CLIENT_STATE_FILE = Path(tmpdir, "client_state.json")
                ctx = self._make_item_context()

                spooled, description = ctx.spool_item_commands(7770010, 3)

                self.assertTrue(spooled)
                self.assertEqual(description, "give weapon/player/chainsaw")
                files = sorted(Path(tmpdir).glob("*.cmd"))
                self.assertEqual([path.name for path in files], [
                    "recv-000003-item-7770010-cmd-00.cmd"
                ])
                self.assertEqual(
                    files[0].read_text(encoding="utf-8"),
                    "ai_ScriptCmdEnt ap_rpc_v3_7770010 activate\n",
                )
            finally:
                bridge_client.QUEUE_DIR = original_queue_dir
                bridge_client.CLIENT_STATE_FILE = original_state_file

    def test_chrispy_string_mapping_spools_map_side_activation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_queue_dir = bridge_client.QUEUE_DIR
            original_state_file = bridge_client.CLIENT_STATE_FILE
            try:
                bridge_client.QUEUE_DIR = tmpdir
                bridge_client.CLIENT_STATE_FILE = Path(tmpdir, "client_state.json")
                ctx = self._make_item_context()

                spooled, description = ctx.spool_item_commands(7770045, 11)

                self.assertTrue(spooled)
                self.assertEqual(description, "chrispy ai/heavy/revenant")
                files = sorted(Path(tmpdir).glob("*.cmd"))
                self.assertEqual(
                    [path.name for path in files],
                    ["recv-000011-item-7770045-cmd-00.cmd"],
                )
                self.assertEqual(
                    files[0].read_text(encoding="utf-8"),
                    "ai_ScriptCmdEnt ap_rpc_v3_7770045 activate\n",
                )
                self.assertNotIn(
                    "chrispy ai/heavy/revenant",
                    files[0].read_text(encoding="utf-8"),
                )
            finally:
                bridge_client.QUEUE_DIR = original_queue_dir
                bridge_client.CLIENT_STATE_FILE = original_state_file

    def test_all_current_item_mappings_delegate_map_side(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_queue_dir = bridge_client.QUEUE_DIR
            original_state_file = bridge_client.CLIENT_STATE_FILE
            try:
                bridge_client.QUEUE_DIR = tmpdir
                bridge_client.CLIENT_STATE_FILE = Path(tmpdir, "client_state.json")
                ctx = self._make_item_context()
                item_ids = [
                    item_id
                    for item_id, command in bridge_client.ITEM_ID_TO_COMMAND.items()
                    if not (isinstance(command, dict) and command.get("type") == "no_op")
                ]

                self.assertIn(7770000, item_ids)
                self.assertIn(7770045, item_ids)
                ctx.items_received = [
                    types.SimpleNamespace(item=item_id) for item_id in item_ids
                ]
                for receive_index, item_id in enumerate(item_ids):
                    spooled, _ = ctx.spool_item_commands(item_id, receive_index)
                    self.assertTrue(spooled)

                for path in Path(tmpdir).glob("*.cmd"):
                    command = path.read_text(encoding="utf-8")
                    self.assertTrue(command.startswith("ai_ScriptCmdEnt ap_rpc_v3_"))
                    self.assertNotIn("chrispy ", command)
                    self.assertNotIn("give ", command)
                    self.assertNotIn("g_giveExtraLives", command)
                    self.assertNotIn("givePlayerPerk", command)
            finally:
                bridge_client.QUEUE_DIR = original_queue_dir
                bridge_client.CLIENT_STATE_FILE = original_state_file

    def test_multi_command_mapping_spools_ordered_child_activations(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_queue_dir = bridge_client.QUEUE_DIR
            original_state_file = bridge_client.CLIENT_STATE_FILE
            try:
                bridge_client.QUEUE_DIR = tmpdir
                bridge_client.CLIENT_STATE_FILE = Path(tmpdir, "client_state.json")
                ctx = self._make_item_context()

                spooled, description = ctx.spool_item_commands(7770012, 7)

                self.assertTrue(spooled)
                self.assertEqual(
                    description,
                    "give equipmentlauncher/equipmentlauncherleft -> "
                    "give weapon/player/equipment_flame_belch",
                )
                files = sorted(Path(tmpdir).glob("*.cmd"))
                self.assertEqual(
                    [path.name for path in files],
                    [
                        "recv-000007-item-7770012-cmd-00.cmd",
                        "recv-000007-item-7770012-cmd-01.cmd",
                    ],
                )
                self.assertEqual(
                    [path.read_text(encoding="utf-8") for path in files],
                    [
                        "ai_ScriptCmdEnt ap_rpc_v3_7770012_0 activate\n",
                        "ai_ScriptCmdEnt ap_rpc_v3_7770012_1 activate\n",
                    ],
                )
            finally:
                bridge_client.QUEUE_DIR = original_queue_dir
                bridge_client.CLIENT_STATE_FILE = original_state_file

    def test_multi_command_chrispy_element_uses_child_entity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_queue_dir = bridge_client.QUEUE_DIR
            original_state_file = bridge_client.CLIENT_STATE_FILE
            original_mapping = bridge_client.ITEM_ID_TO_COMMAND.get(7770997)
            try:
                bridge_client.QUEUE_DIR = tmpdir
                bridge_client.CLIENT_STATE_FILE = Path(tmpdir, "client_state.json")
                bridge_client.ITEM_ID_TO_COMMAND[7770997] = [
                    "give ammo",
                    "chrispy ai/fodder/imp",
                ]
                ctx = self._make_item_context()

                spooled, _ = ctx.spool_item_commands(7770997, 12)

                self.assertTrue(spooled)
                files = sorted(Path(tmpdir).glob("*.cmd"))
                self.assertEqual(
                    [path.name for path in files],
                    [
                        "recv-000012-item-7770997-cmd-00.cmd",
                        "recv-000012-item-7770997-cmd-01.cmd",
                    ],
                )
                self.assertEqual(
                    [path.read_text(encoding="utf-8") for path in files],
                    [
                        "ai_ScriptCmdEnt ap_rpc_v3_7770997_0 activate\n",
                        "ai_ScriptCmdEnt ap_rpc_v3_7770997_1 activate\n",
                    ],
                )
            finally:
                bridge_client.QUEUE_DIR = original_queue_dir
                bridge_client.CLIENT_STATE_FILE = original_state_file
                if original_mapping is None:
                    bridge_client.ITEM_ID_TO_COMMAND.pop(7770997, None)
                else:
                    bridge_client.ITEM_ID_TO_COMMAND[7770997] = original_mapping

    def test_multi_command_reconnect_does_not_duplicate_existing_spools(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_queue_dir = bridge_client.QUEUE_DIR
            original_state_file = bridge_client.CLIENT_STATE_FILE
            try:
                bridge_client.QUEUE_DIR = tmpdir
                bridge_client.CLIENT_STATE_FILE = Path(tmpdir, "client_state.json")
                ctx = self._make_item_context()

                self.assertTrue(ctx.spool_item_commands(7770012, 7)[0])
                self.assertTrue(ctx.spool_item_commands(7770012, 7)[0])

                self.assertEqual(len(list(Path(tmpdir).glob("*.cmd"))), 2)
            finally:
                bridge_client.QUEUE_DIR = original_queue_dir
                bridge_client.CLIENT_STATE_FILE = original_state_file

    def test_multi_command_partial_crash_spools_remaining_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_queue_dir = bridge_client.QUEUE_DIR
            original_state_file = bridge_client.CLIENT_STATE_FILE
            try:
                bridge_client.QUEUE_DIR = tmpdir
                bridge_client.CLIENT_STATE_FILE = Path(tmpdir, "client_state.json")
                ctx = self._make_item_context()
                first = Path(tmpdir, "recv-000007-item-7770012-cmd-00.cmd")
                first.write_text(
                    "ai_ScriptCmdEnt ap_rpc_v3_7770012_0 activate\n",
                    encoding="utf-8",
                )
                ctx.session_state["item_command_groups"] = {
                    "7": {
                        "item_id": 7770012,
                        "next_command": 1,
                        "total_commands": 2,
                    }
                }

                spooled, _ = ctx.spool_item_commands(7770012, 7)

                self.assertTrue(spooled)
                files = sorted(Path(tmpdir).glob("*.cmd"))
                self.assertEqual(
                    [path.name for path in files],
                    [
                        "recv-000007-item-7770012-cmd-00.cmd",
                        "recv-000007-item-7770012-cmd-01.cmd",
                    ],
                )
                self.assertNotIn("item_command_groups", ctx.session_state)
            finally:
                bridge_client.QUEUE_DIR = original_queue_dir
                bridge_client.CLIENT_STATE_FILE = original_state_file

    def test_empty_list_mapping_fails_closed_without_spool(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_queue_dir = bridge_client.QUEUE_DIR
            original_mapping = bridge_client.ITEM_ID_TO_COMMAND.get(7770998)
            try:
                bridge_client.QUEUE_DIR = tmpdir
                bridge_client.ITEM_ID_TO_COMMAND[7770998] = []
                ctx = self._make_item_context()

                spooled, description = ctx.spool_item_commands(7770998, 9)

                self.assertFalse(spooled)
                self.assertEqual(description, "mapping list is empty")
                self.assertEqual(list(Path(tmpdir).glob("*.cmd")), [])
            finally:
                bridge_client.QUEUE_DIR = original_queue_dir
                if original_mapping is None:
                    bridge_client.ITEM_ID_TO_COMMAND.pop(7770998, None)
                else:
                    bridge_client.ITEM_ID_TO_COMMAND[7770998] = original_mapping

    def test_legacy_processing_direct_item_job_is_migrated_to_cmd(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_queue_dir = bridge_client.QUEUE_DIR
            try:
                bridge_client.QUEUE_DIR = tmpdir
                processing = Path(tmpdir, "recv-000016-item-7770000-cmd-00.processing")
                processing.write_text(
                    "give weapon/player/heavy_cannon\n", encoding="utf-8"
                )

                bridge_client.migrate_direct_item_command_jobs()

                migrated = Path(tmpdir, "recv-000016-item-7770000-cmd-00.cmd")
                self.assertTrue(migrated.exists())
                self.assertFalse(processing.exists())
                self.assertEqual(
                    migrated.read_text(encoding="utf-8"),
                    "ai_ScriptCmdEnt ap_rpc_v3_7770000 activate\n",
                )
            finally:
                bridge_client.QUEUE_DIR = original_queue_dir

    def test_legacy_cmd_list_item_job_uses_child_entity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_queue_dir = bridge_client.QUEUE_DIR
            try:
                bridge_client.QUEUE_DIR = tmpdir
                queued = Path(tmpdir, "recv-000007-item-7770012-cmd-01.cmd")
                queued.write_text(
                    "give weapon/player/equipment_flame_belch\n", encoding="utf-8"
                )

                bridge_client.migrate_direct_item_command_jobs()

                self.assertTrue(queued.exists())
                self.assertEqual(
                    queued.read_text(encoding="utf-8"),
                    "ai_ScriptCmdEnt ap_rpc_v3_7770012_1 activate\n",
                )
            finally:
                bridge_client.QUEUE_DIR = original_queue_dir

    def test_manual_diagnostic_command_is_not_migrated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_queue_dir = bridge_client.QUEUE_DIR
            try:
                bridge_client.QUEUE_DIR = tmpdir
                manual = Path(tmpdir, "manual-test.cmd")
                manual.write_text("give weapon/player/heavy_cannon\n", encoding="utf-8")

                bridge_client.migrate_direct_item_command_jobs()

                self.assertEqual(
                    manual.read_text(encoding="utf-8"),
                    "give weapon/player/heavy_cannon\n",
                )
            finally:
                bridge_client.QUEUE_DIR = original_queue_dir

    def test_named_weapon_items_spool_expected_entities(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_queue_dir = bridge_client.QUEUE_DIR
            original_state_file = bridge_client.CLIENT_STATE_FILE
            try:
                bridge_client.QUEUE_DIR = tmpdir
                bridge_client.CLIENT_STATE_FILE = Path(tmpdir, "client_state.json")
                ctx = self._make_item_context()

                expectations = {
                    7770000: "ai_ScriptCmdEnt ap_rpc_v3_7770000 activate\n",
                    7770001: "ai_ScriptCmdEnt ap_rpc_v3_7770001 activate\n",
                    7770004: "ai_ScriptCmdEnt ap_rpc_v3_7770004 activate\n",
                    7770045: "ai_ScriptCmdEnt ap_rpc_v3_7770045 activate\n",
                }
                for receive_index, item_id in enumerate(expectations):
                    self.assertTrue(ctx.spool_item_commands(item_id, receive_index)[0])

                for receive_index, (item_id, expected) in enumerate(expectations.items()):
                    path = Path(
                        tmpdir,
                        f"recv-{receive_index:06d}-item-{item_id}-cmd-00.cmd",
                    )
                    self.assertEqual(path.read_text(encoding="utf-8"), expected)
            finally:
                bridge_client.QUEUE_DIR = original_queue_dir
                bridge_client.CLIENT_STATE_FILE = original_state_file

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
