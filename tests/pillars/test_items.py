import asyncio
import gc
import json
import sys
from types import SimpleNamespace

from websockets.legacy.client import connect

from doom_eap.runtime import delivery
from doom_eap.runtime.delivery import RoomDelivery
from doom_eap.runtime.ledger import CommandState, NetworkReceipt
from doom_eap.runtime.native_transport import NativeTransport
from tests.delivery_scenario_support import FIXTURES, Scenario, environment, runtime_bridge


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
    packet = await asyncio.wait_for(received.get(), 30)
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
        (sys.executable, str(FIXTURES / "fake_delivery_core.py"), "--wire", str(peer.root)), scope, env=env, timeout=30))

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
                    packet = await asyncio.wait_for(received.get(), 30)
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
            received, = json.loads(await asyncio.wait_for(socket.recv(), 30))
            await process_server_cmd(ctx, received)
            assert [item.item for item in ctx.items_received] == [7770900]
            assert ctx.receipt_session.packet_is_live_tail(0)

    try:
        asyncio.run(play())
    finally:
        peer.close()
