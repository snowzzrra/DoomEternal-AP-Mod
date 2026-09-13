"""Project authored stage exits onto the universal Fortress destination."""
from __future__ import annotations

import re

from doom_eap.content.content_catalog import ContentCatalog
from tools.maps.ap_map_generator import find_entity_block_bounds


def project_stage_return(catalog: ContentCatalog, map_key: str, text: str) -> tuple[str, list[str]]:
    spec = catalog.maps[map_key]
    owners = spec.data.get("campaign_return_owners", ())
    if map_key == "hub":
        return text, []
    if not owners:
        raise ValueError(f"{map_key}: missing authored unified campaign exit")
    changed = []
    if map_key == "e5m4_boss":
        # The authored final-defeat event used to start the saga-ending movie.
        # Preserve encounter exit/XP, then run the existing defeat publisher and
        # native transition. AP GoalPolicy alone decides whether this ends a seed.
        defeat = "death_of_the_darklord_start_trigger"
        bounds = find_entity_block_bounds(text, defeat)
        if bounds is None:
            raise ValueError("Dark Lord: missing authored final-defeat event")
        start, end = bounds
        block = text[start:end]
        before = ('item[0] = "death_of_the_darklord_start";',
                  'item[1] = "stage_1_encounter_trigger_exit_3";',
                  'item[2] = "stage_1_encounter_trigger_user_award_xp";')
        after = ('item[0] = "stage_1_encounter_trigger_exit_3";',
                 'item[1] = "stage_1_encounter_trigger_user_award_xp";',
                 'item[2] = "stage_3_target_level_transition_1";')
        if all(value in block for value in before):
            for old, new in zip(before, after):
                block = block.replace(old, new, 1)
            text = text[:start] + block + text[end:]
            changed.append(defeat)
        elif not all(value in block for value in after):
            raise ValueError("Dark Lord: final-defeat target graph differs from audited source")
    for owner in owners:
        # Existing publisher adapters retain the original native transition under
        # one of these names; preserve its allocation, activation and statistics.
        candidates = (f"{owner}_ap_native_transition", f"{owner}_native", owner)
        resolved = next(((name, bounds) for name in candidates
                         if (bounds := find_entity_block_bounds(text, name)) is not None), None)
        if resolved is None:
            raise ValueError(f"{map_key}: missing authored exit {owner}")
        name, (start, end) = resolved
        original = text[start:end]
        if not re.search(r'\bclass\s*=\s*"idTarget_LevelTransition";', original):
            raise ValueError(f"{map_key}: exit is not a native level transition: {name}")
        block = original
        for field, value in (("nextMapName", '"maps/game/hub/hub.map"'),
                             ("checkpointName", '"from_intro"'),
                             ("returnToMainMenu", "false")):
            pattern = rf'\b{field}\s*=\s*[^;]+;'
            if len(re.findall(pattern, block)) > 1:
                raise ValueError(f"{map_key}: ambiguous exit field {field}")
            if re.search(pattern, block):
                block = re.sub(pattern, f"{field} = {value};", block)
            else:
                block, count = re.subn(r'\bedit\s*=\s*\{',
                                      lambda m: m[0] + f"\n\t\t{field} = {value};", block, count=1)
                if count != 1:
                    raise ValueError(f"{map_key}: native exit has no edit block")
        if block != original:
            text = text[:start] + block + text[end:]
            changed.append(name)
    return text, changed
