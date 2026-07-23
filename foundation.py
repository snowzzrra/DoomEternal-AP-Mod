"""Small evidence registry and the canonical AP item delivery compiler."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from item_classification import notification_entity_name
from map_registry import load_map_registry, release_plan

VALID_STATUSES = {
    "runtime_verified",
    "runtime_verified_with_map_exception",
    "static_evidence_only",
    "experimental",
    "rejected",
}
PRIMITIVE_REGISTRY: dict[str, Any] = {
    "allowed_statuses": sorted(VALID_STATUSES),
    "primitives": {
        "target_command": {
            "family": "map_side_command", "status": "runtime_verified",
            "source": {"map": "game/sp/e1m1_intro/e1m1_intro", "container": "e1m1_intro_patch3.resources", "file": "vanillamaps/e1m1_intro.map", "entity": "AP map-side command runtime evidence", "source_sha256": "5d8d1a6c6a377a77e5c8246c5eaf5034a1f4f917e82621645bf70e143b43d4a6"},
            "shape": {"class": "idTarget_Command", "inherit": None, "required_fields": ["commandText"], "forbidden_fields": ["targets", "currencyList", "gameStat"]},
            "targets": [], "runtime_verified_maps": ["e1m1_intro", "e1m2_war", "hub", "e1m3_cult"], "allowed_in_release": True, "frozen": True,
        },
        "target_count_relay": {
            "family": "relay", "status": "runtime_verified",
            "source": {"map": "game/sp/e1m1_intro/e1m1_intro", "container": "e1m1_intro_patch3.resources", "file": "vanillamaps/e1m1_intro.map", "entity": "master_level_target_relay_barge_arena_door_close", "source_sha256": "5d8d1a6c6a377a77e5c8246c5eaf5034a1f4f917e82621645bf70e143b43d4a6"},
            "shape": {"class": "idTarget_Count", "inherit": "target/relay", "required_fields": ["count", "targets"], "forbidden_fields": ["commandText", "currencyList", "gameStat"]},
            "targets": ["parameterized"], "runtime_verified_maps": ["e1m1_intro", "e1m2_war", "hub", "e1m3_cult"], "allowed_in_release": True, "frozen": True,
        },
        "currency_grant_direct": {
            "family": "currency", "status": "runtime_verified",
            "source": {"map": "game/sp/e1m2_battle/e1m2_battle", "container": "e1m2_battle_patch3.resources", "file": "vanillamaps/e1m2_war.map", "entity": "tutorial_target_give_item_weapon_points; direct AP topology runtime-PASS", "source_sha256": "c83069ae8c2094ec4aee99ed331f1c30568e6b71cda12b81ce7e5933837dfe65"},
            "shape": {"class": "idTarget_GiveItems", "inherit": None, "required_fields": ["itemList", "addUpToCount", "whenToSave", "saveType", "currencyList"], "forbidden_fields": ["targets", "commandText", "gameStat"]},
            "targets": [], "runtime_verified_maps": ["e1m1_intro", "e1m2_war", "hub", "e1m3_cult"], "repeatability": "repeatable_runtime_proven", "allowed_in_release": True, "frozen": True,
        },
        "pickup_notification": {
            "family": "ap_check", "status": "runtime_verified",
            "source": {"map": "game/sp/e1m1_intro/e1m1_intro", "container": "e1m1_intro_patch3.resources", "file": "hud/weapon_acquired", "entity": "Current major notification", "source_sha256": "runtime-evidence"},
            "shape": {"class": "idTarget_Notification", "inherit": None, "required_fields": ["notificationType", "notificationHudEventID"], "forbidden_fields": ["currencyList", "gameStat"]},
            "targets": [], "runtime_verified_maps": ["e1m1_intro", "e1m2_war", "hub", "e1m3_cult"], "allowed_in_release": True, "frozen": True,
        },
        "independent_location_trigger": {
            "family": "objective_check", "status": "static_evidence_only",
            "source": {"map": "game/hub/hub", "container": "hub_patch2.resources", "file": "vanillamaps/hub.map", "entity": "pickup_equipment_ice_bomb / target_relay_pickup_ice_bomb", "source_sha256": "364a547b6b2239d576e5122af9faa413bd85ffb1ebcaa97773ba708d99585e1b"},
            "shape": {"class": "idTrigger", "inherit": "trigger/trigger", "required_fields": ["triggerOnce", "targets", "clipModelInfo"], "forbidden_fields": ["currencyList", "commandText", "useableComponentDecl"]},
            "targets": ["target_relay_pickup_ice_bomb", "AP_CHECK_PICKUP_EQUIPMENT_ICE_BOMB"], "runtime_verified_maps": [], "allowed_in_release": True, "frozen": False,
        },
        "boolean_stat_modifier_direct": {
            "family": "stat_candidate", "status": "experimental",
            "source": {"map": "game/sp/e1m3_cult/e1m3_cult", "container": "e1m3_cult_patch3.resources", "file": "vanillamaps/e1m3_cult.map", "entity": "fasttravel_target_player_stat_modifier_1", "source_sha256": "ca83b98a7a533ec608b3ea3a4207814eb56bdb283a3d0c27b7a5ad744efa7866"},
            "shape": {"class": "idTarget_PlayerStatModifier", "inherit": None, "required_fields": ["gameStat", "value"], "forbidden_fields": ["modifier", "stat", "currencyList", "targets"]},
            "targets": [], "runtime_verified_maps": [], "allowed_in_release": False, "frozen": False,
        },
        "objective_complete_relay": {
            "family": "objective_only_relay", "status": "static_evidence_only",
            "source": {"map": "game/hub/hub", "container": "hub_patch2.resources", "file": "vanillamaps/hub.map", "entity": "target_relay_pickup_ice_bomb", "source_sha256": "364a547b6b2239d576e5122af9faa413bd85ffb1ebcaa97773ba708d99585e1b"},
            "shape": {"class": "idTarget_Count", "inherit": "target/relay", "required_fields": ["count", "targets"], "forbidden_fields": ["currencyList", "commandText"]},
            "targets": ["target_relay_complete_ice_bomb_obj"], "runtime_verified_maps": [], "allowed_in_release": True, "frozen": False,
        },
        "target_player_stat_modifier_inherit": {
            "family": "rejected_structure", "status": "rejected",
            "source": {"map": "dev1 runtime", "container": "v0.2.1-pre-alpha-dev", "file": "runtime console", "entity": "ap_bootstrap_v1_*", "source_sha256": "runtime-evidence"},
            "shape": {"class": "idTarget_PlayerStatModifier", "inherit": "target/player_stat_modifier", "required_fields": [], "forbidden_fields": []},
            "targets": [], "runtime_verified_maps": [], "allowed_in_release": False, "frozen": True,
        },
        "target_give_item_inherit": {
            "family": "rejected_structure", "status": "rejected",
            "source": {"map": "dev2 runtime", "container": "v0.2.1-pre-alpha-dev2", "file": "runtime console", "entity": "ap_rpc_v3_*_currency", "source_sha256": "runtime-evidence"},
            "shape": {"class": "idTarget_GiveItems", "inherit": "target/give_item", "required_fields": [], "forbidden_fields": []},
            "targets": [], "runtime_verified_maps": [], "allowed_in_release": False, "frozen": True,
        },
        "item_notification_major": {
            "family": "ap_item_notify_major", "status": "runtime_verified",
            "source": {"map": "game/sp/e1m1_intro/e1m1_intro", "container": "e1m1_intro_patch3.resources", "file": "vanillamaps/e1m1_intro.map", "entity": "native HUD notification/AP checks", "source_sha256": "5d8d1a6c6a377a77e5c8246c5eaf5034a1f4f917e82621645bf70e143b43d4a6"},
            "shape": {"class": "idTarget_Notification", "inherit": None, "required_fields": ["notificationType", "notificationHudEventID", "doNotShowDuplicate", "rootWidget", "icon", "header", "notificationSound"], "forbidden_fields": ["currencyList", "gameStat"]},
            "targets": [], "runtime_verified_maps": ["e1m1_intro"], "allowed_in_release": True, "frozen": False,
        },
        "item_notification_filler": {
            "family": "ap_item_notify_filler", "status": "runtime_verified",
            "source": {"map": "game/sp/e1m1_intro/e1m1_intro", "container": "e1m1_intro_patch3.resources", "file": "hud/codex", "entity": "Phase A Codex laboratory runtime approval", "source_sha256": "runtime-evidence"},
            "shape": {"class": "idTarget_Notification", "inherit": None, "required_fields": ["notificationType", "notificationHudEventID", "notificationEndHudEventID", "doNotShowDuplicate", "rootWidget", "icon", "header", "notificationSound"], "forbidden_fields": ["currencyList", "gameStat"]},
            "targets": [], "runtime_verified_maps": ["e1m1_intro"], "allowed_in_release": True, "frozen": False,
        },
        "location_notification_codex": {
            "family": "ap_location_notify", "status": "runtime_verified",
            "source": {"map": "game/sp/e1m1_intro/e1m1_intro", "container": "e1m1_intro_patch3.resources", "file": "hud/codex", "entity": "Phase A Codex laboratory runtime approval", "source_sha256": "runtime-evidence"},
            "shape": {"class": "idTarget_Notification", "inherit": None, "required_fields": ["notificationType", "notificationHudEventID", "notificationEndHudEventID", "doNotShowDuplicate", "rootWidget", "icon", "header", "subtext", "notificationSound"], "forbidden_fields": ["currencyList", "gameStat"]},
            "targets": [], "runtime_verified_maps": ["e1m1_intro"], "allowed_in_release": True, "frozen": False,
        },
    },
}
ITEM_NOTIFICATION_PREFIX = "ap_notify_item_"

DELIVERY_CONTRACTS: dict[str, Any] = {
    "counts": {"items": 116, "locations": 129, "map_checks": 108, "runtime_locations": 21, "runtime_goals": 1, "route_sentinel_batteries": 18},
    "family_primitives": {"simple_give": "target_command", "perk": "target_command", "progressive_perk": "target_command", "multi_command": "target_command", "currency": "currency_grant_direct", "extra_life": "target_command", "resource": "target_command", "trap_spawn": "target_command", "no_op": "target_command"},
    "location_entrypoints": {
        "7770056": {"map": "game/sp/e1m3_cult/e1m3_cult", "entity": "ap_independent_rocket_launcher_7770056", "primitive_id": "independent_location_trigger", "destructive": True},
        "7770074": {"map": "game/sp/hub/hub", "entity": "ap_independent_pickup_equipment_ice_bomb", "primitive_id": "independent_location_trigger", "destructive": True},
    },
    "bootstrap_test_entrypoints": {"rune_page": "ap_bootstrap_v2_rune_page", "frag_acquired": "ap_bootstrap_v2_frag_acquired", "ice_acquired": "ap_bootstrap_v2_ice_acquired"},
    "repeatability": {
        "7770016": "repeatable_runtime_proven",
        "7770142": "repeatable_runtime_proven",
    },
    "map_overrides": {},
}
FROZEN_CONSTRUCTOR_HASHES = {
    "target_command": "1679cb129904c997ed7040862782844d0445b735e496bd27bf04a96223fc0ff6",
    "target_count_relay": "f98420ff14bac0ca887116eef39466b2207bcb58e4fd4827f9e0f00efe139ba4",
    "currency_grant_direct": "52c0c2b7a32450948d8953d8b41a1544dade86348ba80dd06f5a7b97e708f993",
}
GAMEPLAY_COMMAND_PREFIXES = (
    "give ", "chrispy ", "g_giveExtraLives ",
    "ai_ScriptCmdEnt player1 givePlayerPerk ",
)


def load_primitive_registry() -> dict[str, Any]:
    return PRIMITIVE_REGISTRY


def load_foundation_contracts() -> dict[str, Any]:
    contracts = dict(DELIVERY_CONTRACTS)
    plans = release_plan(load_map_registry())
    contracts["active_maps"] = {plan.map_key: plan.runtime_map for plan in plans}
    root = Path(__file__).resolve().parent
    manifest_counts = [
        len(json.loads((root / plan.manifest).read_text(encoding="utf-8")))
        for plan in plans if (root / plan.manifest).exists()
    ]
    runtime_path = root / "data" / "runtime_locations.json"
    if len(manifest_counts) == len(plans) and runtime_path.exists():
        counts = dict(contracts["counts"])
        counts["map_checks"] = sum(manifest_counts)
        counts["runtime_locations"] = len(json.loads(runtime_path.read_text(encoding="utf-8")))
        counts["locations"] = counts["map_checks"] + counts["runtime_locations"]
        contracts["counts"] = counts
    return contracts


def validate_primitive_registry(registry: dict[str, Any] | None = None) -> dict[str, Any]:
    registry = registry or load_primitive_registry()
    primitives = registry.get("primitives", {})
    if not primitives:
        raise ValueError("Primitive registry is empty")
    if set(registry.get("allowed_statuses", ())) != VALID_STATUSES:
        raise ValueError("Primitive registry status vocabulary drifted")
    seen = set()
    for primitive_id, primitive in primitives.items():
        if primitive_id in seen:
            raise ValueError(f"Duplicate primitive ID: {primitive_id}")
        seen.add(primitive_id)
        if primitive.get("status") not in VALID_STATUSES:
            raise ValueError(f"Primitive {primitive_id} has invalid status")
        source = primitive.get("source", {})
        for key in ("map", "container", "file", "entity", "source_sha256"):
            if not source.get(key):
                raise ValueError(f"Primitive {primitive_id} lacks source.{key}")
        shape = primitive.get("shape", {})
        if not shape.get("class"):
            raise ValueError(f"Primitive {primitive_id} lacks a class")
        if not isinstance(shape.get("required_fields"), list):
            raise ValueError(f"Primitive {primitive_id} lacks required_fields")
        if not isinstance(shape.get("forbidden_fields"), list):
            raise ValueError(f"Primitive {primitive_id} lacks forbidden_fields")
        if primitive.get("status") in {"experimental", "rejected"} and primitive.get("allowed_in_release"):
            raise ValueError(f"Primitive {primitive_id} cannot enter release")
    return registry


def primitive(primitive_id: str, *, release: bool = True) -> dict[str, Any]:
    registry = validate_primitive_registry()
    try:
        result = registry["primitives"][primitive_id]
    except KeyError as error:
        raise ValueError(f"Unknown primitive ID: {primitive_id}") from error
    if result["status"] == "rejected":
        raise ValueError(f"Rejected primitive cannot be emitted: {primitive_id}")
    if release and not result.get("allowed_in_release", False):
        raise ValueError(f"Experimental primitive cannot enter release: {primitive_id}")
    return result


def validate_entity_shape(primitive_id: str, entity_text: str, *, release: bool = True) -> None:
    record = primitive(primitive_id, release=release)
    shape = record["shape"]
    if f'class = "{shape["class"]}";' not in entity_text:
        raise ValueError(f"{primitive_id} class mismatch")
    inherit = shape.get("inherit")
    if inherit is None:
        if "inherit =" in entity_text:
            raise ValueError(f"{primitive_id} must not emit inherit")
    elif f'inherit = "{inherit}";' not in entity_text:
        raise ValueError(f"{primitive_id} inherit mismatch")
    for field in shape["required_fields"]:
        if f"{field} =" not in entity_text:
            raise ValueError(f"{primitive_id} lacks required field {field}")
    for field in shape["forbidden_fields"]:
        if f"{field} =" in entity_text:
            raise ValueError(f"{primitive_id} emits forbidden field {field}")
    for rejected in ("target/player_stat_modifier", "target/give_item"):
        if f'inherit = "{rejected}";' in entity_text:
            raise ValueError(f"Generated entity uses rejected inherit {rejected}")


def _entity_header(entity_name: str, class_name: str, inherit: str | None = None) -> str:
    inherit_line = f'\n\t\tinherit = "{inherit}";' if inherit else ""
    return f'''entity {{
\tentityDef {entity_name} {{{inherit_line}
\t\tclass = "{class_name}";
\t\texpandInheritance = false;
\t\tpoolCount = 0;
\t\tpoolGranularity = 2;
\t\tnetworkReplicated = false;
\t\tdisableAIPooling = false;'''


def build_primitive(
    primitive_id: str,
    entity_name: str,
    parameters: dict[str, Any],
    *,
    release: bool = True,
) -> str:
    """Instantiate one narrowly supported primitive from registered evidence."""
    record = primitive(primitive_id, release=release)
    shape = record["shape"]
    header = _entity_header(entity_name, shape["class"], shape.get("inherit"))
    if primitive_id == "target_command":
        if set(parameters) != {"command"} or not isinstance(parameters["command"], str):
            raise ValueError("target_command accepts only a string command")
        block = f'''{header}
\t\tedit = {{
\t\t\tcommandText = "{parameters["command"]}";
\t\t}}
\t}}
}}
'''
    elif primitive_id == "target_count_relay":
        targets = parameters.get("targets")
        if set(parameters) != {"targets"} or not isinstance(targets, list) or not targets:
            raise ValueError("target_count_relay requires a non-empty targets list")
        target_lines = "\n".join(
            f'\t\t\t\titem[{index}] = "{target}";'
            for index, target in enumerate(targets)
        )
        block = f'''{header}
\t\tedit = {{
\t\t\tcount = 1;
\t\t\ttargets = {{
\t\t\t\tnum = {len(targets)};
{target_lines}
\t\t\t}}
\t\t}}
\t}}
}}
'''
    elif primitive_id == "currency_grant_direct":
        if set(parameters) != {"currency", "count"}:
            raise ValueError("currency_grant_direct accepts currency and count")
        currency = parameters["currency"]
        count = parameters["count"]
        if not isinstance(currency, str) or not currency.startswith("CURRENCY_"):
            raise ValueError("Invalid currency")
        if not isinstance(count, int) or count <= 0:
            raise ValueError("Currency count must be positive")
        block = f'''{header}
\t\tedit = {{
\t\t\tflags = {{
\t\t\t\tnoFlood = true;
\t\t\t}}
\t\t\titemList = {{
\t\t\t\tnum = 0;
\t\t\t}}
\t\t\taddUpToCount = false;
\t\t\twhenToSave = "SGT_NO_SAVE";
\t\t\tsaveType = "SGS_NONE";
\t\t\tcurrencyList = {{
\t\t\t\tnum = 1;
\t\t\t\titem[0] = {{
\t\t\t\t\tcurrencyType = "{currency}";
\t\t\t\t\tcount = {count};
\t\t\t\t}}
\t\t\t}}
\t\t}}
\t}}
}}
'''
    elif primitive_id == "item_notification_major":
        if not isinstance(parameters, dict) or set(parameters) != {"header_key"}:
            raise ValueError("item_notification_major requires only header_key")
        header_key = parameters["header_key"]
        block = f'''{header}
\t\tedit = {{
\t\t\tflags = {{
\t\t\t\tnoFlood = false;
\t\t\t}}
\t\t\tnotificationType = "HUD_NOTIFY_SECRET_FOUND";
\t\t\tnotificationHudEventID = "HUD_EVENT_PLAYER_NOTIFICATION_SECRET_FOUND";
\t\t\tpriority = 4;
\t\t\tdoNotShowDuplicate = false;
\t\t\tshowDuringCombat = true;
\t\t\tnotificationTime = 2400;
\t\t\trootWidget = "tier3centered";
\t\t\ticon = "art/ui/dossier/icons/ico_secrets_off";
\t\t\theader = "{header_key}";
\t\t\tsubtext = "";
\t\t\tnotificationSound = "play_secret_encounter_found";
\t\t\tshowCVar = "g_setting_notification_major";
\t\t}}
\t}}
}}
'''
    elif primitive_id in {"item_notification_filler", "location_notification_codex"}:
        expected = (
            {"header_key"}
            if primitive_id == "item_notification_filler"
            else {"header_key", "subtext_key"}
        )
        if not isinstance(parameters, dict) or set(parameters) != expected:
            raise ValueError(f"{primitive_id} has an invalid parameter set")
        header_key = parameters["header_key"]
        subtext_key = parameters.get("subtext_key", "")
        block = f'''{header}
\t\tedit = {{
\t\t\tflags = {{
\t\t\t\tnoFlood = false;
\t\t\t}}
\t\t\thudLocation = "HUD_LOC_LEFT";
\t\t\tnotificationType = "HUD_NOTIFY_CODEX_RECIEVED";
\t\t\tnotificationHudEventID = "HUD_EVENT_PLAYER_NOTIFICATION_CODEX";
\t\t\tnotificationEndHudEventID = "HUD_EVENT_PLAYER_NOTIFICATION_CODEX_END";
\t\t\tdesiredDossierPage = "DOSSIER_PAGE_CODEX";
\t\t\tpriority = 5;
\t\t\tdoNotShowDuplicate = false;
\t\t\trootWidget = "compact_notification";
\t\t\ticon = "art/ui/icons/notifications/demons";
\t\t\theader = "{header_key}";
\t\t\tsubtext = "{subtext_key}";
\t\t\tnotificationSound = "play_hud_lower";
\t\t\tshowCVar = "g_setting_notification_minor";
\t\t}}
\t}}
}}
'''
    elif primitive_id == "boolean_stat_modifier_direct":
        if set(parameters) != {"stat", "value"} or parameters["value"] != 1:
            raise ValueError("boolean_stat_modifier_direct requires one stat set to 1")
        block = f'''{header}
\t\tedit = {{
\t\t\tgameStat = "{parameters["stat"]}";
\t\t\tvalue = 1;
\t\t}}
\t}}
}}
'''
    else:
        raise ValueError(f"No constructor registered for primitive {primitive_id}")
    validate_entity_shape(primitive_id, block, release=release)
    return block


@dataclass(frozen=True)
class DeliveryCommand:
    entity: str
    command: str
    index: int


@dataclass(frozen=True)
class DeliveryPlan:
    item_id: int
    family: str
    primitive_id: str
    commands: tuple[DeliveryCommand, ...]
    stage: int | None
    description: str


def classify_item_definition(definition: Any) -> str:
    if isinstance(definition, list):
        return "multi_command"
    if isinstance(definition, dict):
        return {
            "perk": "perk",
            "progressive_perk": "progressive_perk",
            "currency": "currency",
            "no_op": "no_op",
        }.get(definition.get("type"), "unknown")
    if not isinstance(definition, str):
        return "unknown"
    lowered = definition.lower()
    if lowered.startswith("chrispy "):
        return "trap_spawn"
    if lowered.startswith("g_giveextralives ") or "extra_life" in lowered:
        return "extra_life"
    if lowered.startswith(("give health", "give armor", "give ammo")):
        return "resource"
    if "giveplayerperk" in lowered:
        return "perk"
    return "simple_give"


def compile_item_delivery_plan(
    item_id: int,
    definitions: dict[int, Any],
    *,
    stage: int | None = None,
    receipt: bool = False,
    classification: int | None = None,
    notification_slot: str | None = None,
) -> DeliveryPlan:
    """Compile silent effects and, for new receipts, one final notification."""
    if item_id not in definitions:
        raise ValueError(f"Unknown item ID: {item_id}")
    definition = definitions[item_id]
    family = classify_item_definition(definition)
    contracts = load_foundation_contracts()
    try:
        primitive_id = contracts["family_primitives"][family]
    except KeyError as error:
        raise ValueError(f"Unregistered item family for {item_id}: {family}") from error
    primitive(primitive_id, release=True)
    entities: list[str]
    description: str
    resolved_stage = stage
    prefix = "ap_rpc_v3"
    if family == "progressive_perk":
        perks = definition.get("perks", [])
        if stage is None:
            raise ValueError(f"Progressive item {item_id} requires a stage")
        if not 0 <= stage < len(perks):
            raise ValueError(f"Progressive stage {stage} exceeds {len(perks)} stages")
        entities = [f"{prefix}_{item_id}_{stage}"]
        description = f"stage {stage}: {perks[stage]}"
    elif family == "multi_command":
        if not definition:
            raise ValueError("mapping list is empty")
        entities = [f"ap_rpc_v3_{item_id}_{index}" for index in range(len(definition))]
        description = " -> ".join(definition)
        resolved_stage = None
    elif family == "no_op":
        entities = []
        description = str(definition.get("description", "runtime-only no-op"))
        resolved_stage = None
    else:
        entities = [f"{prefix}_{item_id}"]
        description = str(definition)
        resolved_stage = None
    commands = [
        DeliveryCommand(
            entity=entity,
            command=f"ai_ScriptCmdEnt {entity} activate",
            index=index,
        )
        for index, entity in enumerate(entities)
    ]
    if receipt and family != "no_op":
        if classification is None:
            raise ValueError(f"Received item {item_id} requires classification")
        notification = notification_entity_name(
            item_id, classification, stage=resolved_stage, slot=notification_slot
        )
        commands.append(DeliveryCommand(
            entity=notification,
            command=f"ai_ScriptCmdEnt {notification} activate",
            index=len(commands),
        ))
    if any(command.command.startswith(GAMEPLAY_COMMAND_PREFIXES) for command in commands):
        raise ValueError("Delivery plan contains a raw gameplay-changing command")
    return DeliveryPlan(item_id, family, primitive_id, tuple(commands), resolved_stage, description)


def compile_all_item_plans(definitions: dict[int, Any]) -> list[DeliveryPlan]:
    plans = []
    for item_id, definition in sorted(definitions.items()):
        stage = 0 if classify_item_definition(definition) == "progressive_perk" else None
        plans.append(compile_item_delivery_plan(item_id, definitions, stage=stage))
    return plans


def family_counts(definitions: dict[int, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for definition in definitions.values():
        family = classify_item_definition(definition)
        counts[family] = counts.get(family, 0) + 1
    return dict(sorted(counts.items()))
