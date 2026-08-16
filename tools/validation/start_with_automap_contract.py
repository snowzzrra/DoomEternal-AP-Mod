"""Focused structural contract audit for Start With Automap transforms."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from tools.maps.ap_map_generator import (
    _start_with_automap_station_block,
    apply_composable_map_transforms,
    extract_target_names,
    find_matching_brace,
    load_start_with_automap_positions,
)


GRAPH_RESOURCE = (
    "packaging/mod_assets/gameresources/generated/decls/"
    "interaction/interactables/ap_start_with_automap_station.decl"
)
TRIGGER_RESOURCE = (
    "packaging/mod_assets/gameresources/generated/decls/"
    "entitydef/trigger/interact/ap_start_with_automap.decl"
)

# Native graph with only trigger_To_used's IA_TOUCHING normalized to IA_USE_SUCCEED.
NATIVE_GRAPH_NORMALIZED_SHA256 = "1d7fb0326be388d98dd9f11781ab8dcf2c84dacb79b0ae8df397ca3ac13c8f9a"


def _property_block(text: str, property_name: str) -> str | None:
    match = re.search(rf"\b{re.escape(property_name)}\s*=\s*\{{", text)
    if match is None:
        return None
    opening = text.find("{", match.start())
    return text[match.start() : find_matching_brace(text, opening)]


def _entity_names(text: str) -> list[str]:
    return re.findall(r"\bentityDef\s+([^\s{]+)\s*\{", text)


def _normalize_native_success_transition(graph: str) -> str:
    match = re.search(r'name = "trigger_To_used";', graph)
    if match is None:
        raise ValueError("Automap graph lacks trigger_To_used")
    next_link = graph.find("\n\t\t\titem[", match.end())
    if next_link == -1:
        next_link = len(graph)
    link = graph[match.start() : next_link]
    if link.count('item[0] = "IA_TOUCHING";') != 1:
        raise ValueError("Automap success transition must require IA_TOUCHING")
    normalized_link = link.replace(
        'item[0] = "IA_TOUCHING";', 'item[0] = "IA_USE_SUCCEED";'
    )
    return graph[: match.start()] + normalized_link + graph[next_link:]


def _audit_graph(graph: str) -> None:
    if "IA_USE_SUCCEED" in graph:
        raise ValueError("AP Automap graph retains IA_USE_SUCCEED")
    if graph.count('name = "activate_to_use";') != 1:
        raise ValueError("Automap graph activate_to_use subgraph drifted")
    if graph.count('name = "usable";') != 1 or 'num = 3;' not in graph:
        raise ValueError("Automap graph usable subgraph drifted")
    for node in ("idle", "used", "trigger"):
        if graph.count(f'name = "{node}";') != 1:
            raise ValueError(f"Automap graph node missing: {node}")
    expected_links = {
        "activate_To_idle": ["IA_ACTIVATE_ANY"],
        "trigger_To_used": ["IA_TOUCHING"],
        "trigger_To_idle": ["IA_UNTOUCH"],
        "idle_To_trigger": ["IA_TOUCH", "IA_TOUCHING"],
    }
    for name, actions in expected_links.items():
        match = re.search(rf'name = "{name}";', graph)
        if match is None:
            raise ValueError(f"Automap graph link missing: {name}")
        end = graph.find("\n\t\t\titem[", match.end())
        link = graph[match.start() : end if end != -1 else len(graph)]
        actual = re.findall(r'item\[\d+\] = "(IA_[A-Z_]+)";', link)
        if actual != actions:
            raise ValueError(f"Automap graph actions drifted: {name}={actual}")
    if hashlib.sha256(_normalize_native_success_transition(graph).encode()).hexdigest() != NATIVE_GRAPH_NORMALIZED_SHA256:
        raise ValueError("AP Automap graph differs from native graph beyond success action")


def audit_start_with_automap(root: Path) -> None:
    sources = json.loads(
        (root / "data/map_sources.json").read_text(encoding="utf-8")
    )["maps"]
    positions = load_start_with_automap_positions()
    if len(positions) != 13 or set(positions) - set(sources):
        raise ValueError("Start With Automap must cover exactly 13 campaign maps")

    graph = (root / GRAPH_RESOURCE).read_text(encoding="utf-8")
    _audit_graph(graph)
    trigger = (root / TRIGGER_RESOURCE).read_text(encoding="utf-8")
    if (
        "idTrigger" not in trigger
        or "showInRenderMode = false;" not in trigger
        or "ignoreUnfixCollisionOffsetBug = true;" not in trigger
        or "triggerOnce = false;" not in trigger
        or "x = 1.1;" not in trigger
    ):
        raise ValueError("Start With Automap trigger visibility semantics drifted")
    sizes = {axis: re.search(rf"\b{axis}\s*=\s*([0-9.]+);", trigger) for axis in "xyz"}
    if any(match is None or float(match.group(1)) < 8 for match in sizes.values()):
        raise ValueError("Start With Automap trigger is not large enough")

    for map_key in sorted(positions):
        source_path = root / "vanillamaps" / sources[map_key]["source_file"]
        source = source_path.read_text(encoding="utf-8")
        source_name, source_bounds = _start_with_automap_station_block(source)
        source_block = source[source_bounds[0] : source_bounds[1]]
        off = apply_composable_map_transforms(source, map_key, {"start_with_automap": False})
        if off != source:
            raise ValueError(f"Start With Automap OFF changed map: {map_key}")

        on = apply_composable_map_transforms(source, map_key, {"start_with_automap": True})
        on_name, on_bounds = _start_with_automap_station_block(on)
        on_block = on[on_bounds[0] : on_bounds[1]]
        if on_name != source_name or _entity_names(on) != _entity_names(source):
            raise ValueError(f"Automap entity identity/helper drift: {map_key}")
        if extract_target_names(on_block) != extract_target_names(source_block):
            raise ValueError(f"Automap native targets drifted: {map_key}")
        for field in (
            'class = "idInteractable_Automap";',
            'saveType = "SGS_GAME_DATA";',
            'automapPropertiesDecl = "automap_station";',
            'useStat = "STAT_AUTOMAP";',
        ):
            if on_block.count(field) != source_block.count(field) or on_block.count(field) != 1:
                raise ValueError(f"Automap native field drifted: {map_key}/{field}")
        for property_name in ("stateData", "spawnOrientation"):
            if _property_block(on_block, property_name) != _property_block(source_block, property_name):
                raise ValueError(f"Automap native {property_name} drifted: {map_key}")
        source_position = _property_block(source_block, "spawnPosition")
        on_position = _property_block(on_block, "spawnPosition")
        if on_position is None:
            raise ValueError(f"Automap transformed position missing: {map_key}")
        if on_position == source_position or (source_position and source_position in on_block):
            raise ValueError(f"Automap original position remains ON: {map_key}")
        if any(f"{axis} = {value};" not in on_position for axis, value in zip("xyz", positions[map_key])):
            raise ValueError(f"Automap deterministic position drifted: {map_key}")
        if 'interactionGraph = "interactables/ap_start_with_automap_station";' not in on_block:
            raise ValueError(f"Automap AP graph reference missing: {map_key}")
        if 'triggerDef = "trigger/interact/ap_start_with_automap";' not in on_block:
            raise ValueError(f"Automap AP trigger reference missing: {map_key}")


if __name__ == "__main__":
    audit_start_with_automap(Path(__file__).resolve().parents[2])
