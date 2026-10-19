import pytest

from tools.maps.encounter_catalog import parse_object


def test_parser_rejects_duplicate_properties_instead_of_discarding_events():
    with pytest.raises(ValueError, match="Duplicate entity property"):
        parse_object('num = 1; num = 2;')
