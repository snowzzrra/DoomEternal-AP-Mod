"""Real bridge subprocess with filesystem barriers; AP and console effects stay external."""

import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]


def signal(event, **fields):
    print("DELIVERY\t" + json.dumps({"event": event, **fields}), flush=True)


def session(path):
    if not path.exists():
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    return next(iter(document["sessions"].values()), {})


def transition(source, target):
    if target.suffix == ".cmd":
        return "publication"
    if target.name != "state.json":
        return None
    old, new = session(target), session(source)
    if new.get("processed_items", 0) > old.get("processed_items", 0):
        return "receipt_cursor"
    groups = new.get("item_command_groups", {})
    if groups and groups != old.get("item_command_groups", {}):
        return "group_progress"
    if old.get("item_command_groups") and not groups:
        return "group_completion"
    return None


def install_barrier(target_event):
    def pause(event):
        if event == target_event:
            signal(event)
            if not sys.stdin.readline():
                raise EOFError(event)

    def wrap(operation):
        def transfer(source, destination, *args, **kwargs):
            point = transition(Path(source), Path(destination))
            pause(f"{point}:before")
            result = operation(source, destination, *args, **kwargs)
            pause(f"{point}:after")
            return result
        return transfer

    os.replace = wrap(os.replace)
    os.rename = wrap(os.rename)
    os.link = wrap(os.link)


def bind(context, slot_data, seed):
    from doom_eap.runtime.bridge_client import (
        publish_materialization_lease, validate_slot_contract,
    )

    context.room_seed_name = seed
    context.seed_name = seed
    context.team, context.slot, context.auth = 0, 1, "DeliveryFixture"
    context._connected_slot_data = validate_slot_contract(slot_data)
    context.initialize_item_state(slot_data)
    context.receipt_session.configure_starting_ownership(slot_data)
    context.ammo.bind(context.state_key)
    context.runtime_lifecycle.accept_marker({
        "runtime_map": "game/sp/e1m1_intro/e1m1_intro", "map_key": "e1m1_intro",
        "gameplay_epoch": "1:1", "mtime_ns": 1,
    }, 1)
    published = publish_materialization_lease("1:1")
    context.runtime_lifecycle.record_lease_publication("1:1", published)


def snapshot(context):
    return {
        "processed": context.items_processed, "received": len(context.items_received),
        "ready": context.item_state_ready, "state_key": context.state_key,
        "blocked": bool(getattr(context, "item_delivery_blocked", False)),
        "ammo_pending": context.ammo.pending, "ammo_available": context.ammo.available,
    }


async def act(context, request, slot_data):
    from doom_eap.runtime.ammo_refill import AmmoCommandScope

    action = request["action"]
    if action == "collect":
        context.watcher_event.clear()
        signal("awaiting_packet")
        await context.watcher_event.wait()
        await context.process_pending_item_receipts("packet")
    elif action == "ammo":
        # The public ammo owner accepts readiness without discovering a real game process.
        scope = AmmoCommandScope(context.state_key, context.current_map_name, "GAME-AUTOSAVE0", False)
        await context.ammo.request(scope, (None, {}))
    elif action == "background":
        from doom_eap.runtime.bridge_client import send_command
        if not send_command("give health", coalesce_key="background", state_key=context.state_key, arm_rpc=False):
            raise RuntimeError("Background command was refused")
    elif action == "switch":
        bind(context, slot_data, "DeliveryRoomB")
    elif action == "persist":
        context.persist_session_state()
    elif action == "reconcile":
        await context.process_pending_item_receipts("tracker")
    else:
        raise ValueError(action)
    from doom_eap.runtime.bridge_client import ledger_shadow
    await ledger_shadow.flush()
    signal("done", **snapshot(context))


async def main():
    import Utils

    # CommonClient otherwise stores optional settings beside the AP source checkout.
    Utils.user_path.cached_path = os.getcwd()
    from doom_eap.runtime import bridge_client as bridge

    bridge.start_bridge_logger()
    slot_data = json.loads((ROOT / "tests/fixtures/resolved_room_slot_data.json").read_text())
    context = bridge.DoomEternalContext(None, None)
    bind(context, slot_data, "DeliveryRoomA")
    install_barrier(sys.argv[2])
    context.server_task = asyncio.create_task(bridge.server_loop(context, sys.argv[1]))
    await bridge.ledger_shadow.flush()
    signal("ready", **snapshot(context))
    while line := await asyncio.to_thread(sys.stdin.readline):
        await act(context, json.loads(line), slot_data)


if __name__ == "__main__":
    asyncio.run(main())
