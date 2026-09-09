"""Current AP storage/native-file adapters and request-file worker for Ammo Refill."""
import asyncio
from collections import deque
import os

from doom_eap.contracts.command_publication import queue_session_namespace
from doom_eap.runtime.ammo_refill import AMMO_REFILL_ITEM_ID, AMMO_REFILL_PRIMITIVE_ITEM_ID


class AmmoStorage:
    def __init__(self, send):
        self._send = send

    async def increment(self, key, default, delta):
        await self._send([{"cmd": "Set", "key": key, "default": default,
                           "operations": [{"operation": "add", "value": delta}], "want_reply": True}])

    async def watch(self, keys):
        await self._send([{"cmd": "Get", "keys": list(keys)}, {"cmd": "SetNotify", "keys": list(keys)}])


class AmmoCommandPublication:
    def __init__(self, send, discard, rpc_enabled, set_rpc, logger):
        self._send, self._discard = send, discard
        self._rpc_enabled, self._set_rpc = rpc_enabled, set_rpc
        self._logger = logger
        self._staged = ()
        self._state_key = ""
        self._restore_rpc = False

    def pause(self):
        self._restore_rpc = self._rpc_enabled()
        if self._restore_rpc:
            try:
                if not self._set_rpc(False):
                    raise RuntimeError("could not pause command execution")
            except Exception:
                self._restore_rpc = False
                raise

    def confirm(self):
        self._staged = ()
        self._restore_rpc = False
        return self._set_rpc(True)

    def command_plan(self, crucible_active):
        commands = ["give ammo"]
        if crucible_active:
            commands.append("judgementMeter_Set 3")
        return tuple(commands)


    def stage(self, scope):
        namespace = queue_session_namespace(scope.state_key)
        if namespace is None:
            return False
        crucible_active = scope.crucible_active
        try:
            commands = self.command_plan(crucible_active)
        except Exception as error:
            self._logger.error("[Ammo Refill] Primitive compilation failed: %s", error)
            return False
        expected = ("give ammo",)
        if crucible_active:
            expected += ("judgementMeter_Set 3",)
        if commands != expected:
            self._logger.error("[Ammo Refill] Invalid raw console command plan: %r", commands)
            return False
        command_keys = []
        for stage, command in enumerate(commands):
            command_key = f"ammo-refill-{namespace}-stage{stage}"
            queued = self._send(
                command,
                coalesce_key=command_key,
                arm_rpc=False,
                already_queued_ok=False,
                state_key=scope.state_key,
                delivery_fields={
                    "source": "ammo_refill",
                    "item_id": AMMO_REFILL_ITEM_ID,
                    "primitive_item_id": AMMO_REFILL_PRIMITIVE_ITEM_ID,
                    "stage": stage,
                    "active_map": scope.current_map,
                    "slot": scope.save_slot,
                },
            )
            if not queued:
                self._discard(command_key, scope.state_key)
                for staged_key in command_keys:
                    self._discard(staged_key, scope.state_key)
                return False
            command_keys.append(command_key)
        self._staged = tuple(command_keys)
        self._state_key = scope.state_key
        return True


    def cancel(self, reason):
        command_keys = self._staged
        if command_keys:
            for command_key in command_keys:
                self._discard(command_key, self._state_key)
            self._logger.info("[Ammo Refill] Cancelled staged commands: %s", reason)
        self._staged = ()
        if self._restore_rpc:
            try:
                self._set_rpc(True)
            except Exception as error:
                self._logger.error("[Ammo Refill] Could not restore command execution: %s", error)
        self._restore_rpc = False



class AmmoRequestPump:
    """Remove request files once and supervise serial semantic requests for one room."""
    def __init__(self, ammo, request, exit_event, logger):
        self._ammo, self._request = ammo, request
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
                while self._ammo.pending and not self._exit_event.is_set():
                    await asyncio.sleep(0.05)
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
