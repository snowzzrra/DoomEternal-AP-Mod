"""Current AP storage/native-file adapters and request-file worker for Ammo Refill."""
import asyncio
from doom_eap.runtime.ledger_shadow import ignore
from collections import deque
import os
from uuid import uuid4

from doom_eap.contracts.command_publication import queue_session_namespace
from doom_eap.runtime.ammo_refill import AMMO_REFILL_ITEM_ID, AMMO_REFILL_PRIMITIVE_ITEM_ID


class AmmoStorage:
    def __init__(self, send, observer=ignore):
        self._send = send
        self._observe = observer

    async def increment(self, key, default, delta):
        self._observe("ammo_storage_request", storage_key=key, baseline=default, delta=delta)
        await self._send([{"cmd": "Set", "key": key, "default": default,
                           "operations": [{"operation": "add", "value": delta}], "want_reply": True}])
        self._observe("ammo_storage_sent", storage_key=key, baseline=default, delta=delta)

    async def watch(self, keys):
        await self._send([{"cmd": "Get", "keys": list(keys)}, {"cmd": "SetNotify", "keys": list(keys)}])


class AmmoCommandPublication:
    def __init__(self, send, discard, rpc_enabled, set_rpc, logger, observer=ignore):
        self._send, self._discard = send, discard
        self._rpc_enabled, self._set_rpc = rpc_enabled, set_rpc
        self._logger = logger
        self._observe = observer
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
        staged = self._staged
        self._observe("ammo_commands_authorized", state_key=self._state_key, commands=staged, correlated=False)
        self._staged = ()
        published = []
        try:
            for command, options in staged:
                if not self._send(command, **options):
                    raise RuntimeError("could not publish paid refill command")
                published.append(options["coalesce_key"])
        except Exception:
            for command_key in published:
                self._discard(command_key, self._state_key)
            raise
        finally:
            self._restore_rpc = False
            if not self._set_rpc(True):
                raise RuntimeError("execution gate refused enable")
        return True

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
        staged = []
        attempt_id = uuid4().hex
        for stage, command in enumerate(commands):
            command_key = f"ammo-refill-{namespace}-{attempt_id}-stage{stage}"
            staged.append((command, dict(
                coalesce_key=command_key,
                arm_rpc=False,
                already_queued_ok=False,
                state_key=scope.state_key,
                materialization_lease=scope.materialization_lease,
                delivery_fields={
                    "source": "ammo_refill",
                    "item_id": AMMO_REFILL_ITEM_ID,
                    "primitive_item_id": AMMO_REFILL_PRIMITIVE_ITEM_ID,
                    "stage": stage,
                    "active_map": scope.current_map,
                    "slot": scope.save_slot,
                },
            )))
        self._observe("ammo_commands_staged", state_key=scope.state_key, commands=staged, scope=scope)
        self._staged = tuple(staged)
        self._state_key = scope.state_key
        return True


    def cancel(self, reason):
        self._observe("ammo_cancel", state_key=self._state_key, staged_count=len(self._staged), reason=reason)
        if self._staged:
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
