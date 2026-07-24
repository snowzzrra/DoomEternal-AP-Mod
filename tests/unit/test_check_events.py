import asyncio
import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[2]
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


async def _async_append(target, value):
    target.append(value)


class StickySaveMetricTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _record(count, satisfied, unlocked, entry=None):
        entry = entry or bridge_client.STICKY_MASTERY_ENTRY
        signal = entry["signal"]
        unlockable = signal["unlockable"].encode("ascii")
        stat = signal["rule_0_statname"].encode("ascii")
        def uint(value):
            width = max(1, (value.bit_length() + 7) // 8)
            return bytes([width]) + value.to_bytes(width, "little")
        return (
            b"UnlockableManager_0_1_2\x00idUnlockableManager_2\x00"
            + bytes([len(unlockable) * 2]) + unlockable
            + b"\x0e\x0c$numUnlockableRules" + uint(signal["numUnlockableRules"])
            + b" rule_0_satisfied" + (b"\x0c" if satisfied else b"\x0b")
            + b" rule_0_statCount" + uint(count)
            + b"&rule_0_statDuration" + uint(signal["rule_0_statDuration"])
            + b"\x1erule_0_statname\x0a" + bytes([len(stat) * 2])
            + stat
            + b"(unlockableIsUnlocked" + (b"\x0c" if unlocked else b"\x0b")
        )

    @staticmethod
    def _mission_challenge_entries(mission_key="e1m3"):
        """Return the three mission-challenge entries for one mission."""
        prefix = f"mission_challenge/{mission_key}/"
        entries = tuple(
            entry
            for entry in bridge_client.MISSION_CHALLENGE_ENTRIES
            if entry["signal"]["unlockable"].startswith(prefix)
        )
        if len(entries) != 3:
            raise AssertionError(
                f"expected three mission challenges for {mission_key}, got {len(entries)}"
            )
        return entries

    @classmethod
    def _mission_challenge_records(cls, completion_bits, mission_key="e1m3"):
        """Build all three exact records for one mission; statCount is irrelevant."""
        records = {}
        for complete, entry in zip(
            completion_bits,
            cls._mission_challenge_entries(mission_key),
            strict=True,
        ):
            signal = entry["signal"]
            records[signal["unlockable"]] = {
                "numUnlockableRules": signal["numUnlockableRules"],
                "rule_0_statname": signal["rule_0_statname"],
                "rule_0_statCount": 0 if complete else 999,
                "rule_0_statDuration": signal["rule_0_statDuration"],
                "rule_0_satisfied": complete,
                "unlockableIsUnlocked": complete,
            }
        return records

    def test_reader_decodes_structured_sticky_record(self):
        payload = self._record(25, True, True)
        self.assertEqual(
            bridge_client.read_sticky_mastery_record(payload),
            {
                "rule_0_statname": "STAT_WEAKPOINT_DISABLE_ARACHNOTRON_STICKYBOMB",
                "rule_0_statCount": 25,
                "rule_0_satisfied": True,
                "unlockableIsUnlocked": True,
            },
        )

    def test_each_native_mastery_fixture_requires_its_own_complete_record(self):
        for entry in bridge_client.WEAPON_MASTERY_ENTRIES:
            signal = entry["signal"]
            incomplete = bridge_client.read_weapon_mastery_records(
                self._record(signal["rule_0_statCount"] - 1, False, False, entry)
            )
            self.assertIn(signal["unlockable"], incomplete)
            ctx = bridge_client.DoomEternalContext(None, None)
            ctx.item_state_ready = True
            ctx.session_state = {}
            ctx.persist_session_state = lambda: None
            ctx.observe_weapon_masteries(incomplete, "incomplete-native-fixture")
            self.assertFalse(ctx.weapon_masteries_observed[signal["unlockable"]])

            complete = bridge_client.read_weapon_mastery_records(
                self._record(signal["rule_0_statCount"], True, True, entry)
            )
            ctx.observe_weapon_masteries(complete, "complete-native-fixture")
            self.assertTrue(ctx.weapon_masteries_observed[signal["unlockable"]])
            self.assertEqual(
                {
                    unlockable
                    for unlockable, observed in ctx.weapon_masteries_observed.items()
                    if observed
                },
                {signal["unlockable"]},
            )

    async def test_one_mastery_can_only_send_its_own_location(self):
        entry = bridge_client.WEAPON_MASTERY_ENTRIES[4]
        ctx = bridge_client.DoomEternalContext(None, None)
        ctx.item_state_ready = True
        ctx.runtime_observers_frozen = False
        ctx.session_state = {}
        ctx.persist_session_state = lambda: None
        ctx.observe_weapon_masteries(
            bridge_client.read_weapon_mastery_records(
                self._record(entry["signal"]["rule_0_statCount"], True, True, entry)
            ),
            "heat-blast-complete",
        )

        class Socket:
            closed = False

        ctx.server = types.SimpleNamespace(socket=Socket())
        ctx.server_locations = {mastery["location_id"] for mastery in bridge_client.WEAPON_MASTERY_ENTRIES}
        ctx.checked_locations = set()
        ctx.locations_checked = set()
        sent = []

        async def send_msgs(messages):
            sent.append(messages)

        ctx.send_msgs = send_msgs
        await ctx.check_weapon_mastery_locations()
        self.assertEqual(sent, [[{"cmd": "LocationChecks", "locations": [entry["location_id"]]}]])

    def test_ap_five_spot_ownership_at_24_does_not_trigger_location(self):
        ctx = bridge_client.DoomEternalContext(None, None)
        ctx.items_received = [types.SimpleNamespace(item=7770070)]
        ctx.observe_sticky_mastery(
            bridge_client.read_sticky_mastery_record(self._record(24, False, False)),
            "synthetic-save",
        )
        self.assertFalse(ctx.sticky_mastery_observed)

    async def test_24_does_not_send_25_retries_once_and_server_dedupes_reload(self):
        ctx = bridge_client.DoomEternalContext(None, None)
        ctx.item_state_ready = True
        ctx.runtime_observers_frozen = False
        ctx.session_state = {}
        ctx.client_state = {"version": 1, "sessions": {}}
        ctx.state_key = "seed:1:2"
        ctx.persist_session_state = lambda: None
        ctx.observe_sticky_mastery(
            bridge_client.read_sticky_mastery_record(self._record(24, False, False)),
            "slot2-24",
        )
        self.assertFalse(ctx.sticky_mastery_observed)

        class Socket:
            closed = False

        ctx.server = types.SimpleNamespace(socket=Socket())
        ctx.server_locations = {7770125}
        ctx.checked_locations = set()
        ctx.locations_checked = set()
        sent = []

        async def send_msgs(messages):
            sent.append(messages)
            if len(sent) == 1:
                raise RuntimeError("temporary disconnect")

        ctx.send_msgs = send_msgs
        await ctx.check_sticky_mastery_location()
        self.assertEqual(sent, [])

        ctx.observe_sticky_mastery(
            bridge_client.read_sticky_mastery_record(self._record(25, True, True)),
            "slot2-25",
        )
        self.assertTrue(ctx.sticky_mastery_observed)
        await ctx.check_sticky_mastery_location()
        self.assertEqual(ctx.locations_checked, set())
        await ctx.check_sticky_mastery_location()
        await ctx.check_sticky_mastery_location()
        self.assertEqual(len(sent), 2)
        self.assertEqual(ctx.locations_checked, {7770125})
        self.assertEqual(
            sent[-1], [{"cmd": "LocationChecks", "locations": [7770125]}]
        )
        ctx.locations_checked = set()
        ctx.checked_locations = {7770125}
        await ctx.check_sticky_mastery_location()
        self.assertEqual(len(sent), 2)

    def test_primary_reader_reselects_newest_slot_and_ignores_backups(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = Path(directory)
            slot0 = remote / "GAME-AUTOSAVE0"
            slot2 = remote / "GAME-AUTOSAVE2"
            backup = remote / "GAME-AUTOSAVE9_BACKUP"
            slot0.mkdir()
            slot2.mkdir()
            backup.mkdir()
            slot0_save = slot0 / "game_duration.dat"
            slot2_save = slot2 / "game_duration.dat"
            backup_save = backup / "game_duration.dat"
            slot0_save.write_bytes(b"slot0")
            slot2_save.write_bytes(b"slot2")
            backup_save.write_bytes(b"backup")
            os.utime(slot0_save, ns=(100, 100))
            os.utime(slot2_save, ns=(50, 50))
            os.utime(backup_save, ns=(1000, 1000))
            original_remote = bridge_client.STEAM_REMOTE_DIR
            original_id = bridge_client.STEAM_ID3
            try:
                bridge_client.STEAM_REMOTE_DIR = remote
                bridge_client.STEAM_ID3 = 160032537
                first = bridge_client.mastery_save_selection()
                self.assertEqual(first.slot_directory, "GAME-AUTOSAVE0")
                self.assertEqual(first.path, slot0_save.resolve())
                self.assertEqual(first.cache_key, (
                    "GAME-AUTOSAVE0", str(slot0_save.resolve()), 100,
                ))

                os.utime(slot2_save, ns=(200, 200))
                second = bridge_client.mastery_save_selection()
                self.assertEqual(second.slot_directory, "GAME-AUTOSAVE2")
                self.assertEqual(second.path, slot2_save.resolve())
                self.assertEqual(
                    bridge_client.sticky_mastery_save_file(), slot2_save.resolve()
                )
            finally:
                bridge_client.STEAM_REMOTE_DIR = original_remote
                bridge_client.STEAM_ID3 = original_id

    @staticmethod
    def _gameplay_evidence(
        epoch,
        slot,
        map_name="game/sp/e1m1_intro/e1m1_intro",
        provisional=False,
    ):
        return bridge_client.GameplaySaveEvidence(
            "gameplay", epoch, slot, map_name, provisional
        )

    @staticmethod
    def _details_for(selected, map_name="game/sp/e1m1_intro/e1m1_intro"):
        return {
            "mapName": map_name,
            "_path": str(selected.path.parent / "game.details"),
            "_mtime_ns": selected.mtime_ns,
        }

    async def test_active_fresh_slot_has_no_false_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = Path(directory)
            slot0 = remote / "GAME-AUTOSAVE0"
            slot0.mkdir()
            save = slot0 / "game_duration.dat"
            save.write_bytes(b"fresh")
            evidence = self._gameplay_evidence(1, "GAME-AUTOSAVE0")
            incomplete = bridge_client.read_weapon_mastery_records(
                self._record(24, False, False)
            )
            with (
                patch.object(bridge_client, "STEAM_REMOTE_DIR", remote),
                patch.object(bridge_client, "STEAM_ID3", 160032537),
                patch.object(bridge_client, "read_gameplay_save_evidence", return_value=evidence),
                patch.object(bridge_client, "read_game_details_for_selection", side_effect=self._details_for),
                patch.object(bridge_client, "probe_game_duration", return_value={
                    "mastery_records": incomplete,
                    "mission_challenge_records": {},
                    "checkpoint_death": False,
                }),
            ):
                ctx = bridge_client.DoomEternalContext(None, None)
                ctx.item_state_ready = True
                self.assertTrue(await ctx.check_game_duration_death())
                self.assertEqual(ctx.active_save_slot, "GAME-AUTOSAVE0")
                self.assertFalse(ctx.runtime_observers_frozen)
                self.assertFalse(ctx.sticky_mastery_observed)

    async def test_delete_inactive_100_percent_save_at_menu_sends_zero_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = Path(directory)
            slot0 = remote / "GAME-AUTOSAVE0"
            slot2 = remote / "GAME-AUTOSAVE2"
            slot0.mkdir()
            slot2.mkdir()
            slot0_save = slot0 / "game_duration.dat"
            slot2_save = slot2 / "game_duration.dat"
            slot0_save.write_bytes(b"fresh")
            slot2_save.write_bytes(b"100-percent")
            os.utime(slot0_save, ns=(200, 200))
            os.utime(slot2_save, ns=(100, 100))
            gameplay = self._gameplay_evidence(1, "GAME-AUTOSAVE0")
            menu = bridge_client.GameplaySaveEvidence("menu", 2, "", "")
            evidence = [gameplay, menu]
            probe_calls = []
            with (
                patch.object(bridge_client, "STEAM_REMOTE_DIR", remote),
                patch.object(bridge_client, "STEAM_ID3", 160032537),
                patch.object(bridge_client, "read_gameplay_save_evidence", side_effect=lambda: evidence[0]),
                patch.object(bridge_client, "read_game_details_for_selection", side_effect=self._details_for),
                patch.object(bridge_client, "probe_game_duration", side_effect=lambda path: probe_calls.append(path) or {
                    "mastery_records": {}, "mission_challenge_records": {}, "checkpoint_death": False,
                }),
            ):
                ctx = bridge_client.DoomEternalContext(None, None)
                await ctx.check_game_duration_death()
                evidence[0] = menu
                slot2_save.write_bytes(b"deleted-save-metadata-rewrite")
                os.utime(slot2_save, ns=(300, 300))
                await ctx.check_game_duration_death()
                self.assertEqual(ctx.active_save_slot, "GAME-AUTOSAVE0")
                self.assertTrue(ctx.runtime_observers_frozen)
                self.assertEqual(probe_calls, [slot0_save.resolve()])

    async def test_delete_currently_selected_save_at_menu_sends_zero_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = Path(directory)
            slot0 = remote / "GAME-AUTOSAVE0"
            slot0.mkdir()
            save = slot0 / "game_duration.dat"
            save.write_bytes(b"fresh")
            evidence = [self._gameplay_evidence(1, "GAME-AUTOSAVE0")]
            probe_calls = []
            with (
                patch.object(bridge_client, "STEAM_REMOTE_DIR", remote),
                patch.object(bridge_client, "STEAM_ID3", 160032537),
                patch.object(bridge_client, "read_gameplay_save_evidence", side_effect=lambda: evidence[0]),
                patch.object(bridge_client, "read_game_details_for_selection", side_effect=self._details_for),
                patch.object(bridge_client, "probe_game_duration", side_effect=lambda path: probe_calls.append(path) or {
                    "mastery_records": {}, "mission_challenge_records": {}, "checkpoint_death": False,
                }),
            ):
                ctx = bridge_client.DoomEternalContext(None, None)
                await ctx.check_game_duration_death()
                evidence[0] = bridge_client.GameplaySaveEvidence("menu", 2, "", "")
                save.unlink()
                await ctx.check_game_duration_death()
                self.assertTrue(ctx.runtime_observers_frozen)
                self.assertEqual(probe_calls, [save.resolve()])

    def test_newest_unrelated_slot_mtime_does_not_switch_active_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = Path(directory)
            for slot in ("GAME-AUTOSAVE0", "GAME-AUTOSAVE2"):
                path = remote / slot
                path.mkdir()
                (path / "game_duration.dat").write_bytes(slot.encode())
            slot0_save = remote / "GAME-AUTOSAVE0" / "game_duration.dat"
            slot2_save = remote / "GAME-AUTOSAVE2" / "game_duration.dat"
            os.utime(slot0_save, ns=(200, 200))
            os.utime(slot2_save, ns=(100, 100))
            evidence = self._gameplay_evidence(1, "GAME-AUTOSAVE0")
            with (
                patch.object(bridge_client, "STEAM_REMOTE_DIR", remote),
                patch.object(bridge_client, "STEAM_ID3", 160032537),
                patch.object(bridge_client, "read_gameplay_save_evidence", return_value=evidence),
                patch.object(bridge_client, "read_game_details_for_selection", side_effect=self._details_for),
            ):
                ctx = bridge_client.DoomEternalContext(None, None)
                ctx.update_save_slot_lifecycle()
                os.utime(slot2_save, ns=(300, 300))
                selected = ctx.update_save_slot_lifecycle()
                self.assertEqual(selected.slot_directory, "GAME-AUTOSAVE0")
                self.assertEqual(ctx.active_save_slot, "GAME-AUTOSAVE0")

    def test_menu_candidate_and_rejection_logs_do_not_poll_spam(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = Path(directory)
            slot0 = remote / "GAME-AUTOSAVE0"
            slot0.mkdir()
            save = slot0 / "game_duration.dat"
            save.write_bytes(b"candidate")
            menu = bridge_client.GameplaySaveEvidence("menu", 2, "", "")
            lines = []
            logger = types.SimpleNamespace(
                info=lambda template, *args: lines.append(template % args),
                warning=lambda *args, **kwargs: None,
                error=lambda *args, **kwargs: None,
            )
            with (
                patch.object(bridge_client, "STEAM_REMOTE_DIR", remote),
                patch.object(bridge_client, "STEAM_ID3", 160032537),
                patch.object(bridge_client, "read_gameplay_save_evidence", return_value=menu),
                patch.object(bridge_client, "logger", logger),
            ):
                ctx = bridge_client.DoomEternalContext(None, None)
                ctx.update_save_slot_lifecycle()
                ctx.update_save_slot_lifecycle()
            self.assertEqual(sum("SAVE_SLOT_CANDIDATE" in line for line in lines), 1)
            self.assertEqual(sum("SAVE_SLOT_REJECTED" in line for line in lines), 1)
            self.assertIn("reason=menu", lines[-1])

    def test_map_mismatch_rejects_gameplay_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = Path(directory)
            slot0 = remote / "GAME-AUTOSAVE0"
            slot0.mkdir()
            (slot0 / "game_duration.dat").write_bytes(b"candidate")
            evidence = self._gameplay_evidence(3, "GAME-AUTOSAVE0", "game/hub/hub")
            wrong_map = "game/sp/e1m2_battle/e1m2_battle"
            with (
                patch.object(bridge_client, "STEAM_REMOTE_DIR", remote),
                patch.object(bridge_client, "STEAM_ID3", 160032537),
                patch.object(bridge_client, "read_gameplay_save_evidence", return_value=evidence),
                patch.object(bridge_client, "read_game_details_for_selection", side_effect=lambda selected: self._details_for(selected, wrong_map)),
            ):
                ctx = bridge_client.DoomEternalContext(None, None)
                self.assertIsNone(ctx.update_save_slot_lifecycle())
                self.assertTrue(ctx.runtime_observers_frozen)
                self.assertIsNone(ctx.active_save_slot)

    async def test_recreated_newer_slot_freezes_old_observers_until_e1m1_proves_it(self):
        """A new campaign must not inherit checks from an older 100% slot."""
        entry = bridge_client.STICKY_MASTERY_ENTRY
        complete = bridge_client.read_weapon_mastery_records(
            self._record(25, True, True)
        )
        with tempfile.TemporaryDirectory() as directory:
            remote = Path(directory)
            slot0 = remote / "GAME-AUTOSAVE0"
            slot2 = remote / "GAME-AUTOSAVE2"
            slot0.mkdir()
            slot2.mkdir()
            old_save = slot0 / "game_duration.dat"
            new_save = slot2 / "game_duration.dat"
            old_save.write_bytes(b"old-100-percent")
            new_save.write_bytes(b"recreated-new-campaign")
            os.utime(old_save, ns=(100, 100))
            os.utime(new_save, ns=(200, 200))
            evidence = [self._gameplay_evidence(1, "GAME-AUTOSAVE0")]
            sent = []

            class Socket:
                closed = False

            with (
                patch.object(bridge_client, "STEAM_REMOTE_DIR", remote),
                patch.object(bridge_client, "STEAM_ID3", 160032537),
                patch.object(bridge_client, "read_gameplay_save_evidence", side_effect=lambda: evidence[0]),
                patch.object(bridge_client, "read_game_details_for_selection", side_effect=self._details_for),
                patch.object(bridge_client, "probe_game_duration", side_effect=lambda path: {
                    "mastery_records": complete if path == old_save.resolve() else {},
                    "mission_challenge_records": {},
                    "checkpoint_death": False,
                }),
            ):
                ctx = bridge_client.DoomEternalContext(None, None)
                ctx.item_state_ready = True
                ctx.persist_session_state = lambda: None
                ctx.server = types.SimpleNamespace(socket=Socket())
                ctx.server_locations = {entry["location_id"]}
                ctx.checked_locations = set()
                ctx.locations_checked = set()
                ctx.send_msgs = lambda messages: _async_append(sent, messages)

                await ctx.check_game_duration_death()
                evidence[0] = None
                self.assertTrue(await ctx.check_game_duration_death())
                await ctx.check_sticky_mastery_location()
                self.assertTrue(ctx.runtime_observers_frozen)
                self.assertEqual(ctx.active_save_slot, "GAME-AUTOSAVE0")
                self.assertEqual(sent, [])

                evidence[0] = self._gameplay_evidence(2, "GAME-AUTOSAVE2")
                await ctx.check_game_duration_death()
                self.assertFalse(ctx.runtime_observers_frozen)
                self.assertEqual(ctx.active_save_slot, "GAME-AUTOSAVE2")
                await ctx.check_sticky_mastery_location()
                self.assertEqual(sent, [])

    def test_provisional_hub_evidence_cannot_promote_an_old_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = Path(directory)
            for slot, mtime in (("GAME-AUTOSAVE0", 100), ("GAME-AUTOSAVE1", 200)):
                path = remote / slot
                path.mkdir()
                save = path / "game_duration.dat"
                save.write_bytes(slot.encode())
                os.utime(save, ns=(mtime, mtime))
            evidence = [self._gameplay_evidence(4, "GAME-AUTOSAVE0", "game/hub/hub", True)]
            with (
                patch.object(bridge_client, "STEAM_REMOTE_DIR", remote),
                patch.object(bridge_client, "STEAM_ID3", 160032537),
                patch.object(bridge_client, "read_gameplay_save_evidence", side_effect=lambda: evidence[0]),
                patch.object(bridge_client, "read_game_details_for_selection", side_effect=self._details_for),
            ):
                ctx = bridge_client.DoomEternalContext(None, None)
                self.assertIsNone(ctx.update_save_slot_lifecycle())
                self.assertTrue(ctx.runtime_observers_frozen)
                self.assertIsNone(ctx.active_save_slot)

                evidence[0] = self._gameplay_evidence(6, "GAME-AUTOSAVE1")
                selected = ctx.update_save_slot_lifecycle()
                self.assertEqual(selected.slot_directory, "GAME-AUTOSAVE1")
                self.assertFalse(ctx.runtime_observers_frozen)

    async def test_conclusive_hub_switch_promotes_and_runs_challenge_observers(self):
        challenge = self._mission_challenge_entries("e1m3")[0]
        with tempfile.TemporaryDirectory() as directory:
            remote = Path(directory)
            slots = {}
            for slot, mtime in (("GAME-AUTOSAVE0", 200), ("GAME-AUTOSAVE2", 100)):
                slot_dir = remote / slot
                slot_dir.mkdir()
                save = slot_dir / "game_duration.dat"
                save.write_bytes(slot.encode())
                os.utime(save, ns=(mtime, mtime))
                slots[slot] = save.resolve()
            evidence = self._gameplay_evidence(10, "GAME-AUTOSAVE0", "game/hub/hub")
            sent = []

            class Socket:
                closed = False

            with (
                patch.object(bridge_client, "STEAM_REMOTE_DIR", remote),
                patch.object(bridge_client, "STEAM_ID3", 160032537),
                patch.object(bridge_client, "read_gameplay_save_evidence", return_value=evidence),
                patch.object(
                    bridge_client, "read_game_details_for_selection",
                    side_effect=lambda selected: self._details_for(selected, "game/hub/hub"),
                ),
                patch.object(bridge_client, "probe_game_duration", side_effect=lambda path: {
                    "mastery_records": {},
                    "mission_challenge_records": (
                        self._mission_challenge_records((True, False, False))
                        if path == slots["GAME-AUTOSAVE0"] else {}
                    ),
                    "checkpoint_death": False,
                }),
            ):
                ctx = bridge_client.DoomEternalContext(None, None)
                ctx.activate_save_selection(bridge_client.PrimarySaveSelection(
                    "GAME-AUTOSAVE2", slots["GAME-AUTOSAVE2"], 100
                ))
                ctx.active_gameplay_epoch = 9
                ctx.item_state_ready = True
                ctx.persist_session_state = lambda: None
                ctx.server = types.SimpleNamespace(socket=Socket())
                ctx.server_locations = {challenge["location_id"]}
                ctx.checked_locations = set()
                ctx.locations_checked = set()
                ctx.send_msgs = lambda messages: _async_append(sent, messages)

                await ctx.check_game_duration_death()
                await ctx.check_mission_challenge_locations()

            self.assertEqual(ctx.active_save_slot, "GAME-AUTOSAVE0")
            self.assertFalse(ctx.runtime_observers_frozen)
            self.assertEqual(sent, [[{
                "cmd": "LocationChecks", "locations": [challenge["location_id"]]
            }]])

    def test_recreated_middle_slot_invalidates_its_previous_observer_authority(self):
        entry = bridge_client.STICKY_MASTERY_ENTRY
        with tempfile.TemporaryDirectory() as directory:
            remote = Path(directory)
            slot1 = remote / "GAME-AUTOSAVE1"
            slot1.mkdir()
            save = slot1 / "game_duration.dat"
            save.write_bytes(b"old-slot-2")
            os.utime(save, ns=(100, 100))
            evidence = [self._gameplay_evidence(1, "GAME-AUTOSAVE1")]
            with (
                patch.object(bridge_client, "STEAM_REMOTE_DIR", remote),
                patch.object(bridge_client, "STEAM_ID3", 160032537),
                patch.object(bridge_client, "read_gameplay_save_evidence", side_effect=lambda: evidence[0]),
                patch.object(bridge_client, "read_game_details_for_selection", side_effect=self._details_for),
            ):
                ctx = bridge_client.DoomEternalContext(None, None)
                ctx.update_save_slot_lifecycle()
                ctx.weapon_masteries_observed[entry["signal"]["unlockable"]] = True
                ctx.save_slot_observations["GAME-AUTOSAVE1"]["weapon_masteries"] = (
                    ctx.weapon_masteries_observed
                )
                ctx.checked_locations = {entry["location_id"]}

                save.write_bytes(b"recreated-slot-2")
                os.utime(save, ns=(200, 200))
                evidence[0] = self._gameplay_evidence(2, "GAME-AUTOSAVE1")
                selected = ctx.update_save_slot_lifecycle()

                self.assertEqual(selected.slot_directory, "GAME-AUTOSAVE1")
                self.assertFalse(
                    ctx.weapon_masteries_observed[entry["signal"]["unlockable"]]
                )
                self.assertEqual(ctx.checked_locations, {entry["location_id"]})

    async def test_intentional_100_percent_load_promotes_and_sends_completed_check(self):
        entry = bridge_client.STICKY_MASTERY_ENTRY
        complete = bridge_client.read_weapon_mastery_records(
            self._record(25, True, True)
        )
        with tempfile.TemporaryDirectory() as directory:
            remote = Path(directory)
            slot2 = remote / "GAME-AUTOSAVE2"
            slot2.mkdir()
            (slot2 / "game_duration.dat").write_bytes(b"100-percent")
            evidence = self._gameplay_evidence(7, "GAME-AUTOSAVE2")
            with (
                patch.object(bridge_client, "STEAM_REMOTE_DIR", remote),
                patch.object(bridge_client, "STEAM_ID3", 160032537),
                patch.object(bridge_client, "read_gameplay_save_evidence", return_value=evidence),
                patch.object(bridge_client, "read_game_details_for_selection", side_effect=self._details_for),
                patch.object(bridge_client, "probe_game_duration", return_value={
                    "mastery_records": complete,
                    "mission_challenge_records": {},
                    "checkpoint_death": False,
                }),
            ):
                ctx = bridge_client.DoomEternalContext(None, None)
                ctx.item_state_ready = True
                ctx.persist_session_state = lambda: None
                await ctx.check_game_duration_death()
                class Socket:
                    closed = False
                ctx.server = types.SimpleNamespace(socket=Socket())
                ctx.server_locations = {entry["location_id"]}
                ctx.checked_locations = set()
                ctx.locations_checked = set()
                sent = []
                ctx.send_msgs = lambda messages: _async_append(sent, messages)
                await ctx.check_sticky_mastery_location()
                self.assertEqual(sent, [[{"cmd": "LocationChecks", "locations": [entry["location_id"]]}]])

    async def test_natural_mastery_24_to_25_durable_save_sends_once(self):
        entry = bridge_client.STICKY_MASTERY_ENTRY
        incomplete = bridge_client.read_weapon_mastery_records(
            self._record(24, False, False)
        )
        complete = bridge_client.read_weapon_mastery_records(
            self._record(25, True, True)
        )
        with tempfile.TemporaryDirectory() as directory:
            remote = Path(directory)
            slot0 = remote / "GAME-AUTOSAVE0"
            slot0.mkdir()
            save = slot0 / "game_duration.dat"
            save.write_bytes(b"24")
            os.utime(save, ns=(100, 100))
            records = [incomplete]
            evidence = self._gameplay_evidence(4, "GAME-AUTOSAVE0")
            with (
                patch.object(bridge_client, "STEAM_REMOTE_DIR", remote),
                patch.object(bridge_client, "STEAM_ID3", 160032537),
                patch.object(bridge_client, "read_gameplay_save_evidence", return_value=evidence),
                patch.object(bridge_client, "read_game_details_for_selection", side_effect=self._details_for),
                patch.object(bridge_client, "probe_game_duration", side_effect=lambda path: {
                    "mastery_records": records[0],
                    "mission_challenge_records": {},
                    "checkpoint_death": False,
                }),
            ):
                ctx = bridge_client.DoomEternalContext(None, None)
                ctx.item_state_ready = True
                ctx.persist_session_state = lambda: None
                class Socket:
                    closed = False
                ctx.server = types.SimpleNamespace(socket=Socket())
                ctx.server_locations = {entry["location_id"]}
                ctx.checked_locations = set()
                ctx.locations_checked = set()
                sent = []
                ctx.send_msgs = lambda messages: _async_append(sent, messages)

                await ctx.check_game_duration_death()
                await ctx.check_sticky_mastery_location()
                self.assertEqual(sent, [])

                records[0] = complete
                save.write_bytes(b"25")
                os.utime(save, ns=(200, 200))
                await ctx.check_game_duration_death()
                await ctx.check_sticky_mastery_location()
                await ctx.check_sticky_mastery_location()
                self.assertEqual(sent, [[{
                    "cmd": "LocationChecks",
                    "locations": [entry["location_id"]],
                }]])

    async def test_natural_challenge_durable_save_sends_once(self):
        entry = bridge_client.MISSION_CHALLENGE_ENTRIES[0]
        incomplete = bridge_client.read_mission_challenge_records(
            self._record(0, False, False, entry)
        )
        complete = bridge_client.read_mission_challenge_records(
            self._record(1, True, True, entry)
        )
        with tempfile.TemporaryDirectory() as directory:
            remote = Path(directory)
            slot2 = remote / "GAME-AUTOSAVE2"
            slot2.mkdir()
            save = slot2 / "game_duration.dat"
            save.write_bytes(b"incomplete")
            os.utime(save, ns=(100, 100))
            records = [incomplete]
            evidence = self._gameplay_evidence(9, "GAME-AUTOSAVE2")
            with (
                patch.object(bridge_client, "STEAM_REMOTE_DIR", remote),
                patch.object(bridge_client, "STEAM_ID3", 160032537),
                patch.object(bridge_client, "read_gameplay_save_evidence", return_value=evidence),
                patch.object(bridge_client, "read_game_details_for_selection", side_effect=self._details_for),
                patch.object(bridge_client, "probe_game_duration", side_effect=lambda path: {
                    "mastery_records": {},
                    "mission_challenge_records": records[0],
                    "checkpoint_death": False,
                }),
            ):
                ctx = bridge_client.DoomEternalContext(None, None)
                ctx.item_state_ready = True
                ctx.persist_session_state = lambda: None
                class Socket:
                    closed = False
                ctx.server = types.SimpleNamespace(socket=Socket())
                ctx.server_locations = {entry["location_id"]}
                ctx.checked_locations = set()
                ctx.locations_checked = set()
                sent = []
                ctx.send_msgs = lambda messages: _async_append(sent, messages)

                await ctx.check_game_duration_death()
                await ctx.check_mission_challenge_locations()
                self.assertEqual(sent, [])

                records[0] = complete
                save.write_bytes(b"complete")
                os.utime(save, ns=(200, 200))
                await ctx.check_game_duration_death()
                await ctx.check_mission_challenge_locations()
                await ctx.check_mission_challenge_locations()
                self.assertEqual(sent, [[{
                    "cmd": "LocationChecks",
                    "locations": [entry["location_id"]],
                }]])

    def test_gameplay_switch_across_all_three_slots_requires_new_epoch(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = Path(directory)
            for slot in (
                "GAME-AUTOSAVE0", "GAME-AUTOSAVE1", "GAME-AUTOSAVE2"
            ):
                path = remote / slot
                path.mkdir()
                (path / "game_duration.dat").write_bytes(slot.encode())
            evidence = [self._gameplay_evidence(1, "GAME-AUTOSAVE0")]
            with (
                patch.object(bridge_client, "STEAM_REMOTE_DIR", remote),
                patch.object(bridge_client, "STEAM_ID3", 160032537),
                patch.object(bridge_client, "read_gameplay_save_evidence", side_effect=lambda: evidence[0]),
                patch.object(bridge_client, "read_game_details_for_selection", side_effect=self._details_for),
            ):
                ctx = bridge_client.DoomEternalContext(None, None)
                self.assertEqual(ctx.update_save_slot_lifecycle().slot_directory, "GAME-AUTOSAVE0")
                evidence[0] = self._gameplay_evidence(2, "GAME-AUTOSAVE1")
                self.assertEqual(ctx.update_save_slot_lifecycle().slot_directory, "GAME-AUTOSAVE1")
                evidence[0] = self._gameplay_evidence(3, "GAME-AUTOSAVE2")
                self.assertEqual(ctx.update_save_slot_lifecycle().slot_directory, "GAME-AUTOSAVE2")
                evidence[0] = self._gameplay_evidence(4, "GAME-AUTOSAVE0")
                self.assertEqual(ctx.update_save_slot_lifecycle().slot_directory, "GAME-AUTOSAVE0")

    async def test_mastery_predicate_and_deathlink_baseline_follow_slot_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = Path(directory)
            slot0 = remote / "GAME-AUTOSAVE0"
            slot2 = remote / "GAME-AUTOSAVE2"
            slot0.mkdir()
            slot2.mkdir()
            slot0_save = slot0 / "game_duration.dat"
            slot2_save = slot2 / "game_duration.dat"
            slot0_save.write_bytes(b"slot0")
            slot2_save.write_bytes(b"slot2")
            os.utime(slot0_save, ns=(100, 100))
            os.utime(slot2_save, ns=(50, 50))

            complete = bridge_client.read_weapon_mastery_records(
                self._record(25, True, True)
            )
            incomplete = bridge_client.read_weapon_mastery_records(
                self._record(24, False, False)
            )
            responses = {
                slot0_save.resolve(): (complete, False),
                slot2_save.resolve(): (incomplete, True),
            }
            evidence = [self._gameplay_evidence(1, "GAME-AUTOSAVE0")]

            def probe(path):
                records, died = responses[path]
                return {
                    "mastery_records": records,
                    "mission_challenge_records": {},
                    "checkpoint_death": died,
                }

            with (
                patch.object(bridge_client, "STEAM_REMOTE_DIR", remote),
                patch.object(bridge_client, "STEAM_ID3", 160032537),
                patch.object(bridge_client, "read_gameplay_save_evidence", side_effect=lambda: evidence[0]),
                patch.object(bridge_client, "read_game_details_for_selection", side_effect=self._details_for),
                patch.object(bridge_client, "probe_game_duration", side_effect=probe),
            ):
                ctx = bridge_client.DoomEternalContext(None, None)
                reports = []

                async def report():
                    reports.append(ctx.active_save_slot)

                ctx.report_local_death = report
                await ctx.check_game_duration_death()
                self.assertEqual(ctx.active_save_slot, "GAME-AUTOSAVE0")
                self.assertTrue(ctx.sticky_mastery_observed)
                self.assertEqual(reports, [])

                evidence[0] = self._gameplay_evidence(2, "GAME-AUTOSAVE2")
                os.utime(slot2_save, ns=(200, 200))
                await ctx.check_game_duration_death()
                self.assertEqual(ctx.active_save_slot, "GAME-AUTOSAVE2")
                self.assertFalse(ctx.sticky_mastery_observed)
                self.assertEqual(reports, [])
                self.assertEqual(ctx.last_duration_cache_key, (
                    "GAME-AUTOSAVE2", str(slot2_save.resolve()), 200,
                ))

                responses[slot2_save.resolve()] = (incomplete, False)
                os.utime(slot2_save, ns=(300, 300))
                await ctx.check_game_duration_death()
                responses[slot2_save.resolve()] = (incomplete, True)
                os.utime(slot2_save, ns=(400, 400))
                await ctx.check_game_duration_death()
                self.assertEqual(reports, ["GAME-AUTOSAVE2"])

    async def test_mastery_retry_and_server_dedupe_survive_slot_change(self):
        entry = bridge_client.STICKY_MASTERY_ENTRY
        complete = bridge_client.read_weapon_mastery_records(
            self._record(25, True, True)
        )
        ctx = bridge_client.DoomEternalContext(None, None)
        ctx.item_state_ready = True
        ctx.runtime_observers_frozen = False
        ctx.session_state = {}
        ctx.persist_session_state = lambda: None
        ctx.activate_save_selection(bridge_client.PrimarySaveSelection(
            "GAME-AUTOSAVE0", Path("/tmp/GAME-AUTOSAVE0/game_duration.dat"), 1
        ))
        ctx.observe_weapon_masteries(complete, Path("/tmp/GAME-AUTOSAVE0/game_duration.dat"))

        class Socket:
            closed = False

        ctx.server = types.SimpleNamespace(socket=Socket())
        ctx.server_locations = {entry["location_id"]}
        ctx.checked_locations = set()
        ctx.locations_checked = set()
        sent = []

        async def send_msgs(messages):
            sent.append(messages)
            if len(sent) == 1:
                raise RuntimeError("temporary disconnect")

        ctx.send_msgs = send_msgs
        await ctx.check_sticky_mastery_location()
        await ctx.check_sticky_mastery_location()
        self.assertEqual(len(sent), 2)
        self.assertEqual(ctx.locations_checked, {entry["location_id"]})

        ctx.activate_save_selection(bridge_client.PrimarySaveSelection(
            "GAME-AUTOSAVE2", Path("/tmp/GAME-AUTOSAVE2/game_duration.dat"), 2
        ))
        ctx.observe_weapon_masteries(complete, Path("/tmp/GAME-AUTOSAVE2/game_duration.dat"))
        await ctx.check_sticky_mastery_location()
        self.assertEqual(len(sent), 2)

        ctx.locations_checked.clear()
        ctx.checked_locations = {entry["location_id"]}
        await ctx.check_sticky_mastery_location()
        self.assertEqual(len(sent), 2)

        new_seed = bridge_client.DoomEternalContext(None, None)
        new_seed.item_state_ready = True
        new_seed.runtime_observers_frozen = False
        new_seed.session_state = {}
        new_seed.persist_session_state = lambda: None
        new_seed.activate_save_selection(bridge_client.PrimarySaveSelection(
            "GAME-AUTOSAVE2", Path("/tmp/GAME-AUTOSAVE2/game_duration.dat"), 2
        ))
        new_seed.observe_weapon_masteries(
            complete, Path("/tmp/GAME-AUTOSAVE2/game_duration.dat")
        )
        new_seed.server = types.SimpleNamespace(socket=Socket())
        new_seed.server_locations = {entry["location_id"]}
        new_seed.checked_locations = set()
        new_seed.locations_checked = set()
        new_seed_sent = []

        async def new_seed_send(messages):
            new_seed_sent.append(messages)

        new_seed.send_msgs = new_seed_send
        await new_seed.check_sticky_mastery_location()
        self.assertEqual(new_seed_sent, [[{
            "cmd": "LocationChecks", "locations": [entry["location_id"]],
        }]])

    async def test_proven_active_slot_continues_without_new_epoch_and_switches_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = Path(directory)
            slots = {}
            for name, mtime in (("GAME-AUTOSAVE0", 100), ("GAME-AUTOSAVE2", 200)):
                slot = remote / name
                slot.mkdir()
                path = slot / "game_duration.dat"
                path.write_bytes(name.encode())
                os.utime(path, ns=(mtime, mtime))
                slots[name] = path.resolve()

            def complete_masteries():
                return {
                    entry["signal"]["unlockable"]: {
                        "numUnlockableRules": entry["signal"]["numUnlockableRules"],
                        "rule_0_statname": entry["signal"]["rule_0_statname"],
                        "rule_0_statCount": entry["signal"]["rule_0_statCount"],
                        "rule_0_statDuration": entry["signal"]["rule_0_statDuration"],
                        "rule_0_satisfied": True,
                        "unlockableIsUnlocked": True,
                    }
                    for entry in bridge_client.WEAPON_MASTERY_ENTRIES
                }

            armored_rain = next(
                entry
                for entry in self._mission_challenge_entries("e1m3")
                if entry["location_id"] == 7770139
            )
            evidence = [self._gameplay_evidence(1, "GAME-AUTOSAVE2")]
            sent = []
            probe_calls = []

            async def send_msgs(messages):
                sent.extend(messages)

            def probe(path):
                probe_calls.append(path)
                if path == slots["GAME-AUTOSAVE2"]:
                    return {
                        "mastery_records": {},
                        "mission_challenge_records": self._mission_challenge_records(
                            (False, True, False), mission_key="e1m3"
                        ),
                        "checkpoint_death": False,
                    }
                return {
                    "mastery_records": complete_masteries(),
                    "mission_challenge_records": self._mission_challenge_records(
                        (True, True, True), mission_key="e1m3"
                    ),
                    "checkpoint_death": False,
                }

            class Socket:
                closed = False

            with (
                patch.object(bridge_client, "STEAM_REMOTE_DIR", remote),
                patch.object(bridge_client, "STEAM_ID3", 160032537),
                patch.object(bridge_client, "read_gameplay_save_evidence", side_effect=lambda: evidence[0]),
                patch.object(bridge_client, "read_game_details_for_selection", side_effect=self._details_for),
                patch.object(bridge_client, "probe_game_duration", side_effect=probe),
            ):
                ctx = bridge_client.DoomEternalContext(None, None)
                ctx.item_state_ready = True
                ctx.persist_session_state = lambda: None
                ctx.server = types.SimpleNamespace(socket=Socket())
                ctx.server_locations = {
                    *range(7770125, 7770138),
                    7770138, 7770139, 7770140, 7770141,
                }
                ctx.checked_locations = {7770125}
                ctx.locations_checked = set()
                ctx.send_msgs = send_msgs

                await ctx.check_game_duration_death()
                await ctx.check_mission_challenge_locations()
                self.assertEqual(ctx.active_save_slot, "GAME-AUTOSAVE2")
                self.assertIn(
                    {"cmd": "LocationChecks", "locations": [armored_rain["location_id"]]},
                    sent,
                )

                evidence[0] = None
                Path(slots["GAME-AUTOSAVE2"]).write_bytes(b"slot2-checkpoint")
                os.utime(slots["GAME-AUTOSAVE2"], ns=(300, 300))
                await ctx.check_game_duration_death()
                await ctx.check_mission_challenge_locations()
                self.assertEqual(ctx.active_save_slot, "GAME-AUTOSAVE2")
                self.assertFalse(ctx.runtime_observers_frozen)
                self.assertEqual(
                    sum(message.get("locations") == [7770139] for message in sent), 1
                )

                evidence[0] = bridge_client.GameplaySaveEvidence("menu", 2, "", "")
                Path(slots["GAME-AUTOSAVE0"]).write_bytes(b"menu-delete")
                os.utime(slots["GAME-AUTOSAVE0"], ns=(400, 400))
                probe_count = len(probe_calls)
                sent_count = len(sent)
                await ctx.check_game_duration_death()
                await ctx.check_mission_challenge_locations()
                self.assertEqual(ctx.active_save_slot, "GAME-AUTOSAVE2")
                self.assertTrue(ctx.runtime_observers_frozen)
                self.assertEqual(len(probe_calls), probe_count)
                self.assertEqual(len(sent), sent_count)

                evidence[0] = self._gameplay_evidence(3, "GAME-AUTOSAVE0")
                await ctx.check_game_duration_death()
                await ctx.check_weapon_mastery_locations()
                await ctx.check_mission_challenge_locations()
                self.assertEqual(ctx.active_save_slot, "GAME-AUTOSAVE0")
                self.assertFalse(ctx.runtime_observers_frozen)
                sent_ids = {
                    message["locations"][0]
                    for message in sent if message["cmd"] == "LocationChecks"
                }
                self.assertTrue(set(range(7770126, 7770138)) <= sent_ids)
                self.assertNotIn(7770125, sent_ids)
                self.assertTrue({7770138, 7770140, 7770141} <= sent_ids)

                sent_count = len(sent)
                Path(slots["GAME-AUTOSAVE0"]).write_bytes(b"slot0-reload")
                os.utime(slots["GAME-AUTOSAVE0"], ns=(500, 500))
                await ctx.check_game_duration_death()
                await ctx.check_weapon_mastery_locations()
                await ctx.check_mission_challenge_locations()
                self.assertEqual(len(sent), sent_count)

    def test_slot_switch_log_names_old_new_and_full_path(self):
        ctx = bridge_client.DoomEternalContext(None, None)
        lines = []
        original_logger = bridge_client.logger
        bridge_client.logger = types.SimpleNamespace(
            info=lambda template, *args: lines.append(template % args),
            warning=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: None,
        )
        try:
            slot0 = bridge_client.PrimarySaveSelection(
                "GAME-AUTOSAVE0",
                Path("/tmp/primary/GAME-AUTOSAVE0/game_duration.dat"),
                1,
            )
            slot2 = bridge_client.PrimarySaveSelection(
                "GAME-AUTOSAVE2",
                Path("/tmp/primary/GAME-AUTOSAVE2/game_duration.dat"),
                2,
            )
            ctx.activate_save_selection(slot0)
            ctx.activate_save_selection(slot2)
        finally:
            bridge_client.logger = original_logger
        self.assertEqual(lines, [
            "SAVE_SLOT_ACTIVE old=<none> new=GAME-AUTOSAVE0 "
            "path=/tmp/primary/GAME-AUTOSAVE0/game_duration.dat",
            "SAVE_SLOT_ACTIVE old=GAME-AUTOSAVE0 new=GAME-AUTOSAVE2 "
            "path=/tmp/primary/GAME-AUTOSAVE2/game_duration.dat",
        ])

    def test_each_mission_challenge_requires_its_own_durable_record(self):
        for entry in bridge_client.MISSION_CHALLENGE_ENTRIES:
            incomplete = bridge_client.read_mission_challenge_records(
                self._record(0, False, False, entry)
            )
            ctx = bridge_client.DoomEternalContext(None, None)
            ctx.observe_mission_challenges(incomplete, "incomplete-challenge")
            self.assertFalse(
                ctx.mission_challenges_observed[entry["signal"]["unlockable"]]
            )

            persistent_complete = bridge_client.read_mission_challenge_records(
                self._record(0, True, True, entry)
            )
            ctx.observe_mission_challenges(
                persistent_complete, "mission-select-persistent-challenge"
            )
            self.assertTrue(
                ctx.mission_challenges_observed[entry["signal"]["unlockable"]]
            )
            self.assertEqual(
                {
                    unlockable
                    for unlockable, observed in ctx.mission_challenges_observed.items()
                    if observed
                },
                {entry["signal"]["unlockable"]},
            )

    def test_all_challenge_record_combinations_only_complete_at_111(self):
        for bits in (
            (False, False, False), (False, False, True),
            (False, True, False), (False, True, True),
            (True, False, False), (True, False, True),
            (True, True, False), (True, True, True),
        ):
            ctx = bridge_client.DoomEternalContext(None, None)
            ctx.item_state_ready = True
            ctx.runtime_observers_frozen = False
            ctx.session_state = {}
            ctx.persist_session_state = lambda: None
            # Only pass records for the first map (e1m3); DHB entries stay default False
            ctx.observe_mission_challenges(
                self._mission_challenge_records(bits, mission_key="e1m3"),
                "all-combinations",
            )
            e1m3_done = all(bits)
            self.assertEqual(
                ctx.all_mission_challenges_observed,
                {"e1m3": e1m3_done, "e1m4": False, "e2m1": False},
                bits,
            )

    async def test_all_challenges_location_sends_only_for_111(self):
        class Socket:
            closed = False

        for aggregate in bridge_client.ALL_MISSION_CHALLENGES_ENTRIES:
            location_id = aggregate["location_id"]
            chall_unlockables = aggregate["signal"]["unlockables"]
            for bits in (
                (False, False, False), (False, False, True),
                (False, True, False), (False, True, True),
                (True, False, False), (True, False, True),
                (True, True, False), (True, True, True),
            ):
                ctx = bridge_client.DoomEternalContext(None, None)
                ctx.item_state_ready = True
                ctx.runtime_observers_frozen = False
                ctx.session_state = {}
                ctx.persist_session_state = lambda: None
                # Build records matching real signal values so natural_complete fires
                records = {}
                for ul, complete in zip(chall_unlockables, bits, strict=True):
                    entry = bridge_client.MISSION_CHALLENGE_BY_UNLOCKABLE.get(ul)
                    signal = entry["signal"]
                    records[ul] = {
                        "numUnlockableRules": signal["numUnlockableRules"],
                        "rule_0_statname": signal["rule_0_statname"],
                        "rule_0_statCount": 0 if complete else 999,
                        "rule_0_statDuration": signal["rule_0_statDuration"],
                        "rule_0_satisfied": complete,
                        "unlockableIsUnlocked": complete,
                    }
                ctx.observe_mission_challenges(records, "all-location-combinations")
                ctx.server = types.SimpleNamespace(socket=Socket())
                ctx.server_locations = {location_id}
                ctx.checked_locations = set()
                ctx.locations_checked = set()
                sent = []

                async def send_msgs(messages, sent=sent):
                    sent.append(messages)

                ctx.send_msgs = send_msgs
                await ctx.check_all_mission_challenges_location()
                all_done = all(bits)
                self.assertEqual(
                    sent,
                    [[{"cmd": "LocationChecks", "locations": [location_id]}]]
                    if all_done else [],
                    (aggregate["name"], bits),
                )

    async def test_each_completed_challenge_sends_only_its_registered_location(self):
        class Socket:
            closed = False

        for entry in bridge_client.MISSION_CHALLENGE_ENTRIES:
            ctx = bridge_client.DoomEternalContext(None, None)
            ctx.item_state_ready = True
            ctx.runtime_observers_frozen = False
            ctx.session_state = {}
            ctx.persist_session_state = lambda: None
            records = bridge_client.read_mission_challenge_records(
                self._record(1, True, True, entry)
            )
            ctx.observe_mission_challenges(records, "complete-challenge")
            ctx.server = types.SimpleNamespace(socket=Socket())
            ctx.server_locations = {
                challenge["location_id"]
                for challenge in bridge_client.MISSION_CHALLENGE_ENTRIES
            }
            ctx.checked_locations = set()
            ctx.locations_checked = set()
            sent = []

            async def send_msgs(messages, sent=sent):
                sent.append(messages)

            ctx.send_msgs = send_msgs
            await ctx.check_mission_challenge_locations()
            await ctx.check_mission_challenge_locations()
            self.assertEqual(sent, [[{
                "cmd": "LocationChecks", "locations": [entry["location_id"]],
            }]])

    async def test_challenge_retry_reload_and_slot_switch_do_not_duplicate(self):
        entry = bridge_client.MISSION_CHALLENGE_ENTRIES[1]
        records = bridge_client.read_mission_challenge_records(
            self._record(1, True, True, entry)
        )
        ctx = bridge_client.DoomEternalContext(None, None)
        ctx.item_state_ready = True
        ctx.runtime_observers_frozen = False
        ctx.session_state = {}
        ctx.persist_session_state = lambda: None
        ctx.activate_save_selection(bridge_client.PrimarySaveSelection(
            "GAME-AUTOSAVE0", Path("/tmp/GAME-AUTOSAVE0/game_duration.dat"), 1
        ))
        ctx.observe_mission_challenges(
            records, Path("/tmp/GAME-AUTOSAVE0/game_duration.dat")
        )

        class Socket:
            closed = False

        ctx.server = types.SimpleNamespace(socket=Socket())
        ctx.server_locations = {entry["location_id"]}
        ctx.checked_locations = set()
        ctx.locations_checked = set()
        sent = []

        async def send_msgs(messages):
            sent.append(messages)
            if len(sent) == 1:
                raise RuntimeError("temporary disconnect")

        ctx.send_msgs = send_msgs
        await ctx.check_mission_challenge_locations()
        await ctx.check_mission_challenge_locations()
        self.assertEqual(len(sent), 2)

        ctx.activate_save_selection(bridge_client.PrimarySaveSelection(
            "GAME-AUTOSAVE2", Path("/tmp/GAME-AUTOSAVE2/game_duration.dat"), 2
        ))
        ctx.observe_mission_challenges(
            records, Path("/tmp/GAME-AUTOSAVE2/game_duration.dat")
        )
        await ctx.check_mission_challenge_locations()
        self.assertEqual(len(sent), 2)
        ctx.locations_checked.clear()
        ctx.checked_locations = {entry["location_id"]}
        await ctx.check_mission_challenge_locations()
        self.assertEqual(len(sent), 2)

    async def test_all_challenges_retry_server_dedupe_and_slot_switch(self):
        class Socket:
            closed = False

        location_id = next(
            entry["location_id"]
            for entry in bridge_client.ALL_MISSION_CHALLENGES_ENTRIES
            if entry["mission_key"] == "e1m3"
        )
        ctx = bridge_client.DoomEternalContext(None, None)
        ctx.item_state_ready = True
        ctx.runtime_observers_frozen = False
        ctx.session_state = {}
        ctx.state_key = "aggregate-seed:1:2"
        ctx.persist_session_state = lambda: None
        slot0 = bridge_client.PrimarySaveSelection(
            "GAME-AUTOSAVE0", Path("/tmp/GAME-AUTOSAVE0/game_duration.dat"), 1
        )
        slot2 = bridge_client.PrimarySaveSelection(
            "GAME-AUTOSAVE2", Path("/tmp/GAME-AUTOSAVE2/game_duration.dat"), 2
        )
        ctx.activate_save_selection(slot0)
        ctx.observe_mission_challenges(
            self._mission_challenge_records((True, True, True), mission_key="e1m3"),
            slot0.path,
        )
        ctx.server = types.SimpleNamespace(socket=Socket())
        ctx.server_locations = {location_id}
        ctx.checked_locations = set()
        ctx.locations_checked = set()
        sent = []

        async def send_msgs(messages):
            sent.append(messages)
            if len(sent) == 1:
                raise RuntimeError("temporary disconnect")

        ctx.send_msgs = send_msgs
        await ctx.check_all_mission_challenges_location()
        await ctx.check_all_mission_challenges_location()
        self.assertEqual(len(sent), 2)
        self.assertEqual(ctx.locations_checked, {location_id})

        ctx.activate_save_selection(slot2)
        ctx.observe_mission_challenges(
            self._mission_challenge_records((True, True, False), mission_key="e1m3"),
            slot2.path,
        )
        await ctx.check_all_mission_challenges_location()
        self.assertEqual(len(sent), 2)

        ctx.activate_save_selection(slot0)
        ctx.locations_checked.clear()  # Simulate a fresh bridge process/reload.
        ctx.checked_locations = {location_id}
        await ctx.check_all_mission_challenges_location()
        self.assertEqual(len(sent), 2)

    @unittest.skipUnless(
        (ROOT / "build/client/save_death_probe.exe").is_file()
        and Path("/home/guilherme/.local/share/Steam/userdata/160032537/782330/remote/sticky bombs/24 de 25/GAME-AUTOSAVE2/game_duration.dat").is_file()
        and Path("/home/guilherme/.local/share/Steam/userdata/160032537/782330/remote/sticky bombs/25 de 25/GAME-AUTOSAVE2/game_duration.dat").is_file(),
        "provided Sticky snapshots or native probe unavailable",
    )
    def test_parser_accepts_provided_24_and_25_snapshots(self):
        snapshots = {
            24: Path("/home/guilherme/.local/share/Steam/userdata/160032537/782330/remote/sticky bombs/24 de 25/GAME-AUTOSAVE2/game_duration.dat"),
            25: Path("/home/guilherme/.local/share/Steam/userdata/160032537/782330/remote/sticky bombs/25 de 25/GAME-AUTOSAVE2/game_duration.dat"),
        }
        originals = {
            name: getattr(bridge_client, name)
            for name in (
                "DEATH_PROBE", "OODLE_DLL", "PROTON_PATH", "STEAM_COMPAT_DATA",
                "STEAM_INSTALL", "DEATH_PROBE_RUNTIME", "STEAM_ID3",
                "DISTROBOX_HOST_EXEC",
            )
        }
        try:
            bridge_client.DEATH_PROBE = ROOT / "build/client/save_death_probe.exe"
            bridge_client.OODLE_DLL = ROOT.parent / "Tools/EntitySlayer_Beta_10_1/oo2core_8_win64.dll"
            bridge_client.PROTON_PATH = Path("/run/media/system/Eris/SteamLibrary/steamapps/common/Proton - Experimental/proton")
            bridge_client.STEAM_COMPAT_DATA = Path("/run/media/system/Eris/SteamLibrary/steamapps/compatdata/782330")
            bridge_client.STEAM_INSTALL = Path("/var/home/guilherme/.local/share/Steam")
            bridge_client.STEAM_ID3 = 160032537
            bridge_client.DISTROBOX_HOST_EXEC = None
            with tempfile.TemporaryDirectory() as directory:
                bridge_client.DEATH_PROBE_RUNTIME = Path(directory)
                parsed = {
                    count: bridge_client.probe_game_duration(path)
                    for count, path in snapshots.items()
                }
        finally:
            for name, value in originals.items():
                setattr(bridge_client, name, value)
        self.assertEqual(parsed[24]["rule_0_statCount"], 24)
        self.assertFalse(parsed[24]["rule_0_satisfied"])
        self.assertFalse(parsed[24]["unlockableIsUnlocked"])
        self.assertEqual(parsed[25]["rule_0_statCount"], 25)
        self.assertTrue(parsed[25]["rule_0_satisfied"])
        self.assertTrue(parsed[25]["unlockableIsUnlocked"])

    @unittest.skipUnless(
        (ROOT / "build/client/save_death_probe.exe").is_file()
        and Path("/home/guilherme/.local/share/Steam/userdata/160032537/782330/remote/GAME-AUTOSAVE0/game_duration.dat").is_file()
        and Path("/home/guilherme/.local/share/Steam/userdata/160032537/782330/remote/cultist base - antes da rocket/GAME-AUTOSAVE2/game_duration.dat").is_file(),
        "provided vanilla Cultist Base snapshots or native probe unavailable",
    )
    def test_parser_accepts_vanilla_cultist_challenge_snapshots(self):
        snapshots = {
            "before_rocket": Path("/home/guilherme/.local/share/Steam/userdata/160032537/782330/remote/cultist base - antes da rocket/GAME-AUTOSAVE2/game_duration.dat"),
            "vanilla_100": Path("/home/guilherme/.local/share/Steam/userdata/160032537/782330/remote/GAME-AUTOSAVE0/game_duration.dat"),
        }
        originals = {
            name: getattr(bridge_client, name)
            for name in (
                "DEATH_PROBE", "OODLE_DLL", "PROTON_PATH", "STEAM_COMPAT_DATA",
                "STEAM_INSTALL", "DEATH_PROBE_RUNTIME", "STEAM_ID3",
                "DISTROBOX_HOST_EXEC",
            )
        }
        try:
            bridge_client.DEATH_PROBE = ROOT / "build/client/save_death_probe.exe"
            bridge_client.OODLE_DLL = ROOT.parent / "Tools/EntitySlayer_Beta_10_1/oo2core_8_win64.dll"
            bridge_client.PROTON_PATH = Path("/run/media/system/Eris/SteamLibrary/steamapps/common/Proton - Experimental/proton")
            bridge_client.STEAM_COMPAT_DATA = Path("/run/media/system/Eris/SteamLibrary/steamapps/compatdata/782330")
            bridge_client.STEAM_INSTALL = Path("/var/home/guilherme/.local/share/Steam")
            bridge_client.STEAM_ID3 = 160032537
            bridge_client.DISTROBOX_HOST_EXEC = None
            parsed = {}
            for name, path in snapshots.items():
                with tempfile.TemporaryDirectory() as directory:
                    bridge_client.DEATH_PROBE_RUNTIME = Path(directory)
                    parsed[name] = bridge_client.probe_game_duration(path)[
                        "mission_challenge_records"
                    ]
        finally:
            for name, value in originals.items():
                setattr(bridge_client, name, value)

        expected = {
            entry["signal"]["unlockable"]
            for entry in bridge_client.MISSION_CHALLENGE_ENTRIES
        }
        self.assertLessEqual(
            set(parsed["vanilla_100"]), expected,
            "vanilla_100 snapshot should only contain known registry entries"
        )
        # All e1m3 challenges must be present in the vanilla 100% snapshot
        e1m3_entries = {
            entry["signal"]["unlockable"]
            for entry in bridge_client.MISSION_CHALLENGE_ENTRIES
            if "/e1m3/" in entry["signal"]["unlockable"]
        }
        self.assertLessEqual(
            e1m3_entries, set(parsed["vanilla_100"]),
        )
        for record in parsed["vanilla_100"].values():
            self.assertEqual(record["rule_0_statCount"], 1)
            self.assertEqual(record["rule_0_statDuration"], 5)
            self.assertTrue(record["rule_0_satisfied"])
            self.assertTrue(record["unlockableIsUnlocked"])
        self.assertEqual(
            set(parsed["before_rocket"]),
            {"mission_challenge/e1m3/challenge_3"},
        )


class BootstrapTests(unittest.TestCase):
    def _make_context(self, *received_item_ids):
        ctx = bridge_client.DoomEternalContext(None, None)
        ctx.session_state = {"bootstrap": {"revision": 1, "actions": {}}}
        ctx.client_state = {"version": 1, "sessions": {}}
        ctx.item_state_ready = True
        ctx.current_map_name = "game/sp/e1m1_intro/e1m1_intro"
        ctx.items_received = [types.SimpleNamespace(item=item_id) for item_id in received_item_ids]
        return ctx

    def test_bootstrap_catalogue_is_complete_and_safe(self):
        from bootstrap_actions import (
            BOOTSTRAP_ACTIONS,
            BOOTSTRAP_STAT_ALLOWLIST,
            validate_bootstrap_catalogue,
        )
        self.assertEqual(validate_bootstrap_catalogue(), BOOTSTRAP_ACTIONS)
        self.assertEqual(set(BOOTSTRAP_ACTIONS), set(BOOTSTRAP_STAT_ALLOWLIST))
        for action_name, action in BOOTSTRAP_ACTIONS.items():
            self.assertEqual(action["revision"], 2)
            self.assertEqual(action["entity_name"], f"ap_bootstrap_v2_{action_name}")
            self.assertEqual(action["effects"], ((BOOTSTRAP_STAT_ALLOWLIST[action_name], 1),))
            self.assertEqual(action["status"], "experimental")
            self.assertFalse(action["automatic_enabled"])

    def test_bootstrap_allowlist_rejects_extra_or_wrong_effects(self):
        from copy import deepcopy

        from bootstrap_actions import BOOTSTRAP_ACTIONS, validate_bootstrap_catalogue

        extra = deepcopy(BOOTSTRAP_ACTIONS)
        extra["rune_page"]["effects"] += (("STAT_FRAG_GAINED", 1),)
        with self.assertRaises(ValueError):
            validate_bootstrap_catalogue(extra)
        wrong = deepcopy(BOOTSTRAP_ACTIONS)
        wrong["ice_acquired"]["stat"] = "STAT_FRAG_GAINED"
        with self.assertRaises(ValueError):
            validate_bootstrap_catalogue(wrong)
        unknown = deepcopy(BOOTSTRAP_ACTIONS)
        unknown["unsafe"] = {
            **unknown["rune_page"],
            "action": "unsafe",
            "entity_name": "ap_bootstrap_v2_unsafe",
            "stat": "STAT_UNSAFE",
            "effects": (("STAT_UNSAFE", 1),),
        }
        with self.assertRaises(ValueError):
            validate_bootstrap_catalogue(unknown)

    def test_rune_page_requires_received_rune(self):
        self.assertFalse(self._make_context().bootstrap_eligible("rune_page"))
        self.assertTrue(self._make_context(7770085).bootstrap_eligible("rune_page"))

    def test_suit_page_has_no_active_candidate(self):
        from bootstrap_actions import BOOTSTRAP_ACTIONS, REJECTED_BOOTSTRAP_HISTORY

        self.assertNotIn("suit_page", BOOTSTRAP_ACTIONS)
        self.assertEqual(
            REJECTED_BOOTSTRAP_HISTORY["suit_page_v2"]["status"],
            "runtime_rejected",
        )

    def test_equipment_acquired_actions_are_independent(self):
        from bootstrap_actions import EQUIPMENT_ACQUISITION_STAT_AUDIT

        frag = self._make_context(7770011)
        ice = self._make_context(7770013)
        neither = self._make_context(7770097)
        self.assertTrue(frag.bootstrap_eligible("frag_acquired"))
        self.assertFalse(frag.bootstrap_eligible("ice_acquired"))
        self.assertTrue(ice.bootstrap_eligible("ice_acquired"))
        self.assertFalse(ice.bootstrap_eligible("frag_acquired"))
        self.assertFalse(neither.bootstrap_eligible("frag_acquired"))
        self.assertFalse(neither.bootstrap_eligible("ice_acquired"))
        self.assertEqual(
            EQUIPMENT_ACQUISITION_STAT_AUDIT["frag_acquired"]["vanilla_writer"],
            "throwable/player/frag_grenade.gameStat",
        )
        self.assertEqual(
            EQUIPMENT_ACQUISITION_STAT_AUDIT["ice_acquired"]["vanilla_writer"],
            "throwable/player/ice_bomb.gameStat",
        )
        for audit in EQUIPMENT_ACQUISITION_STAT_AUDIT.values():
            self.assertTrue(audit["ui_loadout_only"])
            self.assertFalse(audit["progression_reader_found"])
            self.assertFalse(audit["force_stat_required"])

    def test_consumed_bootstrap_spool_has_unknown_effect_and_stays_idempotent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_queue_dir = bridge_client.QUEUE_DIR
            original_state_file = bridge_client.CLIENT_STATE_FILE
            original_gate = bridge_client.RPC_GATE_PATH
            try:
                bridge_client.QUEUE_DIR = tmpdir
                bridge_client.CLIENT_STATE_FILE = Path(tmpdir, "client_state.json")
                bridge_client.RPC_GATE_PATH = os.path.join(tmpdir, "ap_rpc_enabled")
                bridge_client.set_rpc_execution(True)
                ctx = self._make_context(7770085)
                self.assertTrue(ctx.enqueue_bootstrap("rune_page", "on_connect"))
                self.assertTrue(ctx.enqueue_bootstrap("rune_page", "on_reconnect"))
                command_id = ctx.bootstrap_command_id("rune_page")
                self.assertTrue(Path(tmpdir, command_id + ".cmd").is_file())
                self.assertNotIn("processed_items", ctx.session_state["bootstrap"])
                self.assertEqual(ctx.bootstrap_action_state("rune_page")["status"], "queued")
                Path(tmpdir, command_id + ".cmd").unlink()
                ctx.reconcile_bootstrap_spool()
                self.assertEqual(
                    ctx.bootstrap_action_state("rune_page")["status"],
                    "delivered_effect_unknown",
                )
                self.assertFalse(ctx.enqueue_bootstrap("rune_page", "on_reconnect"))
            finally:
                bridge_client.QUEUE_DIR = original_queue_dir
                bridge_client.CLIENT_STATE_FILE = original_state_file
                bridge_client.RPC_GATE_PATH = original_gate

    def test_legacy_applied_is_preserved_and_v2_requeues_independently(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_state_file = bridge_client.CLIENT_STATE_FILE
            original_send = bridge_client.send_command
            calls = []
            try:
                bridge_client.CLIENT_STATE_FILE = Path(tmpdir, "client_state.json")
                ctx = self._make_context(7770085)
                ctx.session_state["bootstrap"]["actions"]["rune_page"] = {
                    "revision": 1,
                    "action": "rune_page",
                    "status": "applied",
                    "trigger": "on_connect",
                    "last_map": ctx.current_map_name,
                    "timestamp": 123.0,
                }
                bridge_client.send_command = lambda *args, **kwargs: calls.append(args) or True

                legacy = ctx.bootstrap_action_state("rune_page", revision=1)
                self.assertEqual(legacy["status"], "delivered_effect_unknown")
                self.assertEqual(legacy["legacy_status"], "applied")
                self.assertTrue(ctx.enqueue_bootstrap("rune_page", "on_reconnect"))
                self.assertEqual(ctx.bootstrap_action_state("rune_page")["revision"], 2)
                self.assertTrue(calls)
            finally:
                bridge_client.CLIENT_STATE_FILE = original_state_file
                bridge_client.send_command = original_send

    def test_confirmed_never_arises_from_delivery_without_explicit_confirmation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_state_file = bridge_client.CLIENT_STATE_FILE
            try:
                bridge_client.CLIENT_STATE_FILE = Path(tmpdir, "client_state.json")
                ctx = self._make_context(7770085)
                state = ctx.bootstrap_action_state("rune_page")
                self.assertEqual(state["status"], "pending")
                state["status"] = "queued"
                ctx.reconcile_bootstrap_spool()
                self.assertEqual(state["status"], "delivered_effect_unknown")
                self.assertNotEqual(state["status"], "confirmed")
            finally:
                bridge_client.CLIENT_STATE_FILE = original_state_file

    def test_v1_spool_is_quarantined_without_migration_to_v2(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_queue_dir = bridge_client.QUEUE_DIR
            original_state_file = bridge_client.CLIENT_STATE_FILE
            try:
                bridge_client.QUEUE_DIR = tmpdir
                bridge_client.CLIENT_STATE_FILE = Path(tmpdir, "client_state.json")
                Path(tmpdir, "bootstrap-v1-suit_page.cmd").write_text(
                    "ai_ScriptCmdEnt ap_bootstrap_v1_suit_page activate\n",
                    encoding="utf-8",
                )
                ctx = self._make_context(7770088)
                ctx.reconcile_bootstrap_spool()
                self.assertFalse(Path(tmpdir, "bootstrap-v1-suit_page.cmd").exists())
                self.assertTrue(Path(tmpdir, "bootstrap-v1-suit_page.quarantined").exists())
                self.assertEqual(
                    ctx.bootstrap_action_state("suit_page", revision=1)["status"],
                    "quarantined_runtime_invalid",
                )
                self.assertNotIn("suit_page", bridge_client.BOOTSTRAP_ACTIONS)
            finally:
                bridge_client.QUEUE_DIR = original_queue_dir
                bridge_client.CLIENT_STATE_FILE = original_state_file

    def test_missing_map_entity_keeps_action_pending_and_failure_is_retryable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_state_file = bridge_client.CLIENT_STATE_FILE
            original_send = bridge_client.send_command
            try:
                bridge_client.CLIENT_STATE_FILE = Path(tmpdir, "client_state.json")
                ctx = self._make_context(7770085)
                ctx.current_map_name = "unknown/map"
                self.assertFalse(ctx.enqueue_bootstrap("rune_page", "on_connect"))
                self.assertEqual(ctx.bootstrap_action_state("rune_page")["status"], "pending")
                ctx.current_map_name = "game/sp/e1m1_intro/e1m1_intro"
                bridge_client.send_command = lambda *args, **kwargs: False
                self.assertFalse(ctx.enqueue_bootstrap("rune_page", "on_connect"))
                self.assertEqual(ctx.bootstrap_action_state("rune_page")["status"], "retryable_failure")
            finally:
                bridge_client.CLIENT_STATE_FILE = original_state_file
                bridge_client.send_command = original_send

    def test_existing_slot_discovers_new_actions_without_replaying_rune_page(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_queue_dir = bridge_client.QUEUE_DIR
            original_state_file = bridge_client.CLIENT_STATE_FILE
            original_gate = bridge_client.RPC_GATE_PATH
            try:
                bridge_client.QUEUE_DIR = tmpdir
                bridge_client.CLIENT_STATE_FILE = Path(tmpdir, "client_state.json")
                bridge_client.RPC_GATE_PATH = os.path.join(tmpdir, "ap_rpc_enabled")
                bridge_client.set_rpc_execution(True)
                ctx = self._make_context(7770085, 7770097, 7770011)
                ctx.session_state["bootstrap"]["actions"]["rune_page"] = {
                    "revision": 1, "action": "rune_page",
                    "status": "delivered_effect_unknown", "trigger": "on_connect",
                    "last_map": ctx.current_map_name, "timestamp": 1.0,
                }
                ctx.onboard_bootstrap("on_connect")
                self.assertEqual(ctx.bootstrap_action_state("rune_page", revision=1)["status"], "delivered_effect_unknown")
                self.assertEqual(ctx.bootstrap_action_state("rune_page")["status"], "pending")
                self.assertEqual(ctx.bootstrap_action_state("suit_page")["status"], "pending")
                self.assertEqual(ctx.bootstrap_action_state("frag_acquired")["status"], "pending")
                self.assertEqual(ctx.bootstrap_action_state("ice_acquired")["status"], "pending")
            finally:
                bridge_client.QUEUE_DIR = original_queue_dir
                bridge_client.CLIENT_STATE_FILE = original_state_file
                bridge_client.RPC_GATE_PATH = original_gate

    def test_supported_map_load_discovers_only_missing_or_pending_actions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_queue_dir = bridge_client.QUEUE_DIR
            original_state_file = bridge_client.CLIENT_STATE_FILE
            original_gate = bridge_client.RPC_GATE_PATH
            try:
                bridge_client.QUEUE_DIR = tmpdir
                bridge_client.CLIENT_STATE_FILE = Path(tmpdir, "client_state.json")
                bridge_client.RPC_GATE_PATH = os.path.join(tmpdir, "ap_rpc_enabled")
                bridge_client.set_rpc_execution(True)
                ctx = self._make_context(7770097, 7770011)
                ctx.session_state["bootstrap"]["actions"]["suit_page"] = {
                    "revision": 1, "action": "suit_page",
                    "status": "delivered_effect_unknown", "trigger": "on_connect",
                    "last_map": ctx.current_map_name, "timestamp": 1.0,
                }
                ctx.onboard_bootstrap("on_supported_map_load")
                self.assertEqual(ctx.bootstrap_action_state("suit_page", revision=1)["status"], "delivered_effect_unknown")
                self.assertEqual(ctx.bootstrap_action_state("suit_page")["status"], "pending")
                self.assertEqual(ctx.bootstrap_action_state("frag_acquired")["status"], "pending")
                ctx.onboard_bootstrap("on_supported_map_load")
                self.assertEqual(ctx.bootstrap_action_state("frag_acquired")["status"], "pending")
            finally:
                bridge_client.QUEUE_DIR = original_queue_dir
                bridge_client.CLIENT_STATE_FILE = original_state_file
                bridge_client.RPC_GATE_PATH = original_gate

    def test_onboarding_status_is_compact(self):
        ctx = self._make_context(7770085)
        ctx.bootstrap_action_state("rune_page")
        lines = ctx.onboarding_status_lines()
        text = "\n".join(lines)
        self.assertIn("Bootstrap revision: 2", text)
        self.assertIn("v2 rune_page: eligible=yes, state=pending", text)
        self.assertNotIn("v2 suit_page:", text)
        self.assertNotIn("processed_items", text)
        self.assertNotIn("7770085", text)


class CheckEventTests(unittest.TestCase):
    def setUp(self):
        self.original_item_notifications = bridge_client.ENABLE_ITEM_NOTIFICATIONS
        bridge_client.ENABLE_ITEM_NOTIFICATIONS = True

    def tearDown(self):
        bridge_client.ENABLE_ITEM_NOTIFICATIONS = self.original_item_notifications

    def _make_item_context(self):
        ctx = bridge_client.DoomEternalContext(None, None)
        ctx.session_state = {}
        ctx.client_state = {"version": 1, "sessions": {}}
        ctx.item_state_ready = True
        ctx.items_received = []
        return ctx

    def test_network_item_classification_matches_packaged_identity(self):
        self.assertEqual(
            bridge_client.received_item_classification(7770000, 1), 1
        )
        self.assertEqual(
            bridge_client.received_item_classification(7770031, None), 0
        )
        self.assertEqual(
            bridge_client.received_item_classification(7770031, 1), 0
        )

    def test_receipt_entrypoint_selection_respects_explicit_capability(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_queue_dir = bridge_client.QUEUE_DIR
            original_state_file = bridge_client.CLIENT_STATE_FILE
            try:
                bridge_client.QUEUE_DIR = tmpdir
                bridge_client.CLIENT_STATE_FILE = Path(tmpdir, "client_state.json")
                ctx = self._make_item_context()
                self.assertTrue(ctx.spool_item_commands(7770010, 0, receipt=True)[0])
                self.assertTrue(ctx.spool_item_commands(7770011, 1, receipt=False)[0])
                payloads = {
                    path.name: path.read_text(encoding="utf-8")
                    for path in Path(tmpdir).glob("*.cmd")
                }
                self.assertEqual(
                    payloads["recv-000000-item-7770010-effect-00.cmd"],
                    "ai_ScriptCmdEnt ap_rpc_v3_7770010 activate\n",
                )
                self.assertEqual(
                    payloads["recv-000000-item-7770010-notify.cmd"],
                    "ai_ScriptCmdEnt ap_notify_item_major_7770010_a activate\n",
                )
                self.assertEqual(
                    payloads["recv-000001-item-7770011-effect-00.cmd"],
                    "ai_ScriptCmdEnt ap_rpc_v3_7770011_0 activate\n",
                )
            finally:
                bridge_client.QUEUE_DIR = original_queue_dir
                bridge_client.CLIENT_STATE_FILE = original_state_file

    def test_three_same_filler_receipts_keep_distinct_commands_and_notifications(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_queue_dir = bridge_client.QUEUE_DIR
            original_state_file = bridge_client.CLIENT_STATE_FILE
            try:
                bridge_client.QUEUE_DIR = tmpdir
                bridge_client.CLIENT_STATE_FILE = Path(tmpdir, "client_state.json")
                ctx = self._make_item_context()
                ctx.items_received = [
                    types.SimpleNamespace(item=7770031),
                    types.SimpleNamespace(item=7770010),
                    types.SimpleNamespace(item=7770031),
                    types.SimpleNamespace(item=7770031),
                ]
                for receive_index in range(3):
                    self.assertTrue(ctx.spool_item_commands(
                        7770031, (0, 2, 3)[receive_index], receipt=True
                    )[0])

                files = sorted(Path(tmpdir).glob("*.cmd"))
                self.assertEqual(len(files), 6)
                self.assertEqual(len({path.name for path in files}), 6)
                self.assertEqual(
                    [path.read_text(encoding="utf-8") for path in files],
                    [
                        "ai_ScriptCmdEnt ap_rpc_v3_7770031 activate\n",
                        "ai_ScriptCmdEnt ap_notify_item_filler_7770031_a activate\n",
                        "ai_ScriptCmdEnt ap_rpc_v3_7770031 activate\n",
                        "ai_ScriptCmdEnt ap_notify_item_filler_7770031_b activate\n",
                        "ai_ScriptCmdEnt ap_rpc_v3_7770031 activate\n",
                        "ai_ScriptCmdEnt ap_notify_item_filler_7770031_a activate\n",
                    ],
                )
            finally:
                bridge_client.QUEUE_DIR = original_queue_dir
                bridge_client.CLIENT_STATE_FILE = original_state_file

    def test_mapping_repair_forces_silent_entrypoint(self):
        ctx = self._make_item_context()
        ctx.items_received = [types.SimpleNamespace(item=7770085)]
        ctx.items_processed = 1
        ctx.session_state = {"item_mapping_revision": 0}
        with (
            patch.object(ctx, "spool_item_commands", return_value=(True, "repair")) as spool,
            patch.object(bridge_client, "save_client_state"),
        ):
            self.assertFalse(ctx.repair_item_mappings())
        spool.assert_called_once_with(7770085, 0, receipt=False)

    def test_directed_test_commands_are_always_available(self):
        self.assertFalse(hasattr(bridge_client, "DEV_TOOLS_ENABLED"))

    def test_ap_reconcile_command_surface_exists(self):
        self.assertTrue(hasattr(bridge_client.DoomCommandProcessor, "_cmd_ap_reconcile"))

    def _ready_reconcile_context(self):
        ctx = self._make_item_context()
        ctx.server = types.SimpleNamespace(
            socket=types.SimpleNamespace(closed=False)
        )
        ctx.room_seed_name = "seed-a"
        ctx.seed_name = None
        ctx.team = 1
        ctx.slot = 2
        ctx.active_save_slot = "GAME-AUTOSAVE2"
        ctx.active_gameplay_epoch = 14
        ctx.runtime_observers_frozen = False
        ctx.current_map_name = "game/sp/e1m3_cult/e1m3_cult"
        ctx.items_received = [
            types.SimpleNamespace(item=7770005),
            types.SimpleNamespace(item=7770059),
            types.SimpleNamespace(item=7770089),
            types.SimpleNamespace(item=7770016),
            types.SimpleNamespace(item=7770092),
            types.SimpleNamespace(item=7770092),
        ]
        ctx.items_processed = len(ctx.items_received)
        return ctx

    def test_manual_reconcile_uses_processed_history_without_mutating_ap_state(self):
        ctx = self._ready_reconcile_context()
        evidence = bridge_client.GameplaySaveEvidence(
            "gameplay", 14, "GAME-AUTOSAVE2", "game/sp/e1m3_cult/e1m3_cult"
        )
        spooled = []
        checked_before = set(getattr(ctx, "checked_locations", set()))
        processed_before = ctx.items_processed
        receipts_before = list(ctx.items_received)
        with (
            patch.object(bridge_client, "read_gameplay_save_evidence", return_value=evidence),
            patch.object(
                bridge_client,
                "send_command",
                side_effect=lambda command, **kwargs: spooled.append((command, kwargs)) or True,
            ),
            patch.object(ctx, "send_msgs", create=True) as send_msgs,
        ):
            plan, error = ctx.manual_reconcile_inventory()
        self.assertIsNone(error)
        self.assertEqual(plan.replayed, 3)
        self.assertEqual(plan.special_stages, 2)
        self.assertEqual(plan.skipped_never_replay, 1)
        self.assertEqual(processed_before, ctx.items_processed)
        self.assertEqual(receipts_before, ctx.items_received)
        self.assertEqual(checked_before, set(getattr(ctx, "checked_locations", set())))
        send_msgs.assert_not_called()
        spool_ids = [kwargs["coalesce_key"] for _, kwargs in spooled]
        self.assertEqual(len(spool_ids), len(set(spool_ids)))
        self.assertTrue(all(value.startswith("reconcile-seed-a-1-2-e14-") for value in spool_ids))

    def test_manual_reconcile_rejects_menu_and_unconfirmed_epoch(self):
        ctx = self._ready_reconcile_context()
        menu = bridge_client.GameplaySaveEvidence("menu", 15, "", "")
        with (
            patch.object(bridge_client, "read_gameplay_save_evidence", return_value=menu),
            patch.object(bridge_client, "send_command") as send_command,
        ):
            plan, error = ctx.manual_reconcile_inventory()
        self.assertIsNone(plan)
        self.assertIn("gameplay epoch required", error)
        send_command.assert_not_called()

        gameplay = bridge_client.GameplaySaveEvidence(
            "gameplay", 15, "GAME-AUTOSAVE2", "game/sp/e1m3_cult/e1m3_cult"
        )
        with (
            patch.object(bridge_client, "read_gameplay_save_evidence", return_value=gameplay),
            patch.object(bridge_client, "send_command") as send_command,
        ):
            plan, error = ctx.manual_reconcile_inventory()
        self.assertIsNone(plan)
        self.assertIn("active epoch", error)
        send_command.assert_not_called()

    def test_checked_automap_cleanup_is_map_scoped_and_epoch_idempotent(self):
        self.assertEqual(
            bridge_client.AUTOMAP_COMPLETION_BY_MAP,
            {"game/sp/e1m1_intro/e1m1_intro": {
                7770015: "ap_remove_location_visual_7770015",
            }},
        )
        ctx = self._make_item_context()
        ctx.current_map_name = "game/sp/e1m1_intro/e1m1_intro"
        ctx.checked_locations = {7770015}
        ctx.locations_checked = set()
        calls = []
        original_send = bridge_client.send_command
        try:
            bridge_client.send_command = (
                lambda command, **kwargs: calls.append((command, kwargs)) or True
            )
            self.assertTrue(ctx.reconcile_checked_automap_cleanup("test"))
            self.assertFalse(ctx.reconcile_checked_automap_cleanup("duplicate"))
            ctx.advance_automap_cleanup_epoch()
            self.assertTrue(ctx.reconcile_checked_automap_cleanup("level_ready"))
        finally:
            bridge_client.send_command = original_send
        self.assertEqual(
            [command for command, _ in calls],
            [
                "ai_ScriptCmdEnt ap_remove_location_visual_7770015 activate",
                "ai_ScriptCmdEnt ap_remove_location_visual_7770015 activate",
            ],
        )
        self.assertTrue(all(
            kwargs.get("already_queued_ok") is True for _, kwargs in calls
        ))

    def test_automap_cleanup_never_queues_unchecked_or_wrong_map(self):
        ctx = self._make_item_context()
        ctx.checked_locations = set()
        ctx.locations_checked = set()
        ctx.current_map_name = "game/sp/e1m1_intro/e1m1_intro"
        calls = []
        original_send = bridge_client.send_command
        try:
            bridge_client.send_command = (
                lambda command, **kwargs: calls.append((command, kwargs)) or True
            )
            self.assertFalse(ctx.reconcile_checked_automap_cleanup("unchecked"))
            ctx.checked_locations = {7770015}
            ctx.current_map_name = "game/sp/e1m2_battle/e1m2_battle"
            self.assertFalse(ctx.reconcile_checked_automap_cleanup("wrong_map"))
        finally:
            bridge_client.send_command = original_send
        self.assertEqual(calls, [])

    def test_dev_item_plan_matches_real_pipeline_without_mutating_state(self):
        ctx = self._make_item_context()
        ctx.session_state = {
            "processed_items": [0, 1],
            "bootstrap": {"revision": 2, "actions": {}},
        }
        before = json.dumps(ctx.session_state, sort_keys=True)
        plan = bridge_client.compile_item_delivery_plan(
            7770016, bridge_client.ITEM_ID_TO_COMMAND
        )
        real_commands, _ = ctx.item_activation_commands(7770016, 0)
        calls = []
        original_send = bridge_client.send_command
        try:
            bridge_client.send_command = lambda command, **kwargs: calls.append((command, kwargs)) or True
            correlation = ctx.queue_dev_plan(plan, "item")
        finally:
            bridge_client.send_command = original_send
        self.assertRegex(correlation, r"^devtest-[0-9a-f]{8}-0001$")
        self.assertEqual([call[0] for call in calls], real_commands)
        self.assertEqual(before, json.dumps(ctx.session_state, sort_keys=True))

    def test_dev_lab_rejects_raw_gameplay_command(self):
        ctx = self._make_item_context()
        with self.assertRaisesRegex(ValueError, "map-side entity activation"):
            ctx.queue_dev_commands(["give weapon/player/shotgun"], "unsafe")

    def test_location_lab_contract_uses_map_entrypoint_not_server_check(self):
        contract = bridge_client.load_foundation_contracts()["location_entrypoints"]["7770074"]
        self.assertEqual(contract["entity"], "ap_independent_pickup_equipment_ice_bomb")
        source = Path(bridge_client.__file__).read_text(encoding="utf-8")
        method = source.split("def _cmd_doom_test_location", 1)[1].split("def _cmd_doom_test_status", 1)[0]
        self.assertIn("queue_dev_commands", method)
        self.assertNotIn("LocationChecks", method)
        self.assertNotIn("send_msgs", method)

    def test_rocket_location_lab_uses_independent_entrypoint(self):
        contract = bridge_client.load_foundation_contracts()["location_entrypoints"]["7770056"]
        self.assertEqual(contract["map"], "game/sp/e1m3_cult/e1m3_cult")
        self.assertEqual(contract["entity"], "ap_independent_rocket_launcher_7770056")
        self.assertEqual(contract["primitive_id"], "independent_location_trigger")

    def test_hub_map_aliases_share_one_canonical_identity(self):
        self.assertEqual(
            bridge_client.canonical_map_name("game/hub/hub"),
            bridge_client.canonical_map_name("game/sp/hub/hub"),
        )
        self.assertEqual(
            bridge_client.canonical_map_name("game/sp/hub/hub"),
            "game/hub/hub",
        )
        self.assertNotEqual(
            bridge_client.canonical_map_name("game/sp/e1m2_battle/e1m2_battle"),
            bridge_client.canonical_map_name("game/sp/hub/hub"),
        )
        self.assertEqual(
            bridge_client.canonical_map_name("game/sp/e1m2_battle/e1m2_battle"),
            "game/sp/e1m2_battle/e1m2_battle",
        )
        contract = bridge_client.load_foundation_contracts()["location_entrypoints"]["7770074"]
        for alias in ("game/hub/hub", "game/sp/hub/hub"):
            self.assertEqual(
                bridge_client.canonical_map_name(alias),
                bridge_client.canonical_map_name(contract["map"]),
            )

    def test_native_transition_publisher_is_cultist_only(self):
        source = (ROOT / "native" / "client" / "ap_client_exe.cpp").read_text(encoding="utf-8")
        monitor = source.split("class MissionTransitionMonitor", 1)[1].split(
            "bool ReadCommandFile", 1
        )[0]
        self.assertIn('canonicalFrom == "game/sp/e1m3_cult/e1m3_cult"', monitor)
        self.assertIn('canonicalTo == "game/sp/e1m4_boss/e1m4_boss"', monitor)
        self.assertIn("reason=map_side_owner", monitor)
        self.assertNotIn('canonicalFrom == "game/sp/e1m1_intro/e1m1_intro"', monitor)
        self.assertNotIn('canonicalFrom == "game/sp/e1m2_battle/e1m2_battle"', monitor)
        self.assertNotIn("e1m2_war/e1m2_war", source)
        self.assertIn("MISSION_TRANSITION_SOURCE", source)
        self.assertIn("TRANSITION_EVENT_PUBLISHED", source)

    def test_bridge_log_rotation_uses_only_temp_test_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            current = Path(directory) / "bridge.log"
            previous = Path(directory) / "bridge.previous.log"
            current.write_text("old\n", encoding="utf-8")
            previous.write_text("older\n", encoding="utf-8")
            original_handlers = list(bridge_client.logger.handlers)
            try:
                bridge_client.start_bridge_logger(current)
                bridge_client.logger.info("fresh")
                self.assertEqual(previous.read_text(encoding="utf-8"), "old\n")
                self.assertIn("fresh", current.read_text(encoding="utf-8"))
            finally:
                for handler in list(bridge_client.logger.handlers):
                    bridge_client.logger.removeHandler(handler)
                    handler.close()
                for handler in original_handlers:
                    bridge_client.logger.addHandler(handler)

    def test_native_log_rotation_and_runtime_identity_are_explicit(self):
        source = (ROOT / "native" / "client" / "ap_client_exe.cpp").read_text(encoding="utf-8")
        self.assertIn("RotateClientLog();", source)
        self.assertIn("ap_client.previous.log", source)
        self.assertNotIn("e1m2_war/e1m2_war", source)

    def test_safe_baseline_has_no_mastery_reconciliation(self):
        source = Path(bridge_client.__file__).read_text(encoding="utf-8")
        self.assertNotIn("MASTERY_CONTRACTS", source)
        self.assertNotIn("mastery_reconciliation_state", source)
        self.assertNotIn("pending_masteries", source)

    def test_rejected_suit_v2_does_not_queue_a_spool(self):
        self.assertNotIn("suit_page", bridge_client.BOOTSTRAP_ACTIONS)
        self.assertNotIn(
            "suit_page",
            bridge_client.load_foundation_contracts()["bootstrap_test_entrypoints"],
        )
        source = Path(bridge_client.__file__).read_text(encoding="utf-8")
        method = source.split("def _cmd_doom_test_bootstrap", 1)[1].split(
            "def _cmd_doom_test_location", 1
        )[0]
        self.assertLess(method.index('action_name == "suit_page"'),
                        method.index("queue_dev_commands"))

    def test_string_mapping_spools_one_map_side_activation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_queue_dir = bridge_client.QUEUE_DIR
            original_state_file = bridge_client.CLIENT_STATE_FILE
            try:
                bridge_client.QUEUE_DIR = tmpdir
                bridge_client.CLIENT_STATE_FILE = Path(tmpdir, "client_state.json")
                ctx = self._make_item_context()

                spooled, description = ctx.spool_item_commands(7770010, 3, receipt=True)

                self.assertTrue(spooled)
                self.assertEqual(description, "give weapon/player/chainsaw")
                files = sorted(Path(tmpdir).glob("*.cmd"))
                self.assertEqual([path.name for path in files], [
                    "recv-000003-item-7770010-effect-00.cmd",
                    "recv-000003-item-7770010-notify.cmd",
                ])
                self.assertEqual(
                    files[0].read_text(encoding="utf-8"),
                    "ai_ScriptCmdEnt ap_rpc_v3_7770010 activate\n",
                )
                self.assertEqual(files[1].read_text(encoding="utf-8"), "ai_ScriptCmdEnt ap_notify_item_major_7770010_b activate\n")
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

                spooled, description = ctx.spool_item_commands(7770045, 11, receipt=True)

                self.assertTrue(spooled)
                self.assertEqual(description, "chrispy ai/heavy/revenant")
                files = sorted(Path(tmpdir).glob("*.cmd"))
                self.assertEqual(
                    [path.name for path in files],
                    ["recv-000011-item-7770045-effect-00.cmd", "recv-000011-item-7770045-notify.cmd"],
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
                    spooled, _ = ctx.spool_item_commands(item_id, receive_index, receipt=True)
                    self.assertTrue(spooled)

                for path in Path(tmpdir).glob("*.cmd"):
                    command = path.read_text(encoding="utf-8")
                    self.assertTrue("ai_ScriptCmdEnt ap_rpc_" in command or "ai_ScriptCmdEnt ap_notify_item_" in command)
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

                spooled, description = ctx.spool_item_commands(7770012, 7, receipt=True)

                self.assertTrue(spooled)
                self.assertEqual(
                    description,
                    "give equipmentlauncher/equipmentlauncherleft -> "
                    "give weapon/player/equipment_flame_belch",
                )
                files = sorted(Path(tmpdir).glob("*.cmd"))
                self.assertEqual(
                    [path.name for path in files],
                    ["recv-000007-item-7770012-effect-00.cmd", "recv-000007-item-7770012-effect-01.cmd", "recv-000007-item-7770012-notify.cmd"],
                )
                self.assertEqual(
                    [path.read_text(encoding="utf-8") for path in files],
                    [
                        "ai_ScriptCmdEnt ap_rpc_v3_7770012_0 activate\n",
                        "ai_ScriptCmdEnt ap_rpc_v3_7770012_1 activate\n",
                        "ai_ScriptCmdEnt ap_notify_item_major_7770012_b activate\n",
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

                spooled, _ = ctx.spool_item_commands(
                    7770997, 12, receipt=True, classification=1
                )

                self.assertTrue(spooled)
                files = sorted(Path(tmpdir).glob("*.cmd"))
                self.assertEqual(
                    [path.name for path in files],
                    ["recv-000012-item-7770997-effect-00.cmd", "recv-000012-item-7770997-effect-01.cmd", "recv-000012-item-7770997-notify.cmd"],
                )
                self.assertEqual(
                    [path.read_text(encoding="utf-8") for path in files],
                    [
                        "ai_ScriptCmdEnt ap_rpc_v3_7770997_0 activate\n",
                        "ai_ScriptCmdEnt ap_rpc_v3_7770997_1 activate\n",
                        "ai_ScriptCmdEnt ap_notify_item_major_7770997_a activate\n",
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

                self.assertTrue(ctx.spool_item_commands(7770012, 7, receipt=True)[0])
                self.assertTrue(ctx.spool_item_commands(7770012, 7, receipt=True)[0])

                self.assertEqual(len(list(Path(tmpdir).glob("*.cmd"))), 3)
            finally:
                bridge_client.QUEUE_DIR = original_queue_dir
                bridge_client.CLIENT_STATE_FILE = original_state_file

    def test_release_sized_history_spools_100_ordered_unique_recv_jobs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_queue_dir = bridge_client.QUEUE_DIR
            original_state_file = bridge_client.CLIENT_STATE_FILE
            try:
                bridge_client.QUEUE_DIR = tmpdir
                bridge_client.CLIENT_STATE_FILE = Path(tmpdir, "client_state.json")
                ctx = self._make_item_context()
                for receive_index in range(100):
                    self.assertTrue(
                        ctx.spool_item_commands(7770000, receive_index, receipt=True)[0]
                    )
                for receive_index in range(100):
                    self.assertTrue(
                        ctx.spool_item_commands(7770000, receive_index, receipt=True)[0]
                    )

                jobs = sorted(Path(tmpdir).glob("recv-*.cmd"))
                self.assertEqual(len(jobs), 200)
                self.assertEqual(
                    [path.name for path in jobs],
                    [
                        name
                        for index in range(100)
                        for name in (
                            f"recv-{index:06d}-item-7770000-effect-00.cmd",
                            f"recv-{index:06d}-item-7770000-notify.cmd",
                        )
                    ],
                )
                self.assertFalse(any(path.name.startswith("reconcile-") for path in jobs))
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

                spooled, _ = ctx.spool_item_commands(7770012, 7, receipt=True)

                self.assertTrue(spooled)
                files = sorted(Path(tmpdir).glob("*.cmd"))
                self.assertEqual(
                    [path.name for path in files],
                    [
                        "recv-000007-item-7770012-cmd-00.cmd",
                        "recv-000007-item-7770012-effect-01.cmd",
                        "recv-000007-item-7770012-notify.cmd",
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

                spooled, description = ctx.spool_item_commands(
                    7770998, 9, receipt=True, classification=1
                )

                self.assertFalse(spooled)
                self.assertEqual(description, "mapping list is empty")
                self.assertEqual(list(Path(tmpdir).glob("*.cmd")), [])
            finally:
                bridge_client.QUEUE_DIR = original_queue_dir
                if original_mapping is None:
                    bridge_client.ITEM_ID_TO_COMMAND.pop(7770998, None)
                else:
                    bridge_client.ITEM_ID_TO_COMMAND[7770998] = original_mapping

    def test_native_owned_processing_job_is_never_requeued_by_bridge(self):
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
                self.assertFalse(migrated.exists())
                self.assertTrue(processing.exists())
                self.assertEqual(
                    processing.read_text(encoding="utf-8"),
                    "give weapon/player/heavy_cannon\n",
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

    def test_existing_progressive_activation_keeps_stage_and_bytes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_queue_dir = bridge_client.QUEUE_DIR
            try:
                bridge_client.QUEUE_DIR = tmpdir
                processing = Path(
                    tmpdir, "recv-000020-item-7770092-cmd-00.processing"
                )
                payload = "ai_ScriptCmdEnt ap_rpc_v3_7770092_3 activate\n"
                processing.write_bytes(payload.encode("utf-8"))

                bridge_client.migrate_direct_item_command_jobs()
                queued = processing.with_suffix(".cmd")
                self.assertFalse(queued.exists())
                self.assertEqual(processing.read_bytes(), payload.encode("utf-8"))

                bridge_client.migrate_direct_item_command_jobs()
                self.assertFalse(queued.exists())
                self.assertEqual(processing.read_bytes(), payload.encode("utf-8"))
            finally:
                bridge_client.QUEUE_DIR = original_queue_dir

    def test_valid_map_side_activation_is_not_rewritten_from_filename(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_queue_dir = bridge_client.QUEUE_DIR
            try:
                bridge_client.QUEUE_DIR = tmpdir
                queued = Path(tmpdir, "recv-000021-item-7770012-cmd-00.cmd")
                payload = "ai_ScriptCmdEnt ap_rpc_v3_7770012_1 activate\n"
                queued.write_text(payload, encoding="utf-8")
                bridge_client.migrate_direct_item_command_jobs()
                self.assertEqual(queued.read_text(encoding="utf-8"), payload)
            finally:
                bridge_client.QUEUE_DIR = original_queue_dir

    def test_frag_and_ice_spool_launcher_then_equipment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_queue_dir = bridge_client.QUEUE_DIR
            original_state_file = bridge_client.CLIENT_STATE_FILE
            try:
                bridge_client.QUEUE_DIR = tmpdir
                bridge_client.CLIENT_STATE_FILE = Path(tmpdir, "state.json")
                ctx = self._make_item_context()
                for receive_index, item_id in enumerate((7770011, 7770013)):
                    self.assertTrue(ctx.spool_item_commands(item_id, receive_index, receipt=True)[0])
                payloads = [
                    path.read_text(encoding="utf-8").strip()
                    for path in sorted(Path(tmpdir).glob("*.cmd"))
                ]
                self.assertEqual(payloads, [
                    "ai_ScriptCmdEnt ap_rpc_v3_7770011_0 activate",
                    "ai_ScriptCmdEnt ap_rpc_v3_7770011_1 activate",
                    "ai_ScriptCmdEnt ap_notify_item_major_7770011_a activate",
                    "ai_ScriptCmdEnt ap_rpc_v3_7770013_0 activate",
                    "ai_ScriptCmdEnt ap_rpc_v3_7770013_1 activate",
                    "ai_ScriptCmdEnt ap_notify_item_major_7770013_b activate",
                ])
            finally:
                bridge_client.QUEUE_DIR = original_queue_dir
                bridge_client.CLIENT_STATE_FILE = original_state_file

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
                    self.assertTrue(ctx.spool_item_commands(item_id, receive_index, receipt=True)[0])

                for receive_index, (item_id, expected) in enumerate(expectations.items()):
                    path = Path(
                        tmpdir,
                        f"recv-{receive_index:06d}-item-{item_id}-effect-00.cmd",
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

    def test_exact_runtime_transition_pairs_and_unknown_rejection(self):
        expected = {
            ("game/sp/e1m3_cult/e1m3_cult", "game/sp/e1m4_boss/e1m4_boss"): 7770124,
            ("game/sp/e2m1_nest/e2m1_nest", "game/hub/hub"): 7770210
        }
        self.assertEqual(
            {pair: entry["location_id"] for pair, entry in bridge_client.MISSION_COMPLETE_TRANSITIONS.items()},
            expected,
        )
        self.assertNotIn(
            ("game/sp/e1m1_intro/e1m1_intro", "unknown/map"),
            bridge_client.MISSION_COMPLETE_TRANSITIONS,
        )
        terminals = {
            entry["location_id"]: entry["signal"]
            for entry in bridge_client.CHALLENGE_LOCATION_REGISTRY["mission_complete"]
            if entry["signal"]["kind"] == "map_terminal"
        }
        self.assertEqual(
            terminals,
            {
                7770122: {"kind": "map_terminal", "runtime_map": "game/sp/e1m1_intro/e1m1_intro"},
                7770123: {"kind": "map_terminal", "runtime_map": "game/sp/e1m2_battle/e1m2_battle"},
                7770162: {"kind": "map_terminal", "runtime_map": "game/sp/e1m4_boss/e1m4_boss"},
            },
        )
        self.assertNotIn("e1m2_war", json.dumps(terminals))

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

    def test_cultist_transition_sends_only_mission_complete(self):
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
                        self.checked_locations = set()
                        self.server_locations = {7770124}
                        self.server = types.SimpleNamespace(
                            socket=types.SimpleNamespace(closed=False)
                        )

                    async def send_msgs(self, messages):
                        sent.extend(messages)
                        for message in messages:
                            self.checked_locations.update(message.get("locations", ()))

                    def persist_session_state(self):
                        persisted.append(True)

                    def output(self, message):
                        pass

                    async def send_mission_complete(self, *args, **kwargs):
                        return await bridge_client.DoomEternalContext.send_mission_complete(
                            self, *args, **kwargs
                        )

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
                        {"cmd": "LocationChecks", "locations": [7770124]},
                    ],
                )
                self.assertEqual(ctx.session_state["goal_sent"], False)
                self.assertEqual(ctx.locations_checked, {7770124})
                self.assertEqual(persisted, [])
                self.assertFalse(event_path.exists())
            finally:
                bridge_client.DOOM_BASE_DIR = original_base_dir

    def test_map_side_missions_are_not_consumed_as_transition_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_base_dir = bridge_client.DOOM_BASE_DIR
            try:
                bridge_client.DOOM_BASE_DIR = tmpdir
                sent = []

                class FakeContext:
                    def __init__(self):
                        self.session_state = {"goal_sent": False}
                        self.locations_checked = set()
                        self.checked_locations = set()
                        self.server_locations = {7770122}
                        self.server = types.SimpleNamespace(
                            socket=types.SimpleNamespace(closed=False)
                        )

                    async def send_msgs(self, messages):
                        sent.extend(messages)
                        for message in messages:
                            self.checked_locations.update(message.get("locations", ()))

                    def persist_session_state(self):
                        raise AssertionError("ordinary mission completion is not goal state")

                    async def send_mission_complete(self, *args, **kwargs):
                        return await bridge_client.DoomEternalContext.send_mission_complete(
                            self, *args, **kwargs
                        )

                ctx = FakeContext()
                for sequence in (1, 2):
                    event_path = Path(tmpdir, f"ap_transition_42_{sequence}.evt")
                    event_path.write_text(
                        "from_map=game/sp/e1m1_intro/e1m1_intro\n"
                        "to_map=game/sp/hub/hub\n",
                        encoding="utf-8",
                    )
                    asyncio.run(
                        bridge_client.DoomEternalContext.check_campaign_goal_event(ctx)
                    )
                    self.assertFalse(event_path.exists())

                self.assertEqual(sent, [])
                self.assertEqual(ctx.locations_checked, set())
                self.assertFalse(ctx.session_state["goal_sent"])
            finally:
                bridge_client.DOOM_BASE_DIR = original_base_dir

    def test_mission_complete_reserves_concurrent_duplicate_send(self):
        class Socket:
            closed = False

        class FakeContext:
            def __init__(self):
                self.session_state = {"goal_sent": False}
                self.locations_checked = set()
                self.checked_locations = set()
                self.server_locations = {7770122}
                self.server = types.SimpleNamespace(socket=Socket())
                self.mission_locations_in_flight = set()
                self.mission_goal_in_flight = False
                self.started = asyncio.Event()
                self.release = asyncio.Event()
                self.sent = []

            async def send_msgs(self, messages):
                self.sent.append(messages)
                self.started.set()
                await self.release.wait()
                for message in messages:
                    self.checked_locations.update(message.get("locations", ()))

        async def exercise():
            ctx = FakeContext()
            first = asyncio.create_task(
                bridge_client.DoomEternalContext.send_mission_complete(
                    ctx, 7770122, "concurrent test"
                )
            )
            await ctx.started.wait()
            duplicate = await bridge_client.DoomEternalContext.send_mission_complete(
                ctx, 7770122, "concurrent duplicate"
            )
            ctx.release.set()
            self.assertTrue(await first)
            self.assertFalse(duplicate)
            self.assertEqual(len(ctx.sent), 1)
            self.assertEqual(ctx.locations_checked, {7770122})

        asyncio.run(exercise())

    def test_mission_complete_failure_keeps_event_until_successful_retry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_base_dir = bridge_client.DOOM_BASE_DIR
            try:
                bridge_client.DOOM_BASE_DIR = tmpdir
                event_path = Path(tmpdir, bridge_client.GOAL_EVENT_FILENAME)
                event_path.write_text(
                    "from_map=game/sp/e1m3_cult/e1m3_cult\n"
                    "to_map=game/sp/e1m4_boss/e1m4_boss\n",
                    encoding="utf-8",
                )

                class Socket:
                    closed = False

                class FakeContext:
                    def __init__(self):
                        self.session_state = {"goal_sent": False}
                        self.locations_checked = set()
                        self.checked_locations = set()
                        self.server_locations = {7770124}
                        self.server = types.SimpleNamespace(socket=Socket())
                        self.mission_locations_in_flight = set()
                        self.mission_goal_in_flight = False
                        self.send_attempts = 0
                        self.persisted = 0

                    async def send_msgs(self, messages):
                        self.send_attempts += 1
                        if self.send_attempts == 1:
                            raise RuntimeError("temporary network failure")
                        for message in messages:
                            self.checked_locations.update(message.get("locations", ()))

                    def persist_session_state(self):
                        self.persisted += 1

                    async def send_mission_complete(self, *args, **kwargs):
                        return await bridge_client.DoomEternalContext.send_mission_complete(
                            self, *args, **kwargs
                        )

                ctx = FakeContext()
                asyncio.run(bridge_client.DoomEternalContext.check_campaign_goal_event(ctx))
                self.assertTrue(event_path.exists())
                self.assertEqual(ctx.locations_checked, set())
                self.assertFalse(ctx.session_state["goal_sent"])
                self.assertEqual(ctx.persisted, 0)

                asyncio.run(bridge_client.DoomEternalContext.check_campaign_goal_event(ctx))
                self.assertFalse(event_path.exists())
                self.assertEqual(ctx.send_attempts, 2)
                self.assertEqual(ctx.locations_checked, {7770124})
                self.assertFalse(ctx.session_state["goal_sent"])
                self.assertEqual(ctx.persisted, 0)
            finally:
                bridge_client.DOOM_BASE_DIR = original_base_dir

    def test_mission_complete_server_dedupe_and_transition_aliases(self):
        class Socket:
            closed = False

        class FakeContext:
            def __init__(self):
                self.session_state = {"goal_sent": False}
                self.locations_checked = set()
                self.checked_locations = {7770123}
                self.server_locations = {7770123}
                self.server = types.SimpleNamespace(socket=Socket())
                self.mission_locations_in_flight = set()
                self.mission_goal_in_flight = False
                self.sent = []

            async def send_msgs(self, messages):
                self.sent.append(messages)

        ctx = FakeContext()
        self.assertTrue(asyncio.run(
            bridge_client.DoomEternalContext.send_mission_complete(
                ctx, 7770123, "server dedupe"
            )
        ))
        self.assertEqual(ctx.sent, [])
        self.assertNotIn(
            ("game/sp/e1m2_battle/e1m2_battle", "game/hub/hub"),
            bridge_client.MISSION_COMPLETE_TRANSITIONS,
        )
        self.assertNotIn(
            ("game/hub/hub", "game/sp/e1m2_battle/e1m2_battle"),
            bridge_client.MISSION_COMPLETE_TRANSITIONS,
        )

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
                        self.server_locations = {7770124}
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

    def test_goal_ui_failure_does_not_retry_or_undo_commit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_base_dir = bridge_client.DOOM_BASE_DIR
            original_dump_dir = bridge_client.INV_DUMP_DIR
            try:
                bridge_client.DOOM_BASE_DIR = tmpdir
                bridge_client.INV_DUMP_DIR = tmpdir
                event_path = Path(tmpdir, bridge_client.FORTRESS_GOAL_EVENT_FILENAME)
                event_path.write_text(
                    "AP_GOAL_EVENT_FORTRESS_VISIT_3\n",
                    encoding="utf-8",
                )
                sent = []

                class FakeContext:
                    def __init__(self):
                        self.session_state = {"goal_sent": False}
                        self.locations_checked = set()
                        self.checked_locations = set()
                        self.server_locations = {7770162}
                        self.server = types.SimpleNamespace(
                            socket=types.SimpleNamespace(closed=False)
                        )
                    async def send_msgs(self, messages):
                        sent.extend(messages)
                        for message in messages:
                            self.checked_locations.update(message.get("locations", ()))
                    def persist_session_state(self):
                        pass
                    def output(self, message):
                        raise AttributeError("output unavailable")
                    async def send_mission_complete(self, *args, **kwargs):
                        return await bridge_client.DoomEternalContext.send_mission_complete(
                            self, *args, **kwargs
                        )
                    async def send_campaign_goal(self, source_description):
                        return await bridge_client.DoomEternalContext.send_campaign_goal(
                            self, source_description
                        )

                ctx = FakeContext()
                asyncio.run(
                    bridge_client.DoomEternalContext.check_campaign_goal_event(ctx)
                )
                self.assertTrue(ctx.session_state["goal_sent"])
                self.assertFalse(event_path.exists())
                self.assertEqual(len(sent), 2)

                asyncio.run(
                    bridge_client.DoomEternalContext.check_campaign_goal_event(ctx)
                )
                self.assertEqual(len(sent), 2)
            finally:
                bridge_client.DOOM_BASE_DIR = original_base_dir
                bridge_client.INV_DUMP_DIR = original_dump_dir
