"""Minimal Archipelago world registry for frozen headless client runtime.

Server supplies DOOM Eternal data package after connection. Bundling every
Archipelago world would add unrelated generators and dependencies.
"""

from __future__ import annotations


network_data_package: dict[str, object] = {
    "version": 0,
    "games": {
        "DOOM Eternal": {
            "checksum": "standalone-server-supplied",
            "item_name_to_id": {},
            "location_name_to_id": {},
        }
    },
}


class AutoWorldRegister:
    """Compatibility surface imported by Archipelago CommonClient."""

    world_types: dict[str, object] = {}
