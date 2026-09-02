"""Bounded per-owned Rune state reconciliation planning."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from doom_eap.runtime.item_reconciliation import ReconciliationCommand

_RUNE_COMMAND = re.compile(
    r"ai_ScriptCmdEnt player1 givePlayerPerk (?P<perk>perk/player/runes/[A-Za-z0-9_./-]+)"
)


def _clean_decl(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().strip('"')
    if not cleaned or cleaned.lower() in {"none", "null", "<none>"}:
        return None
    return cleaned


def _field_values(details: Mapping[str, Any], prefix: str) -> frozenset[str]:
    values = set()
    for key, value in details.items():
        if not isinstance(key, str):
            continue
        if key == prefix or re.fullmatch(re.escape(prefix) + r"(?:_|\[)?\d+\]?", key):
            cleaned = _clean_decl(value)
            if cleaned is not None:
                values.add(cleaned)
    return frozenset(values)


def _slot_value(details: Mapping[str, Any], index: int) -> str | None:
    for key in (f"runeSlotName_{index}", f"runeSlotName[{index}]"):
        if key in details:
            return _clean_decl(details[key])
    return None


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return None


@dataclass(frozen=True)
class RuneNativeState:
    available_perks: frozenset[str]
    active_perks: frozenset[str]
    registered_runes: frozenset[str]
    equipped_slots: tuple[str | None, str | None, str | None]
    page_unlocked: bool | None
    save_slot: str
    evidence_epoch: int | str
    source_path: str | None = None
    source_mtime_ns: int | None = None

    @property
    def observed(self) -> bool:
        return bool(
            self.available_perks
            or self.active_perks
            or self.registered_runes
            or any(self.equipped_slots)
            or self.page_unlocked is not None
        )

    @classmethod
    def from_game_details(
        cls,
        details: Mapping[str, Any] | None,
        *,
        save_slot: str,
        evidence_epoch: int | str,
    ) -> "RuneNativeState":
        source = details if isinstance(details, Mapping) else {}
        return cls(
            available_perks=_field_values(source, "availablePerkDeclName"),
            active_perks=_field_values(source, "activePerkDeclName"),
            registered_runes=_field_values(source, "runeName"),
            equipped_slots=(
                _slot_value(source, 0),
                _slot_value(source, 1),
                _slot_value(source, 2),
            ),
            page_unlocked=_optional_bool(source.get("STAT_RUNE_PAGE_UNLOCKED")),
            save_slot=save_slot,
            evidence_epoch=evidence_epoch,
            source_path=_clean_decl(source.get("_path")),
            source_mtime_ns=(
                source.get("_mtime_ns")
                if isinstance(source.get("_mtime_ns"), int)
                and not isinstance(source.get("_mtime_ns"), bool)
                else None
            ),
        )


@dataclass(frozen=True)
class RunePlanEntry:
    item_id: int
    perk: str
    disposition: str
    reason: str


@dataclass(frozen=True)
class RuneReconciliationPlan:
    entries: tuple[RunePlanEntry, ...]
    fingerprint: str
    status: str
    commands: tuple[Any, ...] = ()

    @property
    def repairs(self) -> tuple[RunePlanEntry, ...]:
        return tuple(entry for entry in self.entries if entry.disposition == "repair_candidate")

    @property
    def noops(self) -> tuple[RunePlanEntry, ...]:
        return tuple(entry for entry in self.entries if entry.disposition == "noop")


def compile_rune_reconciliation_commands(
    plan: RuneReconciliationPlan,
    slot_identity: str,
    epoch: int | str,
) -> tuple[Any, ...]:
    """Compile one existing generated effect activation per repair candidate."""
    if not isinstance(slot_identity, str) or not slot_identity:
        raise ValueError("Rune reconciliation requires slot identity")
    if isinstance(epoch, bool) or not isinstance(epoch, (int, str)):
        raise ValueError("Rune reconciliation requires an evidence epoch")
    return tuple(
        ReconciliationCommand(
            item_id=entry.item_id,
            name=entry.perk.rsplit("/", 1)[-1],
            policy="rune_visual_repair",
            stage=0,
            spool_id=f"rune-reconcile-{slot_identity}-e{epoch}-item{entry.item_id}",
            command=f"ai_ScriptCmdEnt ap_rpc_v3_{entry.item_id} activate",
            description="reapply one AP-owned Rune perk without changing equipped slots",
        )
        for entry in plan.repairs
    )


def with_rune_reconciliation_commands(
    plan: RuneReconciliationPlan,
    slot_identity: str,
    epoch: int | str,
) -> RuneReconciliationPlan:
    return RuneReconciliationPlan(
        entries=plan.entries,
        fingerprint=plan.fingerprint,
        status=plan.status,
        commands=compile_rune_reconciliation_commands(plan, slot_identity, epoch),
    )


def rune_plan_already_recorded(
    state: Mapping[str, Any], plan: RuneReconciliationPlan
) -> bool:
    return (
        state.get("fingerprint") == plan.fingerprint
        and state.get("status") == plan.status
    )


def rune_item_perk_mapping(
    definitions: Mapping[int, Any], rune_item_ids: Iterable[int]
) -> dict[int, str]:
    mapping: dict[int, str] = {}
    for item_id in sorted(set(rune_item_ids)):
        definition = definitions.get(item_id)
        if not isinstance(definition, str):
            raise ValueError(f"Rune item {item_id} does not have a direct perk command")
        match = _RUNE_COMMAND.fullmatch(definition.strip())
        if match is None:
            raise ValueError(f"Rune item {item_id} has an unsupported perk command")
        mapping[item_id] = match.group("perk")
    return mapping


def compile_rune_reconciliation_plan(
    ap_owned_item_ids: Iterable[int],
    native: RuneNativeState,
    mapping: Mapping[int, str],
    *,
    expected_rune_item_ids: Iterable[int] | None = None,
) -> RuneReconciliationPlan:
    owned = set(ap_owned_item_ids)
    expected = set(mapping) if expected_rune_item_ids is None else set(expected_rune_item_ids)
    unknown = sorted((owned & expected) - set(mapping))
    if unknown:
        raise ValueError("AP-owned Rune has no mapping: " + ", ".join(map(str, unknown)))
    if any(isinstance(item_id, bool) or not isinstance(item_id, int) for item_id in owned):
        raise ValueError("AP Rune ownership contains a non-integer item ID")

    entries: list[RunePlanEntry] = []
    for item_id in sorted(owned & expected):
        perk = mapping[item_id]
        available = perk in native.available_perks
        active = perk in native.active_perks
        registered = perk in native.registered_runes
        equipped = perk in native.equipped_slots
        if registered and available:
            reason = "registered_and_available"
            if equipped:
                reason = "equipped_player_choice_preserved"
            entries.append(RunePlanEntry(item_id, perk, "noop", reason))
            continue
        if not native.observed:
            reason = "native_snapshot_unavailable"
        elif not registered and (available or active or equipped):
            reason = "manager_registration_missing"
        elif registered:
            reason = "perk_availability_missing"
        else:
            reason = "perk_and_manager_registration_missing"
        entries.append(RunePlanEntry(item_id, perk, "repair_candidate", reason))

    payload = {
        "owned": sorted(owned & expected),
        "available": sorted(native.available_perks),
        "active": sorted(native.active_perks),
        "registered": sorted(native.registered_runes),
        "slots": list(native.equipped_slots),
        "page": native.page_unlocked,
        "save_slot": native.save_slot,
        "evidence_epoch": native.evidence_epoch,
    }
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    status = "repair_needed" if any(
        entry.disposition == "repair_candidate" for entry in entries
    ) else "noop"
    return RuneReconciliationPlan(tuple(entries), fingerprint, status)
