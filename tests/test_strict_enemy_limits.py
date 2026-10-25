import pytest

from doom_eap.content.enemy_plan import LIMITED_SPECIES, _limit_suffix
from doom_eap.content.native_enemy_plan import native_body_name


def settings():
    return {'revision': 7, 'special_limits': {'marauder': 2, 'archvile': 2, 'spirit': 2, 'combined_special': 4},
            'population_limits': {'maximum': 8, 'superheavy': 2, 'pressure': 8,
                                  'mixed': 1, 'heavy': 0, 'scope': 123, 'strict': True,
                                  'species': {name: index for index, name in enumerate(LIMITED_SPECIES)}}}


def test_strict_caps_keep_zero_heavies_separate_from_fodder():
    options = settings()
    suffix = _limit_suffix(options, 'lost_soul')
    assert suffix.endswith('_ko')
    assert '_v' + ''.join(f'{index:08x}' for index in range(9)) in suffix
    body = {'species': 'lost_soul', 'spawn_type': 'ENCOUNTER_SPAWN_LOST_SOUL',
            'attributes': {'speed_percent': 100}, 'population_limits': options['population_limits']}
    definition = native_body_name('a' * 20, 0, 1, 1, 'ENCOUNTER_SPAWN_IMP', body, options)
    assert len(definition.encode('ascii')) < 512
    assert suffix in definition
