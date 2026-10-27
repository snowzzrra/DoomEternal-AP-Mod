"""Accounting/publication boundaries use isolated files and the actual AP adapter."""
import asyncio
import logging

import pytest

from doom_eap.runtime.ammo_refill import AmmoRefill, AmmoCommandScope, active_crucible, ammo_readiness
from doom_eap.runtime.ammo_adapters import AmmoCommandPublication, AmmoStorage, AmmoRequestPump
from doom_eap.runtime.command_spool import CommandSpool
from doom_eap.runtime.command_spool import discard_unclaimed_command


def setup_ammo(tmp_path, send_storage, *, refuse_stage=None):
    logger = logging.getLogger("ammo-contract")
    spool = CommandSpool(tmp_path, arm_rpc=lambda *_: None, log_delivery=lambda *_, **__: None, logger=logger)
    gate = {"enabled": True}
    events = []

    def send(command, **kwargs):
        if kwargs["delivery_fields"]["stage"] == refuse_stage:
            return False
        return spool.publish(command, **kwargs).accepted

    def set_gate(enabled):
        gate["enabled"] = enabled
        return True

    publication = AmmoCommandPublication(
        send, lambda key, room: discard_unclaimed_command(tmp_path, spool.scoped_id(key, room)),
        lambda: gate["enabled"], set_gate, logger,
    )
    ammo = AmmoRefill(publication, AmmoStorage(send_storage), lambda *a, **kw: events.append((a, kw)), logger)
    ammo.bind("room-A")
    ammo.observe_receipts(3)
    ammo.consume_discarded(0)
    ammo.consume_consumed(0)
    return ammo, spool, gate, events


@pytest.mark.parametrize("reply", [1, 0, 2, -1, True, None])
def test_charge_confirmation_is_required_before_arming_and_refusals_cancel(tmp_path, reply):
    messages = []

    async def send(messages_in):
        messages.extend(messages_in)

    ammo, spool, gate, events = setup_ammo(tmp_path, send)
    scope = AmmoCommandScope("room-A", "map", "GAME-AUTOSAVE0", True)
    assert asyncio.run(ammo.request(scope, (None, {})))
    assert ammo.pending and not gate["enabled"]
    assert not list(tmp_path.glob("*.cmd"))
    assert messages == [{"cmd": "Set", "key": "doom_eap:room-A:ammo_refill_consumed", "default": 0,
                         "operations": [{"operation": "add", "value": 1}], "want_reply": True}]
    ammo.consume_consumed(reply)
    assert not ammo.pending and gate["enabled"]
    assert ammo.available == (2 if reply == 1 and reply is not True else 3)
    assert len(list(tmp_path.glob("*.cmd"))) == (2 if reply == 1 and reply is not True else 0)
    assert not any(event[1].get("status") == "verified" for event in events)


def test_partial_publication_refusal_reports_paid_failure_without_retrying_debit(tmp_path):
    messages = []

    async def send(packets):
        messages.extend(packets)

    ammo, _, gate, events = setup_ammo(tmp_path, send, refuse_stage=1)
    assert asyncio.run(ammo.request(AmmoCommandScope("room-A", "map", "slot", True), (None, {})))
    assert not list(tmp_path.glob("*.cmd"))
    ammo.consume_consumed(1)
    assert len(messages) == 1 and not list(tmp_path.glob("*.cmd"))
    assert ammo.available == 2 and not ammo.pending and gate["enabled"]
    assert "charged; command publication failed" in events[-1][1]["message"]


def test_separate_f9_uses_keep_distinct_durable_command_ids(tmp_path):
    messages = []

    async def send(packets):
        messages.extend(packets)

    ammo, _, gate, events = setup_ammo(tmp_path, send)
    scope = AmmoCommandScope("room-A", "map", "slot", True)
    assert asyncio.run(ammo.request(scope, (None, {})))
    assert not list(tmp_path.glob("*.cmd")) and not gate["enabled"]
    ammo.consume_consumed(1)
    first = {path.name for path in tmp_path.glob("*.cmd")}
    assert len(first) == 2
    assert gate["enabled"] and ammo.available == 2
    assert asyncio.run(ammo.request(scope, (None, {})))
    assert {path.name for path in tmp_path.glob("*.cmd")} == first and not gate["enabled"]
    ammo.consume_consumed(2)
    second = {path.name for path in tmp_path.glob("*.cmd")} - first
    assert len(second) == 2
    assert gate["enabled"] and ammo.available == 1
    assert len(messages) == 2 and first.isdisjoint(second)
    assert not any(event[1].get("status") == "verified" for event in events)


def test_uncertain_storage_result_cancels_commands_without_replaying_debit(tmp_path):
    async def send(_packets):
        raise OSError("ack lost")

    ammo, _, gate, events = setup_ammo(tmp_path, send)
    scope = AmmoCommandScope("room-A", "map", "slot", True)
    assert not asyncio.run(ammo.request(scope, (None, {})))
    assert not list(tmp_path.glob("*.cmd")) and gate["enabled"]
    assert events[-1][1]["status"] == "uncertain"
    ammo.consume_consumed(1)
    assert ammo.available == 2


@pytest.mark.parametrize("send_hangs", [False, True])
def test_confirmation_deadline_discards_effects_and_late_reply_only_updates_balance(tmp_path, monkeypatch, send_hangs):
    async def run():
        async def send(_packets):
            if send_hangs:
                await asyncio.Event().wait()

        monkeypatch.setattr("doom_eap.runtime.ammo_refill.AMMO_REFILL_CONFIRMATION_TIMEOUT", 0.02)
        ammo, _, gate, events = setup_ammo(tmp_path, send)
        result = await ammo.request(AmmoCommandScope("room-A", "map", "slot", True), (None, {}))
        assert result is not send_hangs
        await asyncio.sleep(0.04)
        assert not ammo.pending and gate["enabled"]
        assert ammo.available is None
        assert not list(tmp_path.glob("*.cmd"))
        assert events[-1][1]["status"] == "uncertain"
        ammo.consume_consumed(1)
        assert ammo.available == 2 and not list(tmp_path.glob("*.cmd"))
        ammo.invalidate("test shutdown")

    asyncio.run(run())


def test_invalid_plan_does_not_debit(tmp_path, monkeypatch):
    async def send(_packets):
        pytest.fail("invalid plan must not spend a charge")

    monkeypatch.setattr(AmmoCommandPublication, "command_plan", lambda *_: ("invalid",))
    ammo, _, gate, _ = setup_ammo(tmp_path, send)
    assert not asyncio.run(ammo.request(AmmoCommandScope("room-A", "map", "slot", True), (None, {})))
    assert not list(tmp_path.glob("*.cmd")) and gate["enabled"] and ammo.available == 3


def test_confirmed_debit_survives_late_send_error(tmp_path):
    async def send(_packets):
        ammo.consume_consumed(1)
        raise OSError("transport closed after reply")

    ammo, _, gate, events = setup_ammo(tmp_path, send)
    assert asyncio.run(ammo.request(AmmoCommandScope("room-A", "map", "slot", False), (None, {})))
    assert not ammo.pending and gate["enabled"] and ammo.available == 2
    assert len(list(tmp_path.glob("*.cmd"))) == 1
    assert events[-1][1]["status"] == "ready"


@pytest.mark.parametrize("fail", [False, True])
def test_late_storage_completion_cannot_cancel_or_report_in_a_new_room(tmp_path, fail):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def send(_packets):
            entered.set()
            await release.wait()
            if fail:
                raise OSError("old room transport")

        ammo, _, gate, events = setup_ammo(tmp_path, send)
        task = asyncio.create_task(ammo.request(AmmoCommandScope("room-A", "map", "slot", False), (None, {})))
        await asyncio.wait_for(entered.wait(), timeout=5)
        ammo.bind("room-B")
        ammo.observe_receipts(2)
        ammo.consume_discarded(0)
        ammo.consume_consumed(0)
        before = (ammo.balance(), len(events), dict(gate))
        release.set()
        assert await task is False
        assert (ammo.balance(), len(events), dict(gate)) == before
        assert not list(tmp_path.glob("*.cmd"))
        ammo.invalidate("test shutdown")

    asyncio.run(run())


def test_overflow_uses_monotonic_discard_storage_and_cannot_restore_consumed_ammo(tmp_path):
    messages = []

    async def send(packets):
        messages.extend(packets)

    ammo, _, _, _ = setup_ammo(tmp_path, send)
    ammo.observe_receipts(6)
    assert ammo.available == 3
    asyncio.run(ammo.normalize_overflow("test"))
    assert messages[-1]["operations"] == [{"operation": "add", "value": 3}]
    assert messages[-1]["key"] == "doom_eap:room-A:ammo_refill_discarded"
    ammo.consume_discarded(2)
    assert ammo.balance()["discarded"] == 0
    ammo.consume_discarded(3)
    ammo.consume_consumed(1)
    assert ammo.available == 2
    ammo.consume_discarded(0)
    ammo.consume_consumed(0)
    assert ammo.available == 2
    assert active_crucible("progressive_special_weapon", [7770901])
    assert active_crucible("progressive_special_weapon", [7770901, 7770901])


def test_readiness_preserves_order_and_requires_no_universal_save_proof():
    facts = dict(placement_ready=True, item_ready=True, connected=True, namespace_match=True,
                 marker={"gameplay_epoch": "1:2", "runtime_map": "tag-map"}, active_lease="1:2",
                 native_safe=True, runtime_ready=True, active_slot=None, available=2)
    assert ammo_readiness(**facts)[0] is None
    facts.update(placement_ready=False, native_safe=False)
    assert ammo_readiness(**facts)[0] == "placement_connected"


@pytest.mark.parametrize("changes,predicate,message", [
    ({"placement_ready": False}, "placement_connected", "Disconnected from Archipelago"),
    ({"item_ready": False}, "item_state_ready", "waiting for items from Archipelago"),
    ({"connected": False}, "connected", "Disconnected from Archipelago"),
    ({"namespace_match": False}, "namespace_match", "waiting for the current room's game session"),
    ({"marker": None}, "marker_valid", "current level to finish preparing"),
    ({"active_lease": "other"}, "lease_match", "current level to finish preparing"),
    ({"native_safe": False}, "native_safe", "return to DOOM and unpause"),
    ({"runtime_ready": False}, "runtime_effects_ready", "waiting for the active save and game session"),
])
def test_unavailable_explains_failed_requirement_without_spending(tmp_path, caplog, changes, predicate, message):
    messages = []

    async def send(packets):
        messages.extend(packets)

    ammo, spool, _, events = setup_ammo(tmp_path, send)
    facts = dict(placement_ready=True, item_ready=True, connected=True, namespace_match=True,
                 marker={"gameplay_epoch": "1:2"}, active_lease="1:2", native_safe=True,
                 runtime_ready=True, active_slot="GAME-AUTOSAVE0", available=3)
    caplog.set_level(logging.INFO, logger="ammo-contract")
    assert not asyncio.run(ammo.request(AmmoCommandScope("room-A", "map", "GAME-AUTOSAVE0", False),
                                       ammo_readiness(**(facts | changes))))
    assert message in events[-1][1]["message"]
    assert f"predicate={predicate}" in caplog.text
    assert messages == [] and ammo.available == 3 and not list(tmp_path.glob("*.cmd"))


def test_request_file_worker_cancels_old_room_without_replaying_removed_files(tmp_path):
    async def run():
        calls = []
        entered = asyncio.Event()
        ammo, _, _, _ = setup_ammo(tmp_path, lambda _: None)

        async def request():
            calls.append("request")
            entered.set()
            await asyncio.Event().wait()

        pump = AmmoRequestPump(ammo, request, asyncio.Event(), logging.getLogger("ammo-contract"))
        paths = [tmp_path / "AP_REFILL_REQUEST.txt", tmp_path / "AP_REFILL_REQUEST_1.txt"]
        for path in paths:
            path.write_text("request")
        assert pump.consume(paths)
        await asyncio.wait_for(entered.wait(), timeout=5)
        pump.reset()
        await asyncio.sleep(0)
        assert calls == ["request"]
        assert not pump.consume(paths)
        ammo.invalidate("test shutdown")

    asyncio.run(run())


def test_one_refill_file_observation_debits_at_most_once(tmp_path):
    async def run():
        messages = []

        async def send(packets):
            messages.extend(packets)

        ammo, _, _, _ = setup_ammo(tmp_path, send)
        scope = AmmoCommandScope("room-A", "map", "slot", True)
        accepted = asyncio.Event()

        async def request():
            assert await ammo.request(scope, (None, {}))
            accepted.set()

        pump = AmmoRequestPump(ammo, request, asyncio.Event(), logging.getLogger("ammo-contract"))
        path = tmp_path / "AP_REFILL_REQUEST.txt"
        path.write_text("request")
        assert pump.consume([path])
        assert not pump.consume([path])
        await asyncio.wait_for(accepted.wait(), timeout=5)
        assert len(messages) == 1
        assert messages[0]["operations"] == [{"operation": "add", "value": 1}]
        assert ammo.pending
        pump.reset()
        ammo.invalidate("test shutdown")

    asyncio.run(run())
