#!/usr/bin/env python3
"""Reject partially enabled Archipelago item-notification packages."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from tools.maps.notification_lab import (
    NOTIFICATION_LAB_CONTRACTS,
    NOTIFICATION_LAB_MAP,
    NOTIFICATION_LAB_PREFIX,
)

# Any entityDef in this namespace is a forbidden legacy receipt root.
RECEIPT_RE = re.compile(r"entityDef\s+ap_rpc_item_[^\s{]+")
NOTIFICATION_RE = re.compile(r"entityDef ap_notify_item_(\d+(?:_\d+)?) \{")
HEADER_RE = re.compile(r'header\s*=\s*"(#str_ap_notify_item_\d+(?:_\d+)?)";')
LAB_NOTIFICATION_RE = re.compile(r"entityDef (ap_notify_lab_[a-z_]+) \{")
LAB_HEADER_RE = re.compile(
    r'header\s*=\s*"(#str_ap_notification_lab_[a-z_]+)";'
)
STRING_TABLES = (
    Path("gameresources_patch1/EternalMod/strings/english.json"),
    Path("gameresources_patch1/EternalMod/strings/portuguese.json"),
)
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


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


def string_table_names(path: Path) -> set[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if set(data) != {"strings"}:
        raise AssertionError(f"string table root must contain only strings: {path}")
    strings = data["strings"]
    if not isinstance(strings, list):
        raise AssertionError(f"string table strings must be a list: {path}")
    names = set()
    for entry in strings:
        if not isinstance(entry, dict):
            raise AssertionError(f"string table entry must be a dict: {path}")
        if set(entry) != {"name", "text"}:
            raise AssertionError(f"string table entry keys must be name/text: {path}")
        name, text = entry["name"], entry["text"]
        if not isinstance(name, str) or not name.strip():
            raise AssertionError(f"string table name is empty: {path}")
        if not isinstance(text, str) or not text.strip():
            raise AssertionError(f"string table text is empty: {path}")
        if CONTROL_CHARACTERS.search(name) or CONTROL_CHARACTERS.search(text):
            raise AssertionError(f"string table contains control characters: {path}")
        if name in names:
            raise AssertionError(f"string table name is duplicated: {name}")
        names.add(name)
    return names


def validate(enabled: bool, maps_dir: Path, mod_root: Path, client_dir: Path, manifest_path: Path) -> None:
    maps = sorted(maps_dir.rglob("*.entities"))
    if not maps:
        raise AssertionError(f"no generated maps found: {maps_dir}")
    content = "\n".join(path.read_text(encoding="utf-8") for path in maps)
    receipts = set(RECEIPT_RE.findall(content))
    notifications = set(NOTIFICATION_RE.findall(content))
    headers = set(HEADER_RE.findall(content))
    lab_notifications = set(LAB_NOTIFICATION_RE.findall(content))
    lab_headers = set(LAB_HEADER_RE.findall(content))
    table_paths = tuple(mod_root / table for table in STRING_TABLES)

    if capability(client_dir / "bridge_identity.json") is not enabled:
        raise AssertionError("client identity notification capability diverges from build mode")
    if capability(manifest_path) is not enabled:
        raise AssertionError("release manifest notification capability diverges from build mode")
    bridge = (client_dir / "bridge_client.py").read_text(encoding="utf-8")
    if "bridge_identity.json" not in bridge or "receipt=ENABLE_ITEM_NOTIFICATIONS" not in bridge:
        raise AssertionError("packaged bridge lacks capability-gated receipt routing")

    if receipts:
        raise AssertionError("package contains forbidden ap_rpc_item receipt root")

    if not enabled:
        if notifications or headers or any(path.exists() for path in table_paths):
            raise AssertionError("disabled notifier build contains notification or string-table artifacts")
        return

    if not notifications:
        raise AssertionError("enabled notifier build lacks notification entities")
    expected_headers = {f"#str_ap_notify_item_{suffix}" for suffix in notifications}
    if headers != expected_headers:
        raise AssertionError("enabled notifier headers diverge from notification entities")
    expected_lab_notifications = {
        f"{NOTIFICATION_LAB_PREFIX}{contract['name']}"
        for contract in NOTIFICATION_LAB_CONTRACTS
    }
    expected_lab_headers = {
        f"#str_ap_notification_lab_{contract['name']}"
        for contract in NOTIFICATION_LAB_CONTRACTS
    }
    if lab_notifications and lab_notifications != expected_lab_notifications:
        raise AssertionError("notification lab entity set is incomplete")
    if bool(lab_notifications) != bool(lab_headers):
        raise AssertionError("notification lab entities and headers diverge")
    if lab_headers and lab_headers != expected_lab_headers:
        raise AssertionError("notification lab header set is incomplete")
    for path in maps:
        path_content = path.read_text(encoding="utf-8")
        if LAB_NOTIFICATION_RE.search(path_content) and path.stem != NOTIFICATION_LAB_MAP:
            raise AssertionError(f"notification lab entered the wrong map: {path}")
    if not all(path.is_file() for path in table_paths):
        raise AssertionError("enabled notifier build lacks English or Portuguese strings")
    locale_names = [string_table_names(path) for path in table_paths]
    expected_locale_names = headers | lab_headers
    if locale_names[0] != expected_locale_names:
        raise AssertionError("english.json keys diverge from generated notification headers")
    if locale_names[1] != expected_locale_names:
        raise AssertionError("portuguese.json keys diverge from generated notification headers")
    if locale_names[0] != locale_names[1]:
        raise AssertionError("English and Portuguese string keys diverge")

    required_notification_fields = (
        'class = "idTarget_Notification";',
        'notificationType = "HUD_NOTIFY_SECRET_FOUND";',
        'notificationHudEventID = "HUD_EVENT_PLAYER_NOTIFICATION_SECRET_FOUND";',
        'doNotShowDuplicate = false;',
        'rootWidget = "tier3centered";',
        'icon = "art/ui/dossier/icons/ico_secrets_off";',
        'notificationSound = "play_secret_encounter_found";',
        'noFlood = false;',
    )
    for suffix in notifications:
        notification = entity_block(content, f"ap_notify_item_{suffix}")
        if 'inherit = ' in notification:
            raise AssertionError(f"item notification must use direct HUD contract: {suffix}")
        if any(field not in notification for field in required_notification_fields):
            raise AssertionError(f"item notification HUD contract is incomplete: {suffix}")
        if any(field in notification for field in (
            'noFlood = true;', 'triggerOnce = true;', 'removeAfterActivation = true;',
            'disableAfterActivation = true;', 'startOff = true;',
        )):
            raise AssertionError(f"item notification is not reactivatable: {suffix}")
        entity_block(content, f"ap_rpc_v3_{suffix}")

    for name in lab_notifications:
        notification = entity_block(content, name)
        if 'class = "idTarget_Notification";' not in notification:
            raise AssertionError(f"notification lab entity has wrong class: {name}")
        if any(field in notification for field in (
            'class = "idTarget_Count";', 'triggerOnce = true;',
            'removeAfterActivation = true;', 'disableAfterActivation = true;',
            'noFlood = true;',
        )):
            raise AssertionError(f"notification lab entity is not reusable: {name}")


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
