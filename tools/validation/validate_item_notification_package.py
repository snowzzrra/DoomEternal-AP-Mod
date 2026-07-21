#!/usr/bin/env python3
"""Reject partially enabled Archipelago item-notification packages."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


RECEIPT_RE = re.compile(r"entityDef ap_rpc_item_(\d+(?:_\d+)?) \{")
NOTIFICATION_RE = re.compile(r"entityDef ap_notify_item_(\d+(?:_\d+)?) \{")
HEADER_RE = re.compile(r'header\s*=\s*"(#str_ap_notify_item_\d+(?:_\d+)?)";')
STRING_TABLE = Path("gameresources_patch1/EternalMod/strings/english.json")


def entity_block(content: str, entity_name: str) -> str:
    marker = f"entityDef {entity_name} {{"
    start = content.find(marker)
    if start < 0:
        raise AssertionError(f"missing entity: {entity_name}")
    open_brace = content.find("{", start)
    depth = 0
    for index in range(open_brace, len(content)):
        if content[index] == "{":
            depth += 1
        elif content[index] == "}":
            depth -= 1
            if depth == 0:
                return content[start:index + 1]
    raise AssertionError(f"unterminated entity: {entity_name}")


def capability(path: Path) -> bool:
    data = json.loads(path.read_text(encoding="utf-8"))
    value = data.get("item_notifications", {}).get("enabled")
    if not isinstance(value, bool):
        raise AssertionError(f"item_notifications.enabled must be boolean: {path}")
    return value


def validate(enabled: bool, maps_dir: Path, mod_root: Path, client_dir: Path, manifest_path: Path) -> None:
    maps = sorted(maps_dir.rglob("*.entities"))
    if not maps:
        raise AssertionError(f"no generated maps found: {maps_dir}")
    content = "\n".join(path.read_text(encoding="utf-8") for path in maps)
    receipts = set(RECEIPT_RE.findall(content))
    notifications = set(NOTIFICATION_RE.findall(content))
    headers = set(HEADER_RE.findall(content))
    table_path = mod_root / STRING_TABLE

    if capability(client_dir / "bridge_identity.json") is not enabled:
        raise AssertionError("client identity notification capability diverges from build mode")
    if capability(manifest_path) is not enabled:
        raise AssertionError("release manifest notification capability diverges from build mode")
    bridge = (client_dir / "bridge_client.py").read_text(encoding="utf-8")
    if "bridge_identity.json" not in bridge or "receipt=ENABLE_ITEM_NOTIFICATIONS" not in bridge:
        raise AssertionError("packaged bridge lacks capability-gated receipt routing")

    if not enabled:
        if receipts or notifications or headers or table_path.exists():
            raise AssertionError("disabled notifier build contains receipt, notification, or string-table artifacts")
        return

    if not receipts or receipts != notifications:
        raise AssertionError("enabled notifier receipt and notification entities diverge")
    expected_headers = {f"#str_ap_notify_item_{suffix}" for suffix in notifications}
    if headers != expected_headers:
        raise AssertionError("enabled notifier headers diverge from notification entities")
    if not table_path.is_file():
        raise AssertionError("enabled notifier build lacks english.json")
    strings = json.loads(table_path.read_text(encoding="utf-8")).get("strings")
    if not isinstance(strings, dict) or set(strings) != headers:
        raise AssertionError("english.json keys diverge from generated notification headers")

    required_notification_fields = (
        'class = "idTarget_Notification";',
        'notificationType = "HUD_NOTIFY_SECRET_FOUND";',
        'notificationHudEventID = "HUD_EVENT_PLAYER_NOTIFICATION_SECRET_FOUND";',
        'doNotShowDuplicate = false;',
        'rootWidget = "tier3centered";',
        'icon = "art/ui/dossier/icons/ico_secrets_off";',
        'notificationSound = "play_secret_encounter_found";',
        'noFlood = true;',
    )
    for suffix in notifications:
        notification = entity_block(content, f"ap_notify_item_{suffix}")
        if 'inherit = ' in notification:
            raise AssertionError(f"item notification must use direct HUD contract: {suffix}")
        if any(field not in notification for field in required_notification_fields):
            raise AssertionError(f"item notification HUD contract is incomplete: {suffix}")
        receipt = entity_block(content, f"ap_rpc_item_{suffix}")
        if f'item[0] = "ap_rpc_v3_{suffix}";' not in receipt:
            raise AssertionError(f"receipt first target is not the silent effect: {suffix}")
        if f'item[1] = "ap_notify_item_{suffix}";' not in receipt:
            raise AssertionError(f"receipt second target is not the notification: {suffix}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enabled", required=True, choices=("0", "1"))
    parser.add_argument("--maps-dir", required=True, type=Path)
    parser.add_argument("--mod-root", required=True, type=Path)
    parser.add_argument("--client-dir", required=True, type=Path)
    parser.add_argument("--release-manifest", required=True, type=Path)
    args = parser.parse_args()
    validate(args.enabled == "1", args.maps_dir, args.mod_root, args.client_dir, args.release_manifest)


if __name__ == "__main__":
    main()
