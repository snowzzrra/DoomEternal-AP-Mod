"""Pure, fail-closed compiler for manual AP inventory reconciliation."""

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from foundation import compile_item_delivery_plan

REPLAY_IDEMPOTENT = "replay_idempotent"
SPECIAL_PROGRESSIVE = "special_progressive"
NEVER_REPLAY = "never_replay"
SUPPORTED_POLICIES = frozenset(
    {REPLAY_IDEMPOTENT, SPECIAL_PROGRESSIVE, NEVER_REPLAY}
)


@dataclass(frozen=True)
class ReplayPolicy:
    item_id: int
    name: str
    policy: str


@dataclass(frozen=True)
class ReconciliationCommand:
    item_id: int
    name: str
    policy: str
    stage: int
    spool_id: str
    command: str
    description: str


@dataclass(frozen=True)
class ReconciliationSelection:
    item_id: int
    name: str
    policy: str
    received_count: int
    commands: tuple[str, ...]


@dataclass(frozen=True)
class ReconciliationPlan:
    commands: tuple[ReconciliationCommand, ...]
    selections: tuple[ReconciliationSelection, ...]
    replayed: int
    special_stages: int
    skipped_never_replay: int
    skipped_unproven: int = 0


def _read_registry(source: Path | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(source, Mapping):
        return source
    with Path(source).open("r", encoding="utf-8") as file:
        loaded = json.load(file)
    if not isinstance(loaded, dict):
        raise ValueError("item replay policy registry must be an object")
    return loaded


def load_policy_registry(
    source: Path | Mapping[str, Any], definitions: Mapping[int, Any]
) -> dict[int, ReplayPolicy]:
    """Load an exact numeric registry; missing, extra and unknown policy fail."""
    raw = _read_registry(source)
    items = raw.get("items")
    if not isinstance(items, dict):
        raise ValueError("item replay policy registry items must be an object")
    parsed: dict[int, ReplayPolicy] = {}
    for raw_id, entry in items.items():
        try:
            item_id = int(raw_id)
        except (TypeError, ValueError) as error:
            raise ValueError(f"invalid numeric item policy ID: {raw_id!r}") from error
        if str(item_id) != str(raw_id):
            raise ValueError(f"item policy ID is not canonical decimal: {raw_id!r}")
        if not isinstance(entry, dict):
            raise ValueError(f"item policy {item_id} must be an object")
        if set(entry) != {"name", "policy"}:
            raise ValueError(
                f"item policy {item_id} must contain exactly name and policy"
            )
        name = entry["name"]
        policy = entry["policy"]
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"item policy {item_id} has invalid name")
        if policy not in SUPPORTED_POLICIES:
            raise ValueError(f"unsupported policy for item {item_id}: {policy!r}")
        parsed[item_id] = ReplayPolicy(item_id, name, policy)

    missing = sorted(set(definitions) - set(parsed))
    extra = sorted(set(parsed) - set(definitions))
    if missing:
        raise ValueError("missing policy for active item ID(s): " + ", ".join(map(str, missing)))
    if extra:
        raise ValueError("policy exists for inactive item ID(s): " + ", ".join(map(str, extra)))
    return parsed


def compile_reconciliation_plan(
    received_item_ids: Iterable[int],
    definitions: Mapping[int, Any],
    registry: Mapping[int, ReplayPolicy],
    slot_identity: str,
    epoch: int,
) -> ReconciliationPlan:
    """Compile ownership replay from authoritative receipts without side effects."""
    if not slot_identity or not isinstance(epoch, int) or epoch < 0:
        raise ValueError("reconciliation requires slot identity and non-negative epoch")
    if set(registry) != set(definitions):
        raise ValueError("reconciliation registry does not exactly cover active items")
    counts = Counter(received_item_ids)
    unknown = sorted(set(counts) - set(registry))
    if unknown:
        raise ValueError("received item has no policy: " + ", ".join(map(str, unknown)))

    commands: list[ReconciliationCommand] = []
    selections: list[ReconciliationSelection] = []
    replayed = 0
    special_stages = 0
    skipped_never_replay = 0

    for item_id in sorted(counts):
        policy = registry[item_id]
        count = counts[item_id]
        selected_commands: list[str] = []
        if policy.policy == NEVER_REPLAY:
            skipped_never_replay += 1
        elif policy.policy == REPLAY_IDEMPOTENT:
            plan = compile_item_delivery_plan(item_id, definitions)
            if not plan.commands:
                raise ValueError(f"replay-safe item {item_id} compiled no commands")
            for delivery in plan.commands:
                stage = delivery.index
                spool_id = (
                    f"reconcile-{slot_identity}-e{epoch}-item{item_id}-stage{stage}"
                )
                commands.append(
                    ReconciliationCommand(
                        item_id, policy.name, policy.policy, stage, spool_id,
                        delivery.command, plan.description,
                    )
                )
                selected_commands.append(delivery.command)
            replayed += 1
        elif policy.policy == SPECIAL_PROGRESSIVE:
            for stage in range(count):
                plan = compile_item_delivery_plan(item_id, definitions, stage=stage)
                if len(plan.commands) != 1:
                    raise ValueError(
                        f"progressive item {item_id} stage {stage} must compile one command"
                    )
                delivery = plan.commands[0]
                spool_id = (
                    f"reconcile-{slot_identity}-e{epoch}-item{item_id}-stage{stage}"
                )
                commands.append(
                    ReconciliationCommand(
                        item_id, policy.name, policy.policy, stage, spool_id,
                        delivery.command, plan.description,
                    )
                )
                selected_commands.append(delivery.command)
                special_stages += 1
        selections.append(
            ReconciliationSelection(
                item_id, policy.name, policy.policy, count,
                tuple(selected_commands),
            )
        )

    spool_ids = [command.spool_id for command in commands]
    if len(spool_ids) != len(set(spool_ids)):
        raise ValueError("reconciliation compiled duplicate spool IDs")
    return ReconciliationPlan(
        tuple(commands), tuple(selections), replayed, special_stages,
        skipped_never_replay,
    )
