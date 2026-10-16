"""Project authored enemy identities without granting runtime eligibility."""

import json
from functools import cache
from importlib.resources import files
from pathlib import Path

from .enemy_options import SPECIES_IDS


def load_species_registry(path):
    registry = json.loads(Path(path).read_bytes().decode("utf-8"))
    project_species_profiles(registry)
    project_speed_capabilities(registry)
    return registry


def project_species_profiles(registry):
    if registry.get("schema_version") != 1 or registry.get("qualification") != "authoral_identity_only":
        raise ValueError("Unsupported enemy identity registry")
    entries = registry.get("species", {})
    if set(entries) != set(SPECIES_IDS):
        raise ValueError("Enemy identity registry differs from the option species")
    result = {}
    for species, entry in entries.items():
        if entry.get("status") == "gap":
            if not entry.get("gaps") or entry.get("spawn_type") is not None or entry.get("templates"):
                raise ValueError(f"Ambiguous enemy identity gap: {species}")
            continue
        if entry.get("status") != "qualified":
            raise ValueError(f"Unsupported enemy identity status: {species}")
        masks, inherits, templates = entry.get("source_spawn_types"), entry.get("inherits"), entry.get("templates")
        if not masks or entry.get("spawn_type") not in masks or not inherits or not templates:
            raise ValueError(f"Incomplete enemy identity: {species}")
        if sum(template.get("role") == "preferred" for template in templates) != 1:
            raise ValueError(f"Enemy identity needs one preferred template: {species}")
        preferred = next(template for template in templates if template.get("role") == "preferred")
        if (set(inherits) != {template.get("inherit") for template in templates}
                or set(masks) != {template.get("proof", {}).get("spawn_type") for template in templates}
                or preferred.get("proof", {}).get("spawn_type") != entry["spawn_type"]):
            raise ValueError(f"Enemy template differs from its identity: {species}")
        result[species] = {"spawn_type": entry["spawn_type"], "spawn_types": tuple(masks), "inherits": tuple(inherits)}
    return result


def project_speed_capabilities(registry):
    result = {}
    for species in SPECIES_IDS:
        capability = registry["species"][species].get("speed", {})
        if not isinstance(capability, dict):
            raise ValueError(f"Invalid Speed capability: {species}")
        status = capability.get("status", "unqualified")
        if status not in ("unqualified", "unsupported", "qualified"):
            raise ValueError(f"Invalid Speed qualification: {species}")
        if status != "unqualified" and not capability.get("evidence"):
            raise ValueError(f"Speed qualification needs evidence: {species}")
        result[species] = status
    return result


@cache
def enemy_speed_capabilities():
    path = files(__package__).joinpath("enemy_species.json")
    if not path.is_file():
        path = Path(__file__).resolve().parents[2] / "content/catalog/enemy_species.json"
    registry = json.loads(path.read_bytes().decode("utf-8"))
    project_species_profiles(registry)
    return project_speed_capabilities(registry)


def validate_enemy_speed_percent(species, speed):
    if type(speed) is not int or not 1 <= speed <= 10000:
        raise ValueError("speed_percent must be an integer in 1..10000")
    capabilities = enemy_speed_capabilities()
    if species not in capabilities:
        raise ValueError(f"Unknown Speed species: {species}")
    if speed != 100 and capabilities[species] != "qualified":
        raise ValueError(f"Speed changes are not qualified for {species}: {capabilities[species]}; use 100%")


def speed_capability_notice(labels=None):
    capabilities = enemy_speed_capabilities()
    labels = labels or {}
    supported = [labels.get(species, species.replace("_", " ").title())
                 for species, status in capabilities.items() if status == "qualified"]
    if supported:
        return f"Speed changes affect: {', '.join(supported)}. Other enemies move at normal speed."
    return "Speed is fixed at 100% (normal) in this build. Your chosen speed ranges are saved in your player file."
