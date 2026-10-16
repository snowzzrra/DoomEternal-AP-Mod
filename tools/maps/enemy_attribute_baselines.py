"""Resolve inherited attribute bases for source-qualified enemy templates."""

import copy
import hashlib
import json
from pathlib import Path

from tools.maps.encounter_catalog import entity_records


_CATALOG = Path(__file__).resolve().parents[2] / "content/catalog/enemy_attribute_baselines.json"


def resolve_enemy_attribute_baselines(source_block, *, catalog=None):
    """Return isolated authored values and SHA-qualified native input defaults."""
    if catalog is None:
        catalog = json.loads(_CATALOG.read_text(encoding="utf-8"))
    if catalog.get("schema_version") != 1 or catalog.get("qualification") != "offline_native_defaults":
        raise ValueError("Unsupported enemy attribute baseline catalog")
    source_sha = hashlib.sha256(source_block.strip().encode("utf-8")).hexdigest()
    template = catalog["templates"].get(source_sha)
    if template is None:
        raise ValueError("Unqualified enemy attribute source SHA-256")
    records = list(entity_records(source_block, include_names={template["entity"]}))
    if (len(records) != 1 or records[0]["class"] != "idAI2"
            or records[0]["name"] != template["entity"] or records[0]["inherit"] != template["inherit"]):
        raise ValueError("Enemy attribute source identity mismatch")
    bases = {
        "aiEditable": {"damageOutModifiers": catalog["native"]["damage_out_modifiers"]["value"]},
        "renderModelInfo": {"scale": template["scale"]},
    }
    health = records[0]["edit"].get("aiHealth")
    if "hitpoint_input" in template:
        health = health or {}
        components = health.setdefault("components", {})
        components["components[0]"] = {
            **template["hitpoint_input"], **components.get("components[0]", {}),
        }
    if health is not None:
        bases["aiHealth"] = health
    return copy.deepcopy(bases)
