"""External AP websocket peer owning stored data; it does not drive bridge internals."""

import asyncio
import json
import os
import sys
from pathlib import Path

from websockets.legacy.server import serve


def signal(event, **fields):
    print("DELIVERY\t" + json.dumps({"event": event, **fields}), flush=True)


def _apply_operations(packet, value):
    for operation in packet["operations"]:
        if operation["operation"] == "add":
            value += operation["value"]
        elif operation["operation"] == "max":
            value = max(value, operation["value"])
        else:
            raise ValueError(operation)
    return value


class Server:
    def __init__(self, root):
        self.path = root / "ap-storage.json"
        self.values = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.socket = None
        self.pending = []
        self.ammo_fault = ""

    async def connect(self, socket):
        self.socket = socket
        signal("connected")
        async for raw in socket:
            for packet in json.loads(raw):
                await self.receive(packet, socket)

    async def receive(self, packet, socket):
        print("AP_RECEIVE " + json.dumps(packet), flush=True)
        if packet["cmd"] == "Set":
            reply = self.debit(packet)
            if reply is None:
                return
            operations = packet["operations"]
            if operations and operations[0]["operation"] == "add":
                self.pending.append(reply)
            else:
                await socket.send(json.dumps([reply]))
                if self.ammo_fault == "duplicate" and "ammo/ops/" in packet["key"]:
                    await socket.send(json.dumps([self.debit(packet)]))
        elif packet["cmd"] == "Get":
            await socket.send(json.dumps([{
                "cmd": "Retrieved", "keys": {key: self.values.get(key) for key in packet["keys"]},
            }]))

    def debit(self, packet):
        key = packet["key"]
        operation_key = "ammo/ops/" in key
        if operation_key and self.ammo_fault == "refused":
            return {"cmd": "SetReply", "key": key, "value": None}
        old = self.values.get(key, packet["default"])
        value = old
        value = _apply_operations(packet, value)
        self.values[key] = value
        self.save()
        if any(operation["operation"] == "add" for operation in packet["operations"]):
            signal("debit", key=key, value=value)
        if operation_key and self.ammo_fault == "lost":
            return None
        if operation_key and self.ammo_fault == "foreign":
            key += "/foreign"
        return {"cmd": "SetReply", "key": key, "value": value, "original_value": old}

    def save(self):
        with self.path.open("w", encoding="utf-8") as stream:
            json.dump(self.values, stream)
            stream.flush()
            os.fsync(stream.fileno())

    async def control(self, request):
        if request["action"] == "store":
            self.values.update(request["values"])
            self.save()
            signal("stored")
            return
        if request["action"] == "ammo_fault":
            self.ammo_fault = request["fault"]
            signal("configured")
            return
        packets = request.get("packets", [])
        if request["action"] == "release":
            packets = self.pending + packets
            self.pending = []
        print("AP_SEND " + json.dumps(packets), flush=True)
        await self.socket.send(json.dumps(packets))
        signal("sent")


async def main():
    peer = Server(Path(sys.argv[1]))
    async with serve(peer.connect, "127.0.0.1", 0) as server:
        signal("ready", port=server.sockets[0].getsockname()[1])
        while line := await asyncio.to_thread(sys.stdin.readline):
            await peer.control(json.loads(line))


if __name__ == "__main__":
    asyncio.run(main())
