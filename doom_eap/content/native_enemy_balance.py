"""Map native encounters onto the campaign's vanilla combat reference."""

import re
from collections import Counter
from fractions import Fraction

from .enemy_options import ENEMY_RECIPES, SPECIES_GROUPS
from .enemy_plan import BASE_COMBAT_COST, _random_for, sample_instance_attributes, sample_species
from .vanilla_enemy_profiles import (
    BOSS_MAPS,
    CANONICAL_ORDER,
    SPECIAL_SPECIES,
    reference_source,
    source_species,
    vanilla_profile,
)

ROLES = {species: role for role, members in SPECIES_GROUPS.items() for species in members}
AMMO_FODDER = frozenset({'zombie_tier_1', 'zombie_tier_3', 'imp', 'gargoyle', 'hell_soldier'})
ZOMBIES = frozenset({'zombie_tier_1', 'zombie_tier_3'})
FLYING_ENEMIES = frozenset({'cacodemon', 'pain_elemental', 'lost_soul', 'spirit', 'blood_angel'})
PURE_BOSS_MAPS = BOSS_MAPS - {'e1m4_boss'}


def _category(species, mobile=False):
    if mobile == 3 and species == 'stone_imp':
        return 'fodder'
    if mobile >= 2:
        category = {'marauder': 'special_superheavy', 'archvile': 'special_superheavy',
            'armored_baron': 'special_superheavy', 'blood_angel': 'special_heavy',
            'cursed_prowler': 'special_heavy', 'spirit': 'special_support',
            'buff_pod': 'special_support', 'zombie_t1_screecher': 'special_support'}.get(species)
        if category:
            return category
    if mobile and species in {'tentacle', 'super_tentacle', 'turret'}:
        return 'fodder' if species == 'tentacle' else 'heavy'
    return species if species in SPECIAL_SPECIES else ROLES[species]


def _owner(source):
    binding = source.get('binding') or {}
    return binding.get('owner', binding.get('entity', source['id']))


def _variant(source):
    wave = source.get('wave', source.get('phase', source['id']))
    return wave.rsplit('/', 1)[0] if '/encounterComponent.' in wave else wave


def _scope(owner):
    value = 14695981039346656037
    for byte in owner.encode('utf-8'):
        value = ((value ^ byte) * 1099511628211) & 0xffffffffffffffff
    return value


def _peak_roles(wave):
    roles, specials = Counter(), Counter()
    for slot in wave['slots']:
        members = slot['species']
        for role in {ROLES[s] for s in members}:
            roles[role] += slot['count']
        for species in SPECIAL_SPECIES.intersection(members):
            specials[species] += slot['count']
    value = dict(maximum=wave['total'], heavy=roles['heavy'], superheavy=roles['superheavy'],
                 pressure=wave['total'], mixed=int(bool(specials) and bool(roles['superheavy'])),
                 specials=dict(specials), combined_special=sum(specials.values()))
    return value


def _peak(waves):
    maximum = dict(maximum=0, heavy=0, superheavy=0, pressure=0, mixed=0, combined_special=0, specials={})
    for wave in waves:
        value = wave.get('peak')
        if value is None:
            value = _peak_roles(wave)
        for field in ('maximum', 'heavy', 'superheavy', 'pressure', 'mixed', 'combined_special'):
            maximum[field] = max(maximum[field], value.get(field, 0))
        for species, count in value['specials'].items():
            maximum['specials'][species] = max(maximum['specials'].get(species, 0), count)
    return maximum


def _encounters_owner(AMBIENT_ENCOUNTERS, profile, row, secret):
    if row['owner'] in AMBIENT_ENCOUNTERS.get(profile['map_key'], ()):
        row['kind'] = 'ambient_encounter'
    elif row['owner'] in secret:
        row['kind'] = 'secret_encounter'
    elif all(set(slot['species']) <= {'cueball'} for slot in row['slots']):
        row['kind'] = 'cueball_encounter'
    elif all(set(slot['species']) <= {'tentacle', 'super_tentacle', 'turret', 'cueball'}
             for slot in row['slots']):
        row['kind'] = 'stationary_encounter'
    stationary_heavy = max((sum(slot['count'] for slot in wave['slots']
        if set(slot['species']) <= {'super_tentacle', 'turret'}) for wave in row['waves']), default=0)
    return stationary_heavy


def _encounters_kind(row):
    row['finite_total'] = sum(slot['maximum_emissions'] for slot in row['slots'] if not slot['renewable'])
    if row['kind'] == 'ambient_encounter' and all(set(slot['species']) <= {'stone_imp'} for slot in row['slots']):
        row['kind'] = 'specialized_encounter'
    if row['kind'] == 'arena_encounter' and re.search(r'_phase(?:one|two|three|\d+)(?:_|$)', row['owner']):
        row['kind'] = 'arena_phase_encounter'


def _encounters_stationary_heavy(AMBIENT_ENCOUNTERS, objective_budgets, profile, row, secret):
    stationary_heavy = _encounters_owner(AMBIENT_ENCOUNTERS, profile, row, secret)
    row['peak']['heavy'] += stationary_heavy
    row['spawn_shape'] = (any(not slot['renewable'] for slot in row['slots']),
                          any(slot['renewable'] for slot in row['slots']))
    if objective_budgets:
        mixed = any(not set(slot['species']) <= ZOMBIES for slot in row['slots'])
        row['slots'] = [{**slot, 'maximum_emissions': slot['count']}
            if mixed and slot['family'] == 'maintenance' and not slot['renewable']
            and set(slot['species']) <= ZOMBIES else slot for slot in row['slots']]
        _encounters_kind(row)


def _encounters(profile, *, mobile=False, objective_budgets=False):
    if 'encounters' in profile:
        if not mobile:
            return profile['encounters']
        from .native_enemy_safety import AMBIENT_ENCOUNTERS
        from .native_enemy_plan import load_secret_encounter_timers
        secret = {row['manager'] for row in load_secret_encounter_timers()['maps'].get(
            profile['map_key'], {}).get('timers', [])}
        result = []
        for source in profile['encounters']:
            row = {**source, 'peak': {**source['peak']}}
            if mobile == 2:
                row['kind'] = source.get('combat_kind', source['kind'])
            _encounters_stationary_heavy(AMBIENT_ENCOUNTERS, objective_budgets, profile, row, secret)
            result.append(row)
        return result
    grouped = {}
    for wave in profile['waves']:
        identity = wave['id'].rsplit('/', 1)[0] if '/encounterComponent.' in wave['id'] else wave['id']
        encounter = grouped.setdefault(identity, dict(id=identity, owner=wave['owner'], waves=[], slots=[]))
        encounter['waves'].append(wave)
        encounter['slots'].extend(wave['slots'])
    for encounter in grouped.values():
        encounter['peak'] = _peak(encounter['waves'])
    return list(grouped.values())


def _prepare_encounter_sources_slot(row, source):
    slot = next((slot for slot in row['slots'] if slot['source_id'] == source['id']), None)
    if slot and not slot['renewable'] and slot['maximum_emissions'] != source['maximum_emissions']:
        source = {**source, 'maximum_emissions': slot['maximum_emissions']}
    if row['kind'] == 'secret_encounter' and all(set(slot['species']) <= FLYING_ENEMIES for slot in row['slots']):
        source = {**source, 'allowed_species': sorted(FLYING_ENEMIES.intersection(
            source.get('allowed_species', FLYING_ENEMIES)))}
    return source


def prepare_encounter_sources(origins, catalog, map_key):
    rows = {row['id']: row for row in _encounters(vanilla_profile(catalog, map_key),
                                                 mobile=2, objective_budgets=True)}
    result = []
    for source in origins:
        row = rows.get(_variant(source))
        if row and not source.get('protected_reason') and not source.get('runtime_only'):
            source = _prepare_encounter_sources_slot(row, source)
        result.append(source)
    return result


def _quantile(index, count, length):
    return 0 if length == 1 or count == 1 else (index * (length - 1) * 2 + count - 1) // (2 * (count - 1))


def _corresponding(index, count, length):
    return min(length - 1, (2 * index + 1) * length // (2 * count))


def _reference_map_order(catalog, progressive, pure_boss_maps, stage_count, stage_index):
    order = [key for key in CANONICAL_ORDER if key in catalog['maps'] and key not in pure_boss_maps
             and vanilla_profile(catalog, key)['waves']]
    if not order:
        order = [key for key in catalog['maps'] if vanilla_profile(catalog, key)['waves']]
    if not order:
        raise ValueError('Vanilla balancing needs a reference encounter')
    return order[_quantile(stage_index, stage_count, len(order))] if progressive else order[-1]


def _reference_map(catalog, campaign, map_key, stage_index, stage_count, progressive, *, contextual=False):
    pure_boss_maps = PURE_BOSS_MAPS - {'e3m4_boss'} if contextual else PURE_BOSS_MAPS
    if campaign.get('mode') in {'mission_access_as_items', 'Mission Access as Items'} or map_key in pure_boss_maps:
        return map_key
    return _reference_map_order(catalog, progressive, pure_boss_maps, stage_count, stage_index)


def _emissions(source, body_count):
    return (source['maximum_emissions'] * body_count + source['logical_count'] - 1) // source['logical_count']


def _renewable(source):
    return source.get('renewable', source['maximum_emissions'] == 0)


def _allocate_counts_choices(original, source, states, target):
    choices = [(count, _emissions(source, count)) for count in range(
        target * source['logical_count'] // source['maximum_emissions'] + 1)]
    following = {}
    for current, (cost, selected) in states.items():
        for count, emitted in choices:
            total = current + emitted
            if total > target:
                break
            distance = cost + abs(emitted * original - target * source['maximum_emissions'])
            if total not in following or distance < following[total][0]:
                following[total] = (distance, (*selected, count))
    return following


def _allocate_counts_emitted(original, single, sources, target):
    counts = {source['id']: target * source['logical_count'] // original for source in sources
              if source not in single}
    emitted = sum(_emissions(source, counts[source['id']]) for source in sources if source not in single)
    while emitted > target:
        source = max((source for source in sources if source not in single and counts[source['id']]),
                     key=lambda source: _emissions(source, counts[source['id']]))
        before = _emissions(source, counts[source['id']])
        counts[source['id']] -= 1
        emitted -= before - _emissions(source, counts[source['id']])
    return counts, emitted


def _allocate_counts(sources, target):
    if not sources:
        return {}, target
    original = sum(source['maximum_emissions'] for source in sources)
    single = [source for source in sources if source['logical_count'] == source['maximum_emissions']]
    if single:
        counts, emitted = _allocate_counts_emitted(original, single, sources, target)
        counts.update(_allocate_pool_counts(single, target - emitted))
        return counts, target
    states = {0: (0, ())}
    for source in sources:
        following = _allocate_counts_choices(original, source, states, target)
        states = following
        if not states:
            break
    counts = states[max(states)][1]
    assigned = {source['id']: count for source, count in zip(sources, counts, strict=False)}
    return assigned, sum(_emissions(source, assigned[source['id']]) for source in sources)


def _allocate_pool_counts(sources, target):
    original = sum(source['logical_count'] for source in sources)
    if not original:
        return {}
    counts = {source['id']: target * source['logical_count'] // original for source in sources}
    order = sorted(sources, key=lambda source: (-(target * source['logical_count'] % original), source['id']))
    for source in order[:target - sum(counts.values())]:
        counts[source['id']] += 1
    return counts


def _native_budget(encounter, *, seed, renewable, mobile=False):
    counts = Counter()
    for slot in encounter['slots']:
        if slot['renewable'] != renewable:
            continue
        total = slot['count'] if renewable else slot['maximum_emissions']
        if not total:
            continue
        quotient, remainder = divmod(total, slot['count'])
        draw = _random_for(seed, slot['source_id'], 'vanilla_pool')
        for index in range(slot['count']):
            species = slot['species'][draw.randrange(len(slot['species']))]
            counts[_category(species, mobile)] += quotient + (index < remainder)
    return counts


def _native_costs(encounter, *, seed, renewable, mobile=False):
    costs = Counter()
    for slot in encounter['slots']:
        if slot['renewable'] != renewable:
            continue
        total = slot['count'] if renewable else slot['maximum_emissions']
        quotient, remainder = divmod(total, slot['count'])
        draw = _random_for(seed, slot['source_id'], 'vanilla_pool')
        for index in range(slot['count']):
            species = slot['species'][draw.randrange(len(slot['species']))]
            costs[_category(species, mobile)] += BASE_COMBAT_COST[species] * (quotient + (index < remainder))
    return costs


def _native_ammo_pool(encounter):
    return sum(slot['count'] for slot in encounter['slots'] if slot['renewable'] and slot['species']
               and set(slot['species']) <= AMMO_FODDER)


def _reserve_ammo_counts_identities(compatible, counts, required, sources, supplies):
    if not compatible or required > sum(counts.values()):
        raise ValueError('Renewable ammunition has no compatible chainsaw fodder')
    identities = {source['id'] for source in compatible}
    missing = max(0, required - sum(counts[identity] for identity in identities))
    if missing:
        chosen = min(compatible, key=lambda source: (source['id'] not in supplies, -source['logical_count'], source['id']))
        for source in sorted((source for source in sources if source['id'] not in identities),
                             key=lambda source: (-counts[source['id']], source['id'])):
            moved = min(missing, counts[source['id']])
            counts[source['id']] -= moved
            counts[chosen['id']] += moved
            missing -= moved
            if not missing:
                break


def _reserve_ammo_counts(sources, counts, required, *, candidates, catalog, supplies, population, special_limits):
    if not required:
        return counts
    compatible = [source for source in sources if AMMO_FODDER.intersection(
        _compatible(source, candidates, population, special_limits))]
    _reserve_ammo_counts_identities(compatible, counts, required, sources, supplies)
    return counts


def _cost_budget(encounter, desired, *, seed, renewable, candidates, mobile=False):
    counts = _native_budget(encounter, seed=seed, renewable=renewable, mobile=mobile)
    costs = _native_costs(encounter, seed=seed, renewable=renewable, mobile=mobile)
    return {category: quantity * (costs[category] / counts[category] if counts[category] else
        min((BASE_COMBAT_COST[name] for name in candidates if _category(name, mobile) == category), default=Fraction(0)))
        for category, quantity in desired.items()}


def _body_costs(sources, bodies, *, renewable, mobile=False):
    costs = Counter()
    for source in sources:
        roster = bodies[source['id']]
        if not roster:
            continue
        total = len(roster) if renewable else _emissions(source, len(roster))
        quotient, remainder = divmod(total, len(roster))
        for ordinal, body in enumerate(roster):
            costs[_category(body['species'], mobile)] += BASE_COMBAT_COST[body['species']] * (quotient + (ordinal < remainder))
    return costs


def _volume(settings):
    return Fraction(settings['length'] * (settings['count_multiplier'] if
        settings['count_mode'] == 'multiplier' else 100), 10000)


def _passthrough_budget(sources, catalog, *, seed, renewable):
    slots = [dict(source_id=source['id'], count=source['logical_count'],
                  maximum_emissions=source['maximum_emissions'],
                  renewable=_renewable(source), species=source_species(source, catalog))
             for source in sources if source.get('vanilla_stationary') or source.get('vanilla_passthrough')]
    return _native_budget({'slots': slots}, seed=seed, renewable=renewable)


def _modified_budget(counts, factor, total):
    result = Counter(counts)
    threats = [key for key in counts if key not in {'fodder', 'stationary'}]
    for key in threats:
        result[key] = max(0, round(counts[key] * factor))
    threat_total = sum(result[key] for key in threats)
    if threat_total > total:
        remainder = total
        for key in sorted(threats, key=lambda name: (name not in SPECIAL_SPECIES, name)):
            result[key] = min(result[key], remainder)
            remainder -= result[key]
    result['stationary'] = min(result['stationary'], max(0, total - sum(result[key] for key in threats)))
    result['fodder'] = max(0, total - result['stationary'] - sum(result[key] for key in threats))
    return result


def _subset(items, needed, candidates):
    states = {0: ()}
    for index in candidates:
        weight = items[index]['weight']
        for total, selected in list(states.items())[::-1]:
            value = total + weight
            if value <= needed and value not in states:
                states[value] = (*selected, index)
        if needed in states:
            return states[needed]
    return states[max(states)]


def _population_density(result, settings):
    density = settings['density'] if settings['count_mode'] == 'auto' else 100
    for key in ('maximum', 'heavy', 'superheavy', 'pressure', 'combined_special'):
        result[key] = (result[key] * density + 99) // 100
    result['species'] = {name: (value * density + 99) // 100 for name, value in result['species'].items()}
    for name, value in settings.get('special_limit_overrides', {}).items():
        if name == 'combined_special':
            result[name] = value
        else:
            result['species'][name] = value
            if name == 'archvile':
                for related in ('buff_pod', 'zombie_t1_screecher'):
                    result['species'][related] = min(value, result['species'].get(related, 0))


def _population(peak, owner, *, opening=False, settings=None, geometry_reserve=0):
    result = dict(maximum=max(1, peak['maximum']), heavy=peak['heavy'],
                superheavy=0 if opening else peak['superheavy'],
                pressure=max(1, peak['maximum']), mixed=peak['mixed'], scope=_scope(owner), strict=True,
                species=dict(peak['specials']), combined_special=peak.get('combined_special', 0))
    if settings is not None:
        if settings.get('mobile_stationary') == 2:
            stage_caps = settings['stage_special_caps']
            for group in (('marauder', 'archvile', 'armored_baron'), ('blood_angel', 'cursed_prowler'),
                          ('spirit', 'buff_pod', 'zombie_t1_screecher')):
                shared = sum(peak['specials'].get(name, 0) for name in group)
                for name in group:
                    result['species'][name] = min(shared, max(1, stage_caps.get(name, 0)))
        _population_density(result, settings)
    if geometry_reserve:
        result['maximum'] += geometry_reserve
        result['pressure'] += geometry_reserve
        result['geometry_reserve'] = geometry_reserve
    return result


def _special_limits(peak, settings):
    values = peak['specials']
    result = {key: values.get(key, 0) for key in ('marauder', 'archvile', 'spirit')}
    result['archvile'] = max(result['archvile'], values.get('buff_pod', 0), values.get('zombie_t1_screecher', 0))
    result['combined_special'] = peak['combined_special']
    result.update(settings.get('special_limit_overrides', {}))
    return result


def _compatible_eligible(candidates, source):
    eligible = [species for species in candidates if not source.get('allowed_species')
                or species in source['allowed_species']]
    return eligible


def _population_allows_species(species, mobile, population):
    return ((ROLES[species] != 'heavy' and (not mobile or species not in {'super_tentacle', 'turret'})) or population['heavy'] > 0) and (ROLES[species] != 'superheavy' or population['superheavy'] > 0)


def _compatible(source, candidates, population, special_limits, *, mobile=False):
    eligible = _compatible_eligible(candidates, source)
    return [species for species in eligible if
        _population_allows_species(species, mobile, population) and (species not in SPECIAL_SPECIES or population['species'].get(species, 0) > 0) and (species not in {'marauder', 'archvile', 'spirit'} or special_limits[species] > 0) and (species not in {'buff_pod', 'zombie_t1_screecher'} or special_limits['archvile'] > 0)]


def _draw_bodies_mobile(candidates, catalog, counts, population, renewable, settings, sources, supplies):
    mobile = settings.get('mobile_stationary', False)
    vary_cost = mobile and (not settings.get('protected_opening', False) or
                           (mobile == 2 and not settings.get('protect_opening_budget', False)))
    items = []
    for source in sources:
        count = counts[source['id']]
        if not count:
            continue
        total = count if renewable else _emissions(source, count)
        quotient, remainder = divmod(total, count)
        eligible = _compatible(source, candidates, population, settings['special_limits'], mobile=mobile)
        if not eligible:
            raise ValueError(f"Vanilla encounter has no enabled compatible enemy: {source['id']}")
        items.extend(dict(source=source, weight=quotient + (ordinal < remainder), eligible=eligible,
                          ordinal=ordinal, selected=None) for ordinal in range(count))
    return items, mobile, vary_cost


def _draw_bodies_order(ammo_required, budget, items, key, seed, supplies):
    order = list(range(len(items)))
    _random_for(seed, key, 'reference_positions').shuffle(order)
    if ammo_required:
        available = sorted((index for index in order if AMMO_FODDER.intersection(items[index]['eligible'])),
                           key=lambda index: items[index]['source']['id'] not in supplies)
        selected = _subset(items, ammo_required, available)
        if sum(items[index]['weight'] for index in selected) != ammo_required:
            raise ValueError(f'Renewable ammunition has no compatible chainsaw fodder: {key}')
        for index in selected:
            items[index]['selected'] = 'fodder'
            items[index]['eligible'] = [species for species in items[index]['eligible'] if species in AMMO_FODDER]
        budget = Counter(budget)
        budget['fodder'] -= ammo_required
    return budget, order


def _draw_bodies_budget(ammo_required, budget, items, key, mobile, seed, supplies):
    budget, order = _draw_bodies_order(ammo_required, budget, items, key, seed, supplies)
    for category in sorted(budget, key=lambda name: (name not in SPECIAL_SPECIES,
            {'superheavy': 0, 'heavy': 1, 'stationary': 2, 'fodder': 3}.get(name, -1), name)):
        available = [index for index in order if items[index]['selected'] is None and
                     any(_category(species, mobile) == category for species in items[index]['eligible'])]
        for index in _subset(items, budget[category], available):
            items[index]['selected'] = category
    return budget


def _draw_bodies_category(eligible, item, mobile, supplies):
    if mobile != 2 and item['source']['id'] in supplies and all(_category(species, mobile) == 'fodder' for species in eligible):
        eligible = [species for species in eligible if species in AMMO_FODDER] or eligible
    category = _category(eligible[0], mobile)
    return category, eligible


def _draw_bodies_drawn(items, mobile, reserved, settings, sources):
    for item in items:
        category = item['selected']
        if category is not None:
            reserved[category] += item['weight'] * min(BASE_COMBAT_COST[species] for species in item['eligible']
                if _category(species, mobile) == category)
    drawn = settings.get('_drawn', Counter())
    result, actual, spent = {source['id']: [] for source in sources}, Counter(), Counter()
    return actual, drawn, result, spent


def _draw_bodies_eligible(item, mobile):
    eligible = [species for species in item['eligible'] if _category(species, mobile) == item['selected']]
    if not eligible:
        eligible = [species for species in item['eligible'] if _category(species, mobile) == 'fodder'] or [
            min(item['eligible'], key=lambda species: (BASE_COMBAT_COST[species], species))]
    return eligible


def _draw_bodies_weight(eligible, item, zombie_limit, zombies):
    if zombie_limit is not None and zombies + item['weight'] > zombie_limit:
        eligible = [species for species in eligible if species not in ZOMBIES] or eligible
    return eligible


def _draw_bodies_fitting(category, cost_budget, eligible, item, mobile, reserved, spent):
    if item['selected'] == category:
        reserved[category] -= item['weight'] * min(BASE_COMBAT_COST[species] for species in item['eligible']
            if _category(species, mobile) == category)
    fitting = [species for species in eligible if
        spent[category] + BASE_COMBAT_COST[species] * item['weight'] + reserved[category] <= cost_budget[category]]
    eligible = fitting or [min(eligible, key=lambda name: (BASE_COMBAT_COST[name], name))]
    return eligible


def _draw_bodies_weights(budget, category, cost_budget, drawn, eligible, settings, vary_cost):
    weights = {species: max(1, settings['weights'][species] * 100 // (1 + drawn[species])) for species in eligible}
    if vary_cost and cost_budget and cost_budget.get(category):
        average = cost_budget[category] / max(1, budget[category])
        weights = {species: max(1, round(weight * min(BASE_COMBAT_COST[species], average) /
            max(BASE_COMBAT_COST[species], average))) for species, weight in weights.items()}
    return weights


def _draw_bodies(sources, counts, budget, *, seed, key, settings, catalog, candidates, supplies,
                 population, stage_index, stage_count, hitpoints, renewable=False, cost_budget=None, ammo_required=0):
    items, mobile, vary_cost = _draw_bodies_mobile(candidates, catalog, counts, population, renewable, settings, sources, supplies)
    budget = _draw_bodies_budget(ammo_required, budget, items, key, mobile, seed, supplies)
    reserved = Counter()
    actual, drawn, result, spent = _draw_bodies_drawn(items, mobile, reserved, settings, sources)
    zombies = 0
    zombie_limit = max(2, budget.get('fodder', 0) // 3) if settings.get('limit_zombies') and not renewable else None
    for index, item in enumerate(items):
        eligible = _draw_bodies_eligible(item, mobile)
        category, eligible = _draw_bodies_category(eligible, item, mobile, supplies)
        eligible = _draw_bodies_weight(eligible, item, zombie_limit, zombies)
        if not vary_cost and cost_budget is not None and category in cost_budget:
            eligible = _draw_bodies_fitting(category, cost_budget, eligible, item, mobile, reserved, spent)
        weights = _draw_bodies_weights(budget, category, cost_budget, drawn, eligible, settings, vary_cost)
        draw_key = [key, index]
        species = sample_species({**settings, 'weights': weights}, eligible, seed=seed, instance_key=draw_key)
        attributes = sample_instance_attributes(settings, species, seed=seed, instance_key=draw_key,
            stage_index=stage_index, stage_count=stage_count)
        result[item['source']['id']].append(dict(species=species, attributes=attributes,
            spawn_type=catalog['species'][species]['spawn_type'], ai=dict(catalog['species'][species]['ai']),
            hitpoints=hitpoints(catalog['species'][species], attributes), population_limits=dict(population),
            special_limits=_special_limits(dict(specials=population['species'],
                combined_special=population['combined_special']), settings)))
        actual[_category(species, mobile)] += item['weight']
        spent[_category(species, mobile)] += BASE_COMBAT_COST[species] * item['weight']
        drawn[species] += 1
        if species in ZOMBIES:
            zombies += item['weight']
    return result, actual


def _compatible_reference_finite(catalog, chosen, rows, seed, sources):
    finite = [source for source in sources if not _renewable(source) and not source.get('protected_reason')]
    renewable = [source for source in sources if _renewable(source) and not source.get('protected_reason')]
    native = _passthrough_budget(sources, catalog, seed=seed, renewable=False)
    anchor = next(index for index, row in enumerate(rows) if row['id'] == chosen['id'])
    return anchor, finite, native, renewable


def _compatible_reference(rows, chosen, sources, *, seed, settings, catalog, candidates, supplies, opening, factor):
    anchor, finite, native, renewable = _compatible_reference_finite(catalog, chosen, rows, seed, sources)
    def _score_missing(categories, desired, maintained, native, renewable, wanted):
        missing = sum(value for key, value in desired.items() if key != 'stationary' and key not in categories)
        missing += sum(max(0, value - wanted[key]) for key, value in native.items() if key != 'stationary')
        if maintained and not renewable:
            missing += sum(maintained.values())
        return missing


    def score(entry):
        index, row = entry
        wanted = _native_budget(row, seed=seed, renewable=False)
        maintained = _native_budget(row, seed=seed, renewable=True)
        raw = Counter({key: max(0, value - native[key]) for key, value in wanted.items() if key != 'stationary'})
        target = round(sum(raw.values()) * _volume(settings))
        desired = _modified_budget(raw, factor, target)
        population = _population(row['peak'], _owner(sources[0]), opening=opening, settings=settings)
        categories = {_category(species) for source in finite for species in
            _compatible(source, candidates, population, settings['special_limits'])}
        missing = _score_missing(categories, desired, maintained, native, renewable, wanted)
        if _native_ammo_pool(row) and not any(AMMO_FODDER.intersection(
                _compatible(source, candidates, population, settings['special_limits']))
                for source in renewable):
            missing += _native_ammo_pool(row)
        if not missing and finite:
            _, emitted = _allocate_counts(finite, target)
            missing += abs(emitted - target)
        return missing, abs(wanted['stationary'] - native['stationary']), abs(index - anchor), index
    compatible = [(index, row) for index, row in enumerate(rows) if not finite or any(
        not slot['renewable'] and any(_category(species) != 'stationary' for species in slot['species'])
        for slot in row['slots'])]
    return min(compatible or list(enumerate(rows)), key=score)[1]


def _build_vanilla_stage_deviations(active, desired_renewable, folded_renewable, geometry_reserve, maintained, native_renewable, settings, unknown_context):
    active.update(native_renewable)
    desired_renewable.update({key: min(value, maintained[key]) for key, value in native_renewable.items()})
    desired_renewable['stationary'] = maintained['stationary']
    deviations = []
    if folded_renewable:
        deviations.append('renewable_supply_as_finite_bodies')
    if unknown_context:
        deviations.append('native_unknown_context')
    if geometry_reserve:
        deviations.append('geometry_reserve')
    if _volume(settings) != 1 or (settings['count_mode'] == 'multiplier' and settings['count_multiplier'] != 100):
        deviations.append('custom_quantity_scale')
    if settings['count_mode'] == 'auto' and settings['density'] != 100:
        deviations.append('custom_simultaneous_scale')
    return deviations


def _build_vanilla_stage_translation(chosen, kind, local_kind, map_key, own_row, recipe_revision, reference_map, same_kind):
    translation = 'vanilla_encounter'
    row_reference_map = map_key if local_kind else reference_map
    if (recipe_revision >= 17 and kind in {'ambient_encounter', 'arena_encounter'} and own_row.get('finite_total') == 1
            and chosen['finite_total'] >= 10):
        smaller = [row for row in same_kind if 0 < row['finite_total'] <= 2]
        chosen = min(smaller, key=lambda row: abs(same_kind.index(row) - same_kind.index(chosen))) if smaller else own_row
        if not smaller:
            translation, row_reference_map = 'native_incidental_scale', map_key
    return chosen, row_reference_map, translation


def _build_vanilla_stage_same_kind(ammo_geometry, encounters, kind, local_kind, own_encounters, same_kind, shape):
    same_kind = ([row for row in own_encounters if row.get('kind', 'scripted_encounter') == kind
                  and row['spawn_shape'][0] == shape[0]
                  and (shape[1] or not row['spawn_shape'][1])
                  and (ammo_geometry or not _native_ammo_pool(row))]
                 if local_kind else same_kind or encounters)
    return same_kind


def _group_native_sources(catalog, grouped, origins):
    for source in origins:
        if reference_source(source, catalog) or source.get('vanilla_stationary') or source.get('vanilla_passthrough'):
            grouped.setdefault((_owner(source), _variant(source)), []).append(source)


def _target_encounter_owners(grouped, own_encounters):
    target_owners = list(dict.fromkeys(row['owner'] for row in own_encounters
        if any(owner == row['owner'] for owner, _ in grouped)))
    target_owners.extend(owner for owner, _ in grouped if owner not in target_owners)
    return target_owners


def _mobile_special_limits(limits, map_peak, mobile, settings):
    if mobile == 2:
        limits['marauder'] = max(limits['marauder'], limits['archvile'], map_peak['specials'].get('armored_baron', 0))
        limits['archvile'] = max(limits['archvile'], limits['marauder'])
        limits['spirit'] = max(map_peak['specials'].get(name, 0) for name in ('spirit', 'buff_pod', 'zombie_t1_screecher'))
        settings.update(stage_special_caps=map_peak['specials'], _drawn=Counter())


def _stage_intensity_factor(settings, stage_count, stage_index):
    progress = Fraction(stage_index, max(1, stage_count - 1)) if settings['progressive'] else Fraction(1)
    expected = sum(value * part for value, part in zip(ENEMY_RECIPES['slayers_path'].intensity,
        (1 - progress, progress), strict=False)) if settings['progressive'] else ENEMY_RECIPES['slayers_path'].intensity[0]
    factor = sum(value * part for value, part in zip(settings['intensity'], (1 - progress, progress), strict=False)) / expected
    return factor


def _choose_reference_encounter_same_kind(ammo_geometry, encounters, kind, mobile, shape):
    same_kind = [row for row in encounters if row.get('kind', 'scripted_encounter') == kind
                 and (not mobile or (row['spawn_shape'][0] == shape[0]
                      and (shape[1] or not row['spawn_shape'][1])
                      and (ammo_geometry or not _native_ammo_pool(row))))]
    return same_kind


def _choose_reference_encounter_local_kind(ammo_geometry, encounters, kind, mobile, own_encounters, own_row, same_kind, shape, target_owners):
    local_kind = mobile and not same_kind and bool(own_row)
    same_kind = _build_vanilla_stage_same_kind(ammo_geometry, encounters, kind, local_kind, own_encounters, same_kind, shape)
    reference_kind_owners = list(dict.fromkeys(row['owner'] for row in same_kind))
    target_kind_owners = list(dict.fromkeys(row['owner'] for row in own_encounters
        if row.get('kind', 'scripted_encounter') == kind and row['owner'] in target_owners
        and (not mobile or row['spawn_shape'][0] == shape[0]))) or target_owners
    return local_kind, reference_kind_owners, same_kind, target_kind_owners


def _choose_reference_encounter_chosen(grouped, local_kind, map_key, owner, reference_map, references, sources, variant, variants):
    variants.extend(key for current, key in grouped if current == owner and key not in variants)
    chosen = next((row for row in references if row['id'] == variant), None) if reference_map == map_key or local_kind else None
    finite_target = any(not _renewable(source) and not source.get('protected_reason') for source in sources)
    return chosen, finite_target


def _choose_reference_encounter_ref_owner(encounters, local_kind, map_key, mobile, owner, reference_kind_owners, reference_map, same_kind, target_kind_owners):
    index = _corresponding if mobile else _quantile
    ref_owner = owner if (reference_map == map_key or local_kind) and owner in reference_kind_owners else reference_kind_owners[
        index(target_kind_owners.index(owner), len(target_kind_owners), len(reference_kind_owners))]
    references = [row for row in (same_kind if mobile else encounters) if row['owner'] == ref_owner]
    return index, references


def _choose_reference_encounter_variants(grouped, local_kind, map_key, own_encounters, owner, reference_map, references, sources, variant):
    variants = [row['id'] for row in own_encounters if row['owner'] == owner and (owner, row['id']) in grouped]
    chosen, finite_target = _choose_reference_encounter_chosen(grouped, local_kind, map_key, owner, reference_map, references, sources, variant, variants)
    compatible = [row for row in references if any(not slot['renewable'] for slot in row['slots']) == finite_target]
    references = compatible or references
    return chosen, references, variants


def _choose_reference_encounter_own_row(own_encounters, sources, variant):
    own_row = next((row for row in own_encounters if row['id'] == variant), {})
    kind = own_row.get('kind', 'scripted_encounter')
    shape = own_row.get('spawn_shape')
    ammo_geometry = any(_renewable(source) and not source.get('protected_reason') and
        (not source.get('allowed_species') or AMMO_FODDER.intersection(source['allowed_species']))
        for source in sources)
    return ammo_geometry, kind, own_row, shape


def _choose_reference_encounter_broader(encounters, kind, same_kind):
    broader = [row for row in encounters if row.get('kind') == kind and row['spawn_shape'][0]
               and row.get('combat_rating_scale') != 'COMBAT_RATING_SCALE_SMALL']
    if len({row['owner'] for row in broader}) > 1:
        same_kind = broader
    return same_kind


def _choose_reference_encounter_owner(encounters, kind, own_row, recipe_revision, same_kind):
    if (recipe_revision >= 17 and kind == 'arena_encounter' and own_row.get('finite_total', 0) >= 10
            and len({row['owner'] for row in same_kind}) <= 1):
        same_kind = _choose_reference_encounter_broader(encounters, kind, same_kind)
    return same_kind


def _choose_reference_encounter_large(encounters, kind, same_kind):
    large = [row for row in encounters if row.get('kind') == kind and row['spawn_shape'][0]
             and row.get('combat_rating_scale') == 'COMBAT_RATING_SCALE_LARGE']
    if large:
        same_kind = large
    return same_kind


def _choose_reference_encounter(encounters, grouped, map_key, own_encounters, owner, recipe_revision, reference_map, sources, target_owners, variant):
    mobile = 2 if recipe_revision >= 15 else recipe_revision >= 14
    ammo_geometry, kind, own_row, shape = _choose_reference_encounter_own_row(own_encounters, sources, variant)
    same_kind = _choose_reference_encounter_same_kind(ammo_geometry, encounters, kind, mobile, shape)
    same_kind = _choose_reference_encounter_owner(encounters, kind, own_row, recipe_revision, same_kind)
    if (recipe_revision >= 17 and kind == 'arena_encounter' and own_row.get('finite_total', 0) >= 10
            and own_row.get('combat_rating_scale') == 'COMBAT_RATING_SCALE_LARGE'
            and any(row.get('combat_rating_scale') == 'COMBAT_RATING_SCALE_SMALL' for row in same_kind)):
        same_kind = _choose_reference_encounter_large(encounters, kind, same_kind)
    local_kind, reference_kind_owners, same_kind, target_kind_owners = _choose_reference_encounter_local_kind(ammo_geometry, encounters, kind, mobile, own_encounters, own_row, same_kind, shape, target_owners)
    index, references = _choose_reference_encounter_ref_owner(encounters, local_kind, map_key, mobile, owner, reference_kind_owners, reference_map, same_kind, target_kind_owners)
    chosen, references, variants = _choose_reference_encounter_variants(grouped, local_kind, map_key, own_encounters, owner, reference_map, references, sources, variant)
    chosen = chosen or references[index(variants.index(variant), len(variants), len(references))]
    return ammo_geometry, chosen, kind, local_kind, mobile, own_row, same_kind


def _preserve_native_encounter_renewable(chosen, map_key, own_row, renewable_only, row_reference_map, translation):
    if renewable_only and own_row and any(not slot['renewable'] for slot in chosen['slots']):
        chosen = own_row
        translation = 'native_renewable_passthrough'
        row_reference_map = map_key
    return chosen, row_reference_map, translation


def _preserve_native_encounter_renewable_only(owner, sources, unknown, variant):
    renewable_only = all(_renewable(source) for source in sources)
    unknown_context = [source['id'] for source in unknown if _owner(source) == owner and _variant(source) == variant]
    return renewable_only, unknown_context


def _finite_reference_slots(same_kind):
    return (not slot['renewable'] for row in same_kind for slot in row['slots'])


def _native_passthrough_reason(unknown_context, native_only, renewable_only, own_row, same_kind):
    if not own_row:
        return None
    if unknown_context:
        return 'native_unknown_context'
    if native_only:
        return 'native_geometry_passthrough'
    if renewable_only and not any(not slot['renewable'] for row in same_kind for slot in row['slots']):
        return 'native_renewable_passthrough'
    return None


def _preserve_native_encounter(candidates, catalog, chosen, factor, map_key, mobile, native_only, opening_sources, own_row, owner, reference_map, row_reference_map, same_kind, seed, settings, sources, supplies, translation, unknown, variant):
    renewable_only, unknown_context = _preserve_native_encounter_renewable_only(owner, sources, unknown, variant)
    reason = _native_passthrough_reason(unknown_context, native_only, renewable_only, own_row, same_kind)
    if reason:
        chosen, translation, row_reference_map = own_row, reason, map_key
    elif reference_map != map_key and not mobile:
        chosen = _compatible_reference(same_kind, chosen, sources, seed=seed, settings=settings,
            catalog=catalog, candidates=candidates, supplies=supplies,
            opening=any(source['id'] in opening_sources for source in sources), factor=factor)
        chosen, row_reference_map, translation = _preserve_native_encounter_renewable(chosen, map_key, own_row, renewable_only, row_reference_map, translation)
    return chosen, row_reference_map, translation, unknown_context


def _fold_renewable_slots(ammo_geometry, chosen, finite_supply, kind, recipe_revision):
    folded_renewable = recipe_revision >= 17 and not ammo_geometry and kind == 'arena_encounter' and chosen['spawn_shape'][1]
    if folded_renewable:
        finite_supply = _native_ammo_pool(chosen)
        chosen = {**chosen, 'slots': [{**slot, 'renewable': False, 'maximum_emissions': slot['count']}
            if slot['renewable'] else slot for slot in chosen['slots']]}
    return chosen, finite_supply, folded_renewable


def _reserve_renewable_ammunition_ammo_required(desired_renewable, native_ammo_pool, random_maintained, target_ammo_pool):
    ammo_required = max(0, target_ammo_pool - native_ammo_pool)
    shortfall = max(0, ammo_required - desired_renewable['fodder'])
    for surplus_only in (True, False):
        for category in sorted(name for name in desired_renewable if name not in {'fodder', 'stationary'}):
            available = max(0, desired_renewable[category] - random_maintained[category]) if surplus_only else desired_renewable[category]
            removed = min(shortfall, available)
            desired_renewable[category] -= removed
            desired_renewable['fodder'] += removed
            shortfall -= removed
    if shortfall:
        raise ValueError('Requested renewable pool cannot preserve native ammunition')
    return ammo_required


def _reserve_renewable_ammunition(catalog, desired_renewable, random_maintained, sources, target_ammo_pool):
    native_ammo_pool = sum(source['logical_count'] for source in sources if _renewable(source) and
        (source.get('vanilla_stationary') or source.get('vanilla_passthrough')) and source_species(source, catalog)
        and set(source_species(source, catalog)) <= AMMO_FODDER)
    ammo_required = _reserve_renewable_ammunition_ammo_required(desired_renewable, native_ammo_pool, random_maintained, target_ammo_pool)
    return ammo_required, native_ammo_pool


def _finite_budget_deviations_superheavy(active, desired_renewable, deviations, opening, wanted):
    if +active != +desired_renewable:
        deviations.append('native_renewable_geometry_or_disabled_species')
    if opening and (wanted['superheavy'] or any(ROLES.get(key) == 'superheavy' and wanted[key]
            for key in SPECIAL_SPECIES)):
        deviations.append('protected_opening_excludes_superheavies')


def _geometry_deviations(actual, desired, deviations, finite, sources):
    if +actual != +desired:
        if any(source.get('vanilla_stationary') or source.get('vanilla_passthrough') for source in sources):
            deviations.append('native_geometry_passthrough')
        if any(source.get('allowed_species') for source in finite):
            deviations.append('spawn_geometry_compatibility')
        if not deviations:
            deviations.append('native_quantity_cadence_or_disabled_species')


def _finite_budget_deviations(active, actual, actual_cost, cost_budget, desired, desired_renewable, deviations, emitted, finite, mobile, opening, sources, target, wanted):
    if not mobile and any(value > cost_budget.get(key, 0) for key, value in actual_cost.items()):
        deviations.append('compatible_species_minimum_exceeds_native_cost')
    if emitted != target:
        deviations.append('native_quantity_cadence')
    _geometry_deviations(actual, desired, deviations, finite, sources)
    _finite_budget_deviations_superheavy(active, desired_renewable, deviations, opening, wanted)


def _draw_library_sources_population(catalog, map_peak, mobile, reports, settings, source):
    population = _population(map_peak, _owner(source), settings=settings)
    count = source['logical_count']
    budget = Counter(_category(species, mobile) for species in source_species(source, catalog))
    if source.get('darklord_phantom'):
        population['heavy'] = int(bool(budget['heavy']))
        population['superheavy'] = int(bool(budget['superheavy']))
    elif source.get('runtime_only'):
        budget = Counter()
        for report in reports:
            budget.update(report['actual_finite'])
        if not budget:
            budget['fodder'] = 1
    total = sum(budget.values())
    allocated = Counter({key: count * value // total for key, value in budget.items()}) if total else Counter(fodder=count)
    return allocated, budget, count, population


def _draw_library_sources(candidates, catalog, hitpoints, map_key, map_peak, mobile, origins, reports, result, seed, settings, stage_count, stage_index, supplies):
    for source in origins:
        if source.get('protected_reason') or source.get('inactive_reason') or source['id'] in result:
            continue
        allocated, budget, count, population = _draw_library_sources_population(catalog, map_peak, mobile, reports, settings, source)
        allocated['fodder'] += count - sum(allocated.values())
        made, _ = _draw_bodies([source], {source['id']: count}, allocated,
            seed=seed, key=[map_key, source['id'], 'library'], settings={**settings, '_drawn': Counter()} if mobile == 2 else settings, catalog=catalog,
            candidates=candidates, supplies=supplies, population=population,
            stage_index=stage_index, stage_count=stage_count, hitpoints=hitpoints, renewable=True,
            cost_budget={category: sum(BASE_COMBAT_COST[name] for name in source_species(source, catalog)) * count
                         for category in budget} if source.get('darklord_phantom') else None)
        result.update(made)


def _reference_population(chosen, kind, mobile, opening_sources, own_row, owner, recipe_revision, settings, sources):
    peak = chosen['peak']
    mix_stone_imps = (recipe_revision >= 18 and kind == 'specialized_encounter' and
                     all(set(slot['species']) <= {'stone_imp'} for slot in chosen['slots']))
    if mix_stone_imps:
        # the gauntlet keeps its native population while sharing the fodder roster.
        mobile = 3
    opening = any(source['id'] in opening_sources for source in sources)
    native_stationary_peak = own_row.get('peak', {}).get('stationary', 0)
    reference_stationary_peak = peak.get('stationary', 0)
    geometry_reserve = max(0, native_stationary_peak - reference_stationary_peak) if not mobile else 0
    population = _population(peak, owner, opening=opening, settings=settings, geometry_reserve=geometry_reserve)
    return geometry_reserve, mobile, native_stationary_peak, opening, population, reference_stationary_peak


def _draw_finite_reference_finite(sources):
    finite = [source for source in sources if not _renewable(source) and not source.get('protected_reason')]
    renewable = [source for source in sources if _renewable(source) and not source.get('protected_reason')]
    return finite, renewable


def _draw_finite_reference(candidates, catalog, chosen, factor, finite_supply, hitpoints, map_key, mobile, opening, population, recipe_revision, result, seed, settings, sources, stage_count, stage_index, supplies, variant):
    finite, renewable = _draw_finite_reference_finite(sources)
    native_finite = _passthrough_budget(sources, catalog, seed=seed, renewable=False)
    native_renewable = _passthrough_budget(sources, catalog, seed=seed, renewable=True)
    wanted = _native_budget(chosen, seed=seed, renewable=False, mobile=mobile)
    random_wanted = Counter({key: max(0, value - native_finite[key]) for key, value in wanted.items()
                             if key != 'stationary'})
    target = round(sum(random_wanted.values()) * _volume(settings))
    counts, emitted = _allocate_counts(finite, target)
    desired = _modified_budget(random_wanted, factor, target)
    cost_budget = _cost_budget(chosen, desired, seed=seed, renewable=False, candidates=candidates, mobile=mobile)
    encounter_settings = {**settings, 'protected_opening': opening, 'mobile_stationary': mobile,
        'limit_zombies': recipe_revision >= 17 and any(not set(slot['species']) <= ZOMBIES for slot in chosen['slots'])}
    made, actual = _draw_bodies(finite, counts, desired, seed=seed, key=[map_key, variant], settings=encounter_settings,
        catalog=catalog, candidates=candidates, supplies=supplies, population=population,
        stage_index=stage_index, stage_count=stage_count, hitpoints=hitpoints,
        cost_budget=cost_budget, ammo_required=min(finite_supply, desired.get('fodder', 0)))
    actual_cost = _body_costs(finite, made, renewable=False, mobile=mobile)
    result.update(made)
    actual.update(native_finite)
    desired.update({key: min(value, wanted[key]) for key, value in native_finite.items()})
    desired['stationary'] = wanted['stationary']
    return actual, actual_cost, cost_budget, desired, emitted, encounter_settings, finite, native_renewable, renewable, target, wanted


def _draw_renewable_reference(candidates, catalog, chosen, encounter_settings, factor, hitpoints, map_key, mobile, native_renewable, population, renewable, result, seed, settings, sources, stage_count, stage_index, supplies, variant):
    maintained = _native_budget(chosen, seed=seed, renewable=True, mobile=mobile)
    random_maintained = Counter({key: max(0, value - native_renewable[key])
                                for key, value in maintained.items() if key != 'stationary'})
    renewable_target = round(sum(random_maintained.values()) * Fraction(
        settings['count_multiplier'] if settings['count_mode'] == 'multiplier' else 100, 100))
    renewable_counts = _allocate_pool_counts(renewable, renewable_target)
    desired_renewable = _modified_budget(random_maintained, factor, renewable_target)
    reference_ammo_pool = _native_ammo_pool(chosen)
    target_ammo_pool = (reference_ammo_pool * (settings['count_multiplier'] if
        settings['count_mode'] == 'multiplier' else 100) + 99) // 100
    ammo_required, native_ammo_pool = _reserve_renewable_ammunition(catalog, desired_renewable, random_maintained, sources, target_ammo_pool)
    renewable_counts = _reserve_ammo_counts(renewable, renewable_counts, ammo_required,
        candidates=candidates, catalog=catalog, supplies=supplies, population=population,
        special_limits=settings['special_limits'])
    made, active = _draw_bodies(renewable, renewable_counts,
        desired_renewable, seed=seed,
        key=[map_key, variant, 'renewable'], settings=encounter_settings, catalog=catalog, candidates=candidates,
        supplies=supplies, population=population, stage_index=stage_index, stage_count=stage_count,
        hitpoints=hitpoints, renewable=True,
        cost_budget=_cost_budget(chosen, desired_renewable, seed=seed, renewable=True, candidates=candidates, mobile=mobile),
        ammo_required=ammo_required)
    actual_ammo_pool = native_ammo_pool + sum(body['species'] in AMMO_FODDER for bodies in made.values() for body in bodies)
    result.update(made)
    return active, actual_ammo_pool, desired_renewable, maintained, reference_ammo_pool, target_ammo_pool


def _reported_combat_costs(chosen, mobile, seed):
    return {key: str(value) for key, value in _native_costs(chosen, seed=seed, renewable=False, mobile=mobile).items()}


def _build_reference_encounters(candidates, catalog, encounters, factor, grouped, hitpoints, map_key, mobile, opening_sources, own_encounters, recipe_revision, reference_map, reports, result, seed, settings, stage_count, stage_index, supplies, target_owners, unknown):
    for (owner, variant), sources in grouped.items():
        ammo_geometry, chosen, kind, local_kind, mobile, own_row, same_kind = _choose_reference_encounter(encounters, grouped, map_key, own_encounters, owner, recipe_revision, reference_map, sources, target_owners, variant)
        chosen, row_reference_map, translation = _build_vanilla_stage_translation(chosen, kind, local_kind, map_key, own_row, recipe_revision, reference_map, same_kind)
        if local_kind:
            translation = 'native_encounter_kind'
        native_only = all(source.get('protected_reason') for source in sources)
        chosen, row_reference_map, translation, unknown_context = _preserve_native_encounter(candidates, catalog, chosen, factor, map_key, mobile, native_only, opening_sources, own_row, owner, reference_map, row_reference_map, same_kind, seed, settings, sources, supplies, translation, unknown, variant)
        ref_owner = chosen['owner']
        finite_supply = 0
        chosen, finite_supply, folded_renewable = _fold_renewable_slots(ammo_geometry, chosen, finite_supply, kind, recipe_revision)
        geometry_reserve, mobile, native_stationary_peak, opening, population, reference_stationary_peak = _reference_population(chosen, kind, mobile, opening_sources, own_row, owner, recipe_revision, settings, sources)
        actual, actual_cost, cost_budget, desired, emitted, encounter_settings, finite, native_renewable, renewable, target, wanted = _draw_finite_reference(candidates, catalog, chosen, factor, finite_supply, hitpoints, map_key, mobile, opening, population, recipe_revision, result, seed, settings, sources, stage_count, stage_index, supplies, variant)
        active, actual_ammo_pool, desired_renewable, maintained, reference_ammo_pool, target_ammo_pool = _draw_renewable_reference(candidates, catalog, chosen, encounter_settings, factor, hitpoints, map_key, mobile, native_renewable, population, renewable, result, seed, settings, sources, stage_count, stage_index, supplies, variant)
        deviations = _build_vanilla_stage_deviations(active, desired_renewable, folded_renewable, geometry_reserve, maintained, native_renewable, settings, unknown_context)
        _finite_budget_deviations(active, actual, actual_cost, cost_budget, desired, desired_renewable, deviations, emitted, finite, mobile, opening, sources, target, wanted)
        reports.append(dict(owner=owner, variant=variant, reference_owner=ref_owner,
            reference_map=row_reference_map, translation=translation,
            native_stationary_peak=native_stationary_peak, reference_stationary_peak=reference_stationary_peak,
            geometry_reserve=geometry_reserve,
            unknown_native_source_ids=unknown_context,
            reference_ammo_pool=reference_ammo_pool, target_ammo_pool=target_ammo_pool, actual_ammo_pool=actual_ammo_pool,
            reference_variant=chosen['id'], reference_finite=dict(wanted), target_finite=dict(desired),
            kind=kind, reference_kind=chosen.get('kind', 'scripted_encounter'),
            actual_finite=dict(actual), reference_finite_total=sum(wanted.values()),
            actual_finite_total=sum(actual.values()), reference_renewable=dict(maintained),
            target_renewable=dict(desired_renewable),
            reference_base_cost=_reported_combat_costs(chosen, mobile, seed),
            randomized_base_cost_budget={key: str(value) for key, value in cost_budget.items()},
            randomized_base_cost_actual={key: str(value) for key, value in actual_cost.items()},
            actual_renewable=dict(active), population_limits=dict(population), deviations=deviations,
            concurrency_basis=next((wave.get('concurrency_basis') for wave in chosen['waves']
                if wave.get('concurrency_basis')), 'catalog_source_pool_upper_bound'),
            primary=variant in vanilla_profile(catalog, map_key).get('primary_encounters', ()),
            source_ids=[source['id'] for source in sources],
            reference_waves=[wave['id'] for wave in chosen['waves']]))
    return mobile


def build_vanilla_stage(origins, settings, campaign, catalog, *, seed, map_key, stage_index, stage_count,
                        supplies, opening_sources, hitpoints, recipe_revision=13):
    mobile = 2 if recipe_revision >= 15 else recipe_revision >= 14
    reference_map = _reference_map(catalog, campaign, map_key, stage_index, stage_count, settings['progressive'], contextual=mobile == 2)
    reference = vanilla_profile(catalog, reference_map)
    encounters = _encounters(reference, mobile=mobile, objective_budgets=recipe_revision >= 17)
    reference_owners = list(dict.fromkeys(row['owner'] for row in encounters))
    grouped = {}
    _group_native_sources(catalog, grouped, origins)
    own_encounters = _encounters(vanilla_profile(catalog, map_key), mobile=mobile,
                                 objective_budgets=recipe_revision >= 17)
    unknown = [source for source in origins if source.get('vanilla_unknown')]
    target_owners = _target_encounter_owners(grouped, own_encounters)
    result, reports = {}, []
    map_peak = _peak([*reference['waves'], *reference.get('unknown_envelopes', [])])
    limits = _special_limits(map_peak, settings)
    settings = {**settings, 'special_limits': limits, 'mobile_stationary': mobile,
                'protect_opening_budget': recipe_revision >= 16}
    _mobile_special_limits(limits, map_peak, mobile, settings)
    candidates = sorted(species for species in catalog['species'] if settings['weights'].get(species, 0) > 0)
    factor = _stage_intensity_factor(settings, stage_count, stage_index)
    mobile = _build_reference_encounters(candidates, catalog, encounters, factor, grouped, hitpoints, map_key, mobile, opening_sources, own_encounters, recipe_revision, reference_map, reports, result, seed, settings, stage_count, stage_index, supplies, target_owners, unknown)
    _draw_library_sources(candidates, catalog, hitpoints, map_key, map_peak, mobile, origins, reports, result, seed, settings, stage_count, stage_index, supplies)
    return result, limits, dict(map_key=reference_map,
        stage=CANONICAL_ORDER.index(reference_map) + 1 if reference_map in CANONICAL_ORDER else 1,
        boss_map=map_key in BOSS_MAPS, source_sha256=reference['source_sha256'],
        target_encounters=len(set(target_owners) | {_owner(source) for source in unknown}),
        known_target_encounters=len(target_owners), reference_encounters=len(reference_owners),
        quantity_basis=reference['quantity_basis'], encounters=reports,
        unknown_native_sources=[dict(source_id=source['id'], owner=_owner(source), variant=_variant(source),
            logical_count=source['logical_count'], maximum_emissions=source['maximum_emissions']) for source in unknown],
        reference_unknown_sources=reference.get('unknown_sources', []))
