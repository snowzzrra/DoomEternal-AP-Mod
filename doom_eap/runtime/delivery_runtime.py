"""Bind bridge lifecycle to ledger delivery; inventory and the coordinator own effect decisions."""

import asyncio
import os
from dataclasses import asdict, replace
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from doom_eap.contracts.foundation import classify_item_definition
from doom_eap.contracts.inventory_domain import EffectExpectation, InventoryEffect, MissionAccess
from doom_eap.contracts.materialization import MaterializationScope
from doom_eap.strings import text

from . import sentinel_protocol_generated as codec
from .ammo import LedgerAmmoRefill
from .ammo_adapters import AmmoStorage
from .ammo_refill import AMMO_REFILL_CAPACITY, AmmoCommandScope
from .delivery import DeliveryCoordinator, DeliveryMode
from .delivery_plans import DeliveryPlans, console_payload, effect_identity, obligation, restore_confirmed
from .inventory import effect_confirmed, ownership_from_ledger
from .inventory_observation import mission_access_observation, observe_delivery, observe_optional
from .item_reconciliation import NEVER_REPLAY
from .ledger import (
    Authority,
    CommandState,
    Conflict,
    Ledger,
    NetworkReceipt,
    RoomKey,
    StorageError,
)
from .ledger_records import (
    AmmoState,
    CommandPlan,
    Context,
    EffectSource,
    ReplayPolicy,
    SaveEpochState,
    SourceKind,
)
from .ledger_schema import _writer_lock
from .native_transport import NativeTransport, TransportError
from .unified_campaign import ACCESS_IDS, STAGES

REQUIRED_COMMANDS = (12, 18, 25, 29, 33, 37, 46, 53, 54, 55)
ACTIVE = (CommandState.PLANNED, CommandState.QUEUED, CommandState.DISPATCHED, CommandState.EFFECT_UNKNOWN)






async def owner_room_exists(config, seed, team, slot, generation):
    path = Path(config.get("delivery_ledger_file") or Path(config["client_state_file"]).with_name("delivery.sqlite3")).resolve()
    if not path.is_file():
        if config.get("delivery_ledger_expected") or path.with_suffix(".expected").exists():
            raise StorageError(text("launcher.items", "missing"))
        return False
    ledger = await Ledger.open(path, read_only=True)
    try:
        room = await ledger.room(RoomKey(seed, team, slot, generation))
        if room is None:
            return False
        if room.authority is not Authority.LEDGER:
            raise Conflict("Room requires ledger delivery")
        return True
    finally:
        await ledger.close()




class RoomDelivery:
    def __init__(self, bridge, config, definitions, policies, emit):
        self.bridge, self.config, self.definitions, self.policies, self.emit = bridge, config, definitions, policies, emit
        self.ledger = None
        self.coordinator = None
        self.transport = None
        self.ammo = None
        self.game_lock = None
        self.key = RoomKey(bridge.room_seed_name, bridge.team, bridge.slot,
                           bridge._connected_slot_data["native_generation_fingerprint"])
        self.path = Path(config.get("delivery_ledger_file") or Path(config["client_state_file"]).with_name("delivery.sqlite3")).resolve()
        self.lock = asyncio.Lock()
        self.can_spend = False
        self.closed = False
        self.planned_ids = ()
        self.balance = {"available": 0, "consumed": 0, "authoritative": False}
        self._acked = set()

    async def _open(self):
        if self.ledger is not None:
            return
        expected = self.path.with_suffix(".expected")
        if self.path.is_file():
            self.ledger = await Ledger.open(self.path)
        elif expected.exists() or self.config.get("delivery_ledger_expected") or self.bridge.session_state.get("delivery_ledger_expected"):
            raise StorageError(text("launcher.items", "missing"))
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with expected.open("xb") as stream:
                stream.write(b"delivery ledger\n")
                stream.flush()
                os.fsync(stream.fileno())
            self.ledger = await Ledger.create(self.path)
        try:
            await self._bind()
        except BaseException:
            await self.ledger.close()
            self.ledger = None
            raise

    async def _bind(self):
        room = await self.ledger.room(self.key)
        self.binding = await self.ledger.bind(self.key, str(uuid4()), compatibility_revision=1, authority=Authority.LEDGER,
                                               expected_revision=None if room is None else room.revision)
        self.bridge.session_state["delivery_ledger_expected"] = True
        self.bridge.persist_session_state()

    async def _ingest_existing_owner(self):
        if self.coordinator is None:
            if not await owner_room_exists(self.config, self.key.seed, self.key.team, self.key.slot, self.key.generation_fp):
                return
            await self._open()
            self.coordinator = DeliveryCoordinator(self.ledger, self.binding, transport=self.transport,
                                                   mode=DeliveryMode.OWNER, is_current=self.current)
        await self.ingest()

    async def ready(self, evidence):
        if self.closed:
            return False
        bridge = self.bridge
        if not bridge.delivery_actions_ready() or not await bridge.checkpoint_recovery_ready() or not await bridge.runtime_effects_ready(evidence):
            return False
        link = await bridge._native_game_link_async()
        scope = await bridge._native_materialization_scope(evidence, link=link)
        if scope is None:
            return False
        identity = dict(pid=link.pid, process_created=int(scope[3]), instance_id=UUID(scope[4]).bytes, generation=scope[6])
        if self.transport is None or self.transport.scope != identity:
            await self._transport(link, identity)
        else:
            await self.transport.connect()
        admission = await self.transport.observe(12, b"0" * 64, bytes(16))
        if admission is None or not any(admission["save_binding"]):
            return False
        await self._open()
        await self._context(link, scope, identity, admission)
        return True

    async def _context(self, link, scope, identity, admission):
        epoch = await self.ledger.save_epoch(self.key)
        if epoch is not None and epoch.state is SaveEpochState.PREPARED:
            raise Conflict("New save preparation was interrupted; restore or finish the archived restart")
        save_id = f"{epoch.epoch_id if epoch else 'initial'}:{scope[1]}"
        candidate = Context(str(uuid4()), self.binding, link.pid, scope[3], str(UUID(bytes=identity["instance_id"])),
                            self.bridge.context_campaign, scope[-1], save_id, str(scope[6]), scope[6],
                            str(UUID(bytes=bytes(admission["save_binding"]))))
        if self.coordinator is None or not self.coordinator._same_context(candidate):
            await self.ledger.context(self.binding, candidate, ())
            self.coordinator = DeliveryCoordinator(self.ledger, self.binding, candidate, self.transport, mode=DeliveryMode.OWNER)
            self.coordinator.is_current = self.current
            self.bridge.delivery_coordinator = self.coordinator
            self.ammo = LedgerAmmoRefill(self.coordinator, self.storage, self.bridge.state_key)
            self.coordinator.accounting = self.ammo

    def current(self):
        return (not self.closed and self.bridge.room_seed_name == self.key.seed
                and (self.bridge.team, self.bridge.slot) == (self.key.team, self.key.slot)
                and self.bridge._connected_slot_data["native_generation_fingerprint"] == self.key.generation_fp)

    async def _transport(self, link, identity):
        if self.transport is not None:
            await self.transport.close()
        if self.game_lock is None:
            self.game_lock = _writer_lock(Path(self.bridge.base_directory) / "delivery-game")
        probe = str(link.probe)
        if link.command_prefix:
            from .proton import windows_path
            probe = windows_path(link.probe)
        transport = NativeTransport((*link.command_prefix, probe), identity, env=link.environment)
        try:
            await transport.connect()
            if not all(transport.supports(command) for command in REQUIRED_COMMANDS):
                raise Conflict("Core does not support ledger delivery")
        except BaseException:
            await transport.close()
            self.transport = None
            raise
        self.transport = transport
        self.storage = AmmoStorage(self.bridge.send_msgs)

    async def ingest(self):
        room = await self.ledger.room(self.key)
        history = self.bridge.items_received
        start = 0 if not getattr(self, "history_checked", False) else room.receipt_boundary
        for offset in range(start, len(history), 256):
            receipts = tuple(NetworkReceipt(item.item, item.location, item.player, item.flags) for item in history[offset:offset + 256])
            await self.coordinator.ingest(offset, receipts)
            await asyncio.sleep(0)
        self.history_checked = True
        self.bridge.receipt_session.restore_boundary((await self.ledger.room(self.key)).receipt_boundary)

    async def _rows(self, states=tuple(CommandState), *, unresolved_only=False):
        rows, after = [], None
        while True:
            page = await self.ledger.commands(self.key, states, after, 256, unresolved_only=unresolved_only)
            rows.extend(page)
            if len(page) < 256:
                return rows
            after = page[-1].plan.command_id
            await asyncio.sleep(0)

    async def _ownership(self, epoch):
        receipts, offset = [], 0
        while page := await self.ledger.receipts(self.key, offset, 256):
            receipts.extend(page)
            offset += len(page)
        live = frozenset(index for index in range(len(receipts)) if self.bridge.receipt_session.packet_is_live_tail(index))
        ownership = ownership_from_ledger(receipts, self.bridge.receipt_session.resolved_starting_ownership, live_receipt_indices=live)
        effective = self.bridge.effective_delivery_ownership(receipts)
        ownership = replace(ownership, reconciliation_item_ids=tuple(i for i in effective.reconciliation_item_ids if i in self.definitions),
                            blood_punch_upgrades=effective.blood_punch_upgrades,
                            acquisitions=tuple(a for a in ownership.acquisitions if a.item_id in self.definitions))
        return receipts, ownership

    async def _snapshot(self, context, evidence):
        scope = self.coordinator._evidence_scope(self.coordinator.context, self.namespace)
        return await observe_delivery(self.transport, scope, context, self.key.seed, evidence.epoch)

    async def local_command(self, key, command, evidence, *, lease=None, receipt_index=None):
        """Persist a scoped runtime effect before sending; dispatch stays unverified."""
        tag, payload = console_payload(command)
        async with self.lock:
            if not await self.ready(evidence):
                return False
            if lease is not None and lease != self.bridge.published_materialization_lease:
                return False
            context = self.coordinator.context
            plan, sources = self._runtime_plan(key, command, tag, payload, context, receipt_index)
            command_id = plan.command_id
            row = await self.ledger.command(self.key, command_id)
            if row is None:
                await self.coordinator.plan((plan,), sources=sources)
                row = await self.ledger.command(self.key, command_id)
            if row.state in (CommandState.PLANNED, CommandState.QUEUED):
                await self._execute_command(row)
            elif row.state is CommandState.DISPATCHED:
                await self._finalize(row)
            row = await self.ledger.command(self.key, command_id)
            return row.state not in (CommandState.REJECTED, CommandState.STALE)

    def _runtime_plan(self, key, command, tag, payload, context, receipt_index):
        source_id = str(uuid5(NAMESPACE_URL, f"{self.key}:runtime:{key}"))
        source = EffectSource(source_id, SourceKind.BOOTSTRAP if key.startswith("bootstrap-") else SourceKind.DERIVED_FACT,
                              f"runtime:{key}", command.encode("utf-8"))
        realization = (f"{context.native_instance}:{context.save_binding_id}:{context.save_id}:{context.load_id}:{context.map_name}"
                       if receipt_index is None else "acquisition")
        command_id = str(uuid5(NAMESPACE_URL, f"{source_id}:{realization}"))
        item_id = self.bridge.items_received[receipt_index].item if receipt_index is not None else 0
        plan = CommandPlan(command_id, receipt_index, None, f"{item_id}:0:runtime:{key}", realization, 0, None, None,
                           "command", 1, codec.encode_variant(46, 1, tag, payload), context.context_id,
                           ReplayPolicy.ADDITIVE, receipt_index is not None, 1, source_id if receipt_index is None else None)
        return plan, (source,) if receipt_index is None else ()

    @property
    def namespace(self):
        from .weapon_points import namespace_id
        return namespace_id(self.key.seed, self.key.team, self.key.slot, self.key.generation_fp).encode("ascii")

    async def reconcile(self, evidence, *, selected=None):
        async with self.lock:
            await self._ingest_existing_owner()
            if not await self.ready(evidence):
                return None, 'Return to DOOM and unpause within 30 seconds. Inventory resync is waiting for safe gameplay.'
            await self.ingest()
            context = self.bridge._refresh_runtime_context(self.bridge._connected_slot_data, evidence)
            snapshot = await self._snapshot(context, evidence)
            if snapshot is None:
                return None, 'Repair dispatched; context changed before ownership could be observed'
            await self._recover(snapshot)
            plan = await self._plan(context, snapshot, evidence, selected)
            await self._execute(snapshot)
            after = await self._snapshot(context, evidence)
            if after is not None:
                await self._recover(after)
            if selected is None:
                await self._balance()
            await self._publish_local_obligation()
            if selected is not None and (plan.unresolved_item_ids or await self._has_unconfirmed_repairs(selected)):
                return plan.reconciliation, 'Repair dispatched; native ownership has not been confirmed. Consumables were preserved.'
        if selected is None:
            await self._temporaries(plan)
        return plan.reconciliation, None

    async def _temporaries(self, plan):
        for acquisition in plan.acquisitions:
            if acquisition.transient:
                await self.bridge.transient_effect_manager.apply_receipt(
                    acquisition.item_id, self.bridge._observe_transient_runtime(), receipt_index=int(acquisition.source_key[3:]))

    async def _has_unconfirmed_repairs(self, selected):
        for row in await self._rows((*ACTIVE, CommandState.REJECTED), unresolved_only=True):
            if row.plan.required and row.plan.ammo_operation_id is None and effect_identity(row.plan)[0] in selected:
                self.planned_ids = tuple(dict.fromkeys((*self.planned_ids, row.plan.command_id)))
                return True
        return False

    async def _publish_local_obligation(self):
        if await self._publish_ammo_recovery():
            return
        rows = await self._rows((CommandState.EFFECT_UNKNOWN, CommandState.REJECTED), unresolved_only=True)
        row = next((r for r in rows if r.plan.required and r.plan.receipt_index is None), None)
        self.emit("delivery_local_status", room=asdict(self.key),
                  command_id=row.plan.command_id if row else None,
                  expected_revision=row.revision if row else None,
                  item_id=(7770024 if row.plan.ammo_operation_id else effect_identity(row.plan)[0]) if row else None)

    async def _publish_ammo_recovery(self):
        operations = await self.ledger.ammo_operations(self.key, (AmmoState.UNKNOWN,), None, 1)
        operation = next((row for row in operations if row.operation.writer_id == self.ledger.profile_id), None)
        if operation is not None:
            self.emit("delivery_local_status", room=asdict(self.key), item_id=7770024,
                      command_id="ammo-operation:" + operation.operation.operation_id, expected_revision=operation.revision)
            return True
        return False

    async def _plan(self, context, snapshot, evidence, selected):
        epoch = await self.ledger.save_epoch(self.key)
        receipts, ownership = await self._ownership(epoch)
        sources = await self._sources()
        rows = await self._rows()
        replacements = await self._missing_after_core_loss(rows, snapshot.readback.observation)
        planning_rows = _planning_rows(rows, replacements)
        obligations = _obligations(planning_rows, receipts, sources)
        scope = MaterializationScope(self.key.seed, self.key.team, self.key.slot, self.bridge.state_key,
                                     self.bridge.published_materialization_lease, evidence.epoch, "delivery", self.bridge.state_key)
        comparison = _epoch_ownership(ownership, epoch, self.policies)
        if epoch is not None:
            obligations = await self._epoch_obligations(planning_rows, receipts, sources)
        total_points = sum(a.item_id == 7770903 for a in comparison.acquisitions) * 3
        plan = self.coordinator.compare(comparison, definitions=self.definitions, policies=self.policies,
            observation=snapshot.readback.observation, context=context, scope=scope, obligations=obligations,
            special_mode=self.bridge._connected_slot_data["runtime"].get("special_weapon"),
            selected_item_ids=selected, weapon_points=(total_points, snapshot.weapon_points_gained)
            if selected is None or 7770903 in selected else None)
        compiler = DeliveryPlans(self.key, self.coordinator.context, ownership, self.definitions, epoch.epoch_id if epoch else "initial",
                                 snapshot.readback.observation)
        commands, local_sources = compiler.compile(plan)
        commands = restore_confirmed(commands, rows)
        if selected is None:
            commands += self._notifications(compiler, receipts)
        commands = _link_restoration(commands, replacements)
        self.planned_ids = tuple(c.command_id for c in commands)
        await self._store_plan(commands, local_sources)
        await self._points(compiler, plan, snapshot, comparison)
        await self._satisfied(receipts, snapshot.readback.observation)
        return plan

    def _notifications(self, compiler, receipts):
        start = len(compiler.commands)
        for index, receipt in enumerate(receipts):
            if receipt.item not in self.definitions:
                continue
            for ordinal, command in enumerate(self.bridge.delivery_notification_commands(receipt.item, index)):
                compiler.console(receipt.item, 0, ordinal, command, source_key=f"ap:{index}", acquisition=True)
                compiler.commands[-1] = replace(compiler.commands[-1], required=False)
        return tuple(compiler.commands[start:])

    async def _missing_after_core_loss(self, rows, observation):
        contexts, replacements = {}, {}
        for row in rows:
            if not row.plan.required or row.plan.replay is not ReplayPolicy.IDEMPOTENT or row.state is not CommandState.EFFECT_UNKNOWN:
                continue
            if row.plan.context_id not in contexts:
                contexts[row.plan.context_id] = await self.ledger.saved_context(self.key, row.plan.context_id)
            if contexts[row.plan.context_id].native_instance != self.coordinator.context.native_instance:
                item, stage = effect_identity(row.plan)
                if _persistent_missing(observation, item, stage):
                    replacements[row.plan.command_id] = row.plan.effect_key
        return replacements

    async def _store_plan(self, commands, local_sources):
        for start in range(0, len(commands), 256):
            missing = [c for c in commands[start:start + 256] if await self.ledger.command(self.key, c.command_id) is None]
            regular = []
            for command in missing:
                item_id, _ = effect_identity(command)
                if item_id in (7770016, 7770142):
                    await self.coordinator.plan_battery(command, 2 if item_id == 7770142 else 1, self.namespace, sources=local_sources)
                    await self._execute_command(await self.ledger.command(self.key, command.command_id))
                else:
                    regular.append(command)
            await self.coordinator.plan(tuple(regular), sources=local_sources)
            await asyncio.sleep(0)

    async def _epoch_obligations(self, rows, receipts, sources):
        contexts, obligations = {}, []
        for row in rows:
            if row.plan.context_id not in contexts:
                contexts[row.plan.context_id] = await self.ledger.saved_context(self.key, row.plan.context_id)
            if _obligation_in_epoch(row, contexts[row.plan.context_id], self.coordinator.context):
                value = obligation(row, receipts, sources)
                if value is not None:
                    obligations.append(value)
        return tuple(obligations)

    async def _sources(self):
        sources, after = {}, None
        while page := await self.ledger.sources(self.key, after, 256):
            sources.update({source.source_id: source for source in page})
            after = page[-1].source_id
        return sources

    async def _points(self, compiler, plan, snapshot, ownership):
        previous = None
        for baseline in plan.weapon_point_grants:
            acquisitions = [a for a in ownership.acquisitions if a.item_id == 7770903]
            compiler.commands = []
            compiler.add(7770903, baseline // 3, 0, "weapon_points", 1, {"amount": 3, "expected_gained": baseline},
                         source_key=acquisitions[baseline // 3].source_key)
            command, = compiler.commands
            command = replace(command, predecessor_id=previous, replay=ReplayPolicy.ADDITIVE)
            if await self.ledger.command(self.key, command.command_id) is None:
                await self.coordinator.plan((command,), sources=tuple(compiler.sources.values()))
            previous = command.command_id

    async def _satisfied(self, receipts, observation):
        for offset in range(0, len(receipts), 256):
            indices = tuple(offset + i for i, receipt in enumerate(receipts[offset:offset + 256])
                            if receipt.item in self.definitions and
                            (classify_item_definition(self.definitions[receipt.item]) == "no_op" or observation.is_owned(receipt.item)))
            if indices:
                await self.coordinator.satisfy(indices, b"inventory_snapshot")
        await self._satisfy_access(receipts)

    async def _satisfy_access(self, receipts):
        for item_id in sorted({r.item for r in receipts} & ACCESS_IDS.keys()):
            result = await observe_optional(self.transport, 55, self.namespace, UUID(self.coordinator.context.save_binding_id).bytes,
                                            stage_id=item_id)
            scope = self.coordinator._evidence_scope(self.coordinator.context, self.namespace)
            if result is not None and _access_observed(mission_access_observation(result), item_id, scope):
                indices = tuple(index for index, receipt in enumerate(receipts) if receipt.item == item_id)
                for start in range(0, len(indices), 256):
                    await self.coordinator.satisfy(indices[start:start + 256], b"native_mission_access")

    async def _recover(self, snapshot):
        for row in await self._rows((*ACTIVE, CommandState.STALE, CommandState.CONFIRMED, CommandState.REJECTED)):
            if row.state in (CommandState.CONFIRMED, CommandState.REJECTED) or row.resolution is not None:
                await self._ack(row)
                continue
            if row.state is CommandState.STALE:
                await self.coordinator.recover(row.plan.command_id)
                await self._ack(row)
                continue
            if row.attempt_count:
                if row.state in (CommandState.PLANNED, CommandState.QUEUED):
                    await self.coordinator.send(row.plan.command_id)
                    row = await self.ledger.command(self.key, row.plan.command_id)
                await self._recover_one(row, snapshot)
            elif row.plan.context_id != self.coordinator.context.context_id:
                original = await self.ledger.saved_context(self.key, row.plan.context_id)
                if not self.coordinator._same_context(original):
                    await self.coordinator.expire_unsent(row.plan.command_id)

    async def _recover_one(self, row, snapshot):
        proof = {}
        if row.plan.required and row.plan.replay is ReplayPolicy.IDEMPOTENT and row.plan.ammo_operation_id is None:
            attempts = await self.ledger.attempts(self.key, row.plan.command_id, row.attempt_count - 1, 1)
            item_id, stage = effect_identity(row.plan)
            original = await self.ledger.saved_context(self.key, row.plan.context_id)
            if self.coordinator._same_context(original):
                proof = dict(expectation=EffectExpectation(attempts[0].attempt.reference, snapshot.readback.after_scope,
                                                           InventoryEffect(item_id, stage)), evidence=snapshot.readback)
        await self._finalize(row, **proof)

    async def _finalize(self, row, **proof):
        await self.coordinator.recover(row.plan.command_id, **proof)
        current = await self.ledger.command(self.key, row.plan.command_id)
        await self._ack(current)
        if current.revision != row.revision:
            self.emit("delivery_progress", room=asdict(self.key), command_id=row.plan.command_id,
                      state=current.state.value, revision=current.revision, receipt_index=row.plan.receipt_index)

    async def _ack(self, row):
        if (row.plan.command_id in self._acked or not row.attempt_count
                or row.state not in (CommandState.CONFIRMED, CommandState.REJECTED, CommandState.EFFECT_UNKNOWN, CommandState.STALE)):
            return
        try:
            response = await self.coordinator.ack(row.plan.command_id)
        except (TransportError, OSError, TimeoutError):
            return
        if response is not None and response.response["error"] == 0:
            self._acked.add(row.plan.command_id)

    async def _execute(self, snapshot):
        rows = await self._rows((CommandState.PLANNED, CommandState.QUEUED), unresolved_only=True)
        for row in _execution_batch(rows):
            try:
                await self._execute_command(row)
            except Conflict:
                continue
            except (TransportError, TimeoutError):
                if row.plan.required:
                    raise
                return
            await asyncio.sleep(0)

    async def _execute_command(self, row):
        await self.coordinator.prepare(row.plan.command_id, self.namespace)
        await self.coordinator.send(row.plan.command_id)
        await self._finalize(row)

    async def _balance(self):
        self.can_spend = await self.ammo.owner() in (None, self.ledger.profile_id)
        self.coordinator.can_spend = self.can_spend
        if self.can_spend:
            boundary = self.bridge.receipt_session.processed_boundary
            received = sum(item.item == 7770024 for item in self.bridge.items_received[:boundary])
            received += self.bridge.receipt_session.resolved_starting_count(7770024)
            scope = AmmoCommandScope(self.bridge.state_key, self.coordinator.context.map_name, self.coordinator.context.save_id, False)
            totals = await self.ammo.synchronize(received, scope)
            if totals is not None:
                self.balance = dict(available=min(AMMO_REFILL_CAPACITY, max(0, totals.credited - totals.consumed - totals.discarded - totals.reserved)),
                                    consumed=totals.consumed, authoritative=True)
        self.emit("ammo_refill", **self.balance, can_spend=self.can_spend, status="balance",
                  message='Ammo Refill charges available: {count}'.format(count=str(self.balance["available"])))

    async def close(self):
        self.closed = True
        if self.transport is not None:
            await self.transport.close()
        if self.ledger is not None:
            await self.ledger.close()
        if self.game_lock is not None:
            self.game_lock.close()


def _epoch_ownership(ownership, epoch, policies):
    if epoch is None:
        return ownership
    acquisitions = tuple(a for a in ownership.acquisitions
                         if policies[a.item_id].policy != NEVER_REPLAY
                         or (a.source_key.startswith("ap:") and int(a.source_key[3:]) >= epoch.receipt_boundary))
    return replace(ownership, acquisitions=acquisitions)


def _access_observed(observation, item_id, scope):
    access = MissionAccess(observation.revision, item_id, STAGES[ACCESS_IDS[item_id]]["map"])
    return effect_confirmed(EffectExpectation(b"", scope, access), observation)


def _persistent_missing(observation, item, stage):
    from doom_eap.contracts.inventory_domain import UNKNOWN
    if item == 7770904:
        return item in observation.stage_masks and not observation.stage_masks[item] & (1 << stage)
    return observation.get_state(item) != UNKNOWN and not observation.confirms_repair(item, stage)


def _link_restoration(commands, replacements):
    by_effect = {effect: original for original, effect in replacements.items()}
    return tuple(replace(command, supersedes_id=command.supersedes_id or by_effect.get(command.effect_key)) for command in commands)


def _planning_rows(rows, replacements):
    return [row for row in rows if row.plan.command_id not in replacements]


def _obligations(rows, receipts, sources):
    return tuple(value for row in rows if (value := obligation(row, receipts, sources)) is not None)


def _obligation_in_epoch(row, original, current):
    if original.save_id == current.save_id:
        return True
    if row.plan.ammo_operation_id is not None:
        return False
    return row.plan.replay is ReplayPolicy.ADDITIVE and effect_identity(row.plan)[0] != 7770903


def _execution_batch(rows):
    return [row for row in rows if row.plan.required] + [row for row in rows if not row.plan.required][:64]
