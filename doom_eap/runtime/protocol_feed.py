"""Process-local feedback policy; no CommonClient, discovery or UI toolkit."""
from collections import deque
import time

def hints_key(team, slot):
    if (
        not isinstance(team, int)
        or isinstance(team, bool)
        or not isinstance(slot, int)
        or isinstance(slot, bool)
    ):
        return None
    return f"_read_hints_{team}_{slot}"


class ProtocolFeed:
    def __init__(self):
        self._echoes = deque()

    def invalidate(self):
        self._echoes.clear()

    def record_echo(self, accepted):
        self._echoes.append((accepted, time.monotonic() + 5.0))

    def consume_echo(self, event):
        now = time.monotonic()
        while self._echoes and self._echoes[0][1] < now:
            self._echoes.popleft()
        if not any(
            segment.get("type") == "player" and segment.get("self") is True
            for segment in event.get("segments", ())
            if isinstance(segment, dict)
        ):
            return False
        plain = event.get("plain")
        if not isinstance(plain, str):
            return False
        for index, (text, _) in enumerate(self._echoes):
            if plain.endswith(text):
                del self._echoes[index]
                return True
        return False
