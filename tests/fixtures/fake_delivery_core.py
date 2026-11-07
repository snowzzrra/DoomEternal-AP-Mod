"""Model v2 identity and retention in a separate process; this is not a wire codec."""

import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import UUID, uuid4

from delivery_signals import signal
from websockets.exceptions import ConnectionClosed
from websockets.legacy.server import serve

from doom_eap.runtime import sentinel_protocol_generated as codec


def _identity_conflict(stored, request, reference):
    return stored is not None and (
        stored["reference"] != reference
        or (request["op"] == "submit" and stored["envelope"] != request["envelope"])
    )


def _stored_result(reference, request, stored):
    if request["op"] == "dispatch_only":
        stored["result"]["effect"] = "dispatch_only"
    if request["op"] in ("ack", "expire"):
        stored["result"] = {"status": "terminal", "retention": "compact"}
    return {"reference": reference, **stored["result"]}


class Core:
    def __init__(self, root, stop):
        self.root, self.stop = root, stop
        self.instance = str(uuid4())
        self.commands = {}

    async def barrier(self, event):
        if event == self.stop:
            signal(event)
            if not await asyncio.to_thread(sys.stdin.readline):
                raise EOFError(event)

    async def connect(self, socket):
        try:
            async for raw in socket:
                response = await self.receive(json.loads(raw))
                await socket.send(json.dumps(response))
        except ConnectionClosed:
            pass

    async def receive(self, request):
        if request["op"] == "hello":
            return {"instance": self.instance, "version": 2}
        reference = request["reference"]
        decoded = codec.decode("command_reference", bytes.fromhex(reference))
        if bytes(decoded["scope"]["instance_id"]) != UUID(self.instance).bytes:
            return {"status": "instance_changed", "instance": self.instance}
        command_id = decoded["command_id"]
        stored = self.commands.get(command_id)
        if _identity_conflict(stored, request, reference):
            return {"status": "identity_conflict"}
        if request["op"] == "submit" and stored is None:
            return await self.execute(request)
        if stored is None:
            return {"status": "absent"}
        return _stored_result(reference, request, stored)

    async def execute(self, request):
        reference = request["reference"]
        decoded = codec.decode("command_reference", bytes.fromhex(reference))
        command_id = decoded["command_id"]
        stored = {"reference": reference, "envelope": request["envelope"], "result": {"status": "claimed"}}
        self.commands[command_id] = stored
        await self.barrier("attempt_journal:after")
        await self.barrier("external_effect:before")
        with (self.root / "effects.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"command": command_id, "envelope": request["envelope"]}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        effect = "dispatch_only" if decoded["command_type"] == 46 else "applied"
        stored["result"] = {"status": "terminal", "retention": "full", "effect": effect}
        await self.barrier("external_effect:after")
        signal("effect_complete")
        return {"reference": reference, **stored["result"]}


async def main():
    core = Core(Path(sys.argv[1]), sys.argv[2])
    async with serve(core.connect, "127.0.0.1", 0) as server:
        signal("ready", port=server.sockets[0].getsockname()[1], instance=core.instance)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
