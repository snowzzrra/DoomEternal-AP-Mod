"""Room-owned Automap startup projection."""

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


def start_with_automap_entity_name(map_key: str) -> str:
    if map_key not in SUPPORTED_START_WITH_AUTOMAP_MAPS:
        raise ValueError(f"unsupported Start With Automap map: {map_key}")
    return f"{START_WITH_AUTOMAP_ENTITY_PREFIX}{map_key}"


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


def _entity_bounds(text: str, entity_name: str) -> tuple[int, int] | None:
    match = re.search(rf"entityDef\s+{re.escape(entity_name)}\s*\{{", text)
    if match is None:
        return None
    start = text.rfind("entity {", 0, match.start())
    if start < 0:
        raise ValueError(f"entity wrapper missing for {entity_name}")
    opening = text.find("{", start)
    return start, _matching_brace(text, opening)


def _validate_native_automap_station(text: str) -> None:
    matches = list(re.finditer(r'class\s*=\s*"idInteractable_Automap";', text))
    if len(matches) != 1:
        raise ValueError(
            "Start With Automap requires exactly one native idInteractable_Automap"
        )
    marker = matches[0]
    start = text.rfind("entity {", 0, marker.start())
    if start < 0:
        raise ValueError("native Automap station wrapper missing")
    opening = text.find("{", start)
    end = _matching_brace(text, opening)
    block = text[start:end]
    required = (
        'inherit = "interact/automap";',
        'automapPropertiesDecl = "automap_station";',
        'saveType = "SGS_GAME_DATA";',
        'useStat = "STAT_AUTOMAP";',
    )
    if any(block.count(value) != 1 for value in required):
        raise ValueError("native Automap station contract drifted")
    if re.search(r"\btargets\s*=\s*\{", block):
        raise ValueError("native Automap station unexpectedly owns a target chain")
    return None


def _remove_native_automap_station(text: str) -> str:
    match = re.search(r'class\s*=\s*"idInteractable_Automap";', text)
    if match is None:
        raise ValueError("native Automap station missing")
    start = text.rfind("entity {", 0, match.start())
    if start < 0:
        raise ValueError("native Automap station wrapper missing")
    end = _matching_brace(text, text.find("{", start))
    return text[:start] + text[end:]


def _append_target(block: str, target: str) -> str:
    targets = re.search(
        r"targets\s*=\s*\{\s*num\s*=\s*(\d+);(?P<body>.*?)\s*\}",
        block,
        re.DOTALL,
    )
    if targets:
        names = re.findall(r'item\[\d+\]\s*=\s*"([^"]+)";', targets.group("body"))
        if target in names:
            return block
        body = targets.group("body")
        count = int(targets.group(1))
        replacement = (
            f'targets = {{\n{body}\n'
            f'\t\t\t\titem[{count}] = "{target}";\n\t\t\t}}'
        )
        return block[:targets.start()] + replacement + block[targets.end():]

    edit = re.search(r"edit\s*=\s*\{", block)
    if edit is None:
        raise ValueError("idPlayerStart lacks edit block")
    insertion = (
        '\n\t\t\ttargets = {\n'
        f'\t\t\t\tnum = 1;\n\t\t\t\titem[0] = "{target}";\n'
        "\t\t\t}"
    )
    return block[:edit.end()] + insertion + block[edit.end():]


def _attach_to_player_starts(content: str, target: str) -> tuple[str, int]:
    starts = list(re.finditer(r'class\s*=\s*"idPlayerStart";', content))
    replacements: list[tuple[int, int, str]] = []
    for match in starts:
        start = content.rfind("entity {", 0, match.start())
        if start < 0:
            raise ValueError("idPlayerStart wrapper missing")
        end = _matching_brace(content, content.find("{", start))
        block = content[start:end]
        replacements.append((start, end, _append_target(block, target)))
    if not replacements:
        raise ValueError("supported map has no idPlayerStart entity")
    for start, end, block in reversed(replacements):
        content = content[:start] + block + content[end:]
    return content, len(replacements)


def project_start_with_automap(content: str, map_key: str, enabled: bool) -> str:
    """Write native Automap ownership from every player start."""
    if not enabled:
        return content
    entity_name = start_with_automap_entity_name(map_key)
    _validate_native_automap_station(content)
    if f"entityDef {entity_name}" in content:
        raise ValueError(f"Start With Automap entity already exists: {entity_name}")
    writer = f'''entity {{
	entityDef {entity_name} {{
		class = "idTarget_PlayerStatModifier";
		expandInheritance = false;
		poolCount = 0;
		poolGranularity = 2;
		networkReplicated = false;
		disableAIPooling = false;
		edit = {{
			gameStat = "STAT_AUTOMAP";
			value = 1;
		}}
	}}
}}'''
    projected, _ = _attach_to_player_starts(content, entity_name)
    projected = _remove_native_automap_station(projected)
    return projected + "\n" + writer


def validate_start_with_automap_projection(content: str, map_key: str) -> None:
    """Validate AP-owned native Automap writer in generated content."""
    entity_name = start_with_automap_entity_name(map_key)
    bounds = _entity_bounds(content, entity_name)
    if bounds is None:
        raise ValueError(f"Start With Automap entity missing: {entity_name}")
    block = content[bounds[0]:bounds[1]]
    for required in (
        'class = "idTarget_PlayerStatModifier";',
        'gameStat = "STAT_AUTOMAP";',
        'value = 1;',
    ):
        if block.count(required) != 1:
            raise ValueError(f"Start With Automap entity contract drift: {required}")
    if re.search(r"\btargets\s*=\s*\{", block):
        raise ValueError("Start With Automap writer owns an unexpected target chain")
    if re.search(r'class\s*=\s*"idInteractable_Automap";', content):
        raise ValueError("Start With Automap projection retained native station")
