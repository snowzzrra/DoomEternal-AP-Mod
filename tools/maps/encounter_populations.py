"""Add pinned population assignments required by already materialized encounters."""

import hashlib
import json

from tools.maps.ap_map_generator import find_entity_block_bounds
from tools.maps.encounter_catalog import entity_records
from tools.maps.encounter_materializer import _edit
from tools.maps.encounter_population_sources import PopulationSources
from tools.maps.encounter_source_definitions import SourceDefinitions
from tools.maps.enemy_attributes import _render


PRIVATE_POPULATION_PREFIX = "gameresources_patch2/generated/decls/actorpopulation/actorpopulation/ap_v070/"
PRIVATE_POPULATION_ASSETSINFO_PREFIX = "gameresources_patch2/EternalMod/assetsinfo/ap_v070_population_"


def materialize_frozen_populations(materialized_map_text, map_plan, registry, root):
    """Return map text and private DECL bytes without changing original populations."""
    operations = map_plan["operations"]
    if not operations:
        return materialized_map_text, {}
    sources = registry["actorpopulation_sources"]
    populations = PopulationSources(registry, root)
    matches = SourceDefinitions(registry, root).population_matches

    def additional_assignment(inherit, spawn_type):
        common = {f'actorpopulation/default/{name}' for name in ('default_no_bosses', 'dlc1', 'dlc2')}
        for names in (sources.keys() & common, sources.keys() - common):
            candidates = {}
            for name in sorted(names):
                for rows in populations.resolved_variants(name):
                    for row in rows.values():
                        if matches(row, inherit, spawn_type):
                            candidates[json.dumps(row, sort_keys=True)] = row
            if candidates:
                if len(candidates) != 1:
                    raise ValueError(f"Missing or ambiguous population assignment: {inherit}/{spawn_type}")
                return _render(next(iter(candidates.values())), 3)[1:-1]
        raise ValueError(f"Missing or ambiguous population assignment: {inherit}/{spawn_type}")

    selected = {operation["manager"]["entity"] for operation in operations}
    selected.update(instance["ai_name"] for operation in operations for wave in operation["waves"]
                    for instance in wave["instances"])
    records = {record["name"]: record for record in entity_records(materialized_map_text, include_names=selected)}
    if selected - records.keys():
        raise ValueError("Materialized population manager or AI is absent")
    needed = {}
    for operation in operations:
        manager = operation["manager"]["entity"]
        for wave in operation["waves"]:
            for instance in wave["instances"]:
                needed.setdefault(manager, set()).add((records[instance["ai_name"]]["inherit"], instance["spawn_type"]))
    result, declarations, replacements = materialized_map_text, {}, []
    for manager, requests in sorted(needed.items()):
        population = records[manager]["edit"].get("aiTypeDefAssignments")
        existing_variants = populations.resolved_variants(population)
        if all(any(matches(row, inherit, mask) for row in rows.values())
               for rows in existing_variants for inherit, mask in requests):
            continue
        # leave every native index intact; list growth gives unused slots a null entityDef.
        append_index = max(map(len, existing_variants))
        missing = sorted((inherit, mask) for inherit, mask in requests
                         if not all(any(matches(row, inherit, mask) for row in rows.values())
                                    for rows in existing_variants))
        additions = [additional_assignment(inherit, mask) for inherit, mask in missing]
        identity = json.dumps([map_plan["map_key"], manager, population,
                               existing_variants, additions], separators=(",", ":"), sort_keys=True)
        name = "p_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
        fields = "\n".join(f"\t\t\titem[{append_index + index}] = {{{body}}}"
                           for index, body in enumerate(additions))
        declaration = ("{\n\tinherit = " + json.dumps(population) + ";\n\tedit = {\n"
                       "\t\taiTypeDefAssignments = {\n\t\t\tnum = " + str(append_index + len(additions))
                       + ";\n" + fields + "\n\t\t}\n\t}\n}\n")
        declarations[PRIVATE_POPULATION_PREFIX + name + ".decl"] = declaration.encode("utf-8")
        reference = "actorpopulation/ap_v070/" + name
        assetsinfo = {"assets": [{"name": reference, "resourceType": "rs_streamfile",
                                  "mapResourceType": "actorPopulation", "version": 0,
                                  "specialByte1": 0, "specialByte2": 0, "specialByte3": 0}]}
        declarations[PRIVATE_POPULATION_ASSETSINFO_PREFIX + name + ".json"] = (
            json.dumps(assetsinfo, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        start, end = find_entity_block_bounds(result, manager)
        replacements.append((start, end, _edit(result[start:end], ("aiTypeDefAssignments",), reference)))
    for start, end, block in sorted(replacements, reverse=True):
        result = result[:start] + block + result[end:]
    return result, declarations
