"""Keep AP Fortress checks independent of disposable vanilla visit layers."""
from __future__ import annotations

import re

from doom_eap.content.content_catalog import load_content_catalog
from tools.maps.ap_map_generator import find_entity_block_bounds, find_matching_brace

VISITS = ("from_e1m1", "from_e1m2", "from_e1m4", "from_e2m1",
          "from_e2m2", "from_e2m4", "from_e3m1")


def content_layers(phase: int) -> list[str]:
    return [f"game/sp/hub/ap_phase_{i}" for i in range(1, phase + 1)]


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

    Native visit scenery remains authored; Battery doors and transactions are
    untouched. Native story exits cannot bypass the unified Mission Select.
    """
    if "entityDef ap_fortress_phase_0 {" in text:
        raise ValueError("Fortress campaign projection already applied")
    names = ("First", "Second", "Third", "Fourth", "Fifth", "Sixth", "Seventh")
    catalog = load_content_catalog()
    for alias, code in config["entities"].items():
        region = catalog.location_by_id(code).region
        phase = next(i for i, visit in enumerate(names, 1) if f"- {visit} Visit" in region)
        layer = content_layers(phase)[-1]
        source = alias.removeprefix("AP_CHECK_").lower()
        # All generated representations, including cleanup and automap, share
        # the gate. Keeping just the original pickup layered leaks AP triggers.
        entities = [source, f"ap_independent_{source}", f"ap_location_visual_{code}",
                    f"ap_automap_location_{code}", f"ap_remove_location_visual_{code}",
                    f"ap_hide_location_visual_{code}"]
        for name in entities:
            bounds = find_entity_block_bounds(text, name)
            if bounds:
                start, end = bounds
                text = text[:start] + _layer(text[start:end], layer) + text[end:]

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
                 _native_list("activate_Immediately", active + content_layers(phase)) +
                 _native_list("remove_Immediately", remove) + '\t}\n}\n}\n')
        targets = [f"ap_fortress_layers_{phase}"]
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
