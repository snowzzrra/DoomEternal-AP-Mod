"""Publish DeathLink effects through the namespace-bound native domain."""
DEATHLINK_KILL_COALESCE_KEY = "deathlink-kill"


class DeathLinkPublication:
    def __init__(self, link, event, mode):
        self._link, self._event, self._mode = link, event, mode

    def _identity(self):
        digest = bytes.fromhex(self._event.event_id)
        return int.from_bytes(digest[:8], "little") or 1, digest[8:24]

    def dispatch(self, state_key):
        event_id, event_hash = self._identity()
        link = self._link()
        link.deathlink_request(1, enabled=True, mode=self._mode)
        result = link.deathlink_request(2, event_id=event_id, event_hash=event_hash)
        if result["outcome"] not in {0, 1, 7} or result["remote_state"] in {8, 9, 10}:
            raise RuntimeError("Native DeathLink rejected or cancelled")
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
            return self._link().deathlink_request(5)
