import html

import pytest

from tools.maps.notification_formatting import (
    item_receipt_text,
    major_notification_text,
    notification_text,
)


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


@pytest.mark.parametrize("segmented", [False, True])
def test_activity_html_escapes_remote_notification_names(segmented):
    from doom_eap.launcher.launcher_ui import LauncherUI

    text = item_receipt_text("<img src=x>&", local=False, trap=False, recipient_name='Slayer "<>&"')
    event = {"plain": text}
    if segmented:
        event.update(schema=1, segments=[{"type": "item", "text": text}])
    class View:
        _activity_segment_color = LauncherUI._activity_segment_color

    rendered = LauncherUI._archipelago_activity_detail(View(), event)
    assert html.escape(text) in rendered
    assert "<IMG" not in rendered
