import asyncio
import copy
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from doom_eap.contracts.inventory_domain import InventoryObservation, ItemObservation, MISSING, OWNED
from doom_eap.contracts.materialization import MaterializationScope
from doom_eap.contracts.runtime_context import RuntimeContext
from doom_eap.contracts.tag_prerequisites import TAG_REQUIRED_ENGINE_PERKS
from doom_eap.runtime.item_reconciliation import (
    ReceiptSession,
    StartingOwnershipError,
    effective_ownership,
    load_policy_registry,
    load_starting_ownership,
    normalize_session_state,
)
from doom_eap.runtime.materialization import MaterializationPlanError, apply_starting_currencies, compile_materialization_plan
from doom_eap.runtime.receipt_publication import ReceiptPublication
from tools.decls.devinv_builder import (
    build_devinv_loadout,
    build_tag_devinv_overrides,
    load_devinv_mapping,
    output_path_for_map,
    validate_devinv_source,
)
from tools.validation.validate_devinvloadout_package import validate as validate_package

ROOT = Path(__file__).resolve().parents[1]
STARTING = {"items": [[7770004, 1], [7770016, 2], [7770142, 1], [7770902, 1]], "weapon_item_id": 7770004}


def bind_starting(session, starting=STARTING):
    session.configure_starting_ownership(
        {"identity": {"schema": 7}, "starting": starting},
    )


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    configuration = {
        "doom_base_dir": str(tmp_path / "game/base"), "save_games_dir": str(tmp_path / "saves"),
        "steam_remote_dir": str(tmp_path / "remote"), "client_state_file": str(tmp_path / "state.json"),
        "bridge_log_path": str(tmp_path / "bridge.log"),
    }
    for key in ("doom_base_dir", "save_games_dir", "steam_remote_dir"):
        Path(configuration[key]).mkdir(parents=True)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(configuration), encoding="utf-8")
    monkeypatch.setenv("DOOM_AP_CONFIG_FILE", str(path))
    monkeypatch.setenv("DOOM_AP_APPLICATION_DIR", str(tmp_path))
    monkeypatch.syspath_prepend(str(ROOT.parent / "Archipelago"))
    monkeypatch.syspath_prepend(str(ROOT / "packaging/standalone_runtime"))
    module = importlib.import_module("doom_eap.runtime.bridge_client")
    monkeypatch.setattr(module, "ENABLE_ITEM_NOTIFICATIONS", False)
    monkeypatch.setattr(module, "ensure_queue_session_namespace", lambda _: True)
    return module


def context_for(bridge, tmp_path, monkeypatch):
    ctx = bridge.DoomEternalContext(None, None)
    ctx.state_key, ctx.slot, ctx.team, ctx.auth = "StartingRoom:0:1", 1, 0, "StartingSlot"
    ctx.item_state_ready = ctx._queue_session_authoritative = True
    ctx.session_state = normalize_session_state({})
    ctx.receipt_delivery.bind(ctx.session_state)
    ctx._connected_slot_data = {"identity": {"schema": 7}, "starting": STARTING}
    bind_starting(ctx.receipt_session)
    runtime = RuntimeContext("base/hub", "Base", ("game/hub/hub",), ("hub",), frozenset())
    monkeypatch.setattr(ctx, "_refresh_runtime_context", lambda _: runtime)
    monkeypatch.setattr(ctx, "_active_materialization_lease", lambda _=None: "1:1")
    monkeypatch.setattr(ctx, "_flush_deferred_frag_upgrades", lambda: None)
    monkeypatch.setattr(ctx, "_reconcile_native_receipt_owners", lambda _: None)
    state_path = tmp_path / "saved-state.json"
    monkeypatch.setattr(ctx, "persist_session_state", lambda: state_path.write_text(json.dumps(ctx.session_state)))
    monkeypatch.setattr(ctx, "_persist_receipt_progress", lambda _: ctx.persist_session_state())
    return ctx


@pytest.mark.parametrize("schema,expected", [(7, 0b011)])
def test_connect_uses_packaged_schema_before_room_data(bridge, tmp_path, schema, expected):
    script = '''
import asyncio, json, sys
from pathlib import Path
read = Path.read_text
def packaged(path, *args, **kwargs):
    value = read(path, *args, **kwargs)
    if path.name == "content_identity.json":
        data = json.loads(value)
        data["slot_data_schema_version"] = int(sys.argv[1])
        return json.dumps(data)
    return value
Path.read_text = packaged
from doom_eap.runtime.bridge_client import DoomEternalContext
import Utils
Utils.get_unique_identifier = lambda: "starting-test"
async def connect():
    ctx = DoomEternalContext(None, None)
    packets = []
    async def send(messages):
        packets.extend(messages)
    ctx.auth = "StartingSlot"
    ctx.send_msgs = send
    await ctx.server_auth()
    packet = next(packet for packet in packets if packet["cmd"] == "Connect")
    assert packet["items_handling"] == int(sys.argv[2]), packet
    assert ctx._connected_slot_data == {}
asyncio.run(connect())
'''
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join((str(ROOT), str(ROOT.parent / "Archipelago"),
                                                            str(ROOT / "packaging/standalone_runtime"))))
    result = subprocess.run([sys.executable, "-c", script, str(schema), str(expected)], cwd=tmp_path,
                            env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_connected_is_rejected_before_session_binding(bridge, tmp_path, monkeypatch):
    from unittest.mock import Mock

    async def run():
        ctx = bridge.DoomEternalContext(None, None)
        bind = Mock(side_effect=AssertionError("Rejected rooms cannot bind a session"))
        failure = Mock()
        monkeypatch.setattr(ctx, "_handle_package", bind)
        monkeypatch.setattr(ctx, "_report_launcher_connection_failure", failure)
        room = json.loads((ROOT / "tests/fixtures/full_campaign_slot_data.json").read_text())
        before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
        await ctx._validate_connected_package({"slot_data": room})
        bind.assert_not_called()
        assert failure.call_args.kwargs["code"] == "slot_schema"
        assert "DoomEAP " + bridge._CONTENT_IDENTITY["release_version"] in failure.call_args.args[0]
        assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
    asyncio.run(run())


def test_starting_weapon_is_counted_once_and_load_restores_only_missing_ownership():
    session = ReceiptSession()
    bind_starting(session)
    source = build_devinv_loadout(starting=STARTING)
    assert source.count('item = "weapon/player/gauss_rifle";') == 1
    assert "CURRENCY_SENTINEL_BATTERY" not in source
    definitions = {int(key): value for key, value in json.loads((ROOT / "data/items.json").read_text()).items()}
    policies = load_policy_registry(ROOT / "data/item_replay_policies.json", definitions)
    owned = effective_ownership(
        (), starting_item_ids=session.ownership_item_ids((), definitions), slot=1,
        randomize_chainsaw=True, randomize_dash=True, checked_locations=frozenset(),
        local_checked_locations=frozenset(), server_checked_ready=True,
        hell_on_earth_locations=frozenset(), exultia_complete_location=0,
    )
    assert owned.ap_item_ids.count(7770004) == 1
    assert 7770016 not in owned.reconciliation_item_ids
    context = RuntimeContext("base/hub", "Base", ("game/hub/hub",), ("hub",), frozenset())
    scope = MaterializationScope("room", 0, 1, "room", "1:1", 1, "load", "room")
    for state in (MISSING, OWNED):
        observation = InventoryObservation("room", 1, context.identity, "Base", {7770004: ItemObservation(7770004, state)})
        plan = compile_materialization_plan(owned, context, scope, definitions, policies, "the_crucible",
                                            observation=observation, selected_item_ids={7770004})
        assert len(plan.reconciliation.commands) == (1 if state == MISSING else 0)


def test_reconnect_and_save_load_do_not_duplicate_starting_currency(bridge, tmp_path, monkeypatch):
    async def run():
        ctx = context_for(bridge, tmp_path, monkeypatch)
        commands = []
        monkeypatch.setattr(bridge, "send_command", lambda command, **_: commands.append(command) or True)
        assert await ctx.process_pending_item_receipts("packet")
        assert len(commands) == 3 and ctx.items_processed == 0 and ctx.items_received == []
        ctx.receipt_session.begin_rebind()
        bind_starting(ctx.receipt_session)
        assert await ctx.process_pending_item_receipts("reconnect")
        ctx.session_state = normalize_session_state(json.loads((tmp_path / "saved-state.json").read_text()))
        ctx.receipt_delivery.bind(ctx.session_state)
        assert await ctx.process_pending_item_receipts("save_load")
        assert len(commands) == 3
        ctx.items_received = [SimpleNamespace(item=7770016, location=100, player=1, flags=1)]
        assert await ctx.process_pending_item_receipts("packet")
        assert ctx.items_processed == 1 and len(commands) == 4
        assert await ctx.process_pending_item_receipts("reconnect")
        assert len(commands) == 4
    asyncio.run(run())


def test_starting_currency_resumes_after_publication_refusal(bridge, tmp_path, monkeypatch):
    async def run():
        ctx = context_for(bridge, tmp_path, monkeypatch)
        commands = []
        def send(command, **_):
            commands.append(command)
            return len(commands) != 2
        publication = ReceiptPublication(send)
        with pytest.raises(MaterializationPlanError):
            apply_starting_currencies(ctx, bridge.ITEM_ID_TO_COMMAND, publication)
        assert ctx.session_state["starting_currency"]["completed"] == {"7770016": 1}
        apply_starting_currencies(ctx, bridge.ITEM_ID_TO_COMMAND, publication)
        assert len(commands) == 4
        assert ctx.session_state["starting_currency"]["completed"] == {"7770016": 2, "7770142": 1}
    asyncio.run(run())


def test_own_world_progressive_delivery_adds_to_starting_ownership(bridge, tmp_path, monkeypatch):
    async def run():
        ctx = context_for(bridge, tmp_path, monkeypatch)
        targets = []
        def ensure(count, *, hammer_only=False):
            targets.append((count, hammer_only))
            return {"outcome": 0}
        monkeypatch.setattr(ctx, "native_game_link", lambda: SimpleNamespace(ensure_progressive_special_weapon=ensure))
        monkeypatch.setattr(bridge, "send_command", lambda *_args, **_kwargs: True)
        ctx.items_received = [SimpleNamespace(item=7770902, location=100, player=1, flags=1)]
        assert await ctx.process_pending_item_receipts("packet")
        assert targets == [(2, True)] and ctx.items_processed == 1
        bridge.DoomEternalContext._reconcile_native_receipt_owners(ctx, "save_load")
        assert targets == [(2, True), (2, True)]
    asyncio.run(run())


def test_schema_7_blood_punch_repairs_starting_tiers(bridge, tmp_path, monkeypatch):
    async def run():
        ctx = context_for(bridge, tmp_path, monkeypatch)
        ctx._connected_slot_data = {"compatibility": {"required_capabilities": ["slot_data_v7"]}}
        bind_starting(ctx.receipt_session, {"items": [[7770004, 1], [7770904, 2]], "weapon_item_id": 7770004})
        commands = []
        monkeypatch.setattr(ctx, "native_game_link", lambda: SimpleNamespace(observe_blood_punch=lambda: 0))
        monkeypatch.setattr(bridge, "send_command", lambda command, **_: commands.append(command) or True)
        ctx._reconcile_blood_punch("save_load")
        assert len(commands) == 2
    asyncio.run(run())


@pytest.mark.parametrize("change", [
    {"items": [[7770004, True]]}, {"items": [[True, 1]]}, {"items": [[7770004, 1], [7770004, 1]]},
    {"items": [[7770004, 0]]}, {"items": [[7770004, 1], [7770025, 1]]},
    {"weapon_item_id": 7770001}, {"weapon_item_id": True},
])
def test_invalid_starting_plan_has_a_typed_error(change):
    starting = {**copy.deepcopy(STARTING), **change}
    with pytest.raises(StartingOwnershipError):
        load_starting_ownership(starting, load_devinv_mapping())


def test_schema_7_package_uses_one_plan_for_base_and_tag(tmp_path):
    options = {"identity": {"schema": 7}, "starting": STARTING, "runtime": {"use_dlc_content": True}}
    source = build_devinv_loadout(starting=STARTING)
    registry = ROOT / "data/map_sources.json"
    world = tmp_path / "world.map"
    world.write_text('devmapInvLoadout = "devinvloadout/sp/e1m1";')
    decl = output_path_for_map(tmp_path / "mod", registry, "e1m1_intro")
    decl.parent.mkdir(parents=True)
    decl.write_text(source)
    for member, text in build_tag_devinv_overrides(starting=STARTING).items():
        assert text.count('item = "weapon/player/gauss_rifle";') == 1
        assert "CURRENCY_SENTINEL_BATTERY" not in text
        assert all(perk in text for perk in TAG_REQUIRED_ENGINE_PERKS)
        path = tmp_path / "mod" / member
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    manifest = tmp_path / "mod/seed_manifest.json"
    manifest.write_text(json.dumps({"options": options}))
    validate_package(tmp_path / "mod", registry, world)
    additive = build_devinv_loadout({"Sentinel Battery": 2}, "Ballista")
    with pytest.raises(ValueError, match="durable delivery"):
        validate_devinv_source(additive, starting=STARTING)


def test_runtime_mastery_counts_are_the_resolved_targets():
    from doom_eap.content.mastery_requirements import mastery_entries_for_counts
    from doom_eap.contracts.challenge_registry import load_challenge_registry

    room = json.loads((ROOT / "tests/fixtures/resolved_room_slot_data.json").read_text())
    counts = room["runtime"]["mastery_requirement_counts"]
    entries = mastery_entries_for_counts(load_challenge_registry()["weapon_masteries"], counts)
    assert {entry["signal"]["unlockable"]: entry["signal"]["rule_0_statCount"] for entry in entries} == counts


