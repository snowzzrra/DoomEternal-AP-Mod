"""Publish DeathLink effects through the namespace-bound native domain."""
DEATHLINK_KILL_COALESCE_KEY = "deathlink-kill"


class DeathLinkDispatchError(RuntimeError):
    def __init__(self, kind, native_result):
        super().__init__(f"Native DeathLink request {kind} refused: {native_result}")
        self.request_kind = kind
        self.native_result = native_result


class DeathLinkPublication:
    def __init__(self, link, event, mode):
        self._link, self._event, self._mode = link, event, mode

    def _identity(self):
        digest = bytes.fromhex(self._event.event_id)
        return int.from_bytes(digest[:8], "little") or 1, digest[8:24]

    def dispatch(self, state_key):
        event_id, event_hash = self._identity()
        link = self._link()
        for kind, args in ((1, dict(enabled=True, mode=self._mode)),
                           (2, dict(event_id=event_id, event_hash=event_hash))):
            try:
                result = link.deathlink_request(kind, **args)
            except Exception as error:
                native_result = getattr(error, "native_result", None)
                if isinstance(native_result, dict):
                    raise DeathLinkDispatchError(kind, native_result) from error
                raise
        if result["outcome"] not in {0, 1, 7} or result["remote_state"] in {8, 9, 10}:
            raise DeathLinkDispatchError(2, result)
        return True

    def exists(self, state_key):
        if not self._event or not self._event.attempts:
            return False
        result = self._link().deathlink_request(0)
        if int(result["remote_event_id"]) != self._identity()[0]:
            raise RuntimeError("Native DeathLink event identity changed")
        if result["remote_state"] in {8, 9, 10}:
            raise RuntimeError("Native DeathLink failed without confirmation")
        if result["remote_state"] not in {1, 2, 3, 4, 6, 7}:
            raise RuntimeError("Native DeathLink effect remains unknown")
        return result["remote_state"] in {1, 2, 3}

    def cancel(self, state_key):
        if self._event and self._event.attempts:
            event_id, event_hash = self._identity()
            return self._link().deathlink_request(5, event_id=event_id, event_hash=event_hash)
