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
    assert len(list(tmp_path.glob("*.cmd"))) == 2
    assert messages == [{"cmd": "Set", "key": "doom_eap:room-A:ammo_refill_consumed", "default": 0,
                         "operations": [{"operation": "add", "value": 1}], "want_reply": True}]
    ammo.consume_consumed(reply)
    assert not ammo.pending and gate["enabled"]
    assert ammo.available == (2 if reply == 1 and reply is not True else 3)
    assert len(list(tmp_path.glob("*.cmd"))) == (2 if reply == 1 and reply is not True else 0)
    assert not any(event[1].get("status") == "verified" for event in events)


def test_partial_staging_refusal_does_not_spend_or_leave_the_first_command(tmp_path):
    messages = []

    async def send(packets):
        messages.extend(packets)

    ammo, _, gate, _ = setup_ammo(tmp_path, send, refuse_stage=1)
    assert not asyncio.run(ammo.request(AmmoCommandScope("room-A", "map", "slot", True), (None, {})))
    assert messages == [] and not list(tmp_path.glob("*.cmd"))
    assert ammo.available == 3 and not ammo.pending and gate["enabled"]


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
    assert not active_crucible("progressive_special_weapon", [7770901, 7770901])


def test_readiness_preserves_order_and_requires_no_universal_save_proof():
    facts = dict(placement_ready=True, item_ready=True, connected=True, namespace_match=True,
                 marker={"gameplay_epoch": "1:2", "runtime_map": "tag-map"}, active_lease="1:2",
                 native_safe=True, runtime_ready=True, active_slot=None, available=2)
    assert ammo_readiness(**facts)[0] is None
    facts.update(placement_ready=False, native_safe=False)
    assert ammo_readiness(**facts)[0] == "placement_connected"


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
