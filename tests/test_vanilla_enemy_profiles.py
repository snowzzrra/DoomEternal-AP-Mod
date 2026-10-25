from doom_eap.content.vanilla_enemy_profiles import catalog_profile
from tools.maps.vanilla_enemy_profiles import _script_peak, population_bound, script_replenishment_stops


def _catalog():
    return {'species': {
        'imp': {'spawn_type': 'ENCOUNTER_SPAWN_IMP'},
        'hell_knight': {'spawn_type': 'ENCOUNTER_SPAWN_HELL_KNIGHT'},
        'baron': {'spawn_type': 'ENCOUNTER_SPAWN_BARON'},
    }, 'maps': {'map': {'origins': [], 'source_sha256': 'a' * 64}}}


def _source(name, species, count, *, maximum=None, family='group', wave='arena/script/0'):
    return dict(id=name, family=family, wave=wave, logical_count=count,
                logical_types=[f'ENCOUNTER_SPAWN_{name.upper()}' for name in species],
                spawn_type=' '.join(f'ENCOUNTER_SPAWN_{name.upper()}' for name in species),
                maximum_emissions=count if maximum is None else maximum,
                binding={'owner': 'arena', 'entity': 'arena'})


def test_overlapping_wait_constraints_have_a_joint_integer_bound():
    count, solved = population_bound([2, 2, 2], [({0, 1}, 2), ({1, 2}, 2), ({0, 2}, 2)], {0, 1, 2})
    assert solved and count == 3
    assert population_bound([2, 2, 2], [({0, 1}, 2), ({1, 2}, 2), ({0, 2}, 2)], {0, 1, 2},
                            node_budget=0) == (4, False)


def test_opaque_spawn_keeps_a_population_envelope_without_a_fabricated_quota():
    catalog = _catalog()
    catalog['maps']['map']['origins'] = [_source('pool', ['imp', 'hell_knight'], 2)]
    profile = catalog_profile(catalog, 'map')
    assert not profile['encounters']
    envelope = profile['unknown_envelopes'][0]
    assert envelope['peak']['maximum'] == 2 and envelope['peak']['heavy'] == 2
    assert envelope['source_ids'] == ['pool']
