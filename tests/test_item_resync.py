from collections import namedtuple

from doom_eap.runtime.inventory import (
    migrate_client_state,
    migrate_legacy_session_key,
)

Receipt = namedtuple("Receipt", "item location player flags")


def test_state_migration_preserves_history_and_safes_malformed_sessions():
    state, migrated = migrate_client_state(
        {
            "version": 1,
            "sessions": {
                "room-a:0:1": {
                    "processed_items": 7,
                    "never_replay_history": [7001],
                    "custom": {"keep": True},
                },
                "room-b:0:1": "malformed",
            },
        }
    )

    assert migrated is True
    assert state["version"] == 2
    assert state["sessions"]["room-a:0:1"]["processed_items"] == 7
    assert state["sessions"]["room-a:0:1"]["never_replay_history"] == [7001]
    assert state["sessions"]["room-a:0:1"]["custom"] == {"keep": True}
    assert state["sessions"]["room-b:0:1"]["processed_items"] == 0
    assert set(state["sessions"]) == {"room-a:0:1", "room-b:0:1"}


def test_generation_identity_never_adopts_another_campaign_session():
    sessions = {
        "room-a:0:1": {
            "processed_items": 31,
            "campaign_navigation": {"generation": "b" * 64},
        },
    }

    state_key, migrated_from = migrate_legacy_session_key(
        sessions, seed_name="room-a", team=0, slot=1, generation="a" * 64,
    )

    assert state_key == "room-a:0:1:" + "a" * 64
    assert migrated_from is None
    assert sessions["room-a:0:1"]["processed_items"] == 31
    assert state_key not in sessions
