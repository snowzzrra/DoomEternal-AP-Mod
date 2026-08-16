"""Pure, fail-closed item history observation and reconciliation compiler."""

import copy
import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from doom_eap.contracts.foundation import compile_item_delivery_plan

REPLAY_IDEMPOTENT = "replay_idempotent"
SPECIAL_PROGRESSIVE = "special_progressive"
NEVER_REPLAY = "never_replay"
AP_RECEIPT_FEEDBACK = "ap"
NATIVE_ONLY_RECEIPT_FEEDBACK = "native_only"
NEW_RECEIPT = "new_receipt"
HISTORICAL_OWNERSHIP = "historical_ownership"
RECONCILIATION_REPAIR = "reconciliation_repair"
PRESENTATION_REPAIR = "presentation_repair"
DELIVERY_INTENTS = frozenset(
    {
        NEW_RECEIPT,
        HISTORICAL_OWNERSHIP,
        RECONCILIATION_REPAIR,
        PRESENTATION_REPAIR,
    }
)
CLIENT_STATE_VERSION = 2
SUPPORTED_POLICIES = frozenset(
    {REPLAY_IDEMPOTENT, SPECIAL_PROGRESSIVE, NEVER_REPLAY}
)
SUPPORTED_RECEIPT_FEEDBACK = frozenset(
    {AP_RECEIPT_FEEDBACK, NATIVE_ONLY_RECEIPT_FEEDBACK}
)
MATERIALIZATION_EPOCH_PATTERN = re.compile(r"[0-9]+:[0-9]+")


@dataclass(frozen=True)
class ReplayPolicy:
    item_id: int
    name: str
    policy: str
    receipt_feedback: str = AP_RECEIPT_FEEDBACK


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


@dataclass(frozen=True)
class ObservedReceipt:
    """Stable, side-effect-free view of one authoritative receipt."""

    index: int
    item_id: int
    receipt_id: str | None


@dataclass(frozen=True)
class ReceivedItemsObservation:
    """Classification of one ReceivedItems snapshot against durable progress."""

    historical: tuple[ObservedReceipt, ...]
    new: tuple[ObservedReceipt, ...]
    duplicates: tuple[ObservedReceipt, ...]
    processed_boundary: int
    highest_observed_index: int
    receipt_item_ids: tuple[int, ...]
    historical_item_ids: tuple[int, ...]
    new_item_ids: tuple[int, ...]
    receipt_ids: tuple[str, ...]

    @property
    def next_boundary(self) -> int:
        """Highest authoritative list position that can be acknowledged."""
        return self.highest_observed_index + 1

    @staticmethod
    def _deduplicated_item_ids(receipts: Iterable[ObservedReceipt]) -> tuple[int, ...]:
        return tuple(receipt.item_id for receipt in receipts)

    @property
    def authoritative_item_ids(self) -> tuple[int, ...]:
        """Item IDs from historical and currently visible receipts."""
        return self._deduplicated_item_ids((*self.historical, *self.new))

    @property
    def historical_authoritative_item_ids(self) -> tuple[int, ...]:
        """Processed ownership only, with stable duplicate packets removed."""
        return self._deduplicated_item_ids(self.historical)


def _field(receipt: Any, name: str) -> Any:
    if isinstance(receipt, Mapping):
        return receipt.get(name)
    return getattr(receipt, name, None)


def receipt_item_id(receipt: Any) -> int:
    """Extract an AP item ID without depending on CommonClient classes."""
    if isinstance(receipt, int) and not isinstance(receipt, bool):
        return receipt
    value = _field(receipt, "item")
    if value is None:
        value = _field(receipt, "item_id")
    if value is None and isinstance(receipt, (tuple, list)) and receipt:
        value = receipt[0]
    if value is None or isinstance(value, bool):
        raise ValueError("received item ID must be an integer")
    try:
        item_id = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"received item has invalid item ID: {value!r}") from error
    return item_id


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(value[key])
            for key in sorted(value, key=lambda candidate: str(candidate))
        }
    if isinstance(value, (tuple, list)):
        return [_canonical_value(entry) for entry in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def receipt_identity(receipt: Any) -> str | None:
    """Return deterministic packet identity when receipt has AP packet fields."""
    explicit = _field(receipt, "receipt_id")
    if explicit is not None:
        value = _canonical_value(explicit)
        return "receipt:" + json.dumps(value, sort_keys=True, separators=(",", ":"))

    fields = (
        _field(receipt, "location"),
        _field(receipt, "player"),
        receipt_item_id(receipt),
        _field(receipt, "flags"),
    )
    if all(value is None for value in (fields[0], fields[1], fields[3])):
        return None
    return "network:" + json.dumps(
        _canonical_value(fields), sort_keys=True, separators=(",", ":")
    )


def observe_received_items(
    received_items: Iterable[Any],
    processed_boundary: int,
    processed_receipt_ids: Iterable[str] | Mapping[str, int] = (),
) -> ReceivedItemsObservation:
    """Classify authoritative history without delivering or mutating state.

    Positions before durable ``processed_boundary`` are historical. Positions at
    or after it are ordered new receipts unless their stable packet identity was
    already observed. Item IDs alone are intentionally not de-duplicated because
    two equal IDs can be two legitimate AP receipts.
    """
    if isinstance(processed_boundary, bool) or not isinstance(processed_boundary, int):
        raise ValueError("processed boundary must be a non-negative integer")
    if processed_boundary < 0:
        raise ValueError("processed boundary must be a non-negative integer")

    snapshot = tuple(received_items)
    if isinstance(processed_receipt_ids, Mapping):
        known_occurrences = {
            key: count
            for key, count in processed_receipt_ids.items()
            if isinstance(key, str)
            and key
            and isinstance(count, int)
            and not isinstance(count, bool)
            and count > 0
        }
    else:
        known_occurrences = Counter(
            value
            for value in processed_receipt_ids
            if isinstance(value, str) and value
        )
    snapshot_occurrences: Counter[str] = Counter()
    historical: list[ObservedReceipt] = []
    new: list[ObservedReceipt] = []
    duplicates: list[ObservedReceipt] = []
    all_item_ids: list[int] = []
    historical_item_ids: list[int] = []
    new_item_ids: list[int] = []
    receipt_ids: list[str] = []

    for index, receipt in enumerate(snapshot):
        item_id = receipt_item_id(receipt)
        stable_id = receipt_identity(receipt)
        observed = ObservedReceipt(index, item_id, stable_id)
        all_item_ids.append(item_id)
        if stable_id is not None:
            receipt_ids.append(stable_id)
            snapshot_occurrences[stable_id] += 1
        if index < processed_boundary:
            historical.append(observed)
            historical_item_ids.append(item_id)
            continue
        if (
            stable_id is not None
            and snapshot_occurrences[stable_id] <= known_occurrences.get(stable_id, 0)
        ):
            duplicates.append(observed)
            continue
        new.append(observed)
        new_item_ids.append(item_id)

    return ReceivedItemsObservation(
        tuple(historical),
        tuple(new),
        tuple(duplicates),
        processed_boundary,
        len(snapshot) - 1,
        tuple(all_item_ids),
        tuple(historical_item_ids),
        tuple(new_item_ids),
        tuple(receipt_ids),
    )


classify_received_items = observe_received_items


def receipt_history_fingerprint(received_items: Iterable[Any]) -> str:
    """Hash authoritative receipt order and stable packet identity."""
    records = []
    for receipt in received_items:
        stable_id = receipt_identity(receipt)
        records.append(
            {
                "index": len(records),
                "item_id": receipt_item_id(receipt),
                "receipt_id": stable_id,
            }
        )
    payload = json.dumps(records, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_nonnegative_int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return default
    return value


def _safe_receipt_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for entry in value:
        if isinstance(entry, str) and entry and entry not in result:
            result.append(entry)
    return result


def _safe_receipt_counts(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    return {
        key: count
        for key, count in value.items()
        if isinstance(key, str)
        and key
        and isinstance(count, int)
        and not isinstance(count, bool)
        and count > 0
    }


def migrate_legacy_session_key(
    sessions: dict[str, Any],
    *,
    seed_name: Any,
    team: Any,
    slot: Any,
) -> tuple[str | None, str | None]:
    """Move one v1 seed-less session only into matching current identity."""
    if (
        not isinstance(sessions, dict)
        or not isinstance(seed_name, str)
        or re.fullmatch(r"[A-Za-z0-9_.-]+", seed_name) is None
        or isinstance(team, bool)
        or not isinstance(team, int)
        or team < 0
        or isinstance(slot, bool)
        or not isinstance(slot, int)
        or slot < 0
    ):
        return None, None

    state_key = f"{seed_name}:{team}:{slot}"
    legacy_key = f"None:{team}:{slot}"
    if state_key not in sessions and legacy_key in sessions:
        sessions[state_key] = sessions.pop(legacy_key)
        return state_key, legacy_key
    return state_key, None


def normalize_session_state(session: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return safe v2 session state while preserving unrelated legacy fields."""
    if not isinstance(session, Mapping):
        session = {}
    normalized = copy.deepcopy(dict(session))
    processed = _safe_nonnegative_int(normalized.get("processed_items"), 0)
    normalized["processed_items"] = processed

    history = normalized.get("receipt_history")
    if not isinstance(history, Mapping):
        history = {}
    history = copy.deepcopy(dict(history))
    history["processed_boundary"] = processed
    highest = history.get("highest_observed_index", max(processed - 1, -1))
    if isinstance(highest, bool) or not isinstance(highest, int) or highest < -1:
        highest = max(processed - 1, -1)
    history["highest_observed_index"] = highest
    legacy_receipt_ids = _safe_receipt_ids(history.get("receipt_ids"))
    receipt_counts = _safe_receipt_counts(history.get("receipt_counts"))
    if not receipt_counts:
        receipt_counts = dict(Counter(legacy_receipt_ids))
    history["receipt_counts"] = receipt_counts
    history["receipt_ids"] = legacy_receipt_ids
    owned_ids = history.get("owned_item_ids")
    if not isinstance(owned_ids, list):
        owned_ids = []
    history["owned_item_ids"] = sorted(
        {
            entry
            for entry in owned_ids
            if isinstance(entry, int) and not isinstance(entry, bool)
        }
    )
    normalized["receipt_history"] = history

    resync = normalized.get("item_resync")
    normalized["item_resync"] = copy.deepcopy(dict(resync)) if isinstance(resync, Mapping) else {}

    bootstrap = normalized.get("bootstrap")
    if not isinstance(bootstrap, Mapping):
        bootstrap = {"actions": {}}
    else:
        bootstrap = copy.deepcopy(dict(bootstrap))
        revision = bootstrap.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            bootstrap.pop("revision", None)
        if not isinstance(bootstrap.get("actions"), Mapping):
            bootstrap["actions"] = {}
    normalized["bootstrap"] = bootstrap
    if not isinstance(normalized.get("save_slot_observations"), Mapping):
        normalized["save_slot_observations"] = {}
    cleanup = normalized.get("automap_cleanup")
    if not isinstance(cleanup, Mapping):
        cleanup = {}
    normalized["automap_cleanup"] = {
        str(key): value
        for key, value in cleanup.items()
        if (
            str(key)
            and isinstance(value, str)
            and MATERIALIZATION_EPOCH_PATTERN.fullmatch(value) is not None
        )
    }
    if "goal_sent" in normalized and not isinstance(normalized["goal_sent"], bool):
        normalized["goal_sent"] = False
    if "cultist_autosave_path" in normalized and not (
        normalized["cultist_autosave_path"] is None
        or isinstance(normalized["cultist_autosave_path"], str)
    ):
        normalized["cultist_autosave_path"] = None
    normalized["item_mapping_revision"] = _safe_nonnegative_int(
        normalized.get("item_mapping_revision"), 0
    )
    mapping_indices = normalized.get("mapping_repair_indices")
    if isinstance(mapping_indices, list):
        normalized["mapping_repair_indices"] = sorted(
            {
                entry
                for entry in mapping_indices
                if isinstance(entry, int) and not isinstance(entry, bool) and entry >= 0
            }
        )
    elif "mapping_repair_indices" in normalized:
        normalized["mapping_repair_indices"] = []
    if not isinstance(normalized.get("perk_reconciliation"), Mapping):
        normalized["perk_reconciliation"] = {"epoch": 0, "delivered": {}}
    else:
        reconciliation = copy.deepcopy(dict(normalized["perk_reconciliation"]))
        epoch = reconciliation.get("epoch", 0)
        reconciliation["epoch"] = _safe_nonnegative_int(epoch, 0)
        delivered = reconciliation.get("delivered")
        if isinstance(delivered, Mapping):
            reconciliation["delivered"] = {
                str(key): value
                for key, value in delivered.items()
                if str(key) and _safe_nonnegative_int(value, -1) >= 0
            }
        else:
            reconciliation["delivered"] = {}
        normalized["perk_reconciliation"] = reconciliation
    rune_reconciliation = normalized.get("rune_reconciliation")
    normalized["rune_reconciliation"] = (
        copy.deepcopy(dict(rune_reconciliation))
        if isinstance(rune_reconciliation, Mapping)
        else {}
    )
    groups = normalized.get("item_command_groups")
    if isinstance(groups, Mapping):
        safe_groups = {}
        for key, value in groups.items():
            if not isinstance(key, str) or not key or not isinstance(value, Mapping):
                continue
            item_id = value.get("item_id")
            next_command = value.get("next_command", 0)
            total_commands = value.get("total_commands", 0)
            if (
                isinstance(item_id, bool)
                or not isinstance(item_id, int)
                or _safe_nonnegative_int(next_command, -1) < 0
                or _safe_nonnegative_int(total_commands, -1) < 0
            ):
                continue
            next_command = int(next_command)
            total_commands = int(total_commands)
            if next_command > total_commands:
                continue
            safe_group = copy.deepcopy(dict(value))
            safe_group.update(
                item_id=item_id,
                next_command=next_command,
                total_commands=total_commands,
            )
            safe_groups[key] = safe_group
        normalized["item_command_groups"] = safe_groups
    elif "item_command_groups" in normalized:
        normalized.pop("item_command_groups")

    # Older prototypes used one of these names for the durable no-replay set.
    # Keep valid history and make malformed values inert rather than executable.
    for key in ("never_replay_history", "never_replay_items", "never_replayed", "never_replay"):
        if key not in normalized:
            continue
        value = normalized[key]
        if not isinstance(value, list):
            normalized[key] = copy.deepcopy(dict(value)) if isinstance(value, Mapping) else []
        else:
            normalized[key] = [
                entry
                for entry in value
                if (isinstance(entry, str) and entry)
                or (isinstance(entry, int) and not isinstance(entry, bool))
            ]
    return normalized


def default_session_state() -> dict[str, Any]:
    return normalize_session_state({})


def migrate_client_state(raw_state: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    """Migrate v1 state deterministically; reject malformed top-level state."""
    if not isinstance(raw_state, Mapping):
        raise ValueError("client state must be an object")
    version = raw_state.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError("unsupported client state format")
    if version not in (1, CLIENT_STATE_VERSION):
        raise ValueError("unsupported client state version")
    sessions = raw_state.get("sessions")
    if not isinstance(sessions, Mapping):
        raise ValueError("client state sessions must be an object")

    state = copy.deepcopy(dict(raw_state))
    state["version"] = CLIENT_STATE_VERSION
    state["sessions"] = {
        key: normalize_session_state(sessions[key])
        for key in sorted(sessions, key=lambda candidate: str(candidate))
        if isinstance(key, str) and key
    }
    return state, version == 1


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
        if not {"name", "policy"} <= set(entry) or set(entry) - {
            "name", "policy", "receipt_feedback"
        }:
            raise ValueError(
                f"item policy {item_id} must contain name, policy, and optional receipt_feedback"
            )
        name = entry["name"]
        policy = entry["policy"]
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"item policy {item_id} has invalid name")
        if policy not in SUPPORTED_POLICIES:
            raise ValueError(f"unsupported policy for item {item_id}: {policy!r}")
        receipt_feedback = entry.get("receipt_feedback", AP_RECEIPT_FEEDBACK)
        if receipt_feedback not in SUPPORTED_RECEIPT_FEEDBACK:
            raise ValueError(
                f"unsupported receipt feedback for item {item_id}: {receipt_feedback!r}"
            )
        parsed[item_id] = ReplayPolicy(item_id, name, policy, receipt_feedback)

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
    if (
        not isinstance(slot_identity, str)
        or not slot_identity
        or re.fullmatch(r"[A-Za-z0-9_.:-]+", slot_identity) is None
        or isinstance(epoch, bool)
        or not isinstance(epoch, int)
        or epoch < 0
    ):
        raise ValueError("reconciliation requires slot identity and non-negative epoch")
    if set(registry) != set(definitions):
        raise ValueError("reconciliation registry does not exactly cover active items")
    definition_map = dict(definitions)
    try:
        received_history = tuple(received_item_ids)
        if any(
            isinstance(item_id, bool) or not isinstance(item_id, int)
            for item_id in received_history
        ):
            raise ValueError("received item history contains a non-integer item ID")
        counts = Counter(received_history)
    except TypeError as error:
        raise ValueError("received item history is not hashable") from error
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
        if not isinstance(policy, ReplayPolicy) or policy.policy not in SUPPORTED_POLICIES:
            raise ValueError(f"unsupported policy for item {item_id}: {getattr(policy, 'policy', None)!r}")
        count = counts[item_id]
        selected_commands: list[str] = []
        if policy.policy == NEVER_REPLAY:
            skipped_never_replay += 1
        elif policy.policy == REPLAY_IDEMPOTENT:
            plan = compile_item_delivery_plan(item_id, definition_map)
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
            definition = definitions[item_id]
            if not isinstance(definition, Mapping) or definition.get("type") != "progressive_perk":
                raise ValueError(f"progressive item {item_id} has an invalid definition")
            perks = definition.get("perks")
            if not isinstance(perks, list) or not perks:
                raise ValueError(f"progressive item {item_id} has no configured stages")
            configured_stages = len(perks)
            for stage in range(min(count, configured_stages)):
                plan = compile_item_delivery_plan(item_id, definition_map, stage=stage)
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
