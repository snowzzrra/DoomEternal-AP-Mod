import sys
import time
from pathlib import Path
import unittest
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHIPELAGO_ROOT = (REPO_ROOT.parent / "Archipelago").resolve()
if str(ARCHIPELAGO_ROOT) not in sys.path:
    sys.path.insert(0, str(ARCHIPELAGO_ROOT))

from doom_eap.runtime.bridge_client import (
    DoomEternalContext,
    MAP_ENTITY_SAFE,
    CHECKED_VISUAL_HIDE,
)


def _create_test_context():
    context = object.__new__(DoomEternalContext)
    context.state_key = "room:test:1"
    context.session_state = {}
    context.runtime_effects_ready = lambda *args, **kwargs: True
    context.get_ap_state_key = lambda: "room:test:1"
    context.server_checked_locations_ready = True
    context.checked_locations = {7770021}
    context.automap_cleanup_session = "test_sess"
    context.automap_cleanup_epoch = None
    context.automap_cleanup_submitted = set()
    context.automap_cleanup_retry = {}
    context.automap_cleanup_status = {}
    context.automap_local_cleanup_owned = set()
    context.cached_map_identity = None
    context.current_map_name = None
    return context


class TestAutomapCleanup(unittest.TestCase):
    def test_current_exultia_marker_plus_stale_hub_epoch_submits_only_with_exultia_epoch(self):
        context = _create_test_context()
        context.automap_cleanup_epoch = "2:1788279760388090513"
        context.cached_map_identity = {
            "gameplay_epoch": "3:1788279760388090599",
            "runtime_map": "game/sp/e1m2_battle/e1m2_battle",
            "map_key": "e1m2_war",
        }
        context.current_map_name = "game/sp/e1m2_battle/e1m2_battle"

        sent = []
        with patch("doom_eap.runtime.bridge_client.send_command", side_effect=lambda cmd, **kwargs: sent.append((cmd, kwargs)) or True):
            with patch("doom_eap.runtime.bridge_client.rpc_execution_enabled", return_value=True):
                changed = context.reconcile_checked_automap_cleanup("test")

        self.assertTrue(changed)
        self.assertEqual(context.automap_cleanup_epoch, "3:1788279760388090599")
        self.assertEqual(len(sent), 1)
        _, kwargs = sent[0]
        self.assertEqual(kwargs.get("materialization_lease"), "3:1788279760388090599")
        self.assertEqual(kwargs.get("execution_class"), MAP_ENTITY_SAFE)
        self.assertEqual(kwargs.get("operation"), CHECKED_VISUAL_HIDE)

    def test_map_marker_mismatch_writes_no_spool(self):
        context = _create_test_context()
        context.cached_map_identity = {
            "gameplay_epoch": "3:1788279760388090599",
            "runtime_map": "game/sp/e1m2_battle/e1m2_battle",
            "map_key": "e1m2_war",
        }
        context.current_map_name = "game/sp/hub/hub"

        sent = []
        with patch("doom_eap.runtime.bridge_client.send_command", side_effect=lambda cmd, **kwargs: sent.append((cmd, kwargs)) or True):
            with patch("doom_eap.runtime.bridge_client.rpc_execution_enabled", return_value=True):
                changed = context.reconcile_checked_automap_cleanup("test")

        self.assertFalse(changed)
        self.assertEqual(len(sent), 0)

    def test_failed_queue_submission_retries_after_backoff(self):
        context = _create_test_context()
        context.cached_map_identity = {
            "gameplay_epoch": "3:1788279760388090599",
            "runtime_map": "game/sp/e1m2_battle/e1m2_battle",
            "map_key": "e1m2_war",
        }
        context.current_map_name = "game/sp/e1m2_battle/e1m2_battle"

        sent_count = 0
        def fake_send_command_fail(*args, **kwargs):
            nonlocal sent_count
            sent_count += 1
            return False

        def fake_send_command_success(*args, **kwargs):
            nonlocal sent_count
            sent_count += 1
            return True

        current_time = 100.0
        with patch("doom_eap.runtime.bridge_client.time.monotonic", side_effect=lambda: current_time):
            with patch("doom_eap.runtime.bridge_client.rpc_execution_enabled", return_value=True):
                # 1. Failed attempt
                with patch("doom_eap.runtime.bridge_client.send_command", side_effect=fake_send_command_fail):
                    res1 = context.reconcile_checked_automap_cleanup("attempt1")
                self.assertFalse(res1)
                self.assertEqual(sent_count, 1)

                # 2. Immediate second call is suppressed by backoff deadline
                with patch("doom_eap.runtime.bridge_client.send_command", side_effect=fake_send_command_fail):
                    res2 = context.reconcile_checked_automap_cleanup("attempt2")
                self.assertFalse(res2)
                self.assertEqual(sent_count, 1)

                # 3. Advance time past backoff deadline -> retries and succeeds
                current_time = 200.0
                with patch("doom_eap.runtime.bridge_client.send_command", side_effect=fake_send_command_success):
                    res3 = context.reconcile_checked_automap_cleanup("attempt3")
                self.assertTrue(res3)
                self.assertEqual(sent_count, 2)

    def test_successful_queue_submission_remains_terminal_across_events(self):
        context = _create_test_context()
        context.cached_map_identity = {
            "gameplay_epoch": "3:1788279760388090599",
            "runtime_map": "game/sp/e1m2_battle/e1m2_battle",
            "map_key": "e1m2_war",
        }
        context.current_map_name = "game/sp/e1m2_battle/e1m2_battle"

        sent = []
        with patch("doom_eap.runtime.bridge_client.send_command", side_effect=lambda cmd, **kwargs: sent.append((cmd, kwargs)) or True):
            with patch("doom_eap.runtime.bridge_client.rpc_execution_enabled", return_value=True):
                # Initial successful submission
                res_init = context.reconcile_checked_automap_cleanup("initial")
                self.assertTrue(res_init)
                self.assertEqual(len(sent), 1)

                # Subsequent calls across various lifecycle triggers
                for trigger in ("server_connected", "server_checked_update", "level_ready", "connect_or_reconnect", "connect_or_reconnect"):
                    res = context.reconcile_checked_automap_cleanup(trigger)
                    self.assertFalse(res)

                # Total submissions remain exactly 1
                self.assertEqual(len(sent), 1)

    def test_new_gameplay_epoch_permits_one_new_submission(self):
        context = _create_test_context()
        context.cached_map_identity = {
            "gameplay_epoch": "3:1788279760388090599",
            "runtime_map": "game/sp/e1m2_battle/e1m2_battle",
            "map_key": "e1m2_war",
        }
        context.current_map_name = "game/sp/e1m2_battle/e1m2_battle"

        sent = []
        with patch("doom_eap.runtime.bridge_client.send_command", side_effect=lambda cmd, **kwargs: sent.append((cmd, kwargs)) or True):
            with patch("doom_eap.runtime.bridge_client.rpc_execution_enabled", return_value=True):
                # Epoch 1
                self.assertTrue(context.reconcile_checked_automap_cleanup("epoch1"))
                self.assertEqual(len(sent), 1)
                self.assertEqual(sent[0][1]["materialization_lease"], "3:1788279760388090599")

                # Duplicate in Epoch 1 is suppressed
                self.assertFalse(context.reconcile_checked_automap_cleanup("epoch1_repeat"))
                self.assertEqual(len(sent), 1)

                # Transition to Epoch 2
                context.cached_map_identity = {
                    "gameplay_epoch": "4:1788279760388099999",
                    "runtime_map": "game/sp/e1m2_battle/e1m2_battle",
                    "map_key": "e1m2_war",
                }
                self.assertTrue(context.reconcile_checked_automap_cleanup("epoch2"))
                self.assertEqual(len(sent), 2)
                self.assertEqual(sent[1][1]["materialization_lease"], "4:1788279760388099999")

                # Duplicate in Epoch 2 is suppressed
                self.assertFalse(context.reconcile_checked_automap_cleanup("epoch2_repeat"))
                self.assertEqual(len(sent), 2)

    def test_local_pickup_cleanup_ownership_suppresses_reconciliation(self):
        context = _create_test_context()
        epoch = "3:1788279760388090599"
        context.cached_map_identity = {
            "gameplay_epoch": epoch,
            "runtime_map": "game/sp/e1m2_battle/e1m2_battle",
            "map_key": "e1m2_war",
            "mtime_ns": 1000,
        }
        context.automap_cleanup_epoch = epoch
        context.current_map_name = "game/sp/e1m2_battle/e1m2_battle"
        context.automap_local_cleanup_owned.add((epoch, 7770021))

        sent = []
        with patch("doom_eap.runtime.bridge_client.send_command", side_effect=lambda cmd, **kwargs: sent.append((cmd, kwargs)) or True):
            with patch("doom_eap.runtime.bridge_client.rpc_execution_enabled", return_value=True):
                changed = context.reconcile_checked_automap_cleanup("test")

        self.assertFalse(changed)
        self.assertEqual(len(sent), 0)
        status = context.automap_cleanup_status.get((epoch, "room:test:1", "game/sp/e1m2_battle/e1m2_battle", "7770021"))
        self.assertEqual(status, "LOCAL_FLOW_OWNS_EFFECT")
