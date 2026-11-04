"""Protect room locale bytes and remote-name presentation boundaries."""

import html
import json
from pathlib import Path

import pytest

from tools.maps.notification_formatting import (
    item_receipt_text,
    location_sent_text,
    major_notification_text,
    notification_text,
)
from tools.release.build_string_table import build_string_table

FIXTURES = Path(__file__).parent / "fixtures/notification_strings"


@pytest.fixture
def notification_room(tmp_path):
    items = {
        "1": {"type": "item"}, "2": {"type": "currency", "count": 5},
        "3": {"type": "item"}, "4": {"type": "item"}, "5": {"type": "no_op"},
        "7770017": {"type": "progressive_item", "perks": [1, 2]},
    }
    policies = {"items": {str(key): {"name": name} for key, name in (
        (1, "^1{red}Canhão <&>\x00\t"), (2, "Praetor Point"), (3, "Surprise"),
        (4, "Unassigned"), (7770017, "Progressive Dash"),
    )}}
    placements = [dict(
        location_id=location, location_name=f"Room - {location}", item_id=item,
        item_name=policies["items"][str(item)]["name"], recipient_slot=1 if local else 2,
        recipient_name="^2Sláyer <&>\x01", classification=4 if trap else 1, trap=trap, local=local,
    ) for location, item, local, trap in (
        (102, 1, True, False), (103, 2, False, False), (104, 3, False, True),
        (105, 4, False, False), (106, 4, True, False),
    )]
    keys = ["#str_ap_item_received", "#str_ap_location_sent", "#str_ap_location_101",
            "#str_ap_location_sent_102", "#str_ap_location_sent_103"]
    keys += [f"#str_ap_notify_item_{suffix}" for suffix in ("1", "2", "3", "4", "7770017_0", "7770017_1")]
    keys += [f"#str_ap_notify_item_received_{suffix}" for suffix in ("1", "3", "7770017_0", "7770017_1")]
    for filename, document in (
        ("items.json", items), ("policies.json", policies), ("placements.json", placements),
        ("locations.json", {"schema_version": 1, "locations": {"101": "Room - Entrada 日本語"}}),
    ):
        (tmp_path / filename).write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    maps = tmp_path / "maps"
    maps.mkdir()
    (maps / "room.entities").write_text("\n".join(f'header = "{key}";' for key in keys), encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("locale", ["english", "portuguese"])
def test_room_tables_match_reference_bytes(notification_room, locale):
    room = notification_room
    output = room / f"{locale}.json"
    build_string_table(room / "items.json", room / "policies.json", room / "maps", output,
                       room / "locations.json", room / "placements.json")
    assert output.read_bytes() == (FIXTURES / output.name).read_bytes()


@pytest.mark.parametrize("local", [True, False])
@pytest.mark.parametrize("uppercase,expected", [(True, "A TRAP FOR SOMEONE"), (False, "A trap for someone")])
def test_trap_receipts_hide_recipient_without_validating_its_name(local, uppercase, expected):
    assert item_receipt_text("^1Trap\x00", local=local, trap=True,
                             recipient_name="\x00", uppercase=uppercase) == expected


@pytest.mark.parametrize("uppercase,expected", [
    (True, "CANHÃO <&> FOR SLÁYER 日本語"), (False, "Canhão <&> FOR Sláyer 日本語"),
])
def test_remote_receipt_strips_controls_and_colors(uppercase, expected):
    assert item_receipt_text("^1{red}Canhão <&>\x00\t", local=False, trap=False,
                             recipient_name="^2Sláyer 日本語\x7f", uppercase=uppercase) == expected


@pytest.mark.parametrize("name", ["", "^1{red}\x00\t", 7])
def test_empty_or_invalid_notification_name_is_rejected(name):
    with pytest.raises(ValueError):
        notification_text(1, {"type": "item"}, name)


@pytest.mark.parametrize("locale", ["spanish", ""])
def test_unsupported_major_notification_locale_is_rejected(locale):
    with pytest.raises(ValueError, match="unsupported notification locale"):
        major_notification_text(1, {"type": "item"}, "Dash", locale=locale)


def test_local_receipt_preserves_case_and_ignores_unused_recipient():
    assert item_receipt_text("^1Canhão", local=True, trap=False,
                             recipient_name="", uppercase=False) == "YOUR Canhão"
    assert location_sent_text({"item_name": "^1Dash", "recipient_name": "Sláyer", "local": True}) == "FOUND YOUR DASH"


@pytest.mark.parametrize("segmented", [False, True])
def test_activity_html_escapes_remote_notification_names(segmented):
    from doom_eap.launcher.launcher_ui import LauncherUI

    text = item_receipt_text("<img src=x>&", local=False, trap=False, recipient_name='Slayer "<>&"')
    event = {"plain": text}
    if segmented:
        event.update(schema=1, segments=[{"type": "item", "text": text}])
    rendered = LauncherUI._archipelago_activity_detail(None, event)
    assert html.escape(text) in rendered
    assert "<IMG" not in rendered
