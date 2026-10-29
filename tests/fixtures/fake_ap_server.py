"""External AP websocket peer owning stored data; it does not drive bridge internals."""

import asyncio
import json
import os
from pathlib import Path
import sys

from websockets.legacy.server import serve


def signal(event, **fields):
    print("DELIVERY\t" + json.dumps({"event": event, **fields}), flush=True)


class Server:
    def __init__(self, root):
        self.path = root / "ap-storage.json"
        self.values = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.socket = None
        self.pending = []

    async def connect(self, socket):
        self.socket = socket
        signal("connected")
        async for raw in socket:
            for packet in json.loads(raw):
                await self.receive(packet)

    async def receive(self, packet):
        print("AP_RECEIVE " + json.dumps(packet), flush=True)
        if packet["cmd"] == "Set":
            self.debit(packet)
        elif packet["cmd"] == "Get":
            await self.socket.send(json.dumps([{
                "cmd": "Retrieved", "keys": {key: self.values.get(key, 0) for key in packet["keys"]},
            }]))

    def debit(self, packet):
        key = packet["key"]
        old = self.values.get(key, packet["default"])
        value = old
        for operation in packet["operations"]:
            if operation["operation"] != "add":
                raise ValueError(operation)
            value += operation["value"]
        self.values[key] = value
        with self.path.open("w", encoding="utf-8") as stream:
            json.dump(self.values, stream)
            stream.flush()
            os.fsync(stream.fileno())
        self.pending.append({"cmd": "SetReply", "key": key, "value": value, "original_value": old})
        signal("debit", key=key, value=value)

    async def control(self, request):
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
