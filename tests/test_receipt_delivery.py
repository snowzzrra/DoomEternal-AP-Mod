import copy
import json
import logging
import asyncio
import importlib
from types import SimpleNamespace


def test_weapon_point_reconciliation_does_not_hold_other_receipts(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    archipelago = root.parent / "Archipelago"
    if not (archipelago / "CommonClient.py").is_file():
        import pytest
        pytest.skip("Archipelago source required for bridge integration")
    configuration = tmp_path / "config.json"
    for directory in (tmp_path / "game/base", tmp_path / "local", tmp_path / "Steam/userdata/1/782330/remote"):
        directory.mkdir(parents=True)
    configuration.write_text(json.dumps({
        "doom_base_dir": str(tmp_path / "game/base"),
        "save_games_dir": str(tmp_path / "local"),
        "steam_remote_dir": str(tmp_path / "Steam/userdata/1/782330/remote"),
        "client_state_file": str(tmp_path / "state.json"),
        "bridge_log_path": str(tmp_path / "bridge.log"),
    }))
    monkeypatch.setenv("DOOM_AP_CONFIG_FILE", str(configuration))
    monkeypatch.setenv("DOOM_AP_APPLICATION_DIR", str(tmp_path))
    monkeypatch.syspath_prepend(str(archipelago))
    monkeypatch.syspath_prepend(str(root / "packaging/standalone_runtime"))
    bridge = importlib.import_module("doom_eap.runtime.bridge_client")

    async def consume():
        context = bridge.DoomEternalContext(None, None)
        context.state_key = "room"
        context.server = object()
        context.auth = "Review"
        context.slot = 1
        context.item_state_ready = True
        context.session_state = bridge.default_session_state()
        context.session_state["weapon_points"] = {"version": 1}
        context.items_received = [
            SimpleNamespace(item=7770016, location=11, player=1, flags=0),
            SimpleNamespace(item=7770904, location=12, player=1, flags=1),
        ]
        checkpoint = []
        notifications = []

        def reconcile(_history):
            checkpoint.append(True)
            raise RuntimeError("native queue full")

        monkeypatch.setattr(context, "weapon_points_receipt_owner", lambda: (SimpleNamespace(reconcile=reconcile), {}))
        monkeypatch.setattr(context, "_reconcile_native_receipt_owners", lambda _: None)
        from doom_eap.contracts.runtime_context import RuntimeContext
        runtime_context = RuntimeContext("base/e1m1_intro", "Base", ("game/sp/e1m1_intro/e1m1_intro",), ("e1m1_intro",), frozenset())
        monkeypatch.setattr(context, "_refresh_runtime_context", lambda _: runtime_context)
        monkeypatch.setattr(context, "_active_materialization_lease", lambda _: "42:1")
        monkeypatch.setattr(context, "spool_item_commands", lambda item, index, **kw: (notifications.append((item, index)) or True, ""))
        monkeypatch.setattr(context, "spool_deferred_receipt_notification", lambda item, index, **kw: (notifications.append((item, index)) or True, ""))
        monkeypatch.setattr(context, "persist_session_state", lambda: None)
        monkeypatch.setattr(context, "_reconcile_blood_punch", lambda _: None)
        monkeypatch.setattr(context, "_trigger_live_context_materialization", lambda: None)
        monkeypatch.setattr(context, "onboard_bootstrap", lambda _: None)
        await context.process_pending_item_receipts("received_items")
        assert checkpoint == [True]
        assert context.items_processed == 2
        assert notifications == [(7770016, 0), (7770904, 1)]
        assert context.session_state["weapon_points"] == {"version": 1}

    asyncio.run(consume())
from dataclasses import replace
from pathlib import Path

from doom_eap.contracts.receipt_delivery import ReceiptIntent, ReceiptFeedbackFacts, ReceiptPlan, ReceiptPublicationScope
from doom_eap.runtime.receipt_delivery import ReceiptDelivery, compile_receipt_plan
from doom_eap.runtime.receipt_publication import ReceiptPublication, receipt_command_id
from doom_eap.runtime.command_spool import CommandSpool
from doom_eap.contracts.receipt_delivery import NEW_RECEIPT, HISTORICAL_OWNERSHIP, PRESENTATION_REPAIR
from doom_eap.runtime.item_reconciliation import load_policy_registry


def test_feedback_is_live_occurrence_only_alternates_per_item_and_preserves_special_ownership(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    definitions = {int(key): value for key, value in json.loads((root / "data/items.json").read_text()).items()}
    policies = load_policy_registry(root / "data/item_replay_policies.json", definitions)
    facts = ReceiptFeedbackFacts(1, 10, None, True, frozenset(), frozenset(), frozenset())
    monkeypatch.setattr(Path, "read_text", lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("planner discovery")))

    def plan(request, current=facts, stage=None):
        return compile_receipt_plan(request, current, stage, definitions, policies, {7770001: 1, 7770901: 1})

    request = ReceiptIntent(7770001, 10, NEW_RECEIPT)
    first, second = plan(request), plan(request, replace(facts, owned_count=2))
    first_toast = [command for command in first.commands if "ap_notify_item_" in command]
    second_toast = [command for command in second.commands if "ap_notify_item_" in command]
    assert len(first_toast) == len(second_toast) == 1 and first_toast != second_toast
    for current in (replace(facts, owned_count=None),
                    replace(facts, source_location=100, checked=frozenset({100}),
                            local_locations=frozenset({100}), placements=frozenset({100}))):
        assert not any("ap_notify_item_" in command for command in plan(request, current).commands)
    assert plan(replace(request, intent=HISTORICAL_OWNERSHIP)).commands == ()
    assert not any("ap_notify_item_" in command for command in plan(replace(request, intent=PRESENTATION_REPAIR)).commands)
    assert "removeInventoryItem weapon/player/crucible" not in plan(ReceiptIntent(7770901, 10, NEW_RECEIPT), stage=1).commands
    assert "removeInventoryItem weapon/player/crucible" not in plan(ReceiptIntent(7770901, 10, NEW_RECEIPT), stage=0).commands


def test_publication_progress_resumes_and_deferred_feedback_does_not_repeat_effects(tmp_path):
    logger = logging.getLogger("receipt-delivery")
    spool = CommandSpool(tmp_path, arm_rpc=lambda *_: None, log_delivery=lambda *_a, **_kw: None, logger=logger)
    calls, commits, refuse = [], [], {"second": True}

    def send(command, **options):
        calls.append((command, options))
        if options["delivery_fields"].get("command_ordinal") == 1 and refuse["second"]:
            return False
        return spool.publish(command, **options).accepted

    publication = ReceiptPublication(send)
    scope = ReceiptPublicationScope("room:A", "map", "GAME-AUTOSAVE0", "revision", 3,
                                    packet_received_ns=1, materialization_lease="1:2", context_identity="base")
    service, state = ReceiptDelivery(logger), {"unrelated": 7}
    service.bind(state)
    plan = ReceiptPlan(("ai_ScriptCmdEnt ap_rpc_v3_7770001_1 activate",
                        "ai_ScriptCmdEnt ap_notify_item_7770001_1_a activate"), "item")

    def persist(reason):
        commits.append((reason, copy.deepcopy(state)))

    assert not service.spool(plan, 7770001, 10, "Shotgun", scope, publication, persist)[0]
    assert state["item_command_groups"]["10"]["next_command"] == 1
    assert len(list(tmp_path.glob("*.cmd"))) == 1
    refuse["second"] = False
    assert service.spool(plan, 7770001, 10, "Shotgun", scope, publication, persist)[0]
    assert "item_command_groups" not in state and state["unrelated"] == 7
    assert len([command for command, _ in calls if "ap_rpc_v3" in command]) == 1
    assert commits[-1][0] == "item_command_group_complete"
    assert {path.stem for path in tmp_path.glob("*.cmd")} == {
        receipt_command_id(scope.state_key, 7770001, 10, index, command) for index, command in enumerate(plan.commands)
    }
    assert calls[0][1]["delivery_fields"]["context_identity"] == "base"
    assert calls[0][1]["materialization_lease"] == "1:2"

    before = len(calls)
    assert service.deferred_notification(plan, 7770001, 11, "Shotgun", scope, publication, lambda: persist("feedback"))[0]
    assert service.deferred_notification(plan, 7770001, 11, "Shotgun", scope, publication, lambda: persist("feedback"))[0]
    assert len(calls) == before + 1 and "ap_notify_item_" in calls[-1][0]
    assert calls[-1][1]["delivery_fields"]["source"] == "deferred_receipt_notification"
    assert "materialization_lease" not in calls[-1][1]
    assert state["receipt_notifications"]["11"] == {
        "item_id": 7770001, "command_id": receipt_command_id(scope.state_key, 7770001, 11, 0, plan.commands[-1]),
    }
    service.bind({})
    assert service.deferred_notification(plan, 7770001, 11, "Shotgun", replace(scope, state_key="room:B"),
                                         publication, lambda: None)[0]
    assert calls[-1][1]["state_key"] == "room:B"


def test_mapping_repair_ledger_selects_one_unrepaired_occurrence_and_preserves_revision_schema():
    service, state, reasons = ReceiptDelivery(logging.getLogger("receipt-delivery")), {}, []
    service.bind(state)
    revisions = {1: {8}, 2: {9}, 4: {10}, 5: {11}}
    assert service.next_mapping_repair((8,), 2, 5, revisions).action == "deferred"
    first = service.next_mapping_repair((8, 8, 12), 3, 5, revisions)
    assert first.item_index == 0
    service.record_mapping_repair(first, "published", reasons.append)
    second = service.next_mapping_repair((8, 8, 12), 3, 5, revisions)
    assert second.item_index == 1
    service.record_mapping_repair(second, "published", reasons.append)
    assert state["mapping_repair_indices"] == [0, 1]
    assert service.next_mapping_repair((8, 8, 12), 3, 5, revisions).action == "complete"
    service.finish_mapping_repair(5, reasons.append)
    assert state == {"item_mapping_revision": 5}
    assert service.next_mapping_repair((), 3, 5, revisions).action == "done"
    service.reset(5)
    assert state == {"item_mapping_revision": 5, "receipt_notifications": {}}
