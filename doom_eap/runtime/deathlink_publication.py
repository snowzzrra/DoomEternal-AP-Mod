"""Current DeathLink command-spool adapter; no execution-success inference."""
DEATHLINK_KILL_COALESCE_KEY = "deathlink-kill"


class DeathLinkPublication:
    def __init__(self, send, exists, discard):
        self._send, self._exists, self._discard = send, exists, discard

    def dispatch(self, state_key):
        return self._send("ai_ScriptCmdEnt ap_deathlink activate",
                          coalesce_key=DEATHLINK_KILL_COALESCE_KEY, state_key=state_key)

    def exists(self, state_key):
        return self._exists(DEATHLINK_KILL_COALESCE_KEY, state_key)

    def cancel(self, state_key):
        return self._discard(DEATHLINK_KILL_COALESCE_KEY, state_key)
