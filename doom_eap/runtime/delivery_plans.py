"""Encode inventory decisions as durable delivery plans; scheduling belongs to delivery.py."""

import json
import re
from dataclasses import replace
from uuid import NAMESPACE_URL, uuid5

from doom_eap.contracts.foundation import compile_item_delivery_plan
from doom_eap.contracts.materialization import SUPPORT_RUNE_IDS

from . import sentinel_protocol_generated as codec
from .inventory import InventoryObligation, NativeEffectKind
from .inventory_observation import NORMAL_RUNES
from .ledger import CommandPlan, CommandState, EffectSource, ReplayPolicy, SourceKind


def console_payload(command):
    """Translate the bounded console grammar to the Core's typed commands."""
    match = re.fullmatch(r"ai_ScriptCmdEnt ([A-Za-z0-9_-]+) activate", command)
    if match:
        return 1, {"entity": match[1]}
    match = re.fullmatch(r"ai_ScriptCmdEnt player1 givePlayerPerk ([A-Za-z0-9_/-]+)", command)
    if match:
        return 2, {"perk": match[1]}
    match = re.fullmatch(r"(g_damageScaleAllToAI|g_damageScaleAllToSlayer|g_infiniteAmmo) ([0-9]+(?:\.[0-9]+)?)", command)
    if match:
        variables = ("g_damageScaleAllToAI", "g_damageScaleAllToSlayer", "g_infiniteAmmo")
        variable = variables.index(match[1])
        if (variable == 2 and match[2] in ("0", "1")) or (variable != 2 and .01 <= float(match[2]) <= 100):
            return 8, {"variable": variable, "value": match[2]}
    raise ValueError("Unsupported runtime command")


def effect_identity(plan):
    return tuple(map(int, plan.effect_key.split(":")[:2]))


def obligation(row, receipts, sources):
    if row.plan.ammo_operation_id is not None or not row.plan.required:
        return None
    item_id, stage = effect_identity(row.plan)
    key = f"ap:{row.plan.receipt_index}" if row.plan.receipt_index is not None else sources[row.plan.local_source_id].source_key
    return InventoryObligation(item_id, stage, row.state, row.resolution, key)


def restore_confirmed(commands, rows):
    by_id = {row.plan.command_id: row for row in rows}
    children = {row.plan.supersedes_id: row for row in rows if row.plan.supersedes_id is not None}
    restored = []
    for command in commands:
        previous = by_id.get(command.command_id)
        if previous is not None and command.replay is ReplayPolicy.IDEMPOTENT:
            previous = _latest(previous, children)
            if previous.state is CommandState.CONFIRMED:
                identity = str(uuid5(NAMESPACE_URL, f"{previous.plan.command_id}:{previous.revision}:missing"))
                command = replace(command, command_id=identity, realization_key=identity, supersedes_id=previous.plan.command_id)
        restored.append(command)
    return tuple(restored)


def _latest(row, children):
    while row.plan.command_id in children:
        row = children[row.plan.command_id]
    return row


class DeliveryPlans:
    def __init__(self, room, context, ownership, definitions, epoch, observation):
        self.room, self.context, self.ownership, self.definitions = room, context, ownership, definitions
        self.epoch = epoch
        self.observation = observation
        self.commands = []
        self.sources = {}
        self.by_item = {}
        for acquisition in ownership.acquisitions:
            self.by_item.setdefault(acquisition.item_id, []).append(acquisition.source_key)

    def _source(self, item_id, stage, source_key=None):
        candidates = self.by_item.get(item_id, ())
        key = source_key or (candidates[min(stage, len(candidates) - 1)] if candidates else f"derived:{item_id}")
        if key.startswith("ap:"):
            return int(key[3:]), None
        identity = str(uuid5(NAMESPACE_URL, f"{self.room}:{key}"))
        kind = SourceKind.DERIVED_FACT if key.startswith("derived:") else SourceKind.STARTING_GRANT
        self.sources[identity] = EffectSource(identity, kind, key, str(item_id).encode("ascii"))
        return None, identity

    def add(self, item_id, stage, ordinal, kind, tag, payload, *, source_key=None, acquisition=False):
        receipt, local = self._source(item_id, stage, source_key)
        effect = f"{item_id}:{stage}:{kind}:{tag}:{json.dumps(payload, sort_keys=True)}"
        realization = "acquisition" if acquisition else (
            f"{self.epoch}:{self.context.native_instance}:{self.context.save_binding_id}:"
            f"{self.context.save_id}:{self.context.load_id}:{self.context.map_name}"
        )
        identity = str(uuid5(NAMESPACE_URL, f"{self.room}:{receipt}:{local}:{effect}:{realization}"))
        command_type = next(i for i, command in codec.COMMANDS.items() if command["name"] == kind)
        self.commands.append(CommandPlan(identity, receipt, None, effect, realization, ordinal, None, None,
                                          kind, 1, codec.encode_variant(command_type, 1, tag, payload),
                                          self.context.context_id, ReplayPolicy.ADDITIVE if acquisition else ReplayPolicy.IDEMPOTENT,
                                          True, 1, local))

    def console(self, item_id, stage, ordinal, command, **options):
        tag, payload = console_payload(command)
        self.add(item_id, stage, ordinal, "command", tag, payload, **options)

    def persistent(self, command, ordinal):
        item_id, stage = command.item_id, command.stage
        if item_id in NORMAL_RUNES:
            self.add(item_id, stage, 0, "runes", 1, {"normal_runes": 1 << NORMAL_RUNES.index(item_id)})
        elif item_id in SUPPORT_RUNE_IDS:
            self.add(item_id, stage, 0, "runes", 2, {"support_runes": 1 << sorted(SUPPORT_RUNE_IDS).index(item_id)})
        elif item_id == 7770083:
            self.add(item_id, stage, 0, "arsenal", 1, {"mods": 1 << 8})
        elif item_id in (7770901, 7770902):
            self.special(item_id, stage)
        else:
            self.console(item_id, stage, ordinal, command.command)

    def special(self, item_id, stage):
        hammer_only = item_id == 7770902
        hammer = int(hammer_only or stage >= 2)
        tier = 2 if stage >= (2 if hammer_only else 3) else hammer
        self.add(item_id, stage, 0, "special", 1, {"crucible": int(not hammer_only), "hammer": hammer, "hammer_tier": tier})

    def compile(self, plan):
        if plan.reconciliation is not None:
            for ordinal, command in enumerate(plan.reconciliation.commands):
                self.persistent(command, ordinal)
        for effect in plan.native_effects:
            if effect.kind is NativeEffectKind.MASTERY:
                self.add(effect.item_id, effect.stage, 0, "arsenal", 3, {"masteries": effect.mask})
            else:
                compiled = compile_item_delivery_plan(effect.item_id, self.definitions, stage=effect.stage)
                for command in compiled.commands:
                    self.console(effect.item_id, effect.stage, command.index, command.command)
        for acquisition in plan.acquisitions:
            for command in acquisition.commands:
                self.console(acquisition.item_id, command.index, command.index, command.command,
                             source_key=acquisition.source_key, acquisition=True)
        self._dependencies(plan.dependencies)
        return tuple({c.command_id: c for c in self.commands}.values()), tuple(self.sources.values())

    def _dependencies(self, dependencies):
        by_effect = {effect_identity(c): c.command_id for c in self.commands}
        commands = []
        for command in self.commands:
            item_id, stage = effect_identity(command)
            dependency = next((d for d in dependencies if (d.item_id, d.stage) == (item_id, stage)), None)
            if dependency is not None:
                predecessor = by_effect.get((dependency.prerequisite_item_id, dependency.prerequisite_stage))
                if predecessor is None and not self.observation.confirms_repair(dependency.prerequisite_item_id, dependency.prerequisite_stage):
                    continue
                command = replace(command, predecessor_id=predecessor)
            commands.append(command)
        self.commands = commands
