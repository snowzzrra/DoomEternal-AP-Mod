"""Current AP storage/native-file adapters and request-file worker for Ammo Refill."""
import asyncio
import os
from collections import deque


class AmmoStorage:
    def __init__(self, send):
        self._send = send
        self._replies = {}


    async def exchange(self, packet, timeout):
        key = packet["keys"][0] if packet["cmd"] == "Get" else packet["key"]
        expected = ("Retrieved" if packet["cmd"] == "Get" else "SetReply", key)
        future = asyncio.get_running_loop().create_future()
        if expected in self._replies:
            raise RuntimeError("Ammo storage request already pending")
        self._replies[expected] = future
        try:
            await asyncio.wait_for(self._send([packet]), timeout)
            return await asyncio.wait_for(future, timeout)
        finally:
            del self._replies[expected]

    def receive(self, cmd, args):
        if cmd == "Retrieved":
            values = args.get("keys", {})
        elif cmd == "SetReply":
            values = {args.get("key"): args.get("value")}
        else:
            return
        for key, value in values.items():
            future = self._replies.get((cmd, key))
            if future is not None and not future.done():
                future.set_result(value)

    async def create(self, key, value, timeout):
        return await self.exchange({"cmd": "Set", "key": key, "default": value,
                                    "operations": [], "want_reply": True}, timeout)

    async def read(self, key, timeout):
        return await self.exchange({"cmd": "Get", "keys": [key]}, timeout)

    async def counter(self, key, timeout):
        value = await self.read(key, timeout)
        if value is None:
            return 0
        if type(value) is not int or value < 0:
            raise ValueError("Invalid ammo total")
        return value

    async def maximum(self, key, value, timeout):
        reply = await self.exchange({"cmd": "Set", "key": key, "default": 0,
                                     "operations": [{"operation": "max", "value": value}],
                                     "want_reply": True}, timeout)
        if type(reply) is not int or reply < value:
            raise ValueError("Ammo total was refused")
        return reply


class AmmoRequestPump:
    """Remove request files once and supervise serial semantic requests for one room."""
    def __init__(self, request, exit_event, logger):
        self._request = request
        self._exit_event, self._logger = exit_event, logger
        self._queue = deque()
        self._task = None
        self._generation = 0

    def reset(self):
        self._generation += 1
        self._queue.clear()
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self, generation):
        try:
            while self._queue and not self._exit_event.is_set() and generation == self._generation:
                if self._exit_event.is_set() or generation != self._generation:
                    break
                self._queue.popleft()
                await self._request()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger.exception("[Ammo Refill] Request queue failed")
        finally:
            if generation == self._generation:
                self._task = None

    def consume(self, paths):
        accepted = []
        for path in paths:
            try:
                os.remove(path)
            except FileNotFoundError:
                continue
            except OSError as error:
                self._logger.warning("[Ammo Refill] Request removal failed for %s: %s", path, error)
                continue
            accepted.append(path)
        if not accepted:
            return False
        self._queue.extend(accepted)
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run(self._generation))
        return True
