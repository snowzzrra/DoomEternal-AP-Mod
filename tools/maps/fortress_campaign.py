"""Keep AP Fortress checks independent of disposable vanilla visit layers."""
from __future__ import annotations

import re

from doom_eap.content.content_catalog import load_content_catalog
from tools.maps.ap_map_generator import find_entity_block_bounds, find_matching_brace, replace_targets_block

VISITS = ("from_e1m1", "from_e1m2", "from_e1m4", "from_e2m1",
          "from_e2m2", "from_e2m4", "from_e3m1")

CIRCULATION_ACTIONS = (
    "main_deck_target_interact_action_unlock_door_1",
    "main_deck_target_interact_action_unlock_door_2",
    "main_deck_target_interact_action_unlock_engine_door",
    "target_interact_action_unlock_engine_doors",
)


def content_layers(phase: int) -> list[str]:
    return [f"game/sp/hub/ap_phase_{i}" for i in range(1, phase + 1)]


def content_layer(location_id: int) -> str:
    region = load_content_catalog().location_by_id(location_id).region
    names = ("First", "Second", "Third", "Fourth", "Fifth", "Sixth", "Seventh")
    phase = next(i for i, visit in enumerate(names, 1) if f"- {visit} Visit" in region)
    return content_layers(phase)[-1]


def _layer(block: str, name: str | None) -> str:
    block = re.sub(r"\s*layers\s*\{[^}]*\}", "", block, count=1)
    if name:
        block = block.replace("entity {", f'entity {{\n\tlayers {{ "{name}" }}', 1)
    return block


def _native_list(field: str, values: list[str]) -> str:
    return (f"\t\t{field} = {{\n\t\t\tnum = {len(values)};\n" +
            "".join(f'\t\t\titem[{i}] = "{value}";\n' for i, value in enumerate(values)) +
            "\t\t}\n")


def _set_native_list(block: str, field: str, values: list[str]) -> str:
    match = re.search(rf"\b{field}\s*=\s*\{{", block)
    if match:
        start = block.rfind("\n", 0, match.start()) + 1
        end = find_matching_brace(block, block.index("{", match.start()))
    else:
        edit = re.search(r"\bedit\s*=\s*\{", block)
        if edit is None:
            raise ValueError("Fortress visit transition has no edit block")
        closing = find_matching_brace(block, block.index("{", edit.start())) - 1
        start = end = block.rfind("\n", 0, closing) + 1
    return block[:start] + _native_list(field, values) + block[end:]


def _mission_select_transition(name: str) -> str:
    """Use the known-safe relay shape to reach the authored Mission Select action."""
    return ('entity {\n\tentityDef ' + name + ' {\n'
            '\tclass = "idTarget_Count";\n\texpandInheritance = false;\n'
            '\tedit = {\n\t\tcount = 1;\n\t\treuseable = true;\n' +
            _native_list("targets", ["ap_fortress_mission_select"]) + '\t}\n}\n}')


def _mission_select_action(text: str) -> str:
    """Dispatch the native mission_idle/mission_usable transition to mission_used."""
    bounds = find_entity_block_bounds(text, "target_interact_action_portal_usable")
    if bounds is None:
        raise ValueError("Fortress portal interaction action is missing")
    source = text[slice(*bounds)]
    action = source.replace("target_interact_action_portal_usable", "ap_fortress_mission_select", 1)
    action = action.replace("interact_hub_portal_console", "interact_hub_mission_select_1", 1)
    if action == source or 'action = "IA_ACTIVATE_ANY";' not in action:
        raise ValueError("Fortress portal interaction action is not qualified")
    return action.replace('action = "IA_ACTIVATE_ANY";', 'action = "IA_USE_SUCCEED";', 1)


def _portal_navigation(text: str) -> str:
    """Use the reusable native console graph at the existing portal interaction."""
    portal_bounds = find_entity_block_bounds(text, "interact_hub_portal_console")
    menu_bounds = find_entity_block_bounds(text, "interact_hub_mission_select_1")
    if portal_bounds is None or menu_bounds is None:
        raise ValueError("Fortress navigation interaction sources are missing")
    portal, menu = text[slice(*portal_bounds)], text[slice(*menu_bounds)]
    components = []
    for block in (portal, menu):
        match = re.search(r"\binteraction\s*=\s*\{", block)
        if match is None:
            raise ValueError("Fortress navigation interaction component is missing")
        components.append((match.start(), find_matching_brace(block, block.index("{", match.start()))))
    interaction = menu[slice(*components[1])]
    interaction = interaction.replace('initalState = "interactables/console/mission_console/mission_idle";',
                                      'initalState = "interactables/console/mission_console/mission_activate";', 1)
    portal = portal[:components[0][0]] + interaction + portal[components[0][1]:]
    portal = replace_targets_block(portal, [])
    portal = portal.replace('class = "idInteractable_Obstacle";', 'class = "idInteractable";', 1)
    portal = portal.replace('inherit = "interact/hub/portal_console_stairs";',
                            'inherit = "interact/hub/mission_select";', 1)
    portal = portal.replace('whenToSave = "SGT_NO_SAVE";',
                            'whenToSave = "SGT_NO_SAVE";\n\t\tactivateTargetsOnUse = false;\n'
                            '\t\tactivateTargetsOnEndInteraction = true;\n\t\tonUseCodexEntry = "";', 1)
    return text[:portal_bounds[0]] + portal + text[portal_bounds[1]:]


def project_fortress(text: str, config: dict) -> str:
    """Only the server-derived phase activates cumulative AP content layers.

    Native visit scenery remains authored. Common corridors are usable from the
    initial Hub; battery reward doors retain their transactions. Native story
    exits cannot bypass the unified Mission Select.
    """
    if "entityDef ap_fortress_phase_0 {" in text:
        raise ValueError("Fortress campaign projection already applied")
    if find_entity_block_bounds(text, "interact_hub_mission_select_1") is None:
        raise ValueError("Fortress Mission Select interactable is missing")
    mission_select_action = _mission_select_action(text)
    mission_select_enable = mission_select_action.replace("ap_fortress_mission_select", "ap_fortress_mission_select_enable", 1)
    mission_select_enable = mission_select_enable.replace('action = "IA_USE_SUCCEED";', 'action = "IA_ACTIVATE_ANY";', 1)
    text = _portal_navigation(text)
    for alias, code in config["entities"].items():
        layer = content_layer(code)
        source = alias.removeprefix("AP_CHECK_").lower()
        entities = [source, f"ap_independent_{source}", f"ap_location_visual_{code}",
                    f"ap_automap_location_{code}", f"ap_remove_location_visual_{code}",
                    f"ap_hide_location_visual_{code}"]
        if source in {f"interact_hub_2_battery_station_{i}" for i in (1, 2, 3)}:
            entities.remove(source)
        for name in entities:
            bounds = find_entity_block_bounds(text, name)
            if bounds:
                start, end = bounds
                text = text[:start] + _layer(text[start:end], layer) + text[end:]
        # Checked repair hides presentation and removes only the AP check relay.
        # Shared native targets on the source trigger retain their authored edges.
        name = f"ap_hide_location_visual_{code}"
        bounds = find_entity_block_bounds(text, name)
        if bounds:
            start, end = bounds
            model = name + "_model"
            suppress = f"ap_suppress_checked_{code}"
            hidden = text[start:end].replace(f"entityDef {name}", f"entityDef {model}", 1)
            relay = ('entity {\n\tentityDef ' + name + ' {\n'
                     '\tclass = "idTarget_Count";\n\texpandInheritance = false;\n'
                     '\tedit = {\n\t\tcount = 1;\n\t\treuseable = true;\n' +
                     _native_list("targets", [model, suppress]) + '\t}\n}\n}')
            remove = ('entity {\n\tentityDef ' + suppress + ' {\n'
                      '\tinherit = "target/remove";\n\tclass = "idTarget_Remove";\n'
                      '\texpandInheritance = false;\n\tedit = {\n' +
                      _native_list("targets", [alias]) + '\t}\n}\n}')
            text = text[:start] + hidden + _layer(relay, layer) + _layer(remove, layer) + text[end:]
    pattern = r"entity\s*\{\s*(?:layers\s*\{[^}]*\}\s*)?entityDef\s+(\w+)\s*\{"
    for match in reversed(list(re.finditer(pattern, text))):
        start = match.start()
        end = find_matching_brace(text, text.index("{", start))
        block = text[start:end]
        if 'class = "idTarget_Objective_Give";' not in block:
            continue
        objective = re.search(r'\bdecl = "(objective/hub/[^"]+)";', block)
        if objective is None:
            raise ValueError(f"Unrecognized Fortress story objective: {match[1]}")
        replacement = ('entity {\n\tentityDef ' + match[1] + ' {\n'
                       '\tclass = "idTarget_Objective_Complete";\n\texpandInheritance = false;\n'
                       '\tedit = {\n\t\tobjective = "' + objective[1] + '";\n'
                       '\t\tsteps = { num = 0; }\n\t\taddSteps = { num = 0; }\n'
                       '\t\tentities = { num = 0; }\n\t}\n}\n}\n')
        text = text[:start] + replacement + text[end:]

    all_scenery = [f"game/sp/hub/{visit}" for visit in (*VISITS, "from_e3m4", "from_e1m2_post_prison")]
    skies = [f"game/sp/hub/sky_{sky}" for sky in ("earth", "sentinel", "phobos")]
    for phase in range(8):
        visit = VISITS[max(0, phase - 1)]
        visit_bounds = find_entity_block_bounds(text, f"target_change_layer_{visit}")
        if visit_bounds is None:
            raise ValueError(f"Fortress visit transition is missing: {visit}")
        visit_target = text[slice(*visit_bounds)]
        checkpoint = re.search(r'\bcheckpointName = "([^"]+)";', visit_target)
        spawn = re.search(r'\bplayerSpawnSpot = "([^"]+)";', visit_target)
        if checkpoint is None or spawn is None:
            raise ValueError(f"Fortress visit checkpoint is incomplete: {visit}")
        sky = "sentinel" if phase in (2, 6, 7) else "phobos" if phase == 5 else "earth"
        active = [f"game/sp/hub/{visit}", f"game/sp/hub/sky_{sky}"]
        remove = [layer for layer in all_scenery + skies if layer not in active]
        layer_target = _layer(visit_target, None).replace(
            f"entityDef target_change_layer_{visit}", f"entityDef ap_fortress_layers_{phase}", 1)
        layer_target = re.sub(r'\s*(?:checkpointName|playerSpawnSpot)\s*=\s*"[^"]+";', '', layer_target)
        layer_target = _set_native_list(
            layer_target, "activate_Immediately", active + content_layers(max(1, phase)))
        layer_target = _set_native_list(layer_target, "remove_Immediately", remove)
        text += "\n" + layer_target + "\n"
        targets = [f"ap_fortress_layers_{phase}", *CIRCULATION_ACTIONS,
                   "target_interact_action_portal_usable", "ap_fortress_mission_select_enable"]
        if phase == 7:
            targets.append("target_show_engine_room_secret")
        text += ('entity {\n\tentityDef ap_fortress_phase_' + str(phase) + ' {\n'
                 '\tclass = "idTarget_Count";\n\texpandInheritance = false;\n\tedit = {\n'
                 '\t\tcount = 1;\n\t\treuseable = true;\n' + _native_list("targets", targets) +
                 '\t}\n}\n}\n')

    for match in reversed(list(re.finditer(r"entity\s*\{\s*(?:layers\s*\{[^}]*\}\s*)?entityDef\s+(\w+)\s*\{", text))):
        start = match.start()
        end = find_matching_brace(text, text.index("{", start))
        block = text[start:end]
        if 'class = "idTarget_LevelTransition";' in block:
            text = text[:start] + _mission_select_transition(match[1]) + text[end:]
    return text + "\n" + mission_select_action + "\n" + mission_select_enable
