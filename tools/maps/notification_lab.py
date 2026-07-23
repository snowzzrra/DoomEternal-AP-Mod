"""Dev-only notification prototypes copied from resolved vanilla HUD contracts."""

from __future__ import annotations

import os
from collections.abc import Mapping

NOTIFICATION_LAB_ENV = "AP_NOTIFICATION_LAB"
NOTIFICATION_LAB_MAP = "e1m1_intro"
NOTIFICATION_LAB_PREFIX = "ap_notify_lab_"

LAB_STRINGS = {
    "english": {
        "#str_ap_notification_lab_current": "AP LAB: Current Notification",
        "#str_ap_notification_lab_inventory": "AP LAB: Inventory Acquired",
        "#str_ap_notification_lab_codex": "AP LAB: Codex Received",
        "#str_ap_notification_lab_collectible": "AP LAB: Collectible Acquired",
        "#str_ap_notification_lab_generic": "AP LAB: Generic Callout",
    },
    "portuguese": {
        "#str_ap_notification_lab_current": "LAB AP: Notificação atual",
        "#str_ap_notification_lab_inventory": "LAB AP: Inventário adquirido",
        "#str_ap_notification_lab_codex": "LAB AP: Códice recebido",
        "#str_ap_notification_lab_collectible": "LAB AP: Colecionável adquirido",
        "#str_ap_notification_lab_generic": "LAB AP: Chamada genérica",
    },
}

# These are resolved, internally consistent vanilla notification DECL contracts.
NOTIFICATION_LAB_CONTRACTS = (
    {
        "name": "current",
        "source": "hud/secret_found -> hud/tier3 -> hud/non_combat",
        "fields": (
            ("notificationType", '"HUD_NOTIFY_SECRET_FOUND"'),
            ("notificationHudEventID", '"HUD_EVENT_PLAYER_NOTIFICATION_SECRET_FOUND"'),
            ("priority", "4"),
            ("doNotShowDuplicate", "false"),
            ("showDuringCombat", "true"),
            ("notificationTime", "2400"),
            ("rootWidget", '"tier3centered"'),
            ("icon", '"art/ui/dossier/icons/ico_secrets_off"'),
            ("header", '"#str_ap_notification_lab_current"'),
            ("subtext", '""'),
            ("notificationSound", '"play_secret_encounter_found"'),
            ("showCVar", '"g_setting_notification_major"'),
        ),
    },
    {
        "name": "inventory",
        "source": "hud/weapon_acquired -> hud/tier1 -> hud/non_combat",
        "fields": (
            ("notificationType", '"HUD_NOTIFY_INVENTORY_ACQUIRED"'),
            ("notificationHudEventID", '"HUD_EVENT_PLAYER_NOTIFICATION"'),
            ("priority", "3"),
            ("doNotShowDuplicate", "true"),
            ("showDuringCombat", "true"),
            ("notificationTime", "2800"),
            ("rootWidget", '"weapon"'),
            ("icon", '"art/ui/weapon/har"'),
            ("header", '"#str_ap_notification_lab_inventory"'),
            ("subtext", '""'),
            ("notificationSound", '"play_ui_notification_large"'),
            ("showCVar", '"g_setting_notification_major"'),
            ("desiredDossierPage", '"DOSSIER_PAGE_CODEX"'),
            ("ignoreIfPlayerHasPrimaryItem", "true"),
            ("dossierCTAText", '"#str_swf_not_codex_press_to_view_GHOST58900"'),
        ),
    },
    {
        "name": "codex",
        "source": "hud/codex",
        "fields": (
            ("hudLocation", '"HUD_LOC_LEFT"'),
            ("notificationType", '"HUD_NOTIFY_CODEX_RECIEVED"'),
            ("notificationHudEventID", '"HUD_EVENT_PLAYER_NOTIFICATION_CODEX"'),
            ("notificationEndHudEventID", '"HUD_EVENT_PLAYER_NOTIFICATION_CODEX_END"'),
            ("desiredDossierPage", '"DOSSIER_PAGE_CODEX"'),
            ("priority", "5"),
            ("doNotShowDuplicate", "true"),
            ("rootWidget", '"compact_notification"'),
            ("icon", '"art/ui/icons/notifications/demons"'),
            ("header", '"#str_ap_notification_lab_codex"'),
            ("subtext", '""'),
            ("notificationSound", '"play_hud_lower"'),
            ("showCVar", '"g_setting_notification_minor"'),
        ),
    },
    {
        "name": "collectible",
        "source": "hud/collectible_acquired",
        "fields": (
            ("hudLocation", '"HUD_LOC_LEFT"'),
            ("notificationType", '"HUD_NOTIFY_COLLECTIBLE_ACQUIRED"'),
            (
                "notificationHudEventID",
                '"HUD_EVENT_PLAYER_NOTIFICATION_COLLECTIBLE_ACQUIRED"',
            ),
            ("priority", "3"),
            ("rootWidget", '"compact_notification"'),
            ("icon", '"art/ui/dossier/icons/ico_collectible_on"'),
            ("header", '"#str_ap_notification_lab_collectible"'),
            ("subtext", '""'),
            ("notificationSound", '"play_ui_notification_collectible"'),
            ("showCVar", '"g_setting_notification_major"'),
        ),
    },
    {
        "name": "generic",
        "source": "hud/callouts/generic_callout",
        "fields": (
            ("notificationType", '"HUD_NOTIFY_GENERIC_CALLOUT"'),
            ("notificationHudEventID", '"HUD_EVENT_PLAYER_NOTIFICATION_CALLOUT"'),
            ("priority", "4"),
            ("showImmediately", "true"),
            ("notificationTime", "5000"),
            ("goesInList", "true"),
            ("isPvPEnabled", "true"),
            ("header", '"#str_ap_notification_lab_generic"'),
            ("subtext", '""'),
        ),
    },
)


def notification_lab_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return true only for the explicit dev-only opt-in value."""
    values = os.environ if environ is None else environ
    return values.get(NOTIFICATION_LAB_ENV) == "1"


def notification_lab_string_entries(locale: str) -> list[tuple[str, str]]:
    """Return localized lab strings only while the dev-only flag is active."""
    try:
        strings = LAB_STRINGS[locale]
    except KeyError as error:
        raise ValueError(f"unsupported notification lab locale: {locale}") from error
    return sorted(strings.items())


def _render_notification(contract: dict[str, object]) -> str:
    field_lines = "\n".join(
        f"\t\t\t{field} = {value};" for field, value in contract["fields"]
    )
    return f"""entity {{
\tentityDef {NOTIFICATION_LAB_PREFIX}{contract["name"]} {{
\t\tclass = "idTarget_Notification";
\t\texpandInheritance = false;
\t\tpoolCount = 0;
\t\tpoolGranularity = 2;
\t\tnetworkReplicated = false;
\t\tdisableAIPooling = false;
\t\tedit = {{
{field_lines}
\t\t}}
\t}}
}}
"""


def generate_notification_lab(map_key: str, *, enabled: bool | None = None) -> str:
    """Generate exactly five reusable prototypes on the selected safe map."""
    active = notification_lab_enabled() if enabled is None else enabled
    if not active or map_key != NOTIFICATION_LAB_MAP:
        return ""
    return "\n".join(_render_notification(contract) for contract in NOTIFICATION_LAB_CONTRACTS)
