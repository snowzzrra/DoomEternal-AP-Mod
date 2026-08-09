"""Runtime-safe Options schema consumption and canonical player YAML output."""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Mapping, cast

import yaml


OPTIONS_SCHEMA_VERSION = 1
SUPPORTED_UI_TYPES = frozenset({"toggle", "choice", "range"})
GAME_NAME = "DOOM Eternal"


def validate_options_schema(document: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(document, Mapping):
        raise ValueError("options schema must be an object")
    if document.get("schema_version") != OPTIONS_SCHEMA_VERSION:
        raise ValueError("unsupported options schema version")
    if document.get("game") != GAME_NAME:
        raise ValueError("options schema game identity mismatch")
    raw_options = document.get("options")
    if not isinstance(raw_options, list) or not raw_options:
        raise ValueError("options schema must contain exposed options")

    keys: set[str] = set()
    options: list[dict[str, Any]] = []
    for raw in raw_options:
        if not isinstance(raw, Mapping):
            raise ValueError("options schema entry must be an object")
        option = dict(raw)
        key = option.get("key")
        if not isinstance(key, str) or re.fullmatch(r"[a-z][a-z0-9_]*", key) is None:
            raise ValueError(f"invalid option key: {key!r}")
        if key in keys:
            raise ValueError(f"duplicate option key: {key}")
        keys.add(key)
        if not isinstance(option.get("display_name"), str) or not option["display_name"].strip():
            raise ValueError(f"option {key} lacks display name")
        if not isinstance(option.get("description"), str):
            raise ValueError(f"option {key} lacks description")
        if not isinstance(option.get("group"), str) or not option["group"].strip():
            raise ValueError(f"option {key} lacks group")
        ui_type = option.get("ui_type")
        if ui_type not in SUPPORTED_UI_TYPES:
            raise ValueError(f"unsupported UI type for {key}: {ui_type!r}")
        if ui_type == "toggle":
            if not isinstance(option.get("default"), bool):
                raise ValueError(f"toggle {key} default must be boolean")
        elif ui_type == "choice":
            choices = option.get("choices")
            if not isinstance(choices, list) or not choices:
                raise ValueError(f"choice {key} lacks choices")
            choice_keys = []
            for choice in choices:
                if not isinstance(choice, Mapping):
                    raise ValueError(f"choice {key} contains malformed entry")
                choice_key = choice.get("key")
                if not isinstance(choice_key, str) or not choice_key:
                    raise ValueError(f"choice {key} contains invalid key")
                if not isinstance(choice.get("label"), str) or not choice["label"]:
                    raise ValueError(f"choice {key} contains invalid label")
                choice_keys.append(choice_key)
            if len(choice_keys) != len(set(choice_keys)):
                raise ValueError(f"choice {key} contains duplicate keys")
            if option.get("default") not in choice_keys:
                raise ValueError(f"choice {key} default is not canonical")
        else:
            minimum = option.get("minimum")
            maximum = option.get("maximum")
            default = option.get("default")
            if any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in (minimum, maximum, default)
            ):
                raise ValueError(f"range {key} bounds/default must be integers")
            minimum = cast(int, minimum)
            maximum = cast(int, maximum)
            default = cast(int, default)
            if minimum > maximum or not minimum <= default <= maximum:
                raise ValueError(f"range {key} default is outside bounds")
            option["minimum"] = minimum
            option["maximum"] = maximum
            option["default"] = default
        options.append(option)

    excluded = document.get("excluded_options", [])
    if not isinstance(excluded, list) or any(
        not isinstance(entry, Mapping)
        or not isinstance(entry.get("key"), str)
        or not isinstance(entry.get("reason"), str)
        for entry in excluded
    ):
        raise ValueError("excluded option registry is malformed")
    normalized = dict(document)
    normalized["options"] = options
    normalized["excluded_options"] = [dict(entry) for entry in excluded]
    return normalized


def load_options_schema(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"could not load options schema: {error}") from error
    return validate_options_schema(document)


def default_option_values(schema: Mapping[str, Any]) -> dict[str, Any]:
    validated = validate_options_schema(schema)
    return {option["key"]: option["default"] for option in validated["options"]}


def validate_option_values(
    schema: Mapping[str, Any], values: Mapping[str, Any]
) -> dict[str, bool | int | str]:
    validated = validate_options_schema(schema)
    if not isinstance(values, Mapping):
        raise ValueError("option values must be an object")
    expected = {option["key"] for option in validated["options"]}
    if set(values) != expected:
        missing = sorted(expected - set(values))
        extra = sorted(set(values) - expected)
        raise ValueError(f"option value keys mismatch; missing={missing} extra={extra}")
    result: dict[str, bool | int | str] = {}
    for option in validated["options"]:
        key = option["key"]
        value = values[key]
        if option["ui_type"] == "toggle":
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be true or false")
        elif option["ui_type"] == "choice":
            choices = {choice["key"] for choice in option["choices"]}
            if not isinstance(value, str) or value not in choices:
                raise ValueError(f"{key} must be one of {sorted(choices)}")
        else:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{key} must be an integer")
            if not option["minimum"] <= value <= option["maximum"]:
                raise ValueError(
                    f"{key} must be between {option['minimum']} and {option['maximum']}"
                )
        result[key] = value
    return result


def validate_player_name(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("player name must be text")
    name = value.strip()
    if not name:
        raise ValueError("player name is required")
    if len(name) > 16:
        raise ValueError("player name must be at most 16 characters")
    if name == "Archipelago":
        raise ValueError('player name cannot be "Archipelago"')
    if any(ord(character) < 32 for character in name):
        raise ValueError("player name contains a control character")
    return name


def player_yaml_document(
    schema: Mapping[str, Any], player_name: str, values: Mapping[str, Any]
) -> dict[str, Any]:
    validated_schema = validate_options_schema(schema)
    name = validate_player_name(player_name)
    options = validate_option_values(validated_schema, values)
    return {
        "name": name,
        "description": "DOOM Eternal player options",
        "game": GAME_NAME,
        GAME_NAME: options,
    }


def dump_player_yaml(
    schema: Mapping[str, Any], player_name: str, values: Mapping[str, Any]
) -> str:
    document = player_yaml_document(schema, player_name, values)
    return yaml.safe_dump(
        document,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )


def save_player_yaml(
    path: Path,
    schema: Mapping[str, Any],
    player_name: str,
    values: Mapping[str, Any],
) -> Path:
    destination = path.expanduser()
    if destination.suffix.lower() not in {".yaml", ".yml"}:
        destination = destination.with_suffix(".yaml")
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            dump_player_yaml(schema, player_name, values),
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return destination


def suggested_yaml_filename(player_name: str) -> str:
    name = validate_player_name(player_name)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "Player"
    return f"{safe}.yaml"
