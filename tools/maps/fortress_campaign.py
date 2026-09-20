"""Keep AP Fortress checks independent of disposable vanilla visit layers."""
from __future__ import annotations

import re

from doom_eap.content.content_catalog import load_content_catalog
from tools.maps.ap_map_generator import find_entity_block_bounds, find_matching_brace

VISITS = ("from_e1m1", "from_e1m2", "from_e1m4", "from_e2m1",
          "from_e2m2", "from_e2m4", "from_e3m1")

# Common circulation only. The battery-room reward doors keep their own
# stations and costs; these native actions never grant inventory or AP checks.
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


def project_fortress(text: str, config: dict) -> str:
    """Only the server-derived phase activates cumulative AP content layers.

    Native visit scenery remains authored. Common corridors are usable from the
    initial Hub; battery reward doors retain their transactions. Native story
    exits cannot bypass the unified Mission Select.
    """
    if "entityDef ap_fortress_phase_0 {" in text:
        raise ValueError("Fortress campaign projection already applied")
    for alias, code in config["entities"].items():
        layer = content_layer(code)
        source = alias.removeprefix("AP_CHECK_").lower()
        entities = [source, f"ap_location_visual_{code}",
                    f"ap_automap_location_{code}", f"ap_remove_location_visual_{code}",
                    f"ap_hide_location_visual_{code}"]
        if source in {f"interact_hub_2_battery_station_{i}" for i in (1, 2, 3)}:
            entities.remove(source)
        for name in entities:
            bounds = find_entity_block_bounds(text, name)
            if bounds:
                start, end = bounds
                text = text[:start] + _layer(text[start:end], layer) + text[end:]
    clear_objectives = []
    pattern = r"entity\s*\{\s*(?:layers\s*\{[^}]*\}\s*)?entityDef\s+(\w+)\s*\{"
    for match in reversed(list(re.finditer(pattern, text))):
        start = match.start()
        end = find_matching_brace(text, text.index("{", start)) + 1
        block = text[start:end]
        if 'class = "idTarget_Objective_Give";' not in block:
            continue
        objective = re.search(r'\bdecl = "(objective/hub/[^"]+)";', block)
        if objective is None:
            raise ValueError(f"Unrecognized Fortress story objective: {match[1]}")
        clear_objectives.append(match[1])
        replacement = ('entity {\n\tentityDef ' + match[1] + ' {\n'
                       '\tclass = "idTarget_Objective_Complete";\n\texpandInheritance = false;\n'
                       '\tedit = {\n\t\tobjective = "' + objective[1] + '";\n'
                       '\t\tsteps = { num = 0; }\n\t\taddSteps = { num = 0; }\n'
                       '\t\tentities = { num = 0; }\n\t}\n}\n}\n')
        text = text[:start] + replacement + text[end:]

    # Reuse the engine's native layer primitive, not a console map loader.
    # Each command selects scenery and adds all earned content layers; it never
    # removes earlier AP layers or manufactures a check/inventory grant.
    all_scenery = [f"game/sp/hub/{visit}" for visit in (*VISITS, "from_e3m4", "from_e1m2_post_prison")]
    skies = [f"game/sp/hub/sky_{sky}" for sky in ("earth", "sentinel", "phobos")]
    for phase in range(8):
        visit = VISITS[max(0, phase - 1)]
        sky = "sentinel" if phase in (2, 6, 7) else "phobos" if phase == 5 else "earth"
        active = [f"game/sp/hub/{visit}", f"game/sp/hub/sky_{sky}"]
        remove = [layer for layer in all_scenery + skies if layer not in active]
        text += ('\nentity {\n\tentityDef ap_fortress_layers_' + str(phase) + ' {\n'
                 '\tclass = "idTarget_LayerStateChange";\n\texpandInheritance = false;\n\tedit = {\n' +
                 _native_list("activate_Immediately", active + content_layers(max(1, phase))) +
                 _native_list("remove_Immediately", remove) + '\t}\n}\n}\n')
        targets = [f"ap_fortress_layers_{phase}", *sorted(clear_objectives), *CIRCULATION_ACTIONS]
        if phase == 7:
            targets.append("target_show_engine_room_secret")
        text += ('entity {\n\tentityDef ap_fortress_phase_' + str(phase) + ' {\n'
                 '\tclass = "idTarget_Count";\n\texpandInheritance = false;\n\tedit = {\n'
                 '\t\tcount = 1;\n\t\treuseable = true;\n' + _native_list("targets", targets) +
                 '\t}\n}\n}\n')

    # Story transitions must not launch the next Base mission behind AP policy.
    for match in reversed(list(re.finditer(r"entity\s*\{\s*(?:layers\s*\{[^}]*\}\s*)?entityDef\s+(\w+)\s*\{", text))):
        start = match.start()
        end = find_matching_brace(text, text.index("{", start)) + 1
        block = text[start:end]
        if 'class = "idTarget_LevelTransition";' in block:
            replacement = ('entity {\n\tentityDef ' + match[1] + ' {\n'
                           '\tclass = "idTarget_Count";\n\texpandInheritance = false;\n'
                           '\tedit = { count = 1; }\n}\n}')
            text = text[:start] + replacement + text[end:]
    return text
