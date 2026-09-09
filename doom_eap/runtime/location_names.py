"""Resolve current CommonClient placement/name data for the launcher boundary."""
from doom_eap.content.item_classification import ITEM_CLASSIFICATION_TRAP

def resolve_placement_records(location_ids, network_items, slot_info_by_id, names, local_slot):
    records = []
    for location_id in sorted(location_ids):
        network_item = network_items[location_id]
        recipient_slot = network_item.player
        slot_info = slot_info_by_id.get(recipient_slot)
        if slot_info is None:
            raise ValueError(f"recipient slot is unknown: {recipient_slot}")
        location_name = names.location_names.lookup_in_slot(location_id, local_slot)
        item_name = names.item_names.lookup_in_slot(network_item.item, recipient_slot)
        if location_name.startswith("Unknown ") or item_name.startswith("Unknown "):
            raise ValueError(
                f"DataPackage lacks name for location {location_id} or item {network_item.item}"
            )
        classification = network_item.flags
        if not isinstance(classification, int) or isinstance(classification, bool):
            raise ValueError(f"invalid item classification at location {location_id}")
        if classification < 0:
            raise ValueError(f"invalid item classification at location {location_id}")
        local = recipient_slot == local_slot
        recipient_name = names.player_names.get(recipient_slot, str(recipient_slot))
        if not isinstance(recipient_name, str) or not recipient_name.strip():
            raise ValueError(f"recipient name is unavailable: {recipient_slot}")
        records.append({
            "location_id": location_id,
            "location_name": location_name,
            "item_id": network_item.item,
            "item_name": item_name,
            "recipient_slot": recipient_slot,
            "recipient_name": recipient_name,
            "classification": classification,
            "trap": bool(classification & ITEM_CLASSIFICATION_TRAP),
            "local": local,
        })
    return records
