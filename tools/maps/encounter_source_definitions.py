"""Resolve native control slots and source templates."""

import hashlib
import json
from pathlib import Path
from doom_eap.content.enemy_species import AIRBORNE_SPECIES, enemy_spawn_mask

from tools.maps.encounter_population_sources import PopulationSources
from tools.maps.encounter_spawn_admission import admitted_parent_definitions
from tools.maps.encounter_catalog import list_items


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
            self._species=registry['species']
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

    def population_matches(self, row, inherit, spawn_type):
        self._load()
        native_type = self._populations.encounter_type(row)
        return bool(row.get("entityDef")) and native_type is not None and (
            enemy_spawn_mask(native_type) == enemy_spawn_mask(spawn_type)
            and row["entityDef"] in self._ancestors(inherit))

    def species_for_definition(self,name):
        self._load()
        for ancestor in self._ancestors(name):
            matches=[species for species,entry in self._species.items() if ancestor in entry.get('inherits',())]
            if len(matches)>1:
                raise ValueError(f'Ambiguous source species: {name}')
            if matches:
                return matches[0]
        raise ValueError(f'Unqualified source species: {name}')

    def species_for_spawn_type(self, spawn_type):
        self._load()
        mask = enemy_spawn_mask(spawn_type)
        if mask == enemy_spawn_mask('ENCOUNTER_SPAWN_ANY') or mask & (mask-1):
            raise ValueError('Source control needs one native spawn type')
        matches = [species for species,entry in self._species.items()
                   if any(enemy_spawn_mask(value)==mask for value in entry.get('source_spawn_types',()))]
        if len(matches)!=1:
            raise ValueError('Source control has no unique native species')
        return matches[0]

    def point_templates(self, point, record, *, group=None):
        rows = [point]
        merged = (group is not None and list_items(group['edit'].get('targetSpawnSubGroups'))
                  and point['name'] not in list_items(group['edit'].get('spawners')))
        parents = [point['edit'].get('targetSpawnParent')]
        if group is not None and not merged:
            parents.append(group['edit'].get('targetSpawnParent'))
        for name in filter(None,parents):
            parent = record(name)
            if parent['class']!='idTarget_Spawn_Parent':
                raise ValueError('Origin parent is not idTarget_Spawn_Parent')
            rows.append(parent)
        actors = {}
        for row in rows:
            for field in ('entityDefs','initialEntityDefs'):
                for definition in list_items(row['edit'].get(field)):
                    if not isinstance(definition,dict) or set(definition)!={'name'}:
                        raise ValueError('Unqualified source definition fields')
                    actor = record(definition['name'])
                    if actor['class']!='idAI2':
                        raise ValueError('Source definition is not idAI2')
                    actors[actor['name']] = actor
        return list(actors.values())

    def source_slot(self, point, spawn_type, population, record, *, group=None, group_names=(), context_layers=()):
        species = self.species_for_spawn_type(spawn_type)
        # native counters use the event type; generated points select their physical model explicitly.
        try:
            name = self.select_source_point(point,spawn_type,population,record,group=group,
                                            group_names=group_names,context_layers=context_layers)
        except ValueError:
            actor = None
            actors = self.point_templates(point,record,group=group)
        else:
            actor = record(name)
            if actor['class']!='idAI2':
                raise ValueError('Source definition is not idAI2')
            actors = [actor]
        if any((self.species_for_definition(actor['inherit']) in AIRBORNE_SPECIES) != (species in AIRBORNE_SPECIES)
               for actor in actors):
            raise ValueError('Source point mixes navigation families')
        return species,actor,actors

    def select_admitted_point(self, point, group_names, spawn_type, population, record, *, context_layers):
        definitions, parents, guaranteed = admitted_parent_definitions(point, group_names, record, context_layers)
        selected = self.select(definitions, spawn_type, population, record, native=True)
        if selected not in guaranteed:
            raise ValueError('Selected source definition has no guaranteed active group')
        return selected, parents

    def select_source_point(self, point, spawn_type, population, record, *, group=None, group_names=(), context_layers=()):
        definitions = list_items(point['edit'].get('entityDefs'))
        definition_row = point
        merged = (group is not None and list_items(group['edit'].get('targetSpawnSubGroups'))
                  and point['name'] not in list_items(group['edit'].get('spawners')))
        if not definitions and group is not None and not merged:
            parent_name = group['edit'].get('targetSpawnParent')
            if parent_name:
                parent = record(parent_name)
                if parent['class'] != 'idTarget_Spawn_Parent':
                    raise ValueError('Origin group parent is not idTarget_Spawn_Parent')
                parent_definitions = list_items(parent['edit'].get('entityDefs'))
                if len(parent_definitions) == 1:
                    definitions, definition_row = parent_definitions, parent
        native_selection = len(definitions) > 1
        if len(definitions) == 1 and set(definitions[0]) != {'name'}:
            raise ValueError('Origin spawner must select exactly one authored AI')
        admission = None
        if not definitions:
            selected, admission = self.select_admitted_point(point, group_names, spawn_type, population, record,
                                                              context_layers=context_layers)
        else:
            selected = self.select(definitions, spawn_type, population, record,
                                   initial_definitions=list_items(definition_row['edit'].get('initialEntityDefs')))
        parents = [group['edit'].get('targetSpawnParent')] if group is not None and not merged else []
        parents.append(point['edit'].get('targetSpawnParent'))
        for name in filter(None, parents if native_selection and admission is None else []):
            parent = record(name)
            if parent['class'] != 'idTarget_Spawn_Parent':
                raise ValueError('Origin parent is not idTarget_Spawn_Parent')
            choice = self.select(list_items(parent['edit'].get('entityDefs')), spawn_type, population, record,
                                 initial_definitions=list_items(parent['edit'].get('initialEntityDefs')))
            if choice != selected:
                raise ValueError('Origin parent selects another authored AI')
        return selected

    def select(self, definitions, spawn_type, population, record, *, initial_definitions=(), native=False):
        variants = self.native_candidates(definitions, spawn_type, population, record,
                                          initial_definitions=initial_definitions, native=native)
        if any(len(choices) != 1 for choices in variants):
            raise ValueError("Source definitions have no unique native selection")
        selected = {choices[0] for choices in variants}
        if len(selected) != 1:
            raise ValueError("Actor population variants select different source definitions")
        return next(iter(selected))

    def native_candidates(self, definitions, spawn_type, population, record, *, initial_definitions=(), native=False):
        if not definitions:
            raise ValueError("Concrete source point needs one definition or qualified native selection")
        for definition in definitions:
            if not isinstance(definition, dict) or set(definition) != {"name"}:
                raise ValueError("Unqualified source definition fields")
        if len(definitions) == 1 and not native:
            return [(definitions[0]["name"],)]
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
            if not choices:
                raise ValueError("Source definitions have no unique native selection")
            selected.append(tuple(choices))
        return selected
