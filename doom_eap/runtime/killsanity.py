"""Room-bound kill progress and reward publication."""
from copy import deepcopy
import asyncio

from doom_eap.content.killsanity_plan import resolved_rewards


class Killsanity:
    def __init__(self, logger):
        self._logger = logger
        self._epoch = 0
        self._session = None
        self.namespace = None
        self.milestones = ()
        self._toast_range = (0, 0, 0)

    def bind(self, session, namespace, milestones):
        self._epoch += 1
        self._session = session
        self.namespace = namespace
        self.milestones = tuple((row["threshold"], row["location_id"]) for row in milestones)
        self._toast_range = (0, 0, 0)
        state = session.get("killsanity", {})
        if not isinstance(state, dict):
            raise ValueError("Killsanity progress must be an object")
        for key in ("total", "cursor", "source_gaps"):
            if type(state.get(key, 0)) is not int or not 0 <= state.get(key, 0) < 2**64:
                raise ValueError(f"Killsanity progress has invalid {key}")
        if (type(state.get("counter_abi", 2)) is not int or state.get("counter_abi", 2) != 2 or "history_lost" in state
                or state.get("producer") is not None and state.get("counter_abi") != 2):
            raise ValueError("Killsanity progress requires a new room with cumulative combat counters")
        producer = state.get("producer")
        if producer is not None and (
                not isinstance(producer, list) or len(producer) != 4
                or any(not isinstance(value, str) or not value for value in producer)):
            raise ValueError("Killsanity progress has invalid producer identity")
        if state.get("cursor", 0) and producer is None:
            raise ValueError("Killsanity cursor has no producer identity")

    def bind_resolved(self, session, namespace, plan):
        self.bind(session, namespace, resolved_rewards(plan))

    @property
    def enabled(self):
        return bool(self.milestones)

    @property
    def state(self):
        return self._session.get("killsanity", {}) if self._session is not None else {}

    @property
    def total(self):
        return self.state.get("total", 0)

    @property
    def cursor(self):
        return self.state.get("cursor", 0)

    def consume(self, snapshot, persist):
        previous = self.state
        candidate = deepcopy(previous)
        if snapshot["namespace_id"] != self.namespace:
            raise ValueError("Combat observation belongs to another room")
        if candidate.get("producer") != snapshot["producer"]:
            candidate.update(producer=snapshot["producer"], cursor=0, source_gaps=0)
        cursor, gaps = candidate.get("cursor", 0), candidate.get("source_gaps", 0)
        if snapshot["qualified_kills"] < cursor or snapshot["observation_gaps"] < gaps:
            raise ValueError("Combat producer counters moved backwards")
        if snapshot["observation_gaps"] > gaps:
            self._logger.warning("[Killsanity] Observation gap: source_gaps=%d; total contains observed kills only",
                                 snapshot["observation_gaps"])
        total = candidate.get("total", 0) + snapshot["qualified_kills"] - cursor
        if total >= 2**64:
            raise ValueError("Killsanity total exceeds the native counter range")
        candidate.update(counter_abi=2, total=total, cursor=snapshot["qualified_kills"],
                         source_gaps=snapshot["observation_gaps"])
        if candidate == previous:
            return ()
        self._session["killsanity"] = candidate
        try:
            persist()
        except Exception:
            if previous:
                self._session["killsanity"] = previous
            else:
                self._session.pop("killsanity", None)
            raise
        crossed = [(threshold, location) for threshold, location in self.milestones
                   if previous.get("total", 0) < threshold <= self.total]
        if crossed:
            self._toast_range = (self._toast_range[0] or crossed[0][0], crossed[-1][0], self.milestones[0][0])
        return tuple(location for _, location in crossed)

    async def present(self, link, visible):
        epoch, notices = self._epoch, self._toast_range
        counter = self.counter()
        await asyncio.to_thread(link.publish_combat_progress, counter["total"], counter["target"] or 0,
                                *notices, visible and counter["target"] is not None)
        if epoch == self._epoch and notices == self._toast_range:
            self._toast_range = (0, 0, 0)

    async def publish(self, facts, publication):
        if not facts.connected or not facts.checked_ready:
            return
        due = [location for threshold, location in self.milestones
               if threshold <= self.total and location in facts.server_locations
               and location not in facts.checked and location not in facts.submitted]
        if not due:
            return
        epoch = self._epoch
        await publication.locations(due)
        if epoch == self._epoch:
            for location in due:
                publication.mark_submitted(location)

    def counter(self):
        target = next((threshold for threshold, _ in self.milestones if threshold > self.total), None)
        return {"total": self.total, "target": target}
