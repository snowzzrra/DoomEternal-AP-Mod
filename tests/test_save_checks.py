import asyncio
import logging
from copy import deepcopy
from dataclasses import replace

import pytest

from doom_eap.contracts.check_observation import CheckObservation
from doom_eap.contracts.challenge_registry import load_challenge_registry, validate_challenge_registry
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
                                             children=[101, 201], required_count=2,
                                             mission_complete_location_id=401)}


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


def test_save_challenge_catchup_requires_bound_pending_completion():
    service = SaveChecks({}, {"mastery": dict(MASTERY)}, {}, [AGGREGATE], logging.getLogger("checks"))
    observer = SaveObserver(baselines=SaveObserverBaselineStore({}))
    observer.bind_ap_provider("namespace", "ap-" + "a" * 40)
    binding = SaveCheckBinding("seed", 0, 1, "room", "revision", frozenset(), True, True)
    observations = SaveCheckObservations(observer, binding, lambda: None, logging.getLogger("checks"))
    record = {key: value for key, value in SIGNAL.items() if key != "kind"}
    service.observe_challenges({"mastery": record}, "save", "GAME-AUTOSAVE0", None, None,
        observations, authoritative=True)
    assert not service._challenges.get("mastery", False)
    service.observe_challenges({"mastery": record}, "save", "GAME-AUTOSAVE0", None, None,
        observations, authoritative=True, first_sample_locations=frozenset({101}))
    assert service._challenges["mastery"]


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
    assert len(packets) == 2 and submitted == set()
    asyncio.run(service.check_aggregates(readiness, facts(checked={101}, submitted={201}), publication))
    assert len(packets) == 2
    asyncio.run(service.check_aggregates(readiness, facts(checked={101, 201}), publication))
    assert packets[-1] == {"cmd": "LocationChecks", "locations": [301]}


def test_doom_hunter_base_big_reveal_follows_confirmed_contact_check_without_native_automap_use():
    registry = load_challenge_registry()
    entry = next(row for row in registry['mission_challenges'] if row['location_id'] == 7770173)
    service = SaveChecks({}, {entry['signal']['unlockable']: entry}, {}, [], logging.getLogger('checks'))
    packets = []

    async def send(messages):
        packets.extend(messages)

    publication = CheckPublication(send, lambda _: None, 30)
    readiness = SaveCheckReadiness(True, False, None)
    current = CheckObservation(frozenset(), frozenset({7770496}), frozenset({7770496, 7770173}), True, True)
    asyncio.run(service.check_challenge(entry, readiness, current, publication))
    assert packets == []
    current = replace(current, checked=frozenset({7770496}))
    asyncio.run(service.check_challenge(entry, readiness, current, publication))
    assert packets == [{'cmd': 'LocationChecks', 'locations': [7770173]}]


def test_aggregate_requires_active_children_or_its_mission_completion():
    service, packets = owner(), []

    async def send(messages):
        packets.extend(messages)

    publication = CheckPublication(send, lambda _: None, 30)
    readiness = SaveCheckReadiness(True, False, None)

    async def run():
        current = replace(facts(checked={101}), server_locations=frozenset({101, 301, 401}))
        await service.check_aggregates(readiness, current, publication)
        assert packets == [{"cmd": "LocationChecks", "locations": [301]}]
        packets.clear()
        for checked, active in (((), {301, 401}), ({101, 201}, {301, 401}), ({999}, {301, 401}),
                                ({401}, {301}), ({401}, {201, 301, 401})):
            current = replace(facts(checked=checked), server_locations=frozenset(active))
            await service.check_aggregates(readiness, current, publication)
            assert packets == []
        current = replace(facts(checked={401}), server_locations=frozenset({301, 401}))
        await service.check_aggregates(readiness, current, publication)
        assert packets == [{"cmd": "LocationChecks", "locations": [301]}]

    asyncio.run(run())


def test_aggregate_registry_rejects_another_mission_or_invalid_completion_identity():
    registry = load_challenge_registry()
    aggregate = registry["all_mission_challenges"][0]
    complete = aggregate["signal"]["mission_complete_location_id"]
    another = next(entry["location_id"] for entry in registry["mission_complete"] if entry["location_id"] != complete)
    for replacement in (None, another, float(complete)):
        invalid = deepcopy(registry)
        invalid["all_mission_challenges"][0]["signal"]["mission_complete_location_id"] = replacement
        with pytest.raises(ValueError, match="mission completion"):
            validate_challenge_registry(invalid)


@pytest.mark.parametrize("kind", ["mastery", "challenge", "aggregate"])
@pytest.mark.parametrize("fail", [False, True])
def test_rebind_rejects_late_check_completion_and_remaining_aggregate_work(kind, fail):
    async def run():
        service, submitted, packets = owner(), set(), []
        service._masteries["mastery"] = True  # explicit already-observed owner state
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
