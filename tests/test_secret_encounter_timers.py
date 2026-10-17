import pytest

from doom_eap.content.secret_encounter_timers import apply_secret_encounter_timers


@pytest.mark.parametrize("duration,field", [(25000, "challengeDuration = 25000;"), (10000, "")])
def test_secret_timer_uses_source_duration_once(duration, field):
    source = f'''entity {{ entityDef secret {{ class = "idTarget_EncounterChallenge";
        edit = {{ {field} otherDuration = 3; targets = {{ item[0] = "manager"; }} }} }} }}'''
    registry = {"maps": {"test": {"entities_path": "test.entities", "timers": [
        {"entity": "secret", "duration_ms": duration},
    ]}}}
    result = apply_secret_encounter_timers(source, "game/test.entities", registry)
    assert f"challengeDuration = {duration * 3 // 2};" in result
    assert "otherDuration = 3;" in result
    assert apply_secret_encounter_timers(result, "game/test.entities", registry) == result
    assert apply_secret_encounter_timers(source, "game/other.entities", registry) == source
    with pytest.raises(ValueError, match="entity is missing"):
        apply_secret_encounter_timers("", "game/test.entities", registry)


@pytest.mark.parametrize("easier,expected", [(False, 24000), (True, 36000)])
def test_frozen_timer_replaces_source_duration_and_never_compounds(easier, expected):
    source = 'entity { entityDef secret { class = "idTarget_EncounterChallenge"; edit = { challengeDuration = 10000; } } }'
    registry = {"maps": {"test": {"entities_path": "test.entities", "timers": [
        {"entity": "secret", "duration_ms": 10000},
    ]}}}
    frozen = {"test": {"secret": 24000}}
    result = apply_secret_encounter_timers(source, "game/test.entities", registry, frozen, easier=easier)
    assert f"challengeDuration = {expected};" in result
    assert apply_secret_encounter_timers(result, "game/test.entities", registry, frozen, easier=easier) == result
    with pytest.raises(ValueError, match="outside the game's range"):
        apply_secret_encounter_timers(source, "game/test.entities", registry, {"test": {"secret": 0}})
