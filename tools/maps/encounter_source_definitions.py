"""Select source definitions using pinned population masks and DECL ancestry."""

import hashlib
import json
from pathlib import Path
from doom_eap.content.enemy_species import enemy_spawn_mask

from tools.maps.encounter_population_sources import PopulationSources
from tools.maps.encounter_spawn_admission import admitted_parent_definitions


class SourceDefinitions:
    def __init__(self, registry=None, root=None):
        self.root = Path(root) if root is not None else Path(__file__).resolve().parents[2]
        self.registry = registry
        self._populations = None
        self._ancestry = None
        self.ancestry_sha256 = None

    def _load(self):
        if self._ancestry is None:
            payload = (self.root / "content/catalog/enemy_definition_ancestry.json").read_bytes()
            catalog = json.loads(payload)
            if catalog.get("schema_version") != 1 or catalog.get("qualification") != "native_definition_inheritance":
                raise ValueError("Unqualified enemy definition ancestry")
            self._ancestry = catalog["declarations"]
            self.ancestry_sha256 = hashlib.sha256(payload).hexdigest()
            registry = self.registry
            if registry is None:
                registry = json.loads((self.root / "content/catalog/enemy_species.json").read_bytes())
            self._populations = PopulationSources(registry, self.root)

    def _ancestors(self, name):
        result = []
        while name:
            if name in result:
                raise ValueError(f"Cyclic enemy definition ancestry: {name}")
            if name not in self._ancestry:
                raise ValueError(f"Unqualified enemy definition ancestry: {name}")
            result.append(name)
            name = self._ancestry[name]["inherit"]
        return result

    def select_admitted_point(self, point, group_names, spawn_type, population, record, *, context_layers):
        definitions, parents, guaranteed = admitted_parent_definitions(point, group_names, record, context_layers)
        selected = self.select(definitions, spawn_type, population, record, native=True)
        if selected not in guaranteed:
            raise ValueError('Selected source definition has no guaranteed active group')
        return selected, parents

    def select(self, definitions, spawn_type, population, record, *, initial_definitions=(), native=False):
        if not definitions:
            raise ValueError("Concrete source point needs one definition or qualified native selection")
        for definition in definitions:
            if not isinstance(definition, dict) or set(definition) != {"name"}:
                raise ValueError("Unqualified source definition fields")
        if len(definitions) == 1 and not native:
            return definitions[0]["name"]
        if initial_definitions:
            raise ValueError("Multiple source definition resets need review")
        self._load()
        source_mask = enemy_spawn_mask(spawn_type)
        if source_mask & (source_mask - 1):
            raise ValueError("Multiple source definitions need one qualified native spawn type")
        candidates = []
        for definition in definitions:
            actor = record(definition["name"])
            if actor["class"] != "idAI2":
                raise ValueError(f"Source definition is not idAI2: {actor['name']}")
            candidates.append((actor["name"], self._ancestors(actor["inherit"])))
        selected = []
        for rows in self._populations.resolved_variants(population):
            bases = {row["entityDef"] for row in rows.values()
                     if row.get("entityDef") and (native_type := self._populations.encounter_type(row))
                     and enemy_spawn_mask(native_type) == source_mask}
            choices = [name for name, chain in candidates if any(base in chain for base in bases)]
            if len(choices) != 1:
                raise ValueError("Source definitions have no unique native selection")
            selected.append(choices[0])
        if len(set(selected)) != 1:
            raise ValueError("Actor population variants select different source definitions")
        return selected[0]
