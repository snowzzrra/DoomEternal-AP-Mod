import asyncio
import gc
import json
import queue
import sys
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from websockets.exceptions import WebSocketException
from websockets.legacy.client import connect

from doom_eap.runtime import delivery
from doom_eap.runtime import sentinel_protocol_generated as codec
from doom_eap.runtime.delivery import DeliveryCoordinator, DeliveryMode, RoomDelivery
from doom_eap.runtime.ledger import (
    Authority,
    CommandChange,
    CommandPlan,
    CommandState,
    Conflict,
    Context,
    Ledger,
    NetworkReceipt,
    ReplayPolicy,
    RoomKey,
)
from doom_eap.runtime.native_transport import NativeTransport, TransportError
from doom_eap.strings import text as player_text
from tests.delivery_scenario_support import FIXTURES, Scenario, environment, runtime_bridge
from tools.validation.player_literals import mojibake_issues


async def read_packets(socket, received, value):
    async for raw in socket:
        for packet in json.loads(raw):
            if packet["cmd"] == "ReceivedItems":
                await received.put(packet)
            else:
                value.room_delivery.storage.receive(packet["cmd"], packet)


async def assert_initial_delivery(owner, peer):
    commands = await owner._rows()
    assert {row.plan.receipt_index: row.state for row in commands} == {
        0: CommandState.CONFIRMED, 1: CommandState.CONFIRMED,
        2: CommandState.EFFECT_UNKNOWN, 3: CommandState.EFFECT_UNKNOWN,
    }
    effects = [json.loads(line) for line in (peer.root / "wire-effects.jsonl").read_text().splitlines()]
    assert [(row["command"], row["tag"], row["payload"]) for row in effects] == [
        (18, 1, {"amount": 3, "expected_gained": 0}),
        (46, 1, {"entity": "ap_rpc_v3_7770900"}),
        (46, 1, {"entity": "ap_rpc_v3_7770043"}),
        (46, 1, {"entity": "ap_rpc_v3_7770025"}),
    ]


async def resume_delivery(owner):
    await owner.reconcile(SimpleNamespace(epoch=1))


async def receive_offline_item(peer, value, owner, received):
    async def game_offline(*args):
        return False

    online = value.items.runtime_effects_ready
    value.items.runtime_effects_ready = game_offline
    offline = [7770903, 7770005, 1, 0]
    peer.ap.send({"action": "send", "packets": [{"cmd": "ReceivedItems", "index": 4, "items": [offline]}]})
    peer.ap.expect("sent")
    packet = await asyncio.wait_for(received.get(), 5)
    assert packet["index"] == len(value.items_received)
    value.items_received.extend(NetworkReceipt(*item) for item in packet["items"])
    value.receipt_session.observe_packet(4, 1, 5, 2)
    await owner.reconcile(SimpleNamespace(epoch=1))
    assert (await owner.ledger.room(owner.key)).receipt_boundary == 5
    assert len((peer.root / "wire-effects.jsonl").read_text().splitlines()) == 4

    value.items.runtime_effects_ready = online
    await resume_delivery(owner)
    await owner.reconcile(SimpleNamespace(epoch=1))
    after = [json.loads(line) for line in (peer.root / "wire-effects.jsonl").read_text().splitlines()]
    assert (len(after), len({row["id"] for row in after})) == (5, 5)
    assert json.loads((peer.root / "wire-core.json").read_text())["points"] == 6


def test_items_reconnect_and_offline_delivery_once(tmp_path, monkeypatch):
    peer = Scenario(tmp_path / "peers")
    value, definitions, policies = runtime_bridge(tmp_path)
    env = environment(peer.root)
    scope = {"pid": 123, "process_created": 123, "instance_id": [0] * 15 + [2], "generation": 1}
    env["PILLAR_CORE_SCOPE"] = json.dumps(scope)
    monkeypatch.setattr(delivery, "NativeTransport", lambda command, scope, **kw: NativeTransport(
        (sys.executable, str(FIXTURES / "fake_delivery_core.py"), "--wire", str(peer.root)), scope, env=env))

    async def play():
        owner = RoomDelivery(value, {"client_state_file": str(tmp_path / "state.json")}, definitions, policies,
                             lambda *args, **kwargs: None)
        value.room_delivery = owner
        async with connect(f"ws://127.0.0.1:{peer.port}") as socket:
            peer.ap.expect("connected")
            received = asyncio.Queue()

            reader = asyncio.create_task(read_packets(socket, received, value))

            async def send(packets):
                await socket.send(json.dumps(packets))

            value.send_msgs = send
            items = [[7770900, 7770001, 1, 0], [7770903, 7770002, 1, 0],
                     [7770043, 7770003, 1, 4], [7770025, 7770004, 1, 0]]
            try:
                for restart in range(2):
                    peer.ap.send({"action": "send", "packets": [{"cmd": "ReceivedItems", "index": 0, "items": items}]})
                    peer.ap.expect("sent")
                    packet = await asyncio.wait_for(received.get(), 5)
                    value.items_received = [NetworkReceipt(*item) for item in packet["items"]]
                    value.receipt_session.observe_packet(0, len(items), len(items), 1)
                    _, error = await owner.reconcile(SimpleNamespace(epoch=1))
                    assert error is None
                    rows = await owner.ledger.receipts(owner.key, 0, 256)
                    assert [row.item for row in rows] == [item[0] for item in items]
                    if restart == 0:
                        await owner.close()
                        owner = RoomDelivery(value, {"client_state_file": str(tmp_path / "state.json")},
                                             definitions, policies, lambda *args, **kwargs: None)
                        value.room_delivery = owner
                await assert_initial_delivery(owner, peer)
                await receive_offline_item(peer, value, owner, received)
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
                await owner.close()
                gc.collect()
                await asyncio.sleep(0)

    try:
        asyncio.run(play())
    finally:
        peer.close()


@pytest.mark.parametrize("refusal", ["queue_full", "not_admitted", "identity_capacity", None])
def test_delivery_refusals_and_context_loss_reach_a_decision(tmp_path, monkeypatch, refusal):
    async def play():
        scope = {"pid": 123, "process_created": 123, "instance_id": list(UUID(int=2).bytes), "generation": 1}
        env = environment(tmp_path)
        env["PILLAR_CORE_SCOPE"] = json.dumps(scope)
        if refusal:
            env["PILLAR_CORE_REFUSAL"] = refusal
        transport = NativeTransport((sys.executable, str(FIXTURES / "fake_delivery_core.py"), "--wire", str(tmp_path)), scope, env=env)
        ledger = await Ledger.create(tmp_path / "delivery.sqlite3")
        key = RoomKey("seed", 0, 1, "generation")
        binding = await ledger.bind(key, str(uuid4()), compatibility_revision=1, authority=Authority.LEDGER)
        context = Context(str(uuid4()), binding, 123, "123", str(UUID(int=2)), "base", "map", "save", "load", 1, str(UUID(int=1)))
        coordinator = DeliveryCoordinator(ledger, binding, context, transport, mode=DeliveryMode.OWNER)
        clock = SimpleNamespace(now=delivery.time.time_ns() + 3_600_000_000_000)
        clock.time_ns = lambda: clock.now
        monkeypatch.setattr(delivery, "time", clock)
        try:
            await ledger.context(binding, context, ())
            await ledger.ingest(binding, 0, (NetworkReceipt(7770900, 1, 1, 0),))
            plan = CommandPlan(str(uuid4()), 0, None, "grant", "acquisition", 0, None, None, "inventory", 1,
                               codec.encode_variant(25, 1, 1, {"weapons": 1, "equipment": 0}), context.context_id,
                               ReplayPolicy.ADDITIVE, True, 2)
            await coordinator.plan((plan,))
            await transport.connect()
            if refusal is None:
                await assert_context_loss(coordinator, plan)
            else:
                await assert_refusal_limit(coordinator, plan, clock)
            assert not (tmp_path / "wire-effects.jsonl").exists()
            assert len(await ledger.commands(key, tuple(CommandState), None, 10)) <= 3
        finally:
            await transport.close()
            await ledger.close()

    asyncio.run(play())


async def assert_context_loss(coordinator, plan):
    await coordinator.prepare(plan.command_id, b"a" * 64)
    coordinator.context = replace(coordinator.context, context_id=str(uuid4()), map_name="next-map")
    await coordinator.ledger.context(coordinator.binding, coordinator.context, ())
    await coordinator.send(plan.command_id)
    row = await coordinator.ledger.command(coordinator.binding.room, plan.command_id)
    assert row.state is CommandState.EFFECT_UNKNOWN
    await coordinator.ledger.apply(coordinator.binding, (
        CommandChange(plan.command_id, row.revision, CommandState.STALE, None, row.result, None),), (), ())
    await coordinator.recover(plan.command_id)
    row = await coordinator.ledger.command(coordinator.binding.room, plan.command_id)
    assert row.state is CommandState.EFFECT_UNKNOWN
    await coordinator.resolve(plan.command_id, row.revision, delivery.ResolutionAction.TRY_AGAIN)


async def assert_refusal_limit(coordinator, plan, clock):
    for attempt in range(3):
        await coordinator.prepare(plan.command_id, b"a" * 64)
        await coordinator.send(plan.command_id)
        row = await coordinator.ledger.command(coordinator.binding.room, plan.command_id)
        if attempt == 2:
            assert row.state is CommandState.REJECTED and row.reason == "retry_limit"
            await coordinator.resolve(plan.command_id, row.revision, delivery.ResolutionAction.MARK_AS_RECEIVED)
        else:
            plan = await assert_refusal_backoff(coordinator, row, clock)
            assert plan.retry_limit == 1 - attempt


async def assert_refusal_backoff(coordinator, row, clock):
    assert row.state is CommandState.STALE and row.next_eligible_at > clock.time_ns() // 1000
    for _ in range(3):
        await coordinator.recover(row.plan.command_id)
    current = await coordinator.ledger.command(coordinator.binding.room, row.plan.command_id)
    assert current.replacement_id is None
    clock.now = (row.next_eligible_at + 1) * 1000
    await coordinator.recover(row.plan.command_id)
    current = await coordinator.ledger.command(coordinator.binding.room, row.plan.command_id)
    return (await coordinator.ledger.command(coordinator.binding.room, current.replacement_id)).plan


def test_received_items_packet_reaches_real_bridge(bridge, tmp_path):
    from CommonClient import process_server_cmd

    peer = Scenario(tmp_path / "peers")

    async def play():
        ctx = bridge.DoomEternalContext(None, None)
        ctx.exit_event.set()
        async with connect(f"ws://127.0.0.1:{peer.port}") as socket:
            peer.ap.expect("connected")
            packet = {"cmd": "ReceivedItems", "index": 0, "items": [[7770900, 7770001, 1, 0]]}
            peer.ap.send({"action": "send", "packets": [packet]})
            peer.ap.expect("sent")
            received, = json.loads(await asyncio.wait_for(socket.recv(), 5))
            await process_server_cmd(ctx, received)
            assert [item.item for item in ctx.items_received] == [7770900]
            assert ctx.receipt_session.packet_is_live_tail(0)

    try:
        asyncio.run(play())
    finally:
        peer.close()


def test_refill_failure_preserves_launcher_controls(bridge, tmp_path, monkeypatch):
    async def play():
        ctx = bridge.DoomEternalContext(None, None)
        assert await ctx.items._publish_owner_ammo_refill() is None
        attempts = []
        scheduled = []
        failures = [Conflict("map loading"), TransportError("reconnecting"), WebSocketException("connection closed")]

        async def reconcile(*args):
            return None, None

        async def request(*args):
            attempts.append(1)
            if failures:
                raise failures.pop(0)
            return "operation"

        queued = []

        async def queued_request():
            queued.append(1)
            if len(queued) == 1:
                raise Conflict("room blocked")
            return await ctx.items.request_ammo_refill()

        ctx.room_delivery = SimpleNamespace(lock=asyncio.Lock(), current=lambda: True, can_spend=True,
                                           ammo=SimpleNamespace(request=request))
        ctx._connected_slot_data = {"runtime": {"special_weapon": "none"}}
        monkeypatch.setattr(ctx.items, "_owner_reconcile", reconcile)
        monkeypatch.setattr(ctx.items, "_schedule_item_delivery", scheduled.append)
        monkeypatch.setattr(ctx.ammo_requests, "_request", queued_request)
        controls = queue.Queue()
        monkeypatch.setattr(bridge.sys, "stdin", SimpleNamespace(readline=controls.get))
        monkeypatch.setattr(bridge.bridge_config, "LAUNCHER_EVENTS_ENABLED", True)
        ctx.input_task = asyncio.create_task(bridge.launcher_control_loop(ctx))
        try:
            controls.put('AP_CONTROL {"type":"ammo_refill"}\n')
            controls.put('AP_CONTROL {"type":"killsanity_counter_visibility","visible":false}\n')
            for _ in range(600):
                if scheduled:
                    break
                await asyncio.sleep(0.01)
            assert len(attempts) == 4 and scheduled == ["ammo"]
            assert ctx.delivery_actions_ready() and not ctx.killsanity_counter_visible
            assert not ctx.ammo_requests._queue
        finally:
            controls.put("/exit\n")
            await asyncio.wait_for(ctx.input_task, 5)
            ctx.ammo_requests.reset()

    asyncio.run(play())


def test_campaign_rejection_uses_catalog_and_rejects_mojibake(bridge, tmp_path, monkeypatch):
    events = []

    def reject(*args):
        raise ValueError("invalid saved campaign")

    async def play():
        ctx = bridge.DoomEternalContext(None, None)
        ctx.exit_event.set()
        ctx._item_state_error = None
        monkeypatch.setattr(ctx.items, "initialize_item_state", lambda data: None)
        monkeypatch.setattr(bridge, "UnifiedCampaign", reject)
        monkeypatch.setattr(bridge.bridge_config, "LAUNCHER_EVENTS_ENABLED", True)
        monkeypatch.setattr(bridge.bridge_config, "emit_launcher_event", lambda kind, **fields: events.append((kind, fields)))
        ctx._handle_connected({}, {}, None)

    asyncio.run(play())
    _, event = next(event for event in events if event[0] == "error")
    assert event["message"] == player_text("launcher.connection", "campaign_state")
    assert event["technical_message"] == "Campaign state incompatible: invalid saved campaign"
    source = tmp_path / "sample.py"
    source.write_text("message = '" + chr(195) + chr(173) + "'", encoding="utf-8")
    catalog = tmp_path / "data/strings"
    catalog.mkdir(parents=True)
    (catalog / "pt-BR.json").write_text(json.dumps({"message": chr(195) + chr(194)}, ensure_ascii=False), encoding="utf-8")
    assert len(mojibake_issues(tmp_path)) == 2
    source.write_text("message = 'incompatível'", encoding="utf-8")
    (catalog / "pt-BR.json").write_text('{"message":"incompatível"}', encoding="utf-8")
    assert mojibake_issues(tmp_path) == []
