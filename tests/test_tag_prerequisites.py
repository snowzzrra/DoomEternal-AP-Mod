"""Shared prerequisite identity and byte-equivalent TAG authoring output."""

import hashlib
import json

import pytest

from doom_eap.contracts.tag_prerequisites import AUTHORED_TAG_PREREQUISITES
from doom_eap.runtime.item_reconciliation import effective_ownership
from tools.decls.devinv_builder import build_tag_devinv_overrides, validate_tag_devinv_source


@pytest.mark.parametrize("arguments", [
    {}, {"starting_weapon": "Heavy Cannon"},
    {"starting_inventory": {"Dash": 1, "Blood Punch": 1}},
])
def test_tag_outputs_match_pre_extraction_declarations(arguments):
    outputs = build_tag_devinv_overrides(**arguments)
    assert len(outputs) == 8
    encoded = json.dumps(outputs, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(encoded).hexdigest() == "10854ce96cdb169ff5e7f5104b4e34b7f17ea2983e2158cc40d25eae15feae4a"
    for source in outputs.values():
        validate_tag_devinv_source(source)


@pytest.mark.parametrize("perk,error", [
    ("perk/player/weapons/shotgun/pop_rocket_weakpoint_hit", "missing required mod upgrades"),
    ("perk/player/blood_punch/area_of_effect", "missing required Blood Punch perks"),
])
def test_tag_prerequisite_guards_still_reject_missing_perks(perk, error):
    source = next(iter(build_tag_devinv_overrides().values()))
    altered = source.replace(f'perk = "{perk}";', 'perk = "missing";')
    assert altered != source
    with pytest.raises(ValueError, match=error):
        validate_tag_devinv_source(altered)


def test_authored_requirements_are_not_received_or_completion_ownership():
    ownership = effective_ownership(
        (), randomize_chainsaw=False, randomize_dash=False,
        checked_locations=frozenset(), local_checked_locations=frozenset(),
        server_checked_ready=True, hell_on_earth_locations=frozenset({7770002}),
        exultia_complete_location=55, slot=1,
    )
    assert ownership.authored_tag_prerequisites is AUTHORED_TAG_PREREQUISITES
    assert ownership.authored_tag_prerequisites.provenance == "authored_tag_devinv"
    assert ownership.authored_tag_prerequisites.blood_punch_perks
    assert ownership.authored_tag_prerequisites.normal_mod_upgrades
    assert ownership.ap_item_ids == ownership.reconciliation_item_ids == ()
    assert ownership.derived_facts == ownership.blood_punch_upgrades == ()
