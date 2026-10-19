"""Read native entity properties and encounter lists without discarding fields."""

import hashlib
import json
import re

from tools.maps.ap_map_generator import find_matching_brace


ENTITY_START = re.compile(r"(?m)^entity\s*\{")
ENTITY_HEADER = re.compile(r'entityDef\s+(\S+)\s*\{\s*inherit\s*=\s*"([^"]*)";\s*class\s*=\s*"([^"]+)";')
MEMBER = re.compile(r'\s*([\w.\[\]-]+)\s*=\s*(\{|"(?:\\.|[^"\\])*"|[^;{}]+;)')
SPAWN_CLASSES = {"idTarget_Spawn", "idTarget_Spawn_Parent", "idTargetSpawnGroup"}


def parse_object(text):
    """Read ordered entity properties without flattening event or argument lists."""
    result = {}
    offset = 0
    while offset < len(text):
        match = MEMBER.match(text, offset)
        if not match:
            if text[offset:].strip():
                raise ValueError(f"Unsupported entity property near {text[offset:offset + 80]!r}")
            break
        name, value = match.groups()
        if name in result:
            raise ValueError(f"Duplicate entity property: {name}")
        if value == "{":
            end = find_matching_brace(text, match.end() - 1)
            result[name] = parse_object(text[match.end():end - 1])
            offset = end
        elif value.startswith('"'):
            result[name] = json.loads(value)
            offset = match.end()
        else:
            value = value.rstrip(";").strip()
            if value in {"true", "false", "NULL"}:
                result[name] = {"true": True, "false": False, "NULL": None}[value]
            else:
                try:
                    result[name] = int(value)
                except ValueError:
                    result[name] = float(value)
            offset = match.end()
        while offset < len(text) and (text[offset].isspace() or text[offset] == ";"):
            offset += 1
    return result



def list_items(value):
    if not value:
        return []
    items = [value[f"item[{index}]"] for index in range(value["num"])]
    if len(value) != len(items) + 1:
        raise ValueError("Entity list has properties outside its declared item count")
    return items



def entity_records(text, *, include_names=None):
    starts = [match.start() for match in ENTITY_START.finditer(text)]
    source_line = 1 + text[:starts[0]].count("\n") if starts else 1
    for start, end in zip(starts, starts[1:] + [len(text)]):
        block = text[start:end]
        block_line = source_line
        source_line += block.count("\n")
        header = ENTITY_HEADER.search(block)
        if not header:
            raise ValueError(f"Entity header not understood at offset {start}")
        name, inherit, class_name = header.groups()
        selected = name in include_names if include_names is not None else (
            class_name in SPAWN_CLASSES | {"idAI2", "idEncounterManager", "idArchvileTemplate"}
        )
        if not selected:
            continue
        edit = re.search(r"\bedit\s*=\s*\{", block)
        if not edit:
            raise ValueError(f"Entity edit block missing: {name}")
        edit_end = find_matching_brace(block, edit.end() - 1)
        layers = re.search(r"\blayers\s*\{([^}]+)\}", block[:header.start()])
        yield {
            "name": name,
            "inherit": inherit,
            "class": class_name,
            "layers": re.findall(r'"([^"]+)"', layers.group(1)) if layers else [],
            "source_line": block_line,
            "sha256": hashlib.sha256(block.strip().encode("utf-8")).hexdigest(),
            "edit": parse_object(block[edit.end():edit_end - 1]),
        }



def walk_values(value, path=""):
    if isinstance(value, dict):
        for name, child in value.items():
            yield from walk_values(child, f"{path}.{name}" if path else name)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk_values(child, f"{path}[{index}]")
    else:
        yield path, value



def text_sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
