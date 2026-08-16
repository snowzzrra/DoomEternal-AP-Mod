"""Cheap physical-option room compiler contract audit."""

from __future__ import annotations

import json
import tempfile
from itertools import product
from pathlib import Path

from doom_eap.content.physical_options import PHYSICAL_OPTIONS, physical_location_ids
from doom_eap.launcher.launcher_core import ModCompiler, SeedManifest


def audit_physical_option_rooms(root: Path) -> None:
    """Compile all physical states and require manifest/map membership agreement."""
    compiler = ModCompiler(root)
    sources = json.loads((root / "data" / "map_sources.json").read_text(encoding="utf-8"))["maps"]
    options_by_map: dict[str, tuple[str, ...]] = {}
    for key, spec in PHYSICAL_OPTIONS.items():
        options_by_map.setdefault(str(spec["map_key"]), tuple())
        options_by_map[str(spec["map_key"])] += (key,)

    with tempfile.TemporaryDirectory(prefix="doom-ap-physical-options-") as temporary:
        output_root = Path(temporary)
        for map_key, option_keys in options_by_map.items():
            source = sources[map_key]
            for values in product((False, True), repeat=len(option_keys)):
                options = {key: False for key in PHYSICAL_OPTIONS}
                options.update(dict(zip(option_keys, values)))
                manifest = SeedManifest.create(
                    seed_name=f"physical-contract-{map_key}",
                    team=0,
                    slot=1,
                    options=options,
                    active_location_ids=compiler.active_location_ids(options),
                )
                output = output_root / f"{map_key}-{'-'.join(str(int(value)) for value in values)}.entities"
                compiler.compile_map(
                    manifest,
                    root / "vanillamaps" / source["source_file"],
                    output,
                    map_key,
                )
                text = output.read_text(encoding="utf-8")
                active = set(manifest.active_location_ids)
                expected_physical = physical_location_ids(options)
                if not expected_physical <= active:
                    raise ValueError(
                        f"physical option manifest misses active locations: {map_key} {options}"
                    )
                for key, spec in PHYSICAL_OPTIONS.items():
                    if spec["map_key"] != map_key:
                        continue
                    location_id = int(spec["location_id"])
                    ap_entity = str(spec["entity"])
                    ap_present = (
                        f"entityDef {ap_entity}" in text
                        and f"AP_CHECK_EVENT_{location_id}" in text
                    )
                    enabled = bool(options[key])
                    if enabled != (location_id in active) or enabled != ap_present:
                        raise ValueError(
                            f"physical option map/manifest disagreement: {key}={enabled}"
                        )
                    if not enabled and f"entityDef {spec['vanilla_entity']}" not in text:
                        raise ValueError(f"physical option vanilla carrier missing: {key}")
