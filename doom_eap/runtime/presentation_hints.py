"""Explicit native page intents; confirmation comes from the server Hint feed."""
import time


class PresentationHints:
    def __init__(self, namespace, state, persist):
        self.namespace, self.state, self.persist = namespace, state, persist
        self.sent = {}

    async def synchronize(self, intents, allowed, confirmed, send, hints_key):
        pending = {value for value in self.state.get("observed", [])
                   if type(value) is int and value in allowed}
        observed = {value for value in intents if type(value) is int and value in allowed}
        if observed - pending:
            pending.update(observed)
            self.state["observed"] = sorted(pending)
            self.persist()
        now = time.monotonic()
        if send is None:
            return
        requested = sorted(value for value in pending - confirmed
                           if now - self.sent.get(value, -30) >= 30)
        if not requested:
            return
        await send([{"cmd": "LocationScouts", "locations": requested, "create_as_hint": 2},
                    {"cmd": "Get", "keys": [hints_key]}])
        self.sent.update((value, now) for value in requested)
