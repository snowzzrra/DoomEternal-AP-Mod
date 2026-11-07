import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from doom_eap.contracts.check_observation import CheckObservation
from doom_eap.runtime.bridge_checks import PhysicalChecks
from doom_eap.runtime.inventory import default_session_state, normalize_session_state


def context(bridge):
    from doom_eap.runtime import bridge_checks, bridge_config, bridge_saves

    gate_names = {location: name for location, name in bridge_config.DOOM_LOCATION_NAMES.items()
                  if name.endswith(" - Slayer Gate Complete")}
    facts = CheckObservation(frozenset(gate_names), frozenset(), frozenset(gate_names), True, True)
    ctx = SimpleNamespace(
        session_state=default_session_state(),
        get_ap_state_key=lambda: "room",
        item_state_ready=True,
        physical_checks=PhysicalChecks(logging.getLogger("gate-test")),
        server_checked_locations_ready=True,
        unified_campaign=object(),
        _campaign_menu=object(),
        _mission_presentations=object(),
        items=None,
        checks=None,
        saves=None
    )
    ctx.items = bridge.bridge_items.ItemRuntime(ctx)
    ctx.items.check_and_update_event_session = AsyncMock(return_value=True)
    ctx.items.received_item_ids = lambda: frozenset()
    ctx.items._native_game_link_async = AsyncMock(side_effect=RuntimeError("native menu unavailable"))
    ctx.checks = bridge.bridge_checks.CheckRuntime(ctx)
    ctx.checks.check_observation = lambda: facts
    ctx.checks.check_publication = lambda: None
    ctx.saves = bridge.bridge_saves.SaveRuntime(ctx)
    ctx.saves.synchronize_fortress_phase = AsyncMock()


    persisted = []
    ctx.persist_session_state = lambda: persisted.append(json.dumps(ctx.session_state))
    rows = [dict(stage=key, title=stage["name"], revealed=True)
            for key, stage in bridge_checks.STAGES.items() if key in bridge_saves.GATE_KEY_TO_MAP.values()]
    ctx.saves.campaign_projection = lambda: {"rows": rows}
    return ctx, gate_names, persisted, rows


def test_collected_gates_need_individual_physical_events_and_survive_reconnect(bridge, tmp_path):
    ctx, gate_names, persisted, rows = context(bridge)

    def _assert_individual_gate_events(rows):
        assert all(row["slayer_gate"] and not row["gate_complete"] for row in rows)


    def _assert_gate_events_after_reconnect(completed, rows):
        assert {row["title"] for row in rows if row["gate_complete"]} == completed


    async def run():
        await ctx.saves.synchronize_campaign_menu()
        _assert_individual_gate_events(rows)
        completed = set()
        for gate, name in gate_names.items():
            event = tmp_path / f"ap_event_{gate}.txt"
            event.write_text(f"AP_CHECK_EVENT_{gate}")
            await ctx.checks.flush_check_event_files()
            assert not event.exists()
            completed.add(name.removesuffix(" - Slayer Gate Complete"))
            await ctx.saves.synchronize_campaign_menu()
            _assert_gate_events_after_reconnect(completed, rows)
        assert len(persisted) == len(gate_names)
        event.write_text(f"AP_CHECK_EVENT_{gate}")
        await ctx.checks.flush_check_event_files()
        assert len(persisted) == len(gate_names) and not event.exists()
        ctx.session_state = normalize_session_state(json.loads(persisted[-1]))
        await ctx.saves.synchronize_campaign_menu()
        assert all(row["gate_complete"] for row in rows)
        ctx.session_state = default_session_state()
        await ctx.saves.synchronize_campaign_menu()
        assert not any(row["gate_complete"] for row in rows)

    asyncio.run(run())


def test_unbound_or_outside_slot_gate_event_does_not_mark_completion(bridge, tmp_path):
    ctx, gate_names, persisted, _ = context(bridge)
    gate = next(iter(gate_names))
    event = tmp_path / f"ap_event_{gate}.txt"
    event.write_text(f"AP_CHECK_EVENT_{gate}")
    ctx.items.check_and_update_event_session.return_value = False
    asyncio.run(ctx.checks.flush_check_event_files())
    assert not persisted and event.exists()
    ctx.items.check_and_update_event_session.return_value = True
    ctx.checks.check_observation = lambda: CheckObservation(frozenset(), frozenset(), frozenset(), False, True)
    asyncio.run(ctx.checks.flush_check_event_files())
    assert not persisted and event.exists()


def test_gate_event_is_preserved_when_client_state_cannot_be_saved(bridge, tmp_path):
    ctx, gate_names, _, _ = context(bridge)
    gate = next(iter(gate_names))
    event = tmp_path / f"ap_event_{gate}.txt"
    event.write_text(f"AP_CHECK_EVENT_{gate}")

    def fail():
        raise OSError("disk unavailable")

    ctx.persist_session_state = fail
    with pytest.raises(OSError):
        asyncio.run(ctx.checks.flush_check_event_files())
    assert event.exists()
    assert ctx.session_state.get("physical_slayer_gates", []) == []
    saved = []
    ctx.persist_session_state = lambda: saved.append(json.dumps(ctx.session_state))
    asyncio.run(ctx.checks.flush_check_event_files())
    assert len(saved) == 1 and not event.exists()


@pytest.mark.parametrize("value", [None, 1, "7770486", [True, "7770486", 7770486, 7770486]])
def test_invalid_physical_gate_state_cannot_claim_completion(value):
    restored = normalize_session_state({"physical_slayer_gates": value})
    assert restored["physical_slayer_gates"] == ([7770486] if isinstance(value, list) else [])
