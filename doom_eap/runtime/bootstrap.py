"""Retained experimental bootstrap policy and durable evidence, separate from lifecycle."""
import time
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType

from doom_eap.contracts.runtime_context import canonical_map_name
from doom_eap.runtime.bootstrap_actions import (
    BOOTSTRAP_ACTIONS,
    BOOTSTRAP_REVISION,
    BOOTSTRAP_STAT_PRIMITIVE,
    received_any_suit_upgrade,
)


@dataclass(frozen=True)
class BootstrapOwnership:
    item_ids: frozenset[int]
    has_rune: bool


class Bootstrap:
    def __init__(self, logger):
        self._logger = logger
        self._last_rpc_map = None
        self.bind({"revision": BOOTSTRAP_REVISION, "actions": {}})

    def observe_supported_map(self, map_name):
        previous = self._last_rpc_map
        self._last_rpc_map = map_name
        if map_name is None or previous == map_name:
            return False
        if previous is not None:
            self._logger.info(
                f"[RPC] Map transition observed: {previous} -> {map_name}. "
                "The native gameplay gate controls safe execution."
            )
        return True

    def bind(self, state):
        self._state = state
        actions = state.setdefault("actions", {})
        state["revision"] = BOOTSTRAP_REVISION
        self._actions = actions

    @property
    def actions(self):
        return MappingProxyType(deepcopy(self._actions))

    def action_state(self, action_name, revision=None):
        revision = BOOTSTRAP_REVISION if revision is None else revision
        key = f"v{revision}:{action_name}"
        return MappingProxyType(deepcopy(self._actions.get(key, {
            "revision": revision, "action": action_name, "trigger": None, "status": "pending",
            "last_map": None, "timestamp": None, "reapply_on_map_load": False,
        })))

    def _action_state(self, action_name, revision=None):
        revision = BOOTSTRAP_REVISION if revision is None else revision
        state_key = f"v{revision}:{action_name}"
        state = self._actions.setdefault(state_key, {
            "revision": revision,
            "action": action_name, "trigger": None, "status": "pending",
            "last_map": None, "timestamp": None,
            "reapply_on_map_load": False,
        })
        return state

    def eligible(self, action_name, ownership):
        action = BOOTSTRAP_ACTIONS[action_name]
        if action["required_ap_ownership"] == "at_least_one_rune":
            return ownership.has_rune
        if action["required_ap_ownership"] == "at_least_one_suit_page_unlocker":
            return received_any_suit_upgrade(ownership.item_ids)
        if action["required_ap_ownership"] == "frag_grenade":
            return 7770011 in ownership.item_ids
        if action["required_ap_ownership"] == "ice_bomb":
            return 7770013 in ownership.item_ids
        return False

    def ineligibility_reason(self, action_name, ownership):
        if self.eligible(action_name, ownership):
            return "eligible"
        return {
            "rune_page": "needs AP Rune",
            "suit_page": "needs AP Suit Upgrade",
            "frag_acquired": "needs AP Frag Grenade",
            "ice_acquired": "needs AP Ice Bomb",
        }.get(action_name, "ownership predicate unmet")

    def command_id(self, action_name):
        action = BOOTSTRAP_ACTIONS[action_name]
        return f"bootstrap-v{action['revision']}-{action_name}"

    async def enqueue(self, action_name, trigger, *, ownership, current_map, state_key, send, persist):
        """Record dispatch only after the delivery backend accepts the action."""
        action = BOOTSTRAP_ACTIONS[action_name]
        state = self._action_state(action_name)
        non_replayable = {
            "queued",
            "delivered_effect_unknown",
            "delivered_effect_unknown_legacy",
            "confirmed",
            "skipped",
        }
        if state["status"] in non_replayable or not self.eligible(action_name, ownership):
            return False
        if canonical_map_name(current_map) not in {
            canonical_map_name(name) for name in action["maps_supported"]
        }:
            state.update(status="pending", trigger=trigger, timestamp=time.time())
            persist()
            return False
        command_id = self.command_id(action_name)
        if not await send(f"ai_ScriptCmdEnt {action['entity_name']} activate", coalesce_key=command_id,
                          already_queued_ok=True, state_key=state_key):
            state.update(status="retryable_failure", trigger=trigger, timestamp=time.time())
            persist()
            return False
        state.update(status="queued", trigger=trigger, last_map=current_map,
                     timestamp=time.time(), revision=action["revision"])
        persist()
        self._logger.info(
            "[Bootstrap] v%s entity=%s primitive_class=%s inherit=%s map=%s command=%s trigger=%s",
            action["revision"], action["entity_name"],
            BOOTSTRAP_STAT_PRIMITIVE["class"],
            BOOTSTRAP_STAT_PRIMITIVE["inherit"] or "<none>",
            current_map, command_id, trigger,
        )
        return True

    async def onboard(self, trigger, *, ownership, map_identity, state_key, item_ready, rpc_ready, send, persist):
        # Only explicitly enabled, ownership-gated acquisition actions run automatically.
        if not any(action["automatic_enabled"] and self.eligible(name, ownership)
                   for name, action in BOOTSTRAP_ACTIONS.items()):
            return
        if not item_ready or not rpc_ready:
            return
        for action_name, action in BOOTSTRAP_ACTIONS.items():
            if not action["automatic_enabled"]:
                continue
            if trigger in action["trigger_policy"]:
                if trigger == "on_supported_map_load" and self.action_state(action_name)["status"] != "pending":
                    continue
                await self.enqueue(action_name, trigger, ownership=ownership, current_map=map_identity.current_map,
                                   state_key=state_key, send=send, persist=persist)
