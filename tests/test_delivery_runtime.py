"""Exercise the production room adapter with synthetic observations and a real ledger."""

import asyncio
import json
import logging
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from test_delivery_coordinator import Transport, response

from doom_eap.runtime import delivery_runtime
from doom_eap.runtime import sentinel_protocol_generated as codec
from doom_eap.runtime.bootstrap import Bootstrap, BootstrapOwnership
from doom_eap.runtime.context_registry import CONTEXT_BY_IDENTITY
from doom_eap.runtime.delivery_runtime import REQUIRED_COMMANDS, RoomDelivery
from doom_eap.runtime.fast_travel import FastTravel
from doom_eap.runtime.item_reconciliation import ReceiptSession, load_policy_registry
from doom_eap.runtime.item_reconciliation import ReplayPolicy as ItemReplayPolicy
from doom_eap.runtime.ledger import Authority, CommandState, NetworkReceipt, StorageError, WriterBusy
from doom_eap.runtime.transient_effects import TransientEffectManager, TransientRuntime
from doom_eap.runtime.transient_files import TransientPublication


class Core(Transport):
    def __init__(self, command, scope, **kwargs):
        super().__init__()
        self.scope = scope
        self.weapons = 0
        self.inventory_effects = 0
        self.closed = False

    def supports(self, command):
        return command in REQUIRED_COMMANDS

    async def connect(self):
        return await self.hello()

    async def hello(self):
        return {"scope": self.scope}

    async def close(self):
        self.closed = True

    async def observe(self, command, namespace, binding, **kwargs):
        if command == 12:
            return {"save_binding": UUID(int=1).bytes}
        if command == 25:
            return {"weapons_after": self.weapons, "perks_known": 0, "perks_owned": 0}
        return None

    async def submit(self, command):
        if command.reference not in self.commands:
            self.inventory_effects += 1
            self.weapons = 1
        return await super().submit(command)


def bridge(tmp_path):
    root = Path(__file__).resolve().parents[1]
    definitions = {int(k): v for k, v in json.loads((root / "data/items.json").read_text()).items()}
    policies = load_policy_registry(root / "data/item_replay_policies.json", definitions)
    session = ReceiptSession()
    value = SimpleNamespace(
        state_key="seed:0:1:fixture", room_seed_name="seed", team=0, slot=1,
        _connected_slot_data={"native_generation_fingerprint": "a" * 64, "runtime": {"special_weapon": "the_crucible"}},
        base_directory=tmp_path, session_state={"delivery_backend": "ledger"}, receipt_session=session,
        items_received=[NetworkReceipt(7770900, 1, 1, 0)], context_campaign="Base", published_materialization_lease="1:100",
        persist_session_state=lambda: None,
        _refresh_runtime_context=lambda *args: CONTEXT_BY_IDENTITY["base/hub"],
    )
    link = SimpleNamespace(pid=123, namespace="a" * 64, probe=tmp_path / "unused-probe", command_prefix=(), environment=None)
    async def yes(*args, **kwargs):
        return True
    async def get_link():
        return link
    async def scope(*args, **kwargs):
        return (link.namespace, "ap-save", 123, "123", UUID(int=2).hex, "build", 1, "game/hub/hub")
    async def send(packets):
        raise AssertionError("Storage is replaced by the isolated fixture")
    value.checkpoint_recovery_ready = value.runtime_effects_ready = yes
    value._native_game_link_async = get_link
    value._native_materialization_scope = scope
    value.send_msgs = send
    value.delivery_actions_ready = lambda: True
    value.delivery_notification_commands = lambda *args: ()
    value.effective_delivery_ownership = lambda receipts: SimpleNamespace(reconciliation_item_ids=tuple(r.item for r in receipts), blood_punch_upgrades=())
    return value, definitions, policies


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(delivery_runtime, "NativeTransport", Core)
    value, definitions, policies = bridge(tmp_path)
    events = []
    owner = RoomDelivery(value, {"client_state_file": str(tmp_path / "state.json")}, definitions, policies,
                         lambda kind, **data: events.append((kind, data)))
    async def balance():
        return None
    owner._balance = balance
    return owner, events


def test_new_room_persists_authority_before_effect_and_replans_from_ingested_prefix(runtime):
    owner, events = runtime
    async def check():
        try:
            plan, error = await owner.reconcile(SimpleNamespace(epoch=1))
            assert error is None and len(plan.commands) == 1
            assert (await owner.ledger.room(owner.key)).authority is Authority.LEDGER
            assert owner.bridge.receipt_session.processed_boundary == 1
            commands = await owner._rows()
            assert len(commands) == 1 and commands[0].state is CommandState.CONFIRMED
            assert owner.transport.inventory_effects == 1
            await owner.reconcile(SimpleNamespace(epoch=1))
            assert owner.transport.inventory_effects == 1
            assert any(event[1].get("command_id") == commands[0].plan.command_id for event in events)
        finally:
            await owner.close()
    asyncio.run(check())


def test_unready_gate_never_opens_an_owner_database(runtime):
    owner, _ = runtime
    async def no(*args):
        return False
    owner.bridge.runtime_effects_ready = no
    async def check():
        assert not await owner.ready(None)
        assert not owner.path.exists()
    asyncio.run(check())


def test_checkpoint_recovery_defers_owner_then_resumes_without_duplicate_grants(runtime):
    owner, _ = runtime
    verified = False
    async def checkpoint(*args):
        return verified
    owner.bridge.checkpoint_recovery_ready = checkpoint
    async def check():
        nonlocal verified
        try:
            for _ in range(2):
                assert not await owner.ready(None)
                assert not owner.path.exists()
            verified = True
            await owner.reconcile(SimpleNamespace(epoch=1))
            assert owner.transport.inventory_effects == 1
            await owner.reconcile(SimpleNamespace(epoch=1))
            assert owner.transport.inventory_effects == 1
        finally:
            await owner.close()
    asyncio.run(check())


@pytest.mark.parametrize("broken", [None, b"not SQLite"])
def test_expected_ledger_never_falls_back_to_json_or_spool(runtime, broken):
    owner, _ = runtime
    owner.path.with_suffix(".expected").write_bytes(b"expected")
    if broken is not None:
        owner.path.write_bytes(broken)
    (owner.path.parent / "old.cmd").write_text("give health")
    async def check():
        try:
            with pytest.raises(StorageError):
                await owner.ready(SimpleNamespace(epoch=1))
            assert (owner.path.parent / "old.cmd").read_text() == "give health"
        finally:
            await owner.close()
    asyncio.run(check())


def test_old_spool_files_never_produce_owner_work(runtime):
    owner, events = runtime
    owner.bridge.items_received = []
    queue = owner.path.parent / "ap_queue"
    queue.mkdir()
    files = {"old" + suffix: "give health" for suffix in (".cmd", ".processing", ".attempt", ".failed")}
    for name, value in files.items():
        (queue / name).write_text(value)

    async def check():
        try:
            await owner.reconcile(SimpleNamespace(epoch=1))
            assert not owner.transport.effects
            assert not await owner._rows()
            assert {name: (queue / name).read_text() for name in files} == files
            assert all(data.get("command_id") is None for _, data in events)
        finally:
            await owner.close()
    asyncio.run(check())


def test_live_temporary_is_recorded_once_and_cleanup_survives_restart(runtime, monkeypatch):
    owner, _ = runtime
    owner.definitions[7770158] = {'type': 'transient_effect'}
    owner.policies[7770158] = ItemReplayPolicy(7770158, 'Infinite Ammo', 'never_replay')
    now = [100.0]
    monkeypatch.setattr('doom_eap.runtime.transient_effects.time.monotonic', lambda: now[0])
    cleanup = []
    async def send(command, *, coalesce_key, receipt_index=None, **options):
        if receipt_index is None:
            cleanup.append(command)
            return True
        return await owner.local_command(coalesce_key, command, SimpleNamespace(epoch=1), receipt_index=receipt_index)

    publication = TransientPublication(send)
    manager = TransientEffectManager(publication, 1)
    transient_runtime = TransientRuntime('room', ('1', '1'), True)
    owner.bridge.transient_effect_manager = manager
    owner.bridge._observe_transient_runtime = lambda: transient_runtime

    async def check():
        try:
            await owner.reconcile(SimpleNamespace(epoch=1))
            owner.bridge.items_received.append(NetworkReceipt(7770158, 1, 1, 0))
            owner.bridge.receipt_session.observe_packet(1, 1, 2, 1)
            await owner.reconcile(SimpleNamespace(epoch=1))
            effects = len(owner.transport.effects)
            assert effects == 4
            now[0] += 5
            await owner.reconcile(SimpleNamespace(epoch=1))
            assert len(owner.transport.effects) == effects
            now[0] += 6
            assert await manager.tick(transient_runtime)
            assert cleanup[-1] == 'g_infiniteAmmo 0'
            restarted = TransientEffectManager(publication, 2)
            owner.bridge.transient_effect_manager = restarted
            await owner.reconcile(SimpleNamespace(epoch=1))
            assert len(owner.transport.effects) == effects
            assert await restarted.tick(transient_runtime)
            assert cleanup[-1] == 'g_infiniteAmmo 0'
        finally:
            await owner.close()
    asyncio.run(check())


def test_explicit_save_epoch_preserves_receipts_and_restores_only_persistent_items(runtime):
    owner, _ = runtime
    async def check():
        try:
            await owner.reconcile(SimpleNamespace(epoch=1))
            original = await owner._rows()
            epoch = await owner.ledger.new_save_epoch(owner.binding, "new-save", b"verified archive")
            assert epoch.receipt_boundary == 1
            owner.transport.weapons = 0
            owner.coordinator = None
            await owner.reconcile(SimpleNamespace(epoch=2))
            assert owner.transport.inventory_effects == 2
            assert len(await owner.ledger.receipts(owner.key, 0, 256)) == 1
            assert (await owner.ledger.command(owner.key, original[0].plan.command_id)).state is CommandState.CONFIRMED
            await owner.reconcile(SimpleNamespace(epoch=2))
            assert owner.transport.inventory_effects == 2
        finally:
            await owner.close()
    asyncio.run(check())


def test_two_bridges_cannot_control_the_same_game_even_from_different_profiles(runtime, tmp_path):
    owner, _ = runtime
    value, definitions, policies = bridge(tmp_path)
    other_dir = tmp_path / "other"
    other = RoomDelivery(value, {"client_state_file": str(other_dir / "state.json")}, definitions, policies, lambda *args, **kwargs: None)
    async def check():
        try:
            assert await owner.ready(SimpleNamespace(epoch=1))
            with pytest.raises(WriterBusy):
                await other.ready(SimpleNamespace(epoch=1))
            assert not other.path.exists()
        finally:
            await other.close()
            await owner.close()
    asyncio.run(check())




@pytest.mark.parametrize("command", [
    "ai_ScriptCmdEnt ap_fast_travel_unlock activate",
    "ai_ScriptCmdEnt ap_bootstrap_v2_ice_acquired activate",
    "ai_ScriptCmdEnt ap_test_fixture activate",
])
def test_runtime_effect_persists_identity_and_never_replays_dispatch_on_restart(runtime, command):
    owner, _ = runtime
    async def check():
        try:
            assert await owner.local_command("runtime-fixture", command, SimpleNamespace(epoch=1))
            row, = await owner._rows()
            assert row.state is CommandState.EFFECT_UNKNOWN
            assert not row.plan.required and row.plan.receipt_index is None
            assert len(await owner.ledger.attempts(owner.key, row.plan.command_id, 0, 10)) == 1
            effects = len(owner.transport.effects)
            assert await owner.local_command("runtime-fixture", command, SimpleNamespace(epoch=1))
            assert len(owner.transport.effects) == effects
            await owner.close()
            resumed = RoomDelivery(owner.bridge, owner.config, owner.definitions, owner.policies, lambda *args, **kwargs: None)
            try:
                assert await resumed.local_command("runtime-fixture", command, SimpleNamespace(epoch=1))
                assert resumed.transport.effects == []
                assert len(await resumed._rows()) == 1
                assert not (owner.path.parent / "ap_queue").exists()
            finally:
                await resumed.close()
        finally:
            await owner.close()
    asyncio.run(check())


@pytest.mark.parametrize("command", [
    "ai_ScriptCmdEnt ap_fixture activate; give health",
    "ai_ScriptCmdEnt ap_fixture activate player1",
])
def test_runtime_effect_rejects_unsupported_console_text_before_opening_ledger(runtime, command):
    owner, _ = runtime
    async def check():
        with pytest.raises(ValueError):
            await owner.local_command("invalid", command, None)
        assert owner.ledger is None and owner.transport is None
    asyncio.run(check())


def test_runtime_effect_does_not_enter_a_new_load_with_an_old_lease(runtime):
    owner, _ = runtime
    async def check():
        try:
            assert not await owner.local_command("old-travel", "ai_ScriptCmdEnt ap_fast_travel_unlock activate",
                                                  SimpleNamespace(epoch=1), lease="1:99")
            assert owner.transport.effects == [] and not await owner._rows()
        finally:
            await owner.close()
    asyncio.run(check())


def test_bootstrap_records_one_dispatch_across_loads_and_restart(runtime):
    owner, _ = runtime
    bootstrap = Bootstrap(logging.getLogger(__name__))
    state = {"actions": {}}
    bootstrap.bind(state)
    async def send(command, *, coalesce_key, **options):
        return await owner.local_command(coalesce_key, command, SimpleNamespace(epoch=1))
    async def check():
        try:
            options = dict(ownership=BootstrapOwnership(frozenset({7770013}), False),
                           current_map="game/hub/hub", state_key=owner.bridge.state_key,
                           send=send, persist=lambda: None)
            assert await bootstrap.enqueue("ice_acquired", "on_item_received", **options)
            resumed = Bootstrap(logging.getLogger(__name__))
            resumed.bind(state)
            assert not await resumed.enqueue("ice_acquired", "on_supported_map_load", **options)
            assert len(owner.transport.effects) == 1
            source, = await owner.ledger.sources(owner.key, None, 10)
            assert source.kind.value == "bootstrap"
        finally:
            await owner.close()
    asyncio.run(check())


@pytest.mark.parametrize("completed_before_entry", [False, True])
def test_fast_travel_dispatch_requires_completion_before_map_entry(runtime, completed_before_entry):
    owner, _ = runtime
    travel = FastTravel({"hub": "game/hub/hub"}, {"hub"}, {"hub": 42}, logging.getLogger(__name__))
    identity = SimpleNamespace(cached_marker={"runtime_map": "game/hub/hub", "map_key": "hub", "gameplay_epoch": "1:100"})
    travel.capture(identity, owner.bridge.state_key, {42} if completed_before_entry else set())
    async def send(command, *, coalesce_key, materialization_lease, **options):
        return await owner.local_command(coalesce_key, command, SimpleNamespace(epoch=1), lease=materialization_lease)
    async def check():
        try:
            options = dict(map_identity=identity, room_identity=owner.bridge.state_key, state_key=owner.bridge.state_key,
                           runtime_ready=True, item_ready=True, rpc_ready=True, send=send)
            assert await travel.reconcile("entry", **options) is completed_before_entry
            travel.capture(identity, owner.bridge.state_key, {42}, refresh=True)
            assert not await travel.reconcile("server_refresh", **options)
            assert (len(owner.transport.effects) if owner.transport else 0) == int(completed_before_entry)
        finally:
            await owner.close()
    asyncio.run(check())


@pytest.mark.parametrize("generation,instance", [(2, 2), (1, 3)])
def test_reload_and_lost_core_restore_missing_persistent_effects_without_replaying_acquisitions(runtime, generation, instance):
    owner, _ = runtime
    async def check():
        try:
            owner.bridge.items_received.append(NetworkReceipt(7770025, 2, 1, 0))
            await owner.reconcile(SimpleNamespace(epoch=1))
            old = await owner._rows()
            assert len(old) == 2
            async def scope(*args, **kwargs):
                return ("a" * 64, "ap-save", 123, "123", UUID(int=instance).hex, "build", generation, "game/hub/hub")
            owner.bridge._native_materialization_scope = scope
            await owner.reconcile(SimpleNamespace(epoch=2))
            rows = await owner._rows()
            assert len(rows) == 3
            acquisition, = [r for r in rows if r.plan.receipt_index == 1]
            assert acquisition.state is CommandState.EFFECT_UNKNOWN
            assert len([r for r in rows if r.plan.receipt_index == 0]) == 2
        finally:
            await owner.close()
    asyncio.run(check())


def test_owner_delivery_batches_scale_and_leave_the_event_loop_responsive(tmp_path, monkeypatch):
    import time
    monkeypatch.setattr(delivery_runtime, "NativeTransport", Core)
    async def measure(count):
        directory = tmp_path / str(count)
        directory.mkdir()
        value, definitions, policies = bridge(directory)
        value.items_received = [NetworkReceipt(7770025, index, 1, 0) for index in range(count)]
        owner = RoomDelivery(value, {"client_state_file": str(directory / "state.json")}, definitions, policies, lambda *args, **kwargs: None)
        async def balance():
            return None
        owner._balance = balance
        ticks = 0
        async def tick():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.001)
        task = asyncio.create_task(tick())
        started = time.monotonic()
        try:
            await owner.reconcile(SimpleNamespace(epoch=1))
            assert len(await owner._rows()) == owner.transport.inventory_effects == count
            assert (await owner.ledger.room(owner.key)).receipt_boundary == count
            return time.monotonic() - started, ticks
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await owner.close()
    small, large = asyncio.run(measure(2500)), asyncio.run(measure(5000))
    print(f"owner delivery: 2500={small[0]:.2f}s; 5000={large[0]:.2f}s; ratio={large[0] / small[0]:.2f}; ticks={small[1]},{large[1]}")
    assert large[0] < small[0] * 3.5
    assert large[1] > 100 and small[1] > 100


def test_core_loss_after_effect_and_before_save_restores_proven_missing_ownership(runtime):
    owner, _ = runtime
    original_snapshot = owner._snapshot
    calls = 0
    async def interrupted_snapshot(*args):
        nonlocal calls
        calls += 1
        return await original_snapshot(*args) if calls == 1 else None
    owner._snapshot = interrupted_snapshot
    async def check():
        try:
            await owner.reconcile(SimpleNamespace(epoch=1))
            original, = await owner._rows()
            assert original.state is CommandState.EFFECT_UNKNOWN
            async def new_scope(*args, **kwargs):
                return ("a" * 64, "ap-save", 123, "123", UUID(int=3).hex, "build", 1, "game/hub/hub")
            owner.bridge._native_materialization_scope = new_scope
            owner._snapshot = original_snapshot
            await owner.reconcile(SimpleNamespace(epoch=2))
            rows = await owner._rows()
            assert len(rows) == 2
            restored, = [r for r in rows if r.plan.command_id != original.plan.command_id]
            assert restored.plan.supersedes_id == original.plan.command_id
            assert restored.state is CommandState.CONFIRMED
            assert (await owner.ledger.command(owner.key, original.plan.command_id)).state is CommandState.EFFECT_UNKNOWN
            assert (await owner.ledger.items(owner.key, -1, 10))[0].status.value == "delivered"
        finally:
            await owner.close()
    asyncio.run(check())


def test_established_room_ingests_while_game_and_actions_are_unavailable(runtime):
    owner, _ = runtime
    async def check():
        await owner.reconcile(SimpleNamespace(epoch=1))
        await owner.close()
        value = owner.bridge
        value.items_received.append(NetworkReceipt(7770025, 2, 1, 0))
        value.delivery_actions_ready = lambda: False
        resumed = RoomDelivery(value, owner.config, owner.definitions, owner.policies, lambda *args, **kwargs: None)
        try:
            plan, error = await resumed.reconcile(None)
            assert plan is None and error
            assert (await resumed.ledger.room(resumed.key)).receipt_boundary == 2
            assert resumed.transport is None
            assert len(await resumed._rows()) == 1
        finally:
            await resumed.close()
    asyncio.run(check())


def test_selected_resync_never_uses_ap_storage_or_repairs_other_items(runtime):
    owner, _ = runtime
    owner.bridge.items_received.append(NetworkReceipt(7770003, 2, 1, 0))
    async def storage_is_forbidden():
        raise AssertionError("A local resync cannot spend or normalize ammo")
    owner._balance = storage_is_forbidden
    async def check():
        try:
            plan, error = await owner.reconcile(SimpleNamespace(epoch=1), selected=frozenset({7770900}))
            assert error is None and len(plan.commands) == 1
            rows = await owner._rows()
            assert len(rows) == 1 and rows[0].plan.receipt_index == 0
            assert owner.planned_ids == (rows[0].plan.command_id,)
        finally:
            await owner.close()
    asyncio.run(check())


def test_wup_reload_restores_only_the_gained_difference_and_new_save_does_not_refund(runtime, monkeypatch):
    owner, _ = runtime
    state = {"gained": 0, "balance": 0}
    class PointsCore(Core):
        async def observe(self, command, namespace, binding, **kwargs):
            if command == 18:
                return {"flags": 16, "outcome": 1, "native_exception": 0, "gained_after": state["gained"]}
            return await super().observe(command, namespace, binding, **kwargs)

        async def submit(self, prepared):
            if prepared.reference in self.commands:
                return self.commands[prepared.reference]
            submission, end = codec._decode_type("submission", prepared.envelope, 0)
            if submission["reference"]["command_type"] != 18:
                return await super().submit(prepared)
            _, grant = codec.decode_variant(18, 1, prepared.envelope[end:])
            assert grant["expected_gained"] == state["gained"]
            result = {"flags": 16, "native_exception": 0, "gained_before": state["gained"], "balance_before": state["balance"]}
            state["gained"] += grant["amount"]
            state["balance"] += grant["amount"]
            result.update(gained_after=state["gained"], balance_after=state["balance"])
            self.commands[prepared.reference] = replace(response(prepared.reference), result=result)
            return self.commands[prepared.reference]
    monkeypatch.setattr(delivery_runtime, "NativeTransport", PointsCore)
    owner.bridge.items_received = [NetworkReceipt(7770903, index, 1, 0) for index in range(2)]
    async def check():
        try:
            await owner.reconcile(SimpleNamespace(epoch=1))
            await owner.reconcile(SimpleNamespace(epoch=1))
            assert state == {"gained": 6, "balance": 6}
            state.update(gained=3, balance=0)
            async def scope(*args, **kwargs):
                return ("a" * 64, "ap-save", 123, "123", UUID(int=2).hex, "build", 2, "game/hub/hub")
            owner.bridge._native_materialization_scope = scope
            await owner.reconcile(SimpleNamespace(epoch=2))
            assert state == {"gained": 6, "balance": 3}
            state["balance"] = 0
            await owner.reconcile(SimpleNamespace(epoch=2))
            assert state["balance"] == 0
            await owner.ledger.new_save_epoch(owner.binding, "fresh", b"archive")
            state.update(gained=0, balance=0)
            await owner.reconcile(SimpleNamespace(epoch=3))
            assert state == {"gained": 0, "balance": 0}
        finally:
            await owner.close()
    asyncio.run(check())


@pytest.mark.parametrize("item_id,count", [(7770901, 1), (7770901, 2), (7770901, 3), (7770902, 1), (7770902, 2)])
def test_progressive_special_uses_native_ownership_without_changing_selection(runtime, monkeypatch, item_id, count):
    owner, _ = runtime
    owner.bridge._connected_slot_data["runtime"]["special_weapon"] = (
        "progressive_special_weapon" if item_id == 7770901 else "progressive_sentinel_hammer")
    if item_id == 7770902:
        context = next(c for c in CONTEXT_BY_IDENTITY.values() if c.campaign == "TAG2")
        owner.bridge.context_campaign = context.campaign
        owner.bridge._refresh_runtime_context = lambda *args: context
        async def scope(*args, **kwargs):
            return ("a" * 64, "ap-save", 123, "123", UUID(int=2).hex, "build", 1, context.runtime_maps[0])
        owner.bridge._native_materialization_scope = scope
    state = {"native_state_known": 31, "native_crucible": 0, "native_hammer": 0, "native_hammer_perks": 0, "native_selected": 2}
    class SpecialCore(Core):
        async def observe(self, command, namespace, binding, **kwargs):
            return dict(state) if command == 37 else await super().observe(command, namespace, binding, **kwargs)

        async def submit(self, prepared):
            submission, end = codec._decode_type("submission", prepared.envelope, 0)
            if submission["reference"]["command_type"] != 37:
                return await super().submit(prepared)
            tag, payload = codec.decode_variant(37, 1, prepared.envelope[end:])
            assert tag == 1
            state.update(native_crucible=payload["crucible"], native_hammer=payload["hammer"],
                         native_hammer_perks=3 if payload["hammer_tier"] == 2 else 0)
            return await super().submit(prepared)
    monkeypatch.setattr(delivery_runtime, "NativeTransport", SpecialCore)
    owner.bridge.items_received = [NetworkReceipt(item_id, index, 1, 0) for index in range(count)]
    async def check():
        try:
            await owner.reconcile(SimpleNamespace(epoch=1))
            rows = await owner._rows()
            assert len(rows) == 1 and rows[0].plan.kind == "special"
            assert rows[0].state is CommandState.CONFIRMED
            assert state["native_selected"] == 2
            await owner.reconcile(SimpleNamespace(epoch=1))
            assert len(await owner._rows()) == 1
        finally:
            await owner.close()
    asyncio.run(check())


def test_uncertain_notification_is_optional_and_cannot_hide_confirmed_ownership(runtime):
    owner, _ = runtime
    owner.bridge.delivery_notification_commands = lambda *args: ("ai_ScriptCmdEnt ap_notify_item_fixture activate",)
    async def check():
        try:
            await owner.reconcile(SimpleNamespace(epoch=1))
            rows = await owner._rows()
            effect, = [row for row in rows if row.plan.required]
            notification, = [row for row in rows if not row.plan.required]
            assert effect.state is CommandState.CONFIRMED
            assert notification.state is CommandState.EFFECT_UNKNOWN
            assert (await owner.ledger.items(owner.key, -1, 10))[0].status.value == "delivered"
            await owner.reconcile(SimpleNamespace(epoch=1))
            assert len(await owner._rows()) == 2
        finally:
            await owner.close()
    asyncio.run(check())


def test_local_resync_restores_lost_persistent_inventory_more_than_once_in_the_same_load(runtime):
    owner, _ = runtime
    async def check():
        try:
            await owner.reconcile(SimpleNamespace(epoch=1))
            for count in (2, 3):
                owner.transport.weapons = 0
                plan, error = await owner.reconcile(SimpleNamespace(epoch=1), selected=frozenset({7770900}))
                assert error is None and len(plan.commands) == 1
                assert owner.transport.inventory_effects == count
                rows = await owner._rows()
                assert len(rows) == count and all(row.state is CommandState.CONFIRMED for row in rows)
        finally:
            await owner.close()
    asyncio.run(check())


def test_ammo_accounting_ignores_a_new_packet_until_its_ownership_is_durable(runtime):
    from unittest.mock import AsyncMock

    owner, _ = runtime
    async def check():
        try:
            assert await owner.ready(SimpleNamespace(epoch=1))
            await owner.ingest()
            owner.bridge.items_received.append(NetworkReceipt(7770024, 2, 1, 0))
            owner.ammo.owner = AsyncMock(return_value=None)
            owner.ammo.synchronize = AsyncMock(return_value=None)
            await RoomDelivery._balance(owner)
            assert owner.ammo.synchronize.call_args.args[0] == 0
            await owner.ingest()
            await RoomDelivery._balance(owner)
            assert owner.ammo.synchronize.call_args.args[0] == 1
        finally:
            await owner.close()
    asyncio.run(check())


def test_unsent_acquisition_survives_context_change_with_linked_replacement(runtime):
    owner, _ = runtime
    owner.bridge.items_received = [NetworkReceipt(7770025, 1, 1, 0)]
    execute = owner._execute
    async def stop_before_send(snapshot):
        return None
    owner._execute = stop_before_send
    async def check():
        try:
            await owner.reconcile(SimpleNamespace(epoch=1))
            original, = await owner._rows()
            assert original.attempt_count == 0
            async def scope(*args, **kwargs):
                return ("a" * 64, "ap-save", 123, "123", UUID(int=2).hex, "build", 2, "game/hub/hub")
            owner.bridge._native_materialization_scope = scope
            owner._execute = execute
            await owner.reconcile(SimpleNamespace(epoch=2))
            rows = await owner._rows()
            replacement, = [r for r in rows if r.plan.supersedes_id == original.plan.command_id]
            assert replacement.attempt_count == 1
            assert len(owner.transport.effects) == 1
        finally:
            await owner.close()
    asyncio.run(check())


@pytest.mark.parametrize("received", [0, 6])
def test_balance_sync_reads_writer_without_claiming_on_connect(runtime, received):
    from unittest.mock import AsyncMock
    owner, _ = runtime
    async def check():
        try:
            await owner.ready(SimpleNamespace(epoch=1))
            await owner.ingest()
            owner.bridge.items_received.extend([NetworkReceipt(7770024, i + 2, 1, 0) for i in range(received)])
            await owner.ingest()
            owner.ammo.storage.read = AsyncMock(return_value=None)
            owner.ammo.storage.counter = AsyncMock(return_value=0)
            owner.ammo.storage.create = AsyncMock(side_effect=AssertionError("sync cannot claim"))
            await RoomDelivery._balance(owner)
            assert owner.can_spend
            assert owner.balance["available"] == min(3, received)
            owner.ammo.storage.create.assert_not_called()
        finally:
            await owner.close()
    asyncio.run(check())


@pytest.mark.parametrize("optional", [18, 33, 37, 54, 55])
def test_optional_capability_failure_preserves_common_weapon_delivery(runtime, optional):
    from doom_eap.runtime.native_transport import WireError
    owner, _ = runtime
    if optional == 55:
        from doom_eap.runtime.unified_campaign import ACCESS_IDS
        owner.bridge.items_received.append(NetworkReceipt(next(iter(ACCESS_IDS)), 2, 1, 0))
    async def check():
        try:
            await owner.ready(SimpleNamespace(epoch=1))
            observe = owner.transport.observe
            async def missing(command, *args, **kwargs):
                if command == optional:
                    raise WireError(2)
                return await observe(command, *args, **kwargs)
            owner.transport.observe = missing
            await owner.reconcile(SimpleNamespace(epoch=1))
            row, = await owner._rows()
            assert row.state is CommandState.CONFIRMED
        finally:
            await owner.close()
    asyncio.run(check())


@pytest.mark.parametrize("failure", ["transport", "refused"])
def test_late_terminal_result_is_acked_and_failed_ack_is_retried(runtime, failure):
    from doom_eap.runtime.native_transport import TransportError
    owner, _ = runtime
    async def check():
        try:
            await owner.ready(SimpleNamespace(epoch=1))
            submit = owner.transport.submit
            async def pending(prepared):
                await submit(prepared)
                owner.transport.commands[prepared.reference] = response(prepared.reference, progress=0, execution=0, effect=0)
                return owner.transport.commands[prepared.reference]
            owner.transport.submit = pending
            await owner.reconcile(SimpleNamespace(epoch=1))
            row, = await owner._rows()
            attempt, = await owner.ledger.attempts(owner.key, row.plan.command_id, 0, 1)
            owner.transport.commands[attempt.attempt.reference] = response(attempt.attempt.reference)
            ack = owner.transport.ack
            calls = []
            async def flaky(reference, token):
                calls.append(reference)
                if len(calls) == 1:
                    if failure == "refused":
                        return response(reference, error=6)
                    raise TransportError("ack disconnected")
                return await ack(reference, token)
            owner.transport.ack = flaky
            await owner.reconcile(SimpleNamespace(epoch=1))
            await owner.reconcile(SimpleNamespace(epoch=1))
            assert len(calls) >= 2 and "ack" in owner.transport.calls
            assert len(owner.transport.effects) == 1
        finally:
            await owner.close()
    asyncio.run(check())


def test_valid_mission_access_proof_satisfies_receipt(runtime):
    from native_transport_support import evidence_result
    from test_native_effect_evidence import wire_for_scope

    from doom_eap.runtime.unified_campaign import ACCESS_IDS, STAGES
    owner, _ = runtime
    item_id = next(iter(ACCESS_IDS))
    owner.bridge.items_received = [NetworkReceipt(item_id, 1, 1, 0)]
    async def check():
        try:
            await owner.ready(SimpleNamespace(epoch=1))
            scope = owner.coordinator._evidence_scope(owner.coordinator.context, owner.namespace)
            mission = wire_for_scope(evidence_result("mission_access_result"), scope)
            destination = STAGES[ACCESS_IDS[item_id]]["map"]
            mission.update(stage_id=item_id, destination_stage_id=item_id, map_name=destination.encode().ljust(192, b"\0"))
            mission["destination_scope"] = {**mission["menu_scope"], "process": {
                **mission["menu_scope"]["process"], "lifecycle_generation": scope.generation + 1},
                "map_name": mission["map_name"]}
            observe = owner.transport.observe
            async def access(command, *args, **kwargs):
                return mission if command == 55 else await observe(command, *args, **kwargs)
            owner.transport.observe = access
            await owner.reconcile(SimpleNamespace(epoch=1))
            row, = await owner.ledger.items(owner.key, -1, 10)
            assert row.status.value == "delivered"
        finally:
            await owner.close()
    asyncio.run(check())


def test_unpublished_ammo_reservation_is_exposed_to_items_for_its_profile(runtime):
    from unittest.mock import AsyncMock

    from doom_eap.runtime.ammo_refill import AmmoCommandScope
    from doom_eap.runtime.ledger import AmmoState
    owner, events = runtime
    async def check():
        try:
            await owner.ready(SimpleNamespace(epoch=1))
            owner.ammo.storage.read = AsyncMock(return_value=None)
            owner.ammo.storage.counter = AsyncMock(return_value=0)
            owner.ammo.storage.create = AsyncMock(return_value=owner.ledger.profile_id)
            publish = owner.ammo._publish
            owner.ammo._publish = AsyncMock(side_effect=RuntimeError("process stopped before publication"))
            with pytest.raises(RuntimeError, match="process stopped"):
                await owner.ammo.request(AmmoCommandScope(owner.bridge.state_key, "map", "save", False), (None,), 1)
            owner.ammo._publish = publish
            owner._balance = RoomDelivery._balance.__get__(owner)
            await owner.reconcile(SimpleNamespace(epoch=1))
            operation, = await owner.ledger.ammo_operations(owner.key, (AmmoState.UNKNOWN,), None, 1)
            status = [data for kind, data in events if kind == "delivery_local_status"][-1]
            assert status["command_id"] == "ammo-operation:" + operation.operation.operation_id
            assert status["expected_revision"] == operation.revision
            assert status["item_id"] == 7770024
        finally:
            await owner.close()
    asyncio.run(check())


def test_late_local_command_result_is_finalized_and_acked(runtime):
    owner, _ = runtime
    async def check():
        try:
            await owner.ready(SimpleNamespace(epoch=1))
            submit = owner.transport.submit
            async def pending(prepared):
                await submit(prepared)
                owner.transport.commands[prepared.reference] = response(prepared.reference, progress=0, execution=0, effect=0)
                return owner.transport.commands[prepared.reference]
            owner.transport.submit = pending
            args = ("fixture", "ai_ScriptCmdEnt ap_rpc_v3_7770025 activate", SimpleNamespace(epoch=1))
            assert await owner.local_command(*args)
            row, = await owner._rows()
            attempt, = await owner.ledger.attempts(owner.key, row.plan.command_id, 0, 1)
            owner.transport.commands[attempt.attempt.reference] = response(attempt.attempt.reference, effect=2)
            assert await owner.local_command(*args)
            assert "ack" in owner.transport.calls and len(owner.transport.effects) == 1
        finally:
            await owner.close()
    asyncio.run(check())
