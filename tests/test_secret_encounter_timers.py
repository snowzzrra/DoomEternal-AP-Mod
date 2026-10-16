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
