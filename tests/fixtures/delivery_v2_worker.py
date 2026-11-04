"""Drive coordinator entry points with isolated AP/Core processes and commit barriers."""

import asyncio
import json
import sys
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

from delivery_signals import signal
from websockets.legacy.client import connect

from doom_eap.runtime import sentinel_protocol_generated as codec
from doom_eap.runtime.ammo import LedgerAmmoRefill
from doom_eap.runtime.ammo_adapters import AmmoStorage
from doom_eap.runtime.ammo_refill import AmmoCommandScope
from doom_eap.runtime.delivery import DeliveryCoordinator, DeliveryMode
from doom_eap.runtime.ledger import (
    AmmoState,
    Authority,
    CommandPlan,
    CommandState,
    Conflict,
    Context,
    Ledger,
    NetworkReceipt,
    ReplayPolicy,
    ResolutionAction,
    RoomKey,
)
from doom_eap.runtime.native_transport import CommandResponse, IdentityMismatch, PreparedCommand


class BarrierLedger:
    def __init__(self, ledger, worker):
        self.ledger, self.worker = ledger, worker

    def __getattr__(self, name):
        return getattr(self.ledger, name)

    async def ingest(self, *args):
        return await self.worker.commit("ingestion", self.ledger.ingest, *args)

    async def plan(self, *args, **kwargs):
        return await self.worker.commit("plan", self.ledger.plan, *args, **kwargs)

    async def prepare(self, *args):
        return await self.worker.commit("preparation", self.ledger.prepare, *args)

    async def resolve(self, *args):
        return await self.worker.commit("resolution", self.ledger.resolve, *args)

    async def apply(self, binding, changes, *args, **kwargs):
        name = "acceptance" if changes[0].state is CommandState.DISPATCHED else "result_commit"
        return await self.worker.commit(name, self.ledger.apply, binding, changes, *args, **kwargs)

    async def ammo(self, binding, operations, changes, *args, **kwargs):
        name = "ammo_reservation" if operations else "ammo_confirmation" if changes else "ammo_totals"
        return await self.worker.commit(name, self.ledger.ammo, binding, operations, changes, *args, **kwargs)


class Storage(AmmoStorage):
    def __init__(self, worker, ap):
        super().__init__(lambda packets: ap.send(json.dumps(packets)))
        self.worker = worker

    async def maximum(self, *args):
        await self.worker.barrier("ammo_total:before")
        return await super().maximum(*args)


class Transport:
    def __init__(self, worker):
        self.worker = worker
        self.last = None

    async def prepare(self, submission, payload):
        envelope = codec.encode("submission", submission) + payload
        immutable = {**submission["reference"], "save_binding": submission["save_binding"],
                     "deadline_ms": submission["deadline_ms"]}
        del immutable["command_id"]
        fingerprint = sha256(codec.encode("immutable_submission", immutable) + payload).digest()
        reference = {**submission["reference"], "fingerprint": fingerprint}
        return PreparedCommand(envelope, codec.encode("command_reference", reference))

    async def submit(self, command):
        await self.worker.barrier("send:before")
        reply = await self.request("submit", command.reference, command.envelope)
        await self.worker.barrier("send:after")
        return reply

    async def query(self, reference):
        if self.worker.action == "result":
            await self.worker.barrier("result:before")
        reply = await self.request("query", reference)
        if self.worker.action == "result":
            await self.worker.barrier("result:after")
        return reply

    async def cancel(self, reference):
        return await self.request("cancel", reference)

    async def ack(self, reference, token):
        await self.worker.barrier("ack:before")
        reply = await self.request("ack", reference)
        await self.worker.barrier("ack:after")
        return reply

    async def request(self, operation, reference, envelope=b""):
        reply = await self.worker.rpc({"op": operation, "reference": reference.hex(), "envelope": envelope.hex()})
        self.last = reply
        if reply["status"] == "instance_changed":
            raise IdentityMismatch("Core restarted")
        ref = codec.decode("command_reference", reference)
        status = reply["status"]
        dimensions = {"progress": {"claimed": 1, "terminal": 2}.get(status, 3), "execution": 1,
                      "effect": {"applied": 1, "dispatch_only": 2}.get(reply.get("effect"), 4),
                      "retention": {"full": 0, "compact": 1}.get(reply.get("retention"), 2),
                      "result_token": bytes([1]) * 16}
        response = {"reply": {"reference": ref, "actual_instance_id": ref["scope"]["instance_id"],
                               "dimensions": dimensions}, "error": 4 if status == "absent" else 0,
                    "execution": zero_value("sc_diagnostic_result")}
        body = codec.encode("command_response", response)
        result = None
        if reply.get("retention") == "full":
            result = zero_value("sc_inventory_result")
            result["weapons_after"] = 1
            body += codec.encode("sc_inventory_result", result)
        return CommandResponse(response, result, body)


def zero_value(name):
    data = bytearray(4096)
    if name == "sc_inventory_result":
        offset = 4 + len(codec.encode("sc_diagnostic_result", zero_value("sc_diagnostic_result")))
        data[offset:offset + 64] = b"a" * 64
    value, _ = codec._decode_type(name, data, 0)
    return value


class Worker:
    def __init__(self, ledger, core, stop, ap):
        self.ledger, self.core, self.stop = ledger, core, stop
        self.key = RoomKey("DeliveryRoomA", 0, 1, "fixture-v2")
        self.storage = Storage(self, ap)
        self.received = asyncio.Queue()

    async def receive(self, ap):
        async for raw in ap:
            for packet in json.loads(raw):
                if packet["cmd"] == "ReceivedItems":
                    await self.received.put(packet)
                else:
                    self.storage.receive(packet["cmd"], packet)

    async def barrier(self, event):
        if event == self.stop:
            signal(event)
            if not await asyncio.to_thread(sys.stdin.readline):
                raise EOFError(event)

    async def commit(self, name, operation, *args, **kwargs):
        await self.barrier(f"{name}:before")
        result = await operation(*args, **kwargs)
        await self.barrier(f"{name}:after")
        return result

    async def rpc(self, request):
        await self.core.send(json.dumps(request))
        return json.loads(await self.core.recv())

    async def ready(self):
        room = await self.ledger.room(self.key)
        self.binding = await self.ledger.bind(self.key, str(uuid4()), compatibility_revision=1, authority=Authority.LEDGER,
                                              expected_revision=room.revision if room is not None else None)
        hello = await self.rpc({"op": "hello"})
        self.ctx = Context(str(uuid4()), self.binding, 123, "123", hello["instance"],
                           "base", "map", "save", "load", 1, str(UUID(int=1)))
        await self.ledger.context(self.binding, self.ctx, ())
        self.transport = Transport(self)
        self.delivery = DeliveryCoordinator(BarrierLedger(self.ledger, self), self.binding, self.ctx,
                                            self.transport, mode=DeliveryMode.OWNER)
        self.ammo = LedgerAmmoRefill(self.delivery, self.storage, "DeliveryRoomA", timeout=0.15)

    async def snapshot(self):
        room = await self.ledger.room(self.key)
        commands = await self.ledger.commands(self.key, tuple(CommandState), None, 256)
        items = await self.ledger.items(self.key, -1, 256)
        operations = await self.ledger.ammo_operations(self.key, tuple(AmmoState), None, 256)
        totals = await self.ledger.ammo_totals(self.key)
        return {"processed": room.receipt_boundary, "received": room.receipt_boundary,
                "ready": True, "commands": [command.plan.command_id for command in commands],
                "items": [{"status": item.status.value, "actions": [action.kind.value for action in item.actions]}
                          for item in items],
                "ammo_operations": [{"id": row.operation.operation_id, "state": row.state.value,
                                     "revision": row.revision} for row in operations],
                "ammo_consumed": totals.consumed, "ammo_reserved": totals.reserved,
                "ammo_discarded": totals.discarded,
                "ammo_commands": [{"id": command.plan.command_id, "state": command.state.value,
                                   "revision": command.revision} for command in commands
                                  if command.plan.ammo_operation_id is not None]}

    async def collect(self, ap):
        signal("awaiting_packet")
        packet = await self.received.get()
        receipts = tuple(NetworkReceipt(*item) for item in packet["items"])
        await self.delivery.ingest(packet["index"], receipts)

    async def plan(self):
        payload = codec.encode_variant(25, 1, 1, {"weapons": 1, "equipment": 0})
        command = CommandPlan(str(uuid4()), 0, None, "grant", "acquisition", 0, None, None,
                              "inventory", 1, payload, self.ctx.context_id, ReplayPolicy.ADDITIVE, True, 1)
        await self.delivery.plan((command,))

    async def act(self, request, ap):
        action = request["action"]
        blocked = False
        self.action = action
        self.transport.last = None
        if action == "collect":
            await self.collect(ap)
        elif action == "plan":
            await self.plan()
        elif action == "ammo":
            scope = AmmoCommandScope("DeliveryRoomA", "map", "save", request.get("crucible", False))
            await self.ammo.request(scope, (None, {}), request.get("received", 3))
        elif action == "ammo_recover":
            await self.ammo.recover()
        elif action == "ammo_retry":
            await self.ammo.retry(request["operation_id"], request["revision"])
        elif action == "ammo_deliver":
            commands = await self.ledger.commands(self.key, (CommandState.PLANNED, CommandState.QUEUED,
                                                            CommandState.DISPATCHED), None, 256)
            for command in commands:
                try:
                    await self.delivery.prepare(command.plan.command_id, b"a" * 64)
                except Conflict:
                    blocked = True
                    break
                await self.delivery.send(command.plan.command_id)
                if request.get("recover", True):
                    await self.delivery.recover(command.plan.command_id)
        else:
            if "command_id" in request:
                command = await self.ledger.command(self.key, request["command_id"])
            else:
                commands = await self.ledger.commands(self.key, tuple(CommandState), None, 1)
                command, = commands
            command_id = command.plan.command_id
            if action == "prepare":
                await self.delivery.prepare(command_id, b"a" * 64)
            elif action == "send":
                await self.delivery.send(command_id)
            elif action == "result":
                await self.delivery.recover(command_id)
            elif action == "ack":
                await self.delivery.ack(command_id)
            elif action == "resolve":
                await self.delivery.resolve(command_id, request.get("revision", command.revision),
                                            ResolutionAction(request["resolution"]))
            else:
                stored, = await self.ledger.attempts(self.key, command_id, 0, 1)
                self.transport.last = await self.rpc({"op": action, "reference": stored.attempt.reference.hex()})
        signal("done", **await self.snapshot(), reply=self.transport.last, blocked=blocked)


async def main():
    path = Path.cwd() / "delivery.sqlite3"
    expected = path.with_suffix(".expected")
    if expected.exists():
        ledger = await Ledger.open(path)
    else:
        expected.touch()
        ledger = await Ledger.create(path)
    try:
        async with connect(sys.argv[1]) as ap, connect(sys.argv[3]) as core:
            worker = Worker(ledger, core, sys.argv[2], ap)
            await worker.ready()
            reader = asyncio.create_task(worker.receive(ap))
            try:
                signal("ready", **await worker.snapshot())
                while line := await asyncio.to_thread(sys.stdin.readline):
                    await worker.act(json.loads(line), ap)
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
    finally:
        await ledger.close()


if __name__ == "__main__":
    asyncio.run(main())
