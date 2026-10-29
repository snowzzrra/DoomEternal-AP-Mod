"""Mechanical packing and expansion of resolved recipe values."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from itertools import groupby
from types import MappingProxyType
from typing import Mapping

from .slot_data_contract import LimitSet, Recipes, canonical_bytes, catalog_tables, load_slot_catalog

__all__ = ("Body", "ResolvedRow", "ResolvedMap", "ResolvedRecipes", "encode_recipes", "decode_recipes", "recipe_references")


def recipe_references(catalog=None):
    catalog = catalog_tables(load_slot_catalog() if catalog is None else catalog)
    return {'templates': {key: key for key in catalog['templates']},
            'sources': {(row['stage_id'], row['source_id'], row.get('role', 'primary')): ref
                        for ref, row in catalog['sources'].items()},
            **{name: {(row['stage_id'], row[field]): ref for ref, row in catalog[name].items()}
               for name, field in (('finite_sources', 'source_id'), ('timers', 'entity'))},
            **{name: {value: value for value in catalog[name]}
               for name in ('roles', 'spawn_types', 'species')}}


@dataclass(frozen=True)
class Body:
    template_ref: str
    health_percent: int
    damage_percent: int
    speed_percent: int
    size_percent: int
    limits: LimitSet


@dataclass(frozen=True)
class ResolvedRow:
    source_ref: str
    role_ref: str
    spawn_type_ref: str
    bodies: tuple[Body, ...]
    logical_count: int
    maximum_emissions: int
    final_credit_units: int
    renewable: bool
    ratio_numerator: int
    ratio_denominator: int


@dataclass(frozen=True)
class ResolvedMap:
    rows: tuple[ResolvedRow, ...]
    passthrough_sources: tuple[str, ...]
    disabled_sources: tuple[str, ...]


@dataclass(frozen=True)
class ResolvedRecipes:
    enabled: bool
    maps: Mapping[str, ResolvedMap]
    timers: tuple[tuple[str, int], ...]


def _deduplicate(values):
    by_content = {canonical_bytes(value): value for value in values}
    keys = sorted(by_content)
    return [by_content[key] for key in keys], {key: index for index, key in enumerate(keys)}


def _limit_value(limits):
    value = asdict(limits)
    value["special"] = list(value["special"])
    if value["population"] is not None:
        value["population"]["species_caps"] = [list(pair) for pair in value["population"]["species_caps"]]
    return value


def _profile(body):
    return [body.template_ref, body.health_percent, body.damage_percent, body.speed_percent,
            body.size_percent]


def _pattern(row, profile_refs):
    refs = (profile_refs[canonical_bytes(_profile(body))] for body in row.bodies)
    return [[ref, sum(1 for _ in group)] for ref, group in groupby(refs)]


def _encode_row(row, pattern_refs, profile_refs, limit_refs):
    pattern = _pattern(row, profile_refs)
    return [row.source_ref, row.role_ref, row.spawn_type_ref, pattern_refs[canonical_bytes(pattern)],
            row.logical_count, row.maximum_emissions, row.final_credit_units, row.renewable,
            row.ratio_numerator, row.ratio_denominator,
            [limit_refs[canonical_bytes(_limit_value(body.limits))] for body in row.bodies]]


def _encode_map(record, pattern_refs, profile_refs, limit_refs):
    return {"rows": [_encode_row(row, pattern_refs, profile_refs, limit_refs)
                     for row in sorted(record.rows, key=lambda row: row.source_ref)],
            "passthrough_sources": sorted(record.passthrough_sources),
            "disabled_sources": sorted(record.disabled_sources)}


def encode_recipes(resolved: ResolvedRecipes) -> dict:
    """Pack resolved values with content-sorted tables and remapped references."""
    rows = [row for record in resolved.maps.values() for row in record.rows]
    bodies = [body for row in rows for body in row.bodies]
    limits, limit_refs = _deduplicate(_limit_value(body.limits) for body in bodies)
    profiles, profile_refs = _deduplicate(_profile(body) for body in bodies)
    patterns, pattern_refs = _deduplicate(_pattern(row, profile_refs) for row in rows)
    return {"enabled": resolved.enabled, "limit_sets": limits, "profiles": profiles, "patterns": patterns,
            "maps": {stage: _encode_map(resolved.maps[stage], pattern_refs, profile_refs, limit_refs)
                     for stage in sorted(resolved.maps)},
            "timers": [list(pair) for pair in sorted(resolved.timers)]}


def _decode_row(row, recipes):
    source, role, spawn, pattern, logical, maximum, credit, renewable, numerator, denominator, limits = row
    refs = (ref for ref, count in recipes.patterns[pattern] for _ in range(count))
    bodies = tuple(Body(*recipes.profiles[ref], recipes.limit_sets[limit]) for ref, limit in zip(refs, limits))
    return ResolvedRow(source, role, spawn, bodies, logical, maximum, credit, renewable, numerator, denominator)


def decode_recipes(recipes: Recipes) -> ResolvedRecipes:
    """Expand values admitted by validate_slot_data, preserving body order."""
    maps = {stage: ResolvedMap(tuple(_decode_row(row, recipes) for row in record.rows),
                               record.passthrough_sources, record.disabled_sources)
            for stage, record in recipes.maps.items()}
    return ResolvedRecipes(recipes.enabled, MappingProxyType(maps), recipes.timers)
