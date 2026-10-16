"""Apply qualified enemy attributes to one private idAI2 entity definition."""

import json
import math
import re

from tools.maps.ap_map_generator import find_matching_brace
from tools.maps.encounter_catalog import ENTITY_HEADER, MEMBER, list_items, parse_object


_MISSING = object()
_CLONE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*__ap_v070_s([0-9]{5})\Z")
_AI_CLONE_NAME = re.compile(r"ap_v070_ai_[A-Za-z0-9_]+__ap_v070_s([0-9]{5})\Z")


def _get(properties, path):
    for name in path:
        if not isinstance(properties, dict) or name not in properties:
            return _MISSING
        properties = properties[name]
    return properties


def _member(body, name):
    offset = 0
    while offset < len(body):
        match = MEMBER.match(body, offset)
        if not match:
            return None
        value = match.group(2)
        end = find_matching_brace(body, match.end() - 1) if value == "{" else match.end()
        if body[end:end + 1] == ";":
            end += 1
        if match.group(1) == name:
            return match.start(2), end, value == "{"
        offset = end
    return None


def _render(value, level):
    if isinstance(value, dict):
        fields = [
            "\t" * (level + 1) + name + " = " + _render(item, level + 1)
            + ("" if isinstance(item, dict) else ";")
            for name, item in value.items()
        ]
        return "{\n" + "\n".join(fields) + "\n" + "\t" * level + "}"
    if isinstance(value, float):
        return format(value, ".9g")
    return "NULL" if value is None else json.dumps(value, ensure_ascii=False)


def _set(body, path, value, level=2):
    member = _member(body, path[0])
    if member is None:
        for name in reversed(path[1:]):
            value = {name: value}
        end = len(body.rstrip())
        field = "\t" * level + path[0] + " = " + _render(value, level)
        return body[:end] + "\n" + field + ("" if isinstance(value, dict) else ";") + "\n" + body[end:]
    start, end, is_object = member
    if len(path) == 1:
        return body[:start] + _render(value, level) + ("" if isinstance(value, dict) else ";") + body[end:]
    if not is_object:
        raise ValueError(f"Attribute parent is not an object: {path[0]}")
    close = find_matching_brace(body, start) - 1
    nested = _set(body[start + 1:close], path[1:], value, level + 1)
    return body[:start + 1] + nested + body[close:]


def _scaled(value, percent, field, *, zero=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Unqualified numeric base for {field}")
    if value < 0 or (not zero and value == 0):
        raise ValueError(f"Unqualified numeric base for {field}")
    result = value * (percent / 100)
    if not math.isfinite(result) or result > 3.4028234663852886e38:
        raise ValueError(f"Attribute exceeds native float range: {field}")
    return result


def materialize_enemy_attributes(
    source_block,
    clone_name,
    *,
    health_percent=100,
    damage_percent=100,
    size_percent=100,
    base_properties=None,
):
    """Clone one idAI2; base_properties supplies qualified inherited edit values.

    Missing numerical bases are refused. Family eligibility and source selection
    belong to the caller; this helper does not qualify navigation or attachments.
    """
    clone = _AI_CLONE_NAME.fullmatch(clone_name)
    if not clone or len(clone_name) >= 512 or not 1 <= int(clone.group(1)) <= 10000:
        raise ValueError("Private AI clone needs ap_v070_ai_<unique>__ap_v070_sNNNNN")
    for name, value in (("health", health_percent), ("damage", damage_percent), ("size", size_percent)):
        if type(value) is not int or not 1 <= value <= 10000:
            raise ValueError(f"{name}_percent must be an integer in 1..10000")
    headers = list(ENTITY_HEADER.finditer(source_block))
    if len(headers) != 1 or headers[0].group(3) != "idAI2":
        raise ValueError("Enemy attributes require exactly one idAI2 entity definition")
    header = headers[0]
    edit_match = re.search(r"\bedit\s*=\s*\{", source_block[header.end():])
    if edit_match is None:
        raise ValueError("Enemy entity definition has no edit object")
    begin = header.end() + edit_match.end() - 1
    end = find_matching_brace(source_block, begin)
    body = source_block[begin + 1:end - 1]
    properties = parse_object(body)
    if base_properties is not None and not isinstance(base_properties, dict):
        raise ValueError("base_properties must contain qualified edit properties")

    def base(path):
        value = _get(properties, path)
        if value is _MISSING:
            value = _get(base_properties, path)
        if value is _MISSING:
            raise ValueError("Unqualified inherited base: edit." + ".".join(path))
        return value

    if health_percent != 100:
        for field in ("max", "starting"):
            path = ("aiHealth", "components", "components[0]", field)
            value = base(path)
            if isinstance(value, dict):
                if set(value) != {"defaultValue"}:
                    raise ValueError("Unqualified branched health base: " + ".".join(path))
                value = {"defaultValue": _scaled(value["defaultValue"], health_percent,
                    ".".join(path), zero=field == "starting")}
            else:
                value = _scaled(value, health_percent, ".".join(path), zero=field == "starting")
            body = _set(body, path, value)
    if damage_percent != 100:
        path = ("aiEditable", "damageOutModifiers")
        authored = base(path)
        if not isinstance(authored, dict) or type(authored.get("num")) is not int or authored["num"] < 0:
            raise ValueError("Unqualified damageOutModifiers list")
        try:
            modifiers = list_items(authored)
        except (KeyError, ValueError) as error:
            raise ValueError("Unqualified damageOutModifiers list") from error
        scaled = []
        for modifier in modifiers:
            if not isinstance(modifier, dict) or not isinstance(modifier.get("damageSource"), str):
                raise ValueError("Unqualified damageOutModifiers entry")
            item = dict(modifier)
            item["damageModifier"] = _scaled(item.get("damageModifier"), damage_percent, "damageOutModifiers.damageModifier", zero=True)
            scaled.append(item)
        if not any("DAMAGESRC_ALL" in (token.strip() for token in item["damageSource"].split("|")) for item in scaled):
            scaled.append({"damageSource": "DAMAGESRC_ALL", "damageModifier": damage_percent / 100})
        body = _set(body, path, {"num": len(scaled), **{f"item[{i}]": item for i, item in enumerate(scaled)}})
    if size_percent != 100:
        for axis in ("x", "y", "z"):
            path = ("renderModelInfo", "scale", axis)
            body = _set(body, path, _scaled(base(path), size_percent, ".".join(path)))
    result = source_block[:begin + 1] + body + source_block[end - 1:]
    return result[:header.start(1)] + clone_name + result[header.end(1):]
