import struct
from types import SimpleNamespace

import pytest

from doom_eap.runtime.campaign import MissionPresentations, _reward_wire


def test_mastery_fallback_uses_placement_state_and_preserves_wire_layout(monkeypatch):
    from doom_eap.runtime import campaign

    entry = {"location_id": 7, "signal": {}, "name": "Mastery", "gameplay_perk": "perk/mastery"}
    facts = SimpleNamespace(checked={7}, server_locations={7})
    presentations = MissionPresentations.__new__(MissionPresentations)
    monkeypatch.setattr(campaign, "player_text", lambda category, key: f"{category}.{key}")
    unknown, = presentations.mastery_snapshot(facts, {}, [entry])
    assert unknown.text == "game.menu.mastery_unknown"
    placement = {"item_name": "Dash", "local": True, "classification": 0, "recipient_name": "Slayer"}
    known, = presentations.mastery_snapshot(facts, {7: placement}, [entry])
    assert "Dash" in known.text and known.text != unknown.text
    for reward in (unknown, known):
        wire = _reward_wire(True, [(3, reward)])
        assert len(wire) == 740
        location, kind, checked, unlockable, name, text = struct.unpack("<III80s128s512s", wire[8:])
        assert (location, kind, checked) == (7, 3, 1)
        assert unlockable.rstrip(b"\0") == b"perk/mastery"
        assert name.rstrip(b"\0") == b"Mastery"
        assert text.rstrip(b"\0").decode("utf-8") == reward.text


@pytest.mark.parametrize("field,valid,invalid", [
    ("name", "á" * 63 + "X", "á" * 64),
    ("text", "á" * 255 + "X", "á" * 256),
])
def test_reward_wire_preserves_utf8_byte_limits(field, valid, invalid):
    reward = SimpleNamespace(location_id=1, checked=True, unlockable="challenge/1", name="Name", text="Text")
    setattr(reward, field, valid)
    assert len(_reward_wire(True, [(1, reward)])) == 740
    setattr(reward, field, invalid)
    with pytest.raises(ValueError, match="field capacity"):
        _reward_wire(True, [(1, reward)])
