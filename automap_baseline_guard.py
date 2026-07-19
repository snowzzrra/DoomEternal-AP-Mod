"""Static guard for the separate, targetless Automap marker owners."""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

from ap_map_generator import extract_target_names, find_entity_block_bounds, generate_map


ROOT = Path(__file__).resolve().parent
PROTOTYPE_ENTITIES = {
    "mech_street_pickup_collectible_toys_doomguy_1",
    "mech_street_progress_mod_bot_1_e1m1",
}


def _scalar(block: str, field: str) -> str | None:
    match = re.search(rf'\b{re.escape(field)}\s*=\s*"([^"]+)";', block)
    return match.group(1) if match else None


def _position(block: str) -> tuple[float, float, float] | None:
    position = re.search(r'spawnPosition\s*=\s*\{([^}]*)\}', block)
    if not position:
        return None
    values = []
    for axis in ("x", "y", "z"):
        match = re.search(rf'\b{axis}\s*=\s*([-+0-9.eE]+);', position.group(1))
        values.append(float(match.group(1)) if match else 0.0)
    return tuple(values)


def _expected_decl(source_block: str) -> str:
    return _scalar(source_block, "automapPropertiesDecl") or "default"


def _assert_close(actual, expected, context: str) -> None:
    if actual is None or any(abs(a - e) > 0.001 for a, e in zip(actual, expected)):
        raise ValueError(f"Automap helper position drift: {context}: {actual} != {expected}")


def assert_separate_automap_helper_guard() -> int:
    """Verify all physical checks own exactly one retained-schema marker helper."""
    sources = json.loads((ROOT / "data" / "map_sources.json").read_text())["maps"]
    items = json.loads((ROOT / "data" / "items.json").read_text())
    checked = 0
    with tempfile.TemporaryDirectory() as directory:
        output_root = Path(directory)
        for map_key, source in sources.items():
            if not source.get("enabled", True):
                continue
            source_text = (ROOT / "vanillamaps" / source["source_file"]).read_text()
            config = json.loads((ROOT / source["level_config"]).read_text())
            output = output_root / f"{map_key}.entities"
            generate_map(
                ROOT / "vanillamaps" / source["source_file"], output,
                ROOT / source["level_config"], output_root / f"{map_key}.json", items,
            )
            generated = output.read_text()
            helper_names = re.findall(r'entityDef\s+(ap_automap_location_\d+)\s*\{', generated)
            expected_names = {
                f"ap_automap_location_{location_id}"
                for location_id in config["entities"].values()
            }
            if len(helper_names) != len(set(helper_names)) or set(helper_names) != expected_names:
                raise ValueError(
                    f"Automap helper set drift in {map_key}: "
                    f"{sorted(helper_names)} != {sorted(expected_names)}"
                )
            for ap_check, location_id in config["entities"].items():
                entity_name = ap_check.removeprefix("AP_CHECK_").lower()
                source_bounds = find_entity_block_bounds(source_text, entity_name)
                helper_bounds = find_entity_block_bounds(
                    generated, f"ap_automap_location_{location_id}"
                )
                if source_bounds is None or helper_bounds is None:
                    raise ValueError(f"Automap helper source/owner missing: {map_key}/{location_id}")
                source_block = source_text[source_bounds[0]:source_bounds[1]]
                helper = generated[helper_bounds[0]:helper_bounds[1]]
                if _scalar(helper, "class") != "idInfo" or _scalar(helper, "inherit") != "info/null":
                    raise ValueError(f"Automap helper type drift: {map_key}/{location_id}")
                if extract_target_names(helper):
                    raise ValueError(f"Automap helper has targets: {map_key}/{location_id}")
                for forbidden in (
                    "renderModelInfo", "useableComponentDecl", "itemList",
                    "currencyList", "inventory", "bindInfo",
                ):
                    if forbidden in helper:
                        raise ValueError(f"Automap helper retains {forbidden}: {map_key}/{location_id}")
                if _scalar(helper, "automapPropertiesDecl") != _expected_decl(source_block):
                    raise ValueError(f"Automap helper decl drift: {map_key}/{location_id}")
                _assert_close(_position(helper), _position(source_block), f"{map_key}/{location_id}")

                generated_bounds = find_entity_block_bounds(generated, entity_name)
                if generated_bounds is not None:
                    location = generated[generated_bounds[0]:generated_bounds[1]]
                    if _scalar(location, "class") == "idTrigger" and "automapPropertiesDecl" in location:
                        raise ValueError(f"AP trigger owns marker: {map_key}/{location_id}")
                    if entity_name not in PROTOTYPE_ENTITIES and "renderModelInfo" not in location:
                        visual_name = config.get("target_policies", {}).get(
                            entity_name, {}
                        ).get("independent_visual", {}).get("entity_name")
                        visual_bounds = (
                            find_entity_block_bounds(generated, visual_name)
                            if visual_name else None
                        )
                        visual = generated[visual_bounds[0]:visual_bounds[1]] if visual_bounds else ""
                        if "renderModelInfo" not in visual:
                            raise ValueError(
                                f"Physical visual disappeared: {map_key}/{location_id}"
                            )
                checked += 1
    return checked
