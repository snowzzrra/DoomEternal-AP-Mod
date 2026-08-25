#!/usr/bin/env python3
"""Reject partially enabled Archipelago item-notification packages."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from doom_eap.content.item_classification import (
    load_item_classification_identity,
    notification_style_for_item,
)
from tools.maps.notification_formatting import (
    ITEM_NOTIFICATION_HEADER_KEY,
    ITEM_NOTIFICATION_TITLE,
    LOCATION_NOTIFICATION_HEADER_KEY,
    LOCATION_NOTIFICATION_TITLE,
    major_notification_key_from_item_key,
)
from tools.release.release_manifest import load_release_manifest

# Receipt namespace recognized by package validation.
RECEIPT_RE = re.compile(r"entityDef\s+ap_rpc_item_[^\s{]+")
NOTIFICATION_RE = re.compile(
    r"entityDef ap_notify_item_((?:major|filler)_\d+(?:_\d+)?_[ab]) \{"
)
HEADER_RE = re.compile(r'header\s*=\s*"(#str_ap_(?:item_received|notify_item(?:_received)?_\d+(?:_\d+)?))";')
ITEM_KEY_RE = re.compile(r'(?:header|subtext)\s*=\s*"(#str_ap_(?:item_received|notify_item(?:_received)?_\d+(?:_\d+)?))";')
LOCATION_NOTIFICATION_RE = re.compile(
    r"entityDef ap_notify_location_(\d+) \{"
)
LOCATION_STRING_RE = re.compile(
    r'(?:header|subtext)\s*=\s*"(#str_ap_location_(?:sent(?:_\d+)?|\d+))";'
)
STRING_TABLES = (
    Path("gameresources_patch1/EternalMod/strings/english.json"),
    Path("gameresources_patch1/EternalMod/strings/portuguese.json"),
)
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")

MAJOR_NOTIFICATION_FIELDS = (
    'class = "idTarget_Notification";',
    'notificationType = "HUD_NOTIFY_SECRET_FOUND";',
    'notificationHudEventID = "HUD_EVENT_PLAYER_NOTIFICATION_SECRET_FOUND";',
    'priority = 4;',
    'doNotShowDuplicate = false;',
    'showDuringCombat = true;',
    'notificationTime = 2400;',
    'rootWidget = "tier3centered";',
    'icon = "art/ui/dossier/icons/ico_secrets_off";',
    'notificationSound = "play_secret_encounter_found";',
    'showCVar = "g_setting_notification_major";',
    'noFlood = false;',
)
CODEX_NOTIFICATION_FIELDS = (
    'class = "idTarget_Notification";',
    'notificationType = "HUD_NOTIFY_CODEX_RECIEVED";',
    'notificationHudEventID = "HUD_EVENT_PLAYER_NOTIFICATION_CODEX";',
    'notificationEndHudEventID = "HUD_EVENT_PLAYER_NOTIFICATION_CODEX_END";',
    'priority = 5;',
    'doNotShowDuplicate = false;',
    'rootWidget = "compact_notification";',
    'icon = "art/ui/icons/notifications/demons";',
    'notificationSound = "play_hud_lower";',
    'showCVar = "g_setting_notification_minor";',
    'noFlood = false;',
)


def expected_item_key(suffix: str) -> str:
    style, rpc_suffix = suffix.split("_", 1)
    rpc_suffix = rpc_suffix.rsplit("_", 1)[0]
    filler_key = f"#str_ap_notify_item_{rpc_suffix}"
    return (
        major_notification_key_from_item_key(filler_key)
        if style == "major"
        else filler_key
    )


def validate_item_notification_contract(
    notification: str, suffix: str, classification: int
) -> None:
    style, rpc_suffix = suffix.split("_", 1)
    rpc_suffix = rpc_suffix.rsplit("_", 1)[0]
    item_id = int(rpc_suffix.split("_", 1)[0])
    expected_style = notification_style_for_item(item_id, classification)
    if style != expected_style:
        raise AssertionError(
            f"item notification style diverges from classification: {suffix}"
        )

    required_fields = (
        MAJOR_NOTIFICATION_FIELDS if style == "major" else CODEX_NOTIFICATION_FIELDS
    )
    if any(field not in notification for field in required_fields):
        raise AssertionError(f"item notification HUD contract is incomplete: {suffix}")
    expected_subtext = f"#str_ap_notify_item_{rpc_suffix}"
    if style == "major":
        expected_header = expected_item_key(suffix)
        if f'header = "{expected_header}";' not in notification:
            raise AssertionError(f"major item notification header is incomplete: {suffix}")
        subtext_match = re.search(r'subtext\s*=\s*"([^"]*)";', notification)
        if subtext_match and subtext_match.group(1):
            raise AssertionError(f"major item notification has non-empty subtext: {suffix}")
    elif (
        f'header = "{ITEM_NOTIFICATION_HEADER_KEY}";' not in notification
        or f'subtext = "{expected_subtext}";' not in notification
    ):
        raise AssertionError(
            f"item notification title/subtitle contract is incomplete: {suffix}"
        )

    forbidden_contract = (
        (
            'notificationType = "HUD_NOTIFY_CODEX_RECIEVED";',
            'notificationHudEventID = "HUD_EVENT_PLAYER_NOTIFICATION_CODEX";',
            'notificationEndHudEventID = "HUD_EVENT_PLAYER_NOTIFICATION_CODEX_END";',
            'rootWidget = "compact_notification";',
            'icon = "art/ui/icons/notifications/demons";',
            'notificationSound = "play_hud_lower";',
            'showCVar = "g_setting_notification_minor";',
        )
        if style == "major"
        else (
            'notificationType = "HUD_NOTIFY_SECRET_FOUND";',
            'notificationHudEventID = "HUD_EVENT_PLAYER_NOTIFICATION_SECRET_FOUND";',
            'rootWidget = "tier3centered";',
            'icon = "art/ui/dossier/icons/ico_secrets_off";',
            'notificationSound = "play_secret_encounter_found";',
            'showCVar = "g_setting_notification_major";',
        )
    )
    if any(field in notification for field in forbidden_contract):
        raise AssertionError(f"item notification mixes HUD contracts: {suffix}")
    if any(field in notification for field in (
        'noFlood = true;', 'triggerOnce = true;', 'removeAfterActivation = true;',
        'disableAfterActivation = true;', 'startOff = true;',
    )):
        raise AssertionError(f"item notification is not reactivatable: {suffix}")


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


def string_table_values(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {entry["name"]: entry["text"] for entry in data["strings"]}


def validate(enabled: bool, maps_dir: Path, mod_root: Path, client_dir: Path, manifest_path: Path) -> None:
    maps = sorted(maps_dir.rglob("*.entities"))
    if not maps:
        raise AssertionError(f"no generated maps found: {maps_dir}")
    content = "\n".join(path.read_text(encoding="utf-8") for path in maps)
    receipts = set(RECEIPT_RE.findall(content))
    notifications = set(NOTIFICATION_RE.findall(content))
    headers = set(HEADER_RE.findall(content))
    item_keys = set(ITEM_KEY_RE.findall(content))
    location_notifications = set(LOCATION_NOTIFICATION_RE.findall(content))
    location_strings = set(LOCATION_STRING_RE.findall(content))
    table_paths = tuple(mod_root / table for table in STRING_TABLES)
    commands = json.loads(
        (client_dir / "data" / "items.json").read_text(encoding="utf-8")
    )
    classifications = load_item_classification_identity(
        client_dir / "data" / "item_classifications.json"
    )
    notification_classifications = classifications
    policies = json.loads(
        (client_dir / "data" / "item_replay_policies.json").read_text(
            encoding="utf-8"
        )
    ).get("items", {})
    all_commands = commands
    if {int(item_id) for item_id in commands} != set(classifications):
        raise AssertionError(
            "packaged item classifications do not cover item mapping"
        )

    if capability(client_dir / "bridge_identity.json") is not enabled:
        raise AssertionError("client identity notification capability diverges from build mode")
    load_release_manifest(manifest_path, package_root=manifest_path.parent)
    bridge = (client_dir / "bridge_client.py").read_text(encoding="utf-8")
    if "bridge_identity.json" not in bridge or "receipt=ENABLE_ITEM_NOTIFICATIONS" not in bridge:
        raise AssertionError("packaged bridge lacks capability-gated receipt routing")

    if receipts:
        raise AssertionError("package contains forbidden ap_rpc_item receipt root")

    if not enabled:
        if notifications or headers or item_keys:
            raise AssertionError(
                "disabled notifier build contains received-item artifacts"
            )

    if enabled and not notifications:
        raise AssertionError("enabled notifier build lacks notification entities")
    expected_notifications = set()
    native_only_item_ids = set()
    for raw_item_id, definition in all_commands.items():
        if isinstance(definition, dict) and definition.get("type") == "no_op":
            continue
        item_id = int(raw_item_id)
        feedback = policies.get(str(item_id), {}).get("receipt_feedback", "ap")
        if feedback == "native_only":
            native_only_item_ids.add(item_id)
            continue
        if feedback != "ap":
            raise AssertionError(
                f"unsupported packaged receipt feedback for item {item_id}: {feedback!r}"
            )
        classification = notification_classifications[item_id]
        if isinstance(classification, dict):
            classification = classification["classification"]
        style = notification_style_for_item(item_id, classification)
        stages = range(len(definition["perks"])) if (
            isinstance(definition, dict)
            and definition.get("type") == "progressive_perk"
        ) else (None,)
        for stage in stages:
            stage_suffix = f"_{stage}" if stage is not None else ""
            expected_notifications.update(
                f"{style}_{item_id}{stage_suffix}_{slot}" for slot in ("a", "b")
            )
    if enabled and notifications != expected_notifications:
        missing = sorted(expected_notifications - notifications)
        unexpected = sorted(notifications - expected_notifications)
        raise AssertionError(
            f"notification entities diverge from receipt feedback policy; "
            f"missing={missing}, unexpected={unexpected}"
        )
    if any(
        int(suffix.split("_", 2)[1]) in native_only_item_ids
        for suffix in notifications
    ):
        raise AssertionError("native-only item has notification entities")
    expected_headers = {ITEM_NOTIFICATION_HEADER_KEY} | {
        expected_item_key(suffix)
        for suffix in notifications
        if suffix.startswith("major_")
    }
    if headers != expected_headers:
        raise AssertionError("enabled notifier headers diverge from notification entities")
    expected_item_keys = expected_headers | {
        expected_item_key(suffix) for suffix in notifications
    }
    if item_keys != expected_item_keys:
        raise AssertionError("item notification title/subtitle keys diverge")
    if not location_notifications or not any(
        key == "#str_ap_location_sent" or key.startswith("#str_ap_location_sent_")
        for key in location_strings
    ):
        raise AssertionError("package lacks Codex location feedback")
    if not all(path.is_file() for path in table_paths):
        raise AssertionError("enabled notifier build lacks English or Portuguese strings")
    locale_names = [string_table_names(path) for path in table_paths]
    expected_locale_names = item_keys | location_strings
    if locale_names[0] != expected_locale_names:
        raise AssertionError("english.json keys diverge from generated notification headers")
    if locale_names[1] != expected_locale_names:
        raise AssertionError("portuguese.json keys diverge from generated notification headers")
    if locale_names[0] != locale_names[1]:
        raise AssertionError("English and Portuguese string keys diverge")
    locale_values = [string_table_values(path) for path in table_paths]
    for values, locale in zip(locale_values, ("english", "portuguese")):
        if values.get(ITEM_NOTIFICATION_HEADER_KEY) != ITEM_NOTIFICATION_TITLE[locale]:
            raise AssertionError("item notification title is not canonical")
        if (
            LOCATION_NOTIFICATION_HEADER_KEY in values
            and values.get(LOCATION_NOTIFICATION_HEADER_KEY) != LOCATION_NOTIFICATION_TITLE[locale]
        ):
            raise AssertionError("location notification title is not canonical")

    for suffix in notifications:
        notification = entity_block(content, f"ap_notify_item_{suffix}")
        if 'inherit = ' in notification:
            raise AssertionError(f"item notification must use direct HUD contract: {suffix}")
        _, rpc_suffix = suffix.split("_", 1)
        rpc_suffix = rpc_suffix.rsplit("_", 1)[0]
        item_id = int(rpc_suffix.split("_", 1)[0])
        classification = notification_classifications.get(item_id)
        if classification is None:
            raise AssertionError(
                f"item notification has no production classification: {suffix}"
            )
        validate_item_notification_contract(
            notification,
            suffix,
            classification["classification"] if isinstance(classification, dict) else classification,
        )
        entity_block(content, f"ap_rpc_v3_{rpc_suffix}")

    for style in ("major", "filler"):
        pairs = {
            suffix.rsplit("_", 1)[0]
            for suffix in notifications if suffix.startswith(f"{style}_")
        }
        for pair in pairs:
            if {f"{pair}_a", f"{pair}_b"} - notifications:
                raise AssertionError(f"item notification lacks A/B dedupe pool: {pair}")

    for location_id in location_notifications:
        notification = entity_block(
            content, f"ap_notify_location_{location_id}"
        )
        if any(field not in notification for field in CODEX_NOTIFICATION_FIELDS):
            raise AssertionError(
                f"location notification is not Codex: {location_id}"
            )
        if (
            f'header = "{LOCATION_NOTIFICATION_HEADER_KEY}";' not in notification
            or f'subtext = "#str_ap_location_{location_id}";' not in notification
        ):
            raise AssertionError(
                f"location notification title/subtitle contract is incomplete: {location_id}"
            )
        if "SECRET_FOUND" in notification or "secret_found" in notification:
            raise AssertionError(
                f"location notification retains Secret Found: {location_id}"
            )

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
