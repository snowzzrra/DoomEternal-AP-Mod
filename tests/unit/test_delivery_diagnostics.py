"""Bounded delivery-diagnostic contracts; queue semantics stay unchanged."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

sys.modules.setdefault("Utils", types.SimpleNamespace(init_logging=lambda *args, **kwargs: None))
sys.modules.setdefault("colorama", types.SimpleNamespace(init=lambda: None, deinit=lambda: None))
common_client = types.ModuleType("CommonClient")
common_client.CommonContext = object
common_client.server_loop = lambda ctx: None
common_client.gui_enabled = False
common_client.ClientCommandProcessor = object
common_client.get_base_parser = lambda: __import__("argparse").ArgumentParser()
common_client.logger = types.SimpleNamespace(info=lambda *args, **kwargs: None)
sys.modules.setdefault("CommonClient", common_client)
net_utils = types.ModuleType("NetUtils")
net_utils.ClientStatus = types.SimpleNamespace(CLIENT_GOAL=30)
sys.modules.setdefault("NetUtils", net_utils)

import bridge_client


ROOT = Path(__file__).resolve().parents[2]


class _LogCapture:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def info(self, template, payload) -> None:
        assert template == "DELIVERY_EVENT %s"
        self.records.append(json.loads(payload))


def test_normal_receipt_spool_lifecycle_is_compact_and_does_not_change_queue_files(tmp_path: Path) -> None:
    capture = _LogCapture()
    fields = {
        "receipt_index": 7,
        "item_id": 7770000,
        "item_name": "Heavy Cannon",
        "command_ordinal": 0,
        "source": "cmd",
        "bridge_revision": "test",
        "protocol_version": 3,
    }
    with patch.object(bridge_client, "QUEUE_DIR", str(tmp_path)), patch.object(
        bridge_client, "logger", capture
    ):
        bridge_client.log_delivery_event(
            "ITEM_RECEIPT", receipt_index=7, item_id=7770000, item_name="Heavy Cannon"
        )
        assert bridge_client.send_command(
            "ai_ScriptCmdEnt ap_rpc_v3_7770000 activate",
            coalesce_key="recv-000007-item-7770000-effect-00",
            arm_rpc=False,
            already_queued_ok=True,
            delivery_fields=fields,
        )
        assert sorted(path.name for path in tmp_path.iterdir()) == [
            "recv-000007-item-7770000-effect-00.cmd"
        ]

    assert [record["event"] for record in capture.records] == [
        "ITEM_RECEIPT", "SPOOL_CREATE"
    ]
    assert capture.records[1]["command_id"] == "recv-000007-item-7770000-effect-00"
    assert "command" not in capture.records[1]


def test_duplicate_rejection_logs_without_changing_existing_coalescing_result(tmp_path: Path) -> None:
    capture = _LogCapture()
    fields = {"receipt_index": 7, "item_id": 7770000, "item_name": "Heavy Cannon"}
    with patch.object(bridge_client, "QUEUE_DIR", str(tmp_path)), patch.object(
        bridge_client, "logger", capture
    ):
        assert bridge_client.send_command("safe", coalesce_key="recv-7", arm_rpc=False, already_queued_ok=True, delivery_fields=fields)
        assert bridge_client.send_command("safe", coalesce_key="recv-7", arm_rpc=False, already_queued_ok=True, delivery_fields=fields)
    assert [record["event"] for record in capture.records] == [
        "SPOOL_CREATE", "QUEUE_DUPLICATE_REJECT"
    ]
    assert capture.records[-1]["reason"] == "spool_exists"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["recv-7.cmd"]


def test_native_diagnostics_cover_recovery_gate_and_throttled_stall_without_payload_logging() -> None:
    source = (ROOT / "native/client/ap_client_exe.cpp").read_text(encoding="utf-8")
    for event in (
        "QUEUE_RECOVER", "QUEUE_IMPORT", "QUEUE_DUPLICATE_REJECT",
        "GATE_TRANSITION", "RPC_DISPATCH", "RPC_RESULT", "ACK_REMOVE", "QUEUE_STALL",
    ):
        assert event in source
    assert "kRpcStallWarnMs" in source
    assert "result=ack_executed_persistence_unknown" in source
    assert '"Queued command: command_id="' not in source
    assert '"Executing queued command: "' not in source
