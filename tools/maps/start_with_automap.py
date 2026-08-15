"""AP-owned interaction relays for room-selected Automap startup."""

from __future__ import annotations

import re


SUPPORTED_START_WITH_AUTOMAP_MAPS = (
    "e1m1_intro",
    "e1m2_war",
    "e1m3_cult",
    "e1m4_boss",
    "e2m1_nest",
    "e2m2_base",
    "e2m3_core",
    "e2m4_boss",
    "e3m1_slayer",
    "e3m2_hell",
    "e3m2_hell_b",
    "e3m3_maykr",
    "e3m4_boss",
)
START_WITH_AUTOMAP_ENTITY_PREFIX = "ap_start_with_automap_"


def start_with_automap_helper_names(map_key: str) -> tuple[str, str]:
    if map_key not in SUPPORTED_START_WITH_AUTOMAP_MAPS:
        raise ValueError(f"unsupported Start With Automap map: {map_key}")
    prefix = f"{START_WITH_AUTOMAP_ENTITY_PREFIX}{map_key}"
    return f"{prefix}_touch", f"{prefix}_use"


def _matching_brace(text: str, opening: int) -> int:
    depth = 0
    for index in range(opening, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    raise ValueError("unbalanced map entity braces")


def native_automap_station(text: str) -> tuple[str, str] | None:
    matches = list(re.finditer(r'class\s*=\s*"idInteractable_Automap";', text))
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("map must contain exactly one native Automap station")
    marker = matches[0]
    entity_start = text.rfind("entity {", 0, marker.start())
    if entity_start < 0:
        raise ValueError("native Automap station wrapper missing")
    entity_end = _matching_brace(text, text.find("{", entity_start))
    block = text[entity_start:entity_end]
    name = re.search(r"entityDef\s+([^\s{]+)\s*\{", block)
    if name is None:
        raise ValueError("native Automap station name missing")
    required = (
        'inherit = "interact/automap";',
        'class = "idInteractable_Automap";',
        'automapPropertiesDecl = "automap_station";',
        'useStat = "STAT_AUTOMAP";',
    )
    if any(block.count(value) != 1 for value in required):
        raise ValueError("native Automap station contract drifted")
    return name.group(1), block


def generate_start_with_automap_helpers(content: str, map_key: str) -> str:
    """Emit two inert relays targeting the untouched native station."""
    station = native_automap_station(content)
    if station is None:
        if map_key in SUPPORTED_START_WITH_AUTOMAP_MAPS:
            raise ValueError(f"Start With Automap station missing: {map_key}")
        return ""
    station_name, _ = station
    touch_name, use_name = start_with_automap_helper_names(map_key)
    if any(f"entityDef {name} {{" in content for name in (touch_name, use_name)):
        raise ValueError(f"Start With Automap helper already exists: {map_key}")

    def helper(name: str, action: str) -> str:
        return f'''entity {{
\tentityDef {name} {{
\t\tinherit = "target/interact_action";
\t\tclass = "idTarget_InteractionAction";
\t\texpandInheritance = false;
\t\tpoolCount = 0;
\t\tpoolGranularity = 2;
\t\tnetworkReplicated = false;
\t\tdisableAIPooling = false;
\t\tedit = {{
\t\t\trenderModelInfo = {{
\t\t\t\tmaterialRemap = {{
\t\t\t\t\tnum = 1;
\t\t\t\t\titem[0] = {{
\t\t\t\t\t\tto = "art/tile/common/nodraw";
\t\t\t\t\t}}
\t\t\t\t}}
\t\t\t}}
\t\t\ttargets = {{
\t\t\t\tnum = 1;
\t\t\t\titem[0] = "{station_name}";
\t\t\t}}
\t\t\taction = "{action}";
\t\t}}
\t}}
}}
'''

    return helper(touch_name, "IA_TOUCH") + helper(use_name, "IA_USE_SUCCEED")


def validate_start_with_automap_helpers(
    source: str, generated: str, map_key: str
) -> None:
    """Check native station preservation and exact helper contracts."""
    source_station = native_automap_station(source)
    generated_station = native_automap_station(generated)
    if source_station is None:
        if generated_station is not None:
            raise ValueError(f"unexpected native Automap station: {map_key}")
        if f"entityDef {START_WITH_AUTOMAP_ENTITY_PREFIX}" in generated:
            raise ValueError(f"unexpected Start With Automap helper: {map_key}")
        return
    touch_name, use_name = start_with_automap_helper_names(map_key)
    if generated_station is None:
        raise ValueError(f"native Automap station disappeared: {map_key}")
    if source_station != generated_station:
        raise ValueError(f"native Automap station changed: {map_key}")

    station_name = source_station[0]
    for name, action in (
        (touch_name, "IA_TOUCH"),
        (use_name, "IA_USE_SUCCEED"),
    ):
        marker = re.search(rf"entityDef\s+{re.escape(name)}\s*\{{", generated)
        if marker is None:
            raise ValueError(f"Start With Automap helper missing: {map_key}/{name}")
        entity_start = generated.rfind("entity {", 0, marker.start())
        block = generated[entity_start:_matching_brace(generated, generated.find("{", entity_start))]
        required = (
            'inherit = "target/interact_action";',
            'class = "idTarget_InteractionAction";',
            f'item[0] = "{station_name}";',
            f'action = "{action}";',
        )
        if any(block.count(value) != 1 for value in required):
            raise ValueError(f"Start With Automap helper contract drift: {map_key}/{name}")
        if re.search(r"actionFilter|STAT_AUTOMAP|additionalWait|delay|gate", block):
            raise ValueError(f"Start With Automap helper has forbidden gate/stat: {map_key}/{name}")
        if re.findall(r"item\[\d+\]\s*=\s*\"[^\"]+\";", block) != [
            f'item[0] = "{station_name}";'
        ]:
            raise ValueError(f"Start With Automap helper target drift: {map_key}/{name}")
