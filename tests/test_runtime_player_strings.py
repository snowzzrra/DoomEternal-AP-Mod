import logging
import os
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from doom_eap.content.item_classification import ITEM_CLASSIFICATION_TRAP
from doom_eap.runtime.campaign import MissionPresentations, _reward_wire


def test_rewards_preserve_unknown_and_trap_privacy():
    entry = {"location_id": 7, "signal": {"unlockable": "challenge/7"}, "name": "Mission - Mission Challenge - Title"}
    facts = SimpleNamespace(checked={7})
    assert MissionPresentations.reward(entry, facts, {}).text == "REWARD UNKNOWN"
    placement = {"item_name": "Hidden Trap", "local": False, "classification": ITEM_CLASSIFICATION_TRAP,
                 "recipient_name": "Secret Player"}
    reward = MissionPresentations.reward(entry, facts, {7: placement})
    assert reward.text == "A trap for someone"
    assert reward.name == "Title" and reward.checked


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


def test_inactive_native_context_has_a_distinct_error_type():
    import asyncio
    from unittest.mock import AsyncMock

    from doom_eap.runtime.inventory import NativeGameplayInactive, SentinelWeaponPoints, WeaponPointsBlocked

    link = SentinelWeaponPoints(Path('unused'), 1, '0' * 64)
    link._transport = SimpleNamespace(connect=AsyncMock(), scope={}, observe=AsyncMock(return_value={'save_binding': bytes(16)}))
    with pytest.raises(NativeGameplayInactive, match="Native gameplay context is not active") as failure:
        asyncio.run(SentinelWeaponPoints._request(link, 37, 0, {}))
    assert isinstance(failure.value, WeaponPointsBlocked)


def test_protocol_fallbacks_preserve_and_bound_remote_data(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(Path(os.environ.get("ARCHIPELAGO_SOURCE", root.parent / "Archipelago"))))
    from doom_eap.runtime.native_transport import ProtocolNames, emit_hints, format_archipelago_event

    class Names:
        def lookup_in_slot(self, *_):
            raise KeyError

    names = ProtocolNames(Names(), Names(), {}, lambda _: False)
    remote = "Remote {player} <tag>\n" + "á" * 600
    event = format_archipelago_event(names, {"data": [{"type": "text", "text": remote}, None]})
    assert event["plain"] == remote.replace("\n", " ")[:512]
    assert event["segments"][0]["text"] == remote.replace("\n", " ")[:128]
    assert event["segments"][1]["text"] == "[unavailable]"
    events = []
    records = emit_hints("key", [(1, 2, 3, 4, False)], names,
                         lambda *args, **kwargs: events.append((args, kwargs)), logging.getLogger("hints"))
    assert records[0]["item_name"] == "Unknown item (4)"
    assert records[0]["location_name"] == "Unknown location (3)"


@pytest.mark.parametrize("player,expected", [("Slayer {other} <remote>", "Slayer {other} <remote>"), (None, "The Doom Slayer")])
def test_deathlink_local_message_keeps_player_data_and_fallback(monkeypatch, player, expected):
    import asyncio

    from doom_eap.runtime import deathlink
    from doom_eap.runtime.deathlink import DeathLinkReceiver

    monkeypatch.setattr(deathlink.random, "choice", lambda messages: messages[0])
    owner = deathlink.DeathLinkSession(DeathLinkReceiver(), deathlink.DEATHLINK_MESSAGES,
        lambda *args, **kwargs: None, logging.getLogger("deathlink"))
    owner.configure(True)
    sent = []

    async def send(cause):
        sent.append(cause)

    asyncio.run(owner.report_local_death(player, send, None))
    assert sent == [deathlink.DEATHLINK_MESSAGES[0].format(player=expected)]
