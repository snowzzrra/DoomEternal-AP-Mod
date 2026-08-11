#!/usr/bin/env python3
"""Compile launcher option metadata from current DOOM Eternal APWorld classes."""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_AP_ROOT = ROOT.parent / "Archipelago"
DEFAULT_OUTPUT = ROOT / "data" / "options_schema.json"


def _load_apworld(ap_root: Path):
    if not (ap_root / "worlds/doometernal/options.py").is_file():
        raise ValueError(f"invalid Archipelago source: {ap_root}")
    os.environ.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")
    sys.path.insert(0, str(ap_root))
    try:
        from Options import Choice, NamedRange, Range, Toggle  # type: ignore[import-not-found]
        from worlds.doometernal import DoomEternalWorld  # type: ignore[import-not-found]
    finally:
        try:
            sys.path.remove(str(ap_root))
        except ValueError:
            pass
    return DoomEternalWorld, Toggle, Choice, Range, NamedRange


def compile_schema(ap_root: Path) -> dict[str, Any]:
    world, toggle_type, choice_type, range_type, named_range_type = _load_apworld(ap_root)
    options = []
    excluded = []
    for key, option_type in world.options_dataclass.type_hints.items():
        base = {
            "key": key,
            "display_name": getattr(option_type, "display_name", key),
            "description": inspect.cleandoc(option_type.__doc__ or ""),
            "group": "Game Options",
            "source_class": option_type.__name__,
        }
        if issubclass(option_type, toggle_type):
            options.append({**base, "ui_type": "toggle", "default": bool(option_type.default)})
            continue
        if issubclass(option_type, choice_type):
            choices = [
                {
                    "key": option_type.name_lookup[value],
                    "label": option_type.get_option_name(value),
                    "value": value,
                }
                for value in sorted(option_type.name_lookup)
            ]
            options.append(
                {
                    **base,
                    "ui_type": "choice",
                    "default": option_type.name_lookup[option_type.default],
                    "choices": choices,
                }
            )
            continue
        if issubclass(option_type, named_range_type):
            special_names = getattr(option_type, "special_range_names", {})
            if not isinstance(special_names, dict):
                raise ValueError(f"invalid named range values for {key}")
            special_values = [
                {"key": name, "label": name.replace("_", " ").title()}
                for name in sorted(special_names)
            ]
            default = int(option_type.default)
            default = next(
                (
                    special["key"]
                    for special in special_values
                    if special_names[special["key"]] == default
                ),
                default,
            )
            named_range = {
                **base,
                "ui_type": "named_range",
                "default": default,
                "minimum": int(option_type.range_start),
                "maximum": int(option_type.range_end),
                "special_values": special_values,
            }
            if key == "praetor_suit_upgrades_in_pool":
                named_range["maximum_label"] = "All"
            options.append(named_range)
            continue
        if issubclass(option_type, range_type):
            options.append(
                {
                    **base,
                    "ui_type": "range",
                    "default": int(option_type.default),
                    "minimum": int(option_type.range_start),
                    "maximum": int(option_type.range_end),
                }
            )
            continue
        if option_type.__module__ != "Options":
            raise ValueError(
                f"unsupported DOOM Eternal option class: {key} ({option_type.__name__})"
            )
        excluded.append(
            {
                "key": key,
                "source_class": option_type.__name__,
                "reason": "complex_common_option_not_exposed_in_scalar_foundation",
            }
        )

    source = ap_root / "worlds/doometernal/options.py"
    document = {
        "schema_version": 2,
        "game": world.game,
        "source": "Archipelago/worlds/doometernal/options.py",
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "options": options,
        "excluded_options": excluded,
    }
    sys.path.insert(0, str(ROOT))
    try:
        from options_foundation import validate_options_schema

        return validate_options_schema(document)
    finally:
        sys.path.remove(str(ROOT))


def serialized(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archipelago-root", type=Path, default=DEFAULT_AP_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    expected = serialized(compile_schema(args.archipelago_root.resolve()))
    if args.check:
        try:
            actual = args.output.read_text(encoding="utf-8")
        except OSError as error:
            raise SystemExit(f"options schema missing: {error}") from error
        if actual != expected:
            raise SystemExit("data/options_schema.json is stale; regenerate projection")
        print("options schema: up-to-date")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(expected, encoding="utf-8", newline="\n")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
