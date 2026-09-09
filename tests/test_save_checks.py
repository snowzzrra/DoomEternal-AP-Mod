import asyncio
import logging
from dataclasses import replace

import pytest

from doom_eap.contracts.check_observation import CheckObservation
from doom_eap.runtime.check_publication import CheckPublication
from doom_eap.runtime.save_checks import SaveChecks, SaveCheckReadiness
from doom_eap.runtime.save_check_observation import SaveCheckBinding, SaveCheckObservations
from doom_eap.runtime.save_observer import SaveObserver, SaveObserverBaselineStore


SIGNAL = dict(kind="unlockable_record", numUnlockableRules=1, rule_0_statname="kills",
              rule_0_statCount=25, rule_0_statDuration=0, rule_0_satisfied=True, unlockableIsUnlocked=True)
MASTERY = {"location_id": 101, "signal": dict(SIGNAL, unlockable="mastery")}
PHYSICAL = {"location_id": 201, "signal": dict(kind="physical_event_equivalent", unlockable="physical",
                                            physical_location_ids=[101], required_count=1)}
AGGREGATE = {"location_id": 301, "signal": dict(authority="server_checked_locations",
                                             children=[101, 201], required_count=2)}


def owner():
    return SaveChecks({"mastery": MASTERY}, {"physical": PHYSICAL}, {}, [AGGREGATE], logging.getLogger("checks"))


def facts(checked=(), submitted=()):
    return CheckObservation(frozenset(checked), frozenset(submitted), frozenset({101, 201, 301}), True, True)


def test_native_edges_remain_observer_owned_and_mastery_submission_is_separate():
    service = owner()
    persisted, commits, packets, submitted = {}, [], [], set()
    observer = SaveObserver(baselines=SaveObserverBaselineStore(persisted))
    binding = SaveCheckBinding("seed", 0, 1, "room", "revision", frozenset(), True, True)
    observations = SaveCheckObservations(observer, binding, lambda: commits.append(True), logging.getLogger("checks"))
    record = {key: value for key, value in SIGNAL.items() if key != "kind"}
    service.observe_masteries({"mastery": record}, "save", "GAME-AUTOSAVE0", observations, authoritative=False)
    assert persisted == {"observer_baselines": {}} and commits == []
    # A preexisting completed record cannot produce a check.
    service.observe_masteries({"mastery": record}, "save", "GAME-AUTOSAVE0", observations, authoritative=True)

    async def send(messages):
        packets.extend(messages)

    publication = CheckPublication(send, submitted.add, 30)
    readiness = SaveCheckReadiness(True, True, "GAME-AUTOSAVE0")
    asyncio.run(service.check_mastery(MASTERY, readiness, facts(), publication))
    assert packets == []
    # A separately bound slot establishes a false baseline and later completes 24 -> 25.
    record["rule_0_statCount"] = 24
    service.reset_observations()
    service.observe_masteries({"mastery": record}, "save2", "GAME-AUTOSAVE1", observations, authoritative=True)
    record["rule_0_statCount"] = 25
    service.observe_masteries({"mastery": record}, "save2", "GAME-AUTOSAVE1", observations, authoritative=True)
    asyncio.run(service.check_mastery(MASTERY, readiness, facts(), publication))
    asyncio.run(service.check_mastery(MASTERY, readiness, facts(submitted=submitted), publication))
    assert packets == [{"cmd": "LocationChecks", "locations": [101]}]
    assert submitted == {101} and commits


def test_physical_and_aggregate_checks_require_confirmed_children_without_save_proof():
    service, packets, submitted = owner(), [], set()

    async def send(messages):
        packets.extend(messages)

    publication = CheckPublication(send, submitted.add, 30)
    readiness = SaveCheckReadiness(True, False, None)
    asyncio.run(service.check_challenge(PHYSICAL, readiness, facts(submitted={101}), publication))
    assert packets == []
    asyncio.run(service.check_challenge(PHYSICAL, readiness, facts(checked={101}), publication))
    asyncio.run(service.check_challenge(PHYSICAL, readiness, facts(checked={101}, submitted={201}), publication))
    assert len(packets) == 2 and submitted == set()  # Existing retry-until-server-ack behavior.
    asyncio.run(service.check_aggregates(readiness, facts(checked={101}, submitted={201}), publication))
    assert len(packets) == 2
    asyncio.run(service.check_aggregates(readiness, facts(checked={101, 201}), publication))
    assert packets[-1] == {"cmd": "LocationChecks", "locations": [301]}


@pytest.mark.parametrize("kind", ["mastery", "challenge", "aggregate"])
@pytest.mark.parametrize("fail", [False, True])
def test_rebind_rejects_late_check_completion_and_remaining_aggregate_work(kind, fail):
    async def run():
        service, submitted, packets = owner(), set(), []
        service._masteries["mastery"] = True  # Explicit already-observed owner state.
        service._aggregates += ({"location_id": 302, "signal": AGGREGATE["signal"]},)

        async def send(messages):
            packets.extend(messages)
            service.bind()
            if fail:
                raise OSError("old connection")

        publication = CheckPublication(send, submitted.add, 30)
        readiness = SaveCheckReadiness(True, True, "slot")
        current = replace(facts(checked={101, 201}), server_locations=frozenset({101, 201, 301, 302}))
        if kind == "mastery":
            await service.check_mastery(MASTERY, readiness, facts(), publication)
        elif kind == "challenge":
            await service.check_challenge(PHYSICAL, readiness, facts(checked={101}), publication)
        else:
            await service.check_aggregates(readiness, current, publication)
        assert len(packets) == 1 and submitted == set()

    asyncio.run(run())
