"""Publisher effects own pending dispatch and server-confirmed acknowledgements."""
from dataclasses import replace
from doom_eap.contracts.check_observation import CheckObservation, CheckPublicationPort
from doom_eap.contracts.publisher_contracts import publisher_acknowledged


class PublisherDispatch:
    def __init__(self, publishers, goals, logger):
        self._publishers, self._goals, self._logger = tuple(publishers), goals, logger
        self._acknowledgements = {}
        self._in_flight = set()
        self._generation = 0
        self._facts = CheckObservation(frozenset(), frozenset(), frozenset(), False, False)

    def observe_protocol(self, facts):
        self._facts = facts

    def invalidate(self):
        self._generation += 1
        self._in_flight.clear()

    def bind(self, acknowledgements):
        self.invalidate()
        self._acknowledgements = acknowledgements

    def record_ack(self, publisher_key, effect_index, effect, persist):
        state = self._acknowledgements
        publisher_state = state.setdefault(publisher_key, {})
        publisher_state[str(effect_index)] = {
            "strategy": effect["strategy"],
            "location_id": effect.get("location_id"),
        }
        persist()


    async def send_publisher_effect(
        self, publisher, effect_index, effect, source_description, publication, persist
    ):
        strategy = effect["strategy"]
        effect_key = (publisher.key, effect_index)
        checked_locations = self._facts.checked
        if strategy == "preserved_native_target":
            return True
        if strategy == "location_check":
            location_id = effect["location_id"]
            if location_id in checked_locations:
                self._in_flight.discard(effect_key)
                self.record_ack(publisher.key, effect_index, effect, persist)
                self._logger.info(
                    "[PUBLISHER] EFFECT_ACK key=%s effect=location_check location_id=%s",
                    publisher.key,
                    location_id,
                )
                return True
            if location_id in self._facts.submitted or effect_key in self._in_flight:
                self._logger.info(
                    "[PUBLISHER] FALLBACK_SUPPRESSED key=%s reason=already_dispatched",
                    publisher.key,
                )
                return False
            if location_id not in self._facts.server_locations:
                self._logger.warning(
                    "[PUBLISHER] EFFECT_BLOCKED key=%s effect=location_check "
                    "location_id=%s reason=slot_absent",
                    publisher.key,
                    location_id,
                )
                return False
        elif strategy == "campaign_goal":
            if self._goals.sent:
                self._in_flight.discard(effect_key)
                self.record_ack(publisher.key, effect_index, effect, persist)
                self._logger.info(
                    "[PUBLISHER] EFFECT_ACK key=%s effect=campaign_goal",
                    publisher.key,
                )
                return True
            self._in_flight.discard(effect_key)
            self._logger.info(
                "[PUBLISHER] EFFECT_FACT_ONLY key=%s effect=campaign_goal",
                publisher.key,
            )
            return True
        else:
            raise ValueError(f"unsupported publisher effect strategy: {strategy}")

        if not self._facts.connected:
            return False
        self._in_flight.add(effect_key)
        self._logger.info(
            "[PUBLISHER] EFFECT_SEND key=%s effect=%s location_id=%s source=%s",
            publisher.key,
            strategy,
            effect.get("location_id", ""),
            source_description,
        )
        generation = self._generation
        try:
            await publication.location(location_id)
        except Exception:
            if generation != self._generation:
                return False
            self._in_flight.discard(effect_key)
            raise
        if generation != self._generation:
            return False
        if strategy == "location_check":
            publication.mark_submitted(effect["location_id"])
            self._facts = replace(self._facts, submitted=self._facts.submitted | {effect["location_id"]})
            if effect["location_id"] in self._facts.checked:
                self._in_flight.discard(effect_key)
                self.record_ack(publisher.key, effect_index, effect, persist)
                self._logger.info(
                    "[PUBLISHER] EFFECT_ACK key=%s effect=location_check location_id=%s",
                    publisher.key,
                    effect["location_id"],
                )
                return True
            return False
        raise RuntimeError(f"unsupported publisher effect strategy: {strategy}")


    async def execute(self, publisher, trigger_strategy, source_description, publication: CheckPublicationPort, persist):
        if publisher_acknowledged(
            publisher,
            self._facts.checked,
            self._goals.sent,
        ):
            self._logger.info(
                "[PUBLISHER] FALLBACK_SUPPRESSED key=%s reason=already_acknowledged",
                publisher.key,
            )
            return True
        self._logger.info(
            "[PUBLISHER] TRIGGER_OBSERVED key=%s strategy=%s",
            publisher.key,
            trigger_strategy,
        )
        generation = self._generation
        results = []
        for index, effect in enumerate(publisher.effects):
            try:
                results.append(
                    await self.send_publisher_effect(publisher, index, effect, source_description, publication, persist)
                )
            except Exception as error:
                if generation != self._generation:
                    return False
                self._logger.error(
                    "[PUBLISHER] EFFECT_RETRY key=%s effect=%s error=%s",
                    publisher.key,
                    effect["strategy"],
                    error,
                )
                results.append(False)
            if generation != self._generation:
                return False
        return all(results)


    async def mission(self, location_id, source_description, publication, persist):
        matching = next(
            (
                publisher
                for publisher in self._publishers
                if any(
                    effect["strategy"] == "location_check"
                    and effect["location_id"] == location_id
                    for effect in publisher.effects
                )
            ),
            None,
        )
        if matching is None:
            return False
        generation = self._generation
        results = []
        for index, effect in enumerate(matching.effects):
            if effect["strategy"] == "preserved_native_target":
                continue
            if effect["strategy"] == "location_check" and location_id is None:
                continue
            results.append(
                await self.send_publisher_effect(matching, index, effect, source_description, publication, persist)
            )
            if generation != self._generation:
                return False
        return bool(results) and all(results)

