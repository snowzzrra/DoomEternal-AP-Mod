import asyncio
import importlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from doom_eap.contracts.check_observation import CheckObservation
from doom_eap.runtime.item_reconciliation import default_session_state, normalize_session_state
from doom_eap.runtime.physical_checks import PhysicalChecks


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "doom_base_dir": str(tmp_path / "base"), "save_games_dir": str(tmp_path),
        "client_state_file": str(tmp_path / "state.json"), "bridge_log_path": str(tmp_path / "bridge.log"),
    }))
    monkeypatch.setenv("DOOM_AP_CONFIG_FILE", str(config))
    monkeypatch.setenv("DOOM_AP_APPLICATION_DIR", str(tmp_path))
    monkeypatch.syspath_prepend(str(root.parent / "Archipelago"))
    monkeypatch.syspath_prepend(str(root / "packaging/standalone_runtime"))
    module = importlib.import_module("doom_eap.runtime.bridge_client")
    monkeypatch.setattr(module, "INV_DUMP_DIR", str(tmp_path))
    return module


def context(bridge):
    gate_names = {location: name for location, name in bridge.DOOM_LOCATION_NAMES.items()
                  if name.endswith(" - Slayer Gate Complete")}
    facts = CheckObservation(frozenset(gate_names), frozenset(), frozenset(gate_names), True, True)
    ctx = SimpleNamespace(
        session_state=default_session_state(), get_ap_state_key=lambda: "room",
        check_and_update_event_session=AsyncMock(return_value=True), item_state_ready=True,
        check_observation=lambda: facts, check_publication=lambda: None,
        physical_checks=PhysicalChecks(logging.getLogger("gate-test")),
        server_checked_locations_ready=True, unified_campaign=object(),
        received_item_ids=lambda: frozenset(), _campaign_menu=object(), _mission_presentations=object(),
        synchronize_fortress_phase=AsyncMock(),
        _native_game_link_async=AsyncMock(side_effect=RuntimeError("native menu unavailable")),
    )
    persisted = []
    ctx.persist_session_state = lambda: persisted.append(json.dumps(ctx.session_state))
    rows = [dict(stage=key, title=stage["name"], revealed=True)
            for key, stage in bridge.STAGES.items() if key in bridge.GATE_KEY_TO_MAP.values()]
    ctx.campaign_projection = lambda: {"rows": rows}
    return ctx, gate_names, persisted, rows


def test_collected_gates_need_individual_physical_events_and_survive_reconnect(bridge, tmp_path):
    ctx, gate_names, persisted, rows = context(bridge)

    async def run():
        await bridge.DoomEternalContext.synchronize_campaign_menu(ctx)
        assert all(row["slayer_gate"] and not row["gate_complete"] for row in rows)
        completed = set()
        for gate, name in gate_names.items():
            event = tmp_path / f"ap_event_{gate}.txt"
            event.write_text(f"AP_CHECK_EVENT_{gate}")
            await bridge.DoomEternalContext.flush_check_event_files(ctx)
            assert not event.exists()
            completed.add(name.removesuffix(" - Slayer Gate Complete"))
            await bridge.DoomEternalContext.synchronize_campaign_menu(ctx)
            assert {row["title"] for row in rows if row["gate_complete"]} == completed
        assert len(persisted) == len(gate_names)
        event.write_text(f"AP_CHECK_EVENT_{gate}")
        await bridge.DoomEternalContext.flush_check_event_files(ctx)
        assert len(persisted) == len(gate_names) and not event.exists()
        ctx.session_state = normalize_session_state(json.loads(persisted[-1]))
        await bridge.DoomEternalContext.synchronize_campaign_menu(ctx)
        assert all(row["gate_complete"] for row in rows)
        ctx.session_state = default_session_state()
        await bridge.DoomEternalContext.synchronize_campaign_menu(ctx)
        assert not any(row["gate_complete"] for row in rows)

    asyncio.run(run())


def test_unbound_or_outside_slot_gate_event_does_not_mark_completion(bridge, tmp_path):
    ctx, gate_names, persisted, _ = context(bridge)
    gate = next(iter(gate_names))
    event = tmp_path / f"ap_event_{gate}.txt"
    event.write_text(f"AP_CHECK_EVENT_{gate}")
    ctx.check_and_update_event_session.return_value = False
    asyncio.run(bridge.DoomEternalContext.flush_check_event_files(ctx))
    assert not persisted and event.exists()
    ctx.check_and_update_event_session.return_value = True
    ctx.check_observation = lambda: CheckObservation(frozenset(), frozenset(), frozenset(), False, True)
    asyncio.run(bridge.DoomEternalContext.flush_check_event_files(ctx))
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
        asyncio.run(bridge.DoomEternalContext.flush_check_event_files(ctx))
    assert event.exists()
    assert ctx.session_state.get("physical_slayer_gates", []) == []
    saved = []
    ctx.persist_session_state = lambda: saved.append(json.dumps(ctx.session_state))
    asyncio.run(bridge.DoomEternalContext.flush_check_event_files(ctx))
    assert len(saved) == 1 and not event.exists()


@pytest.mark.parametrize("value", [None, 1, "7770486", [True, "7770486", 7770486, 7770486]])
def test_invalid_physical_gate_state_cannot_claim_completion(value):
    restored = normalize_session_state({"physical_slayer_gates": value})
    assert restored["physical_slayer_gates"] == ([7770486] if isinstance(value, list) else [])
