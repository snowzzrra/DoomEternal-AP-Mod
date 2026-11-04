"""Validate launcher resolution controls and route them to the delivery coordinator."""

from dataclasses import asdict
from uuid import UUID

from .ledger_records import Conflict, LedgerError, ResolutionAction, RoomKey
from .native_transport import TransportError


def control_room(value) -> RoomKey:
    if not isinstance(value, dict) or value.keys() != {"seed", "team", "slot", "generation_fp"}:
        raise ValueError("invalid delivery room")
    if not all(isinstance(value[key], str) and value[key] for key in ("seed", "generation_fp")):
        raise ValueError("invalid delivery room identity")
    if not all(type(value[key]) is int and value[key] >= 0 for key in ("team", "slot")):
        raise ValueError("invalid delivery player")
    return RoomKey(**value)


async def handle_delivery_control(coordinator, control, emit):
    kind = control.get("type")
    if kind not in {"delivery_capabilities", "item_resolution"}:
        return False
    request_id = control.get("request_id")
    room = None
    try:
        room = control_room(control.get("room"))
        if not isinstance(request_id, str):
            raise ValueError("invalid delivery request")
        UUID(request_id)
    except (ValueError, TypeError):
        return True
    try:
        replacement = await _execute(coordinator, room, control)
    except (LedgerError, TransportError, ValueError, TypeError, OSError, TimeoutError):
        ok = False
        replacement = None
    else:
        ok = True
    emit(
        "item_resolution_result" if kind == "item_resolution" else "accounting_capability_changed",
        request_id=request_id,
        room=asdict(room) if room else None,
        ok=ok,
        can_resolve=ok if kind == "delivery_capabilities" else False,
        can_spend=ok and getattr(coordinator, "can_spend", False),
        command_id=control.get("command_id"),
        expected_revision=control.get("expected_revision"),
        replacement_id=replacement,
    )
    return True


async def _execute(coordinator, room, control):
    if coordinator is None or coordinator.binding is None or room != coordinator.binding.room:
        raise Conflict("Delivery room is not active")
    await coordinator.require_owner()
    if control["type"] == "item_resolution":
        return await _resolve(coordinator, control)


async def _resolve(coordinator, control):
    command = control.get("command_id")
    revision = control.get("expected_revision")
    if not isinstance(command, str) or not command or type(revision) is not int or revision < 0:
        raise ValueError("invalid delivery resolution target")
    if command.startswith("ammo-operation:"):
        if ResolutionAction(control.get("action")) is not ResolutionAction.TRY_AGAIN:
            raise ValueError("invalid ammo recovery action")
        if not await coordinator.accounting.retry(command.removeprefix("ammo-operation:"), revision):
            raise Conflict("Ammo recovery is waiting for server confirmation")
        return None
    return await coordinator.resolve(command, revision, ResolutionAction(control.get("action")))
