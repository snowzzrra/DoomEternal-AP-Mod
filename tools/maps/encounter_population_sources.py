"""Resolve pinned actorpopulation declarations without rewriting them."""

import hashlib
from pathlib import Path
import re

from tools.maps.ap_map_generator import find_matching_brace
from tools.maps.encounter_catalog import parse_object


class PopulationSources:
    def __init__(self, registry, root):
        self.sources = registry.get("actorpopulation_sources", {})
        self.conversions = registry.get("population_encounter_conversion", {}).get("values", {})
        self.root = Path(root)
        self._cache = {}

    def variants(self, name):
        values = self.sources.get(name, {}).get("variants", ())
        if not values:
            raise ValueError(f"Unqualified actor population: {name}")
        return values

    def load(self, name, variant):
        key = name, variant["path"], variant["sha256"]
        if key not in self._cache:
            payload = (self.root / variant["path"]).read_bytes()
            if hashlib.sha256(payload).hexdigest() != variant["sha256"]:
                raise ValueError(f"Population source hash mismatch: {name}")
            text = payload.decode("utf-8").strip()
            if not text.startswith("{") or find_matching_brace(text, 0) != len(text):
                raise ValueError(f"Unsupported population declaration: {name}")
            document = parse_object(text[1:-1])
            assignments = document["edit"]["aiTypeDefAssignments"]
            count = assignments["num"]
            if type(count) is not int or count < 0:
                raise ValueError(f"Invalid population assignment count: {name}")
            rows = {}
            for field, row in assignments.items():
                if field == "num":
                    continue
                match = re.fullmatch(r"item\[(\d+)\]", field)
                if match is None or int(match[1]) >= count or not isinstance(row, dict):
                    raise ValueError(f"Invalid population assignment: {name}/{field}")
                rows[int(match[1])] = row
            self._cache[key] = document, rows, count, text
        return self._cache[key]

    def _resolve(self, name, parents, require_unique):
        if name in parents:
            raise ValueError(f"Cyclic actor population inheritance: {name}")
        variants = self.variants(name)
        if require_unique and len(variants) != 1:
            raise ValueError(f"Ambiguous actor population variant: {name}")
        result = []
        for variant in variants:
            document, local, count, _ = self.load(name, variant)
            parent = document.get("inherit")
            inherited = self._resolve(parent, (*parents, name), require_unique) if parent else [{}]
            for ancestor in inherited:
                rows = {index: dict(row) for index, row in ancestor.items() if index < count}
                for index, row in local.items():
                    rows.setdefault(index, {}).update(row)
                if not require_unique:
                    # native list growth constructs omitted rows with a null entityDef.
                    for index in range(count):
                        rows.setdefault(index, {})
                if set(rows) != set(range(count)):
                    raise ValueError(f"Unresolved actor population assignments: {name}")
                result.append(rows)
        return result

    def resolved(self, name):
        return self._resolve(name, (), True)[0]

    def resolved_variants(self, name):
        return self._resolve(name, (), False)

    def encounter_type(self, row):
        # native assignment initialization sets aiType to enum zero (Zombie Tier 1)
        return self.conversions.get(row.get("aiType", "POPULATION_TYPE_ZOMBIE_TIER_1"), {}).get("encounter_spawn_type")

